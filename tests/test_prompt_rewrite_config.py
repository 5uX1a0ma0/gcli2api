"""Lazy migration, environment precedence, atomic validation, and hot updates."""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

import config
from src.converter.prompt_rewrite import PromptRewriteConfig, rewrite_prompt_text, rewrite_request
from src.panel import config_routes
from src.panel.utils import get_env_locked_keys
from src.storage import database as storage


@pytest.fixture
async def config_env(monkeypatch, tmp_path):
    for name in config.ENV_MAPPINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("POSTGRESQL_URI", raising=False)
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "_config_initialized", True)
    monkeypatch.setattr(config, "_config_cache", {})
    database = storage.Database()
    await database.initialize()
    monkeypatch.setattr(config, "get_database", AsyncMock(return_value=database))
    monkeypatch.setattr(config_routes, "get_database", AsyncMock(return_value=database))

    async def seed(values):
        for key, value in values.items():
            await database.set_config(key, value)
        await config.reload_config()

    app = FastAPI()
    app.include_router(config_routes.router)
    app.dependency_overrides[config_routes.verify_panel_token] = lambda: "test-token"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://panel.test",
    ) as client:
        yield SimpleNamespace(db=database, client=client, seed=seed)
    await database.close()


async def test_unconfigured_instance_is_disabled_and_get_does_not_write(config_env):
    assert await config.get_prompt_rewrite_config() == PromptRewriteConfig()
    data = (await config_env.client.get("/config/get")).json()["config"]
    assert data["prompt_rewrite_enabled"] is False
    assert data["prompt_rewrite_use_default_keywords"] is True
    assert data["prompt_rewrite_rules"] == []
    assert data["prompt_rewrite_cloak_enabled"] is False
    assert data["sensitive_words_enabled"] is False
    assert data["sensitive_words"] == []
    assert await config_env.db.get_all_config() == {}
    response = await config_env.client.post("/config/save", json={"config": {"host": "test-host"}})
    assert response.status_code == 200
    saved = await config_env.db.get_all_config()
    assert saved["prompt_rewrite_enabled"] is False
    assert saved["prompt_rewrite_use_default_keywords"] is True
    assert saved["prompt_rewrite_rules"] == []
    assert saved["prompt_rewrite_cloak_enabled"] is False
    response = await config_env.client.post("/config/save", json={"config": {"host": "another-host"}})
    assert response.json()["saved_config"] == {"host": "another-host"}


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("words", [[" Codex ", "", "Other Client"], "Codex,\n Other Client", []])
async def test_legacy_state_and_words_migrate_lazily_on_first_save(config_env, enabled, words):
    legacy = {"sensitive_words_enabled": enabled, "sensitive_words": words}
    await config_env.seed(legacy)
    expected = [] if not words else [["Codex", "Antigravity"], ["Other Client", "Antigravity"]]
    data = (await config_env.client.get("/config/get")).json()["config"]
    assert data["prompt_rewrite_enabled"] is enabled
    assert data["prompt_rewrite_rules"] == expected
    assert data["sensitive_words_enabled"] is enabled
    assert data["sensitive_words"] == [rule[0] for rule in expected]
    assert await config_env.db.get_all_config() == legacy
    response = await config_env.client.post("/config/save", json={"config": {"port": 8080}})
    assert response.status_code == 200
    stored = await config_env.db.get_all_config()
    assert stored["prompt_rewrite_enabled"] is enabled
    assert stored["prompt_rewrite_rules"] == expected
    assert stored["prompt_rewrite_use_default_keywords"] is True
    assert stored["sensitive_words"] == words


async def test_new_false_and_empty_fields_override_legacy_database_values(config_env):
    await config_env.seed({
        "sensitive_words_enabled": True, "sensitive_words": ["Codex"],
        "prompt_rewrite_enabled": False, "prompt_rewrite_use_default_keywords": False,
        "prompt_rewrite_rules": [],
    })
    assert await config.get_prompt_rewrite_config() == PromptRewriteConfig(False, False)
    data = (await config_env.client.get("/config/get")).json()["config"]
    assert data["sensitive_words_enabled"] is False
    assert data["sensitive_words"] == []


async def test_environment_priority_and_explicit_empty_values(config_env, monkeypatch):
    await config_env.seed({
        "sensitive_words_enabled": False, "sensitive_words": ["Legacy DB"],
        "prompt_rewrite_enabled": True, "prompt_rewrite_use_default_keywords": False,
        "prompt_rewrite_rules": [["New DB", "DB Agent"]],
    })
    assert (await config.get_prompt_rewrite_config()).rules == (("New DB", "DB Agent"),)
    monkeypatch.setenv("SENSITIVE_WORDS_ENABLED", "false")
    monkeypatch.setenv("SENSITIVE_WORDS", "")
    assert await config.get_prompt_rewrite_config() == PromptRewriteConfig(False, False)
    assert {"prompt_rewrite_enabled", "prompt_rewrite_rules"} <= get_env_locked_keys()
    assert "prompt_rewrite_cloak_enabled" not in get_env_locked_keys()
    monkeypatch.setenv("SENSITIVE_WORDS_ENABLED", "true")
    monkeypatch.setenv("SENSITIVE_WORDS", "Legacy ENV,Other Client\n")
    settings = await config.get_prompt_rewrite_config()
    assert settings.enabled is True
    assert settings.rules == (("Legacy ENV", "Antigravity"), ("Other Client", "Antigravity"))
    monkeypatch.setenv("PROMPT_REWRITE_ENABLED", "false")
    monkeypatch.setenv("PROMPT_REWRITE_RULES", "[]")
    monkeypatch.setenv("PROMPT_REWRITE_USE_DEFAULT_KEYWORDS", "true")
    monkeypatch.setenv("PROMPT_REWRITE_CLOAK_ENABLED", "false")
    assert await config.get_prompt_rewrite_config() == PromptRewriteConfig(False, True)
    data = (await config_env.client.get("/config/get")).json()
    assert config.PROMPT_REWRITE_KEYS | config.LEGACY_PROMPT_REWRITE_KEYS <= set(data["env_locked"])
    assert data["config"]["prompt_rewrite_rules"] == data["config"]["sensitive_words"] == []
    assert data["config"]["prompt_rewrite_enabled"] is data["config"]["sensitive_words_enabled"] is False


@pytest.mark.parametrize("raw", ["not-json", "{}", "null", '[["", "Agent"]]', '[["Codex", "Agent"], ["bad"]]'])
async def test_invalid_rules_environment_warns_and_uses_database_without_lock(config_env, monkeypatch, raw):
    await config_env.seed({"prompt_rewrite_rules": [["Codex", "DB Agent"]]})
    monkeypatch.setenv("PROMPT_REWRITE_RULES", raw)
    monkeypatch.setenv("SENSITIVE_WORDS", "Legacy ENV")
    warning = Mock()
    monkeypatch.setattr(config.log, "warning", warning)
    assert (await config.get_prompt_rewrite_config()).rules == (("Codex", "DB Agent"),)
    assert "prompt_rewrite_rules" not in get_env_locked_keys()
    assert "sensitive_words" not in get_env_locked_keys()
    assert warning.called
    assert "PROMPT_REWRITE_RULES" in warning.call_args.args[0]
    data = (await config_env.client.get("/config/get")).json()
    assert data["config"]["prompt_rewrite_rules"] == [["Codex", "DB Agent"]]
    assert "prompt_rewrite_rules" not in data["env_locked"]


@pytest.mark.parametrize("env_name, key", [
    ("PROMPT_REWRITE_ENABLED", "prompt_rewrite_enabled"),
    ("SENSITIVE_WORDS_ENABLED", "prompt_rewrite_enabled"),
    ("PROMPT_REWRITE_USE_DEFAULT_KEYWORDS", "prompt_rewrite_use_default_keywords"),
    ("PROMPT_REWRITE_CLOAK_ENABLED", "prompt_rewrite_cloak_enabled"),
])
async def test_invalid_boolean_environment_falls_back_and_is_not_locked(config_env, monkeypatch, env_name, key):
    await config_env.seed({key: False})
    monkeypatch.setenv(env_name, "maybe")
    settings = await config.get_prompt_rewrite_config()
    attribute = {
        "prompt_rewrite_enabled": "enabled", "prompt_rewrite_use_default_keywords": "use_default_keywords",
        "prompt_rewrite_cloak_enabled": "cloak_enabled",
    }[key]
    assert getattr(settings, attribute) is False
    assert key not in get_env_locked_keys()


async def test_legacy_environment_locks_new_fields_without_persisting_overrides(config_env, monkeypatch):
    await config_env.seed({"sensitive_words_enabled": False, "sensitive_words": ["DB Client"]})
    monkeypatch.setenv("SENSITIVE_WORDS_ENABLED", "true")
    monkeypatch.setenv("SENSITIVE_WORDS", "ENV Client")
    response = await config_env.client.post("/config/save", json={"config": {
        "host": "changed", "prompt_rewrite_enabled": False,
        "prompt_rewrite_rules": [["Saved Client", "Saved Agent"]],
    }})
    assert response.status_code == 200
    saved = response.json()["saved_config"]
    assert "prompt_rewrite_enabled" not in saved
    assert "prompt_rewrite_rules" not in saved
    stored = await config_env.db.get_all_config()
    assert stored["host"] == "changed"
    assert stored["prompt_rewrite_use_default_keywords"] is True
    assert "prompt_rewrite_enabled" not in stored
    assert "prompt_rewrite_rules" not in stored
    monkeypatch.delenv("SENSITIVE_WORDS_ENABLED")
    monkeypatch.delenv("SENSITIVE_WORDS")
    assert await config.get_prompt_rewrite_config() == PromptRewriteConfig(False, True, (("DB Client", "Antigravity"),))


@pytest.mark.parametrize("key, value", [
    ("prompt_rewrite_rules", None), ("prompt_rewrite_rules", {}), ("prompt_rewrite_rules", "[]"),
    ("prompt_rewrite_rules", [["Codex", "Agent"], ["", "Agent"]]),
    ("prompt_rewrite_rules", [["Codex", 1]]), ("prompt_rewrite_rules", [["Codex", "Agent", "extra"]]),
    ("prompt_rewrite_enabled", "false"), ("prompt_rewrite_enabled", 0),
    ("prompt_rewrite_enabled", None), ("prompt_rewrite_use_default_keywords", 1),
    ("prompt_rewrite_cloak_enabled", 1), ("prompt_rewrite_cloak_enabled", "true"),
    ("prompt_rewrite_cloak_enabled", None),
    ("sensitive_words_enabled", "true"), ("sensitive_words", "Codex"), ("sensitive_words", [1]),
])
async def test_invalid_configuration_rejects_the_entire_save(config_env, key, value):
    await config_env.seed({"host": "original", "sensitive_words_enabled": True})
    before = await config_env.db.get_all_config()
    response = await config_env.client.post("/config/save", json={"config": {"host": "changed", key: value}})
    assert response.status_code == 400
    assert await config_env.db.get_all_config() == before
    assert config._config_cache == before


async def test_old_save_keys_are_normalized_and_new_keys_win_in_the_same_request(config_env):
    response = await config_env.client.post("/config/save", json={"config": {
        "sensitive_words_enabled": True, "sensitive_words": [" Codex ", "", "Custom Client"],
    }})
    assert response.status_code == 200
    saved = response.json()["saved_config"]
    assert saved["prompt_rewrite_enabled"] is True
    assert saved["prompt_rewrite_rules"] == [["Codex", "Antigravity"], ["Custom Client", "Antigravity"]]
    assert not config.LEGACY_PROMPT_REWRITE_KEYS & saved.keys()
    response = await config_env.client.post("/config/save", json={"config": {
        "prompt_rewrite_enabled": False, "sensitive_words_enabled": "invalid ignored alias",
        "prompt_rewrite_rules": [], "sensitive_words": {"invalid": "ignored alias"},
    }})
    assert response.status_code == 200
    assert (await config.get_prompt_rewrite_config()).enabled is False
    assert (await config.get_prompt_rewrite_config()).rules == ()
    assert not config.LEGACY_PROMPT_REWRITE_KEYS & (await config_env.db.get_all_config()).keys()


async def test_save_hot_updates_rules_and_preserves_earlier_request_snapshot(config_env):
    await config_env.seed({
        "prompt_rewrite_enabled": True, "prompt_rewrite_use_default_keywords": False,
        "prompt_rewrite_rules": [["Codex", "First Agent"]],
    })
    first = await config.get_prompt_rewrite_config()
    response = await config_env.client.post("/config/save", json={"config": {
        "prompt_rewrite_rules": [[" Codex ", " Second Agent "]],
    }})
    assert response.status_code == 200
    assert response.json()["saved_config"]["prompt_rewrite_rules"] == [["Codex", "Second Agent"]]
    current = await config.get_prompt_rewrite_config()
    assert rewrite_prompt_text("Codex", first) == ("First Agent", 1)
    assert rewrite_prompt_text("Codex", current) == ("Second Agent", 1)
    assert await config.get_prompt_rewrite_enabled() is True
    assert await config.get_prompt_rewrite_use_default_keywords() is False
    assert await config.get_prompt_rewrite_rules() == [["Codex", "Second Agent"]]
    assert await config.get_sensitive_words_enabled() is True
    assert await config.get_sensitive_words() == ["Codex"]
    with pytest.raises(FrozenInstanceError):
        current.enabled = False


async def test_initialization_and_invalid_database_fields_fall_back_to_legacy(config_env, monkeypatch):
    await config_env.seed({
        "prompt_rewrite_enabled": "invalid", "prompt_rewrite_rules": {"bad": "shape"},
        "sensitive_words_enabled": True, "sensitive_words": ["Codex"],
    })
    monkeypatch.setattr(config, "_config_initialized", False)
    monkeypatch.setattr(config, "_config_cache", {})
    assert await config.get_prompt_rewrite_config() == PromptRewriteConfig(True, True, (("Codex", "Antigravity"),))


@pytest.mark.parametrize("replacement", ["", " \n", "\u0085\u001c"])
async def test_empty_replacement_is_saved_and_hot_updated_as_a_deletion(config_env, replacement):
    response = await config_env.client.post("/config/save", json={"config": {
        "prompt_rewrite_enabled": True, "prompt_rewrite_use_default_keywords": False,
        "prompt_rewrite_rules": [[" Codex ", replacement]],
    }})
    assert response.status_code == 200
    assert response.json()["saved_config"]["prompt_rewrite_rules"] == [["Codex", ""]]
    assert (await config_env.client.get("/config/get")).json()["config"]["prompt_rewrite_rules"] == [["Codex", ""]]
    assert (await config_env.db.get_all_config())["prompt_rewrite_rules"] == [["Codex", ""]]
    assert rewrite_prompt_text("Remove Codex here.", await config.get_prompt_rewrite_config()) == ("Remove  here.", 1)


async def test_empty_replacement_from_environment_is_valid_and_locks_the_field(config_env, monkeypatch):
    await config_env.seed({"prompt_rewrite_enabled": True, "prompt_rewrite_rules": [["Codex", "DB Agent"]]})
    monkeypatch.setenv("PROMPT_REWRITE_RULES", '[["Codex", ""]]')
    settings = await config.get_prompt_rewrite_config()
    assert settings.rules == (("Codex", ""),)
    assert rewrite_prompt_text("Codex", settings) == ("", 1)
    assert "prompt_rewrite_rules" in get_env_locked_keys()
    assert (await config_env.client.get("/config/get")).json()["config"]["prompt_rewrite_rules"] == [["Codex", ""]]


async def test_cloak_hot_update_is_independent_and_preserves_the_earlier_snapshot(config_env):
    first = await config.get_prompt_rewrite_config()
    response = await config_env.client.post("/config/save", json={"config": {"prompt_rewrite_cloak_enabled": True}})
    assert response.status_code == 200
    assert response.json()["saved_config"]["prompt_rewrite_cloak_enabled"] is True
    current = await config.get_prompt_rewrite_config()
    assert current.enabled is False
    assert await config.get_prompt_rewrite_cloak_enabled() is True
    request = {"messages": [
        {"role": "system", "content": "English description"}, {"role": "user", "content": "English description"},
    ]}
    assert rewrite_request(request, "openai", first) == (request, 0)
    changed, count = rewrite_request(request, "openai", current)
    assert count == 2
    assert changed["messages"][0]["content"] == "Eng\u200blish des\u200bcription"
    assert changed["messages"][1] is request["messages"][1]
    assert (await config_env.client.get("/config/get")).json()["config"]["prompt_rewrite_cloak_enabled"] is True


@pytest.mark.parametrize("raw, enabled", [("true", True), ("1", True), ("on", True), ("false", False), ("0", False), ("off", False)])
async def test_cloak_environment_overrides_database_and_is_not_persisted(config_env, monkeypatch, raw, enabled):
    await config_env.seed({"prompt_rewrite_cloak_enabled": not enabled})
    monkeypatch.setenv("PROMPT_REWRITE_CLOAK_ENABLED", raw)
    assert await config.get_prompt_rewrite_cloak_enabled() is enabled
    assert "prompt_rewrite_cloak_enabled" in get_env_locked_keys()
    response = await config_env.client.post("/config/save", json={"config": {
        "host": "changed", "prompt_rewrite_cloak_enabled": not enabled,
    }})
    assert response.status_code == 200
    assert "prompt_rewrite_cloak_enabled" not in response.json()["saved_config"]
    assert (await config_env.db.get_all_config())["prompt_rewrite_cloak_enabled"] is not enabled
