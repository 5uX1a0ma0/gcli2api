"""凭证模型能力、混合池分流和模型不存在兜底的离线回归测试。"""

import asyncio
import copy
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import Response

import config
from src import credential_manager as manager, session_cache
from src.storage import database as storage

# src.utils 导入时会查询 CLI 版本；测试只使用固定版本，禁止外部网络请求。
with patch("httpx.get", return_value=httpx.Response(
    200, json={"tag_name": "v1.4.12"}, request=httpx.Request("GET", "https://github.test")
)):
    from src.api import antigravity as api
    from src.converter.antigravity_fix import resolve_profile
    from src.router.antigravity.model_list import get_antigravity_models_with_features


FLASH = "gemini-3.8-flash"
OLD_CLAUDE = "claude-sonnet-4-6"
NEW_CLAUDE = "claude-opus-5-5-high"
GEMINI4 = "gemini-4-argon-high"


class Upstream:
    def __init__(self):
        self.models = {}
        self.probes = []
        self.generated = []
        self.outcomes = {}

    async def post(self, url, json=None, headers=None, **kwargs):
        token = headers["Authorization"].removeprefix("Bearer ")
        if url.endswith(":fetchAvailableModels"):
            self.probes.append((url, token, json, kwargs))
            await asyncio.sleep(0)
            models = self.models[token]
            if isinstance(models, Exception):
                raise models
            if models is None:
                return httpx.Response(503, json={"error": {"message": "unavailable"}})
            return httpx.Response(200, json={"models": models})
        return self.generate(token, json)

    def generate(self, token, payload):
        self.generated.append((token, copy.deepcopy(payload)))
        outcomes = self.outcomes.get(token, [])
        status, message = outcomes.pop(0) if outcomes else (200, "ok")
        if status != 200:
            return httpx.Response(status, json={"error": {"message": message}})
        return httpx.Response(200, json={
            "candidates": [{"content": {"role": "model", "parts": [{"text": message}]}}]
        })

    async def stream(self, url, body, native=False, headers=None):
        token = headers["Authorization"].removeprefix("Bearer ")
        response = self.generate(token, body)
        if response.status_code != 200:
            yield Response(response.content, response.status_code)
        else:
            line = f"data: {response.text}"
            yield line.encode() if native else line


@pytest.fixture
async def env(monkeypatch, tmp_path):
    for name in config.ENV_MAPPINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("POSTGRESQL_URI", raising=False)
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "_config_initialized", True)
    monkeypatch.setattr(config, "_config_cache", {
        "antigravity_api_url": "https://antigravity.test",
        "credential_select_mode": "random",
        "retry_429_enabled": False,
        "retry_429_max_retries": 0,
        "retry_429_interval": 0,
        "auto_ban_enabled": True,
        "auto_ban_error_codes": [400],
    })
    database = storage.Database()
    await database.initialize()
    upstream = Upstream()
    monkeypatch.setattr(manager, "get_database", AsyncMock(return_value=database))
    monkeypatch.setattr(api, "post_async", upstream.post)
    monkeypatch.setattr(api, "stream_post_async", upstream.stream)
    for name in ("record_usage_log", "record_api_call_success", "record_api_call_error"):
        monkeypatch.setattr(api, name, AsyncMock())
    monkeypatch.setattr(api, "_record_request_body_detail", AsyncMock(return_value=""))
    manager._reset_sticky()
    manager._model_cache.clear()
    manager._model_cache_locks.clear()
    session_cache._memory_store.clear()

    async def add(label, models, tier="pro"):
        email = f"{label}@example.test"
        data = {
            "access_token": label,
            "refresh_token": f"refresh-{label}",
            "project_id": f"project-{label}",
            "expiry": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "tier": tier,
        }
        upstream.models[label] = {model: {} for model in models} if models is not None else None
        await database.store_credential(email, data)
        return email, data

    yield SimpleNamespace(db=database, upstream=upstream, add=add)
    await database.close()
    manager._reset_sticky()
    manager._model_cache.clear()
    manager._model_cache_locks.clear()
    session_cache._memory_store.clear()


def request_body(model=NEW_CLAUDE):
    return {"model": model, "request": {
        "contents": [{"role": "user", "parts": [{"text": "hello"}]}]
    }}


async def call_api(stream, body=None):
    body = body or request_body()
    if not stream:
        return await api.non_stream_request(body)
    chunks = [chunk async for chunk in api.stream_request(body)]
    return chunks[0] if isinstance(chunks[0], Response) else chunks


@pytest.mark.parametrize("mode", ["random", "cache", "sticky"])
@pytest.mark.parametrize("model", [OLD_CLAUDE, NEW_CLAUDE, GEMINI4, FLASH])
async def test_mixed_pool_routes_by_models_independent_of_account_tier(env, mode, model):
    free, _ = await env.add("free", [OLD_CLAUDE, FLASH], tier="ultra")
    paid, _ = await env.add("paid", [NEW_CLAUDE, GEMINI4, FLASH], tier="free")
    config._config_cache["credential_select_mode"] = mode
    wrong = paid if model == OLD_CLAUDE else free
    manager._sticky_email = wrong
    await session_cache.set_session_binding("session", wrong)
    result = await manager.get_valid_credential(model, "session")
    assert result is not None
    if model != FLASH:
        assert result[0] == (free if model == OLD_CLAUDE else paid)
    assert model in env.upstream.models[result[1]["access_token"]]


async def test_search_continues_past_three_ineligible_credentials(env, monkeypatch):
    for index in range(8):
        await env.add(f"free-{index}", [OLD_CLAUDE, FLASH])
    paid, _ = await env.add("paid", [NEW_CLAUDE])
    # 固定随机查询顺序，确保真正走过八个不匹配的候选和数据库排除过滤。
    monkeypatch.setattr(storage, "func", SimpleNamespace(
        random=lambda: storage.AntigravityCredential.id
    ))
    result = await manager.get_valid_credential(NEW_CLAUDE)
    assert result[0] == paid
    assert len(env.upstream.probes) == 9
    assert await env.db.get_next_available_credential(
        excluded_emails=set(await env.db.list_credentials())
    ) is None


async def test_disabled_and_cooling_credentials_remain_excluded(env):
    disabled, _ = await env.add("disabled", [NEW_CLAUDE])
    cooling, _ = await env.add("cooling", [NEW_CLAUDE])
    available, _ = await env.add("available", [NEW_CLAUDE])
    await env.db.update_credential_state(disabled, {"disabled": True})
    await env.db.set_model_cooldown(cooling, NEW_CLAUDE, time.time() + 3600)
    assert (await manager.get_valid_credential(NEW_CLAUDE))[0] == available
    assert [probe[1] for probe in env.upstream.probes] == ["available"]


async def test_model_lists_are_union_cached_and_preserve_aliases(env):
    free, _ = await env.add("free", [OLD_CLAUDE, FLASH])
    paid, _ = await env.add("paid", [NEW_CLAUDE, GEMINI4, "claude-opus-4-6-thinking"])
    disabled, _ = await env.add("disabled", ["gemini-disabled-only"])
    await env.db.update_credential_state(disabled, {"disabled": True})
    await env.db.set_model_cooldown(paid, NEW_CLAUDE, time.time() + 3600)
    config._config_cache["credential_select_mode"] = "sticky"
    manager._sticky_email = free
    first = await api.fetch_available_models()
    second = await api.fetch_available_models()
    ids = {model["id"] for model in first}
    assert ids == {
        OLD_CLAUDE, FLASH, NEW_CLAUDE, GEMINI4,
        "claude-sonnet-4-6-thinking", "claude-opus-4-6-thinking", "claude-opus-4-6",
    }
    assert [model["id"] for model in first] == [model["id"] for model in second]
    assert len(first) == len(ids)
    assert len(env.upstream.probes) == 2
    assert manager._sticky_email == free
    features = await get_antigravity_models_with_features()
    assert set(features) == ids | {f"抗截断-{model}" for model in ids}
    assert len(env.upstream.probes) == 2


async def test_capability_query_filters_disabled_but_keeps_zero_quota(env):
    env.upstream.models["token"] = {
        NEW_CLAUDE: {"quotaInfo": {"remainingFraction": 0}},
        "gemini-disabled": {},
        "gemini-4-blocked-info": {"disabled": True},
        "claude-blocked-info": {"isDisabled": True},
    }
    assert await api.fetch_credential_model_ids("token", "project") == {NEW_CLAUDE}
    _, token, payload, options = env.upstream.probes[0]
    assert token == "token" and payload == {"project": "project"}
    assert options["timeout"] == 15
    await api.fetch_credential_model_ids("token")
    assert env.upstream.probes[-1][2] == {}


@pytest.mark.parametrize("models", [None, [], httpx.ConnectError("offline")])
async def test_discovery_failures_are_unknown_not_empty_or_free(env, models):
    email, data = await env.add("unknown", None)
    env.upstream.models["unknown"] = models
    assert await manager.get_credential_model_ids(email, data) is None
    assert await manager.get_valid_credential(NEW_CLAUDE) is not None
    assert len(env.upstream.probes) == 1


async def test_successful_empty_list_and_missing_model_are_not_selected(env):
    await env.add("empty", [])
    assert await manager.get_valid_credential(NEW_CLAUDE) is None
    assert env.upstream.generated == []


async def test_cache_expires_recovers_and_retains_stale_result_on_failure(env, monkeypatch):
    email, data = await env.add("token", [OLD_CLAUDE])
    now = [0.0]
    monkeypatch.setattr(manager, "time", SimpleNamespace(monotonic=lambda: now[0]))
    assert await manager.get_credential_model_ids(email, data) == {OLD_CLAUDE}
    env.upstream.models["token"] = {NEW_CLAUDE: {}}
    now[0] = 299
    assert await manager.get_credential_model_ids(email, data) == {OLD_CLAUDE}
    now[0] = 301
    assert await manager.get_credential_model_ids(email, data) == {NEW_CLAUDE}
    env.upstream.models["token"] = None
    now[0] = 602
    assert await manager.get_credential_model_ids(email, data) == {NEW_CLAUDE}
    now[0] = 610
    assert await manager.get_credential_model_ids(email, data) == {NEW_CLAUDE}
    assert len(env.upstream.probes) == 3
    env.upstream.models["token"] = {GEMINI4: {}}
    now[0] = 633
    assert await manager.get_credential_model_ids(email, data) == {GEMINI4}


@pytest.mark.parametrize("field", ["access_token", "project_id", "api_url"])
async def test_changed_credential_or_endpoint_invalidates_cache(env, field):
    email, data = await env.add("old", [OLD_CLAUDE])
    assert await manager.get_credential_model_ids(email, data) == {OLD_CLAUDE}
    env.upstream.models["old"] = {NEW_CLAUDE: {}}
    if field == "api_url":
        config._config_cache["antigravity_api_url"] = "https://new-antigravity.test"
    else:
        data[field] = "new"
        env.upstream.models["new"] = {NEW_CLAUDE: {}}
    assert await manager.get_credential_model_ids(email, data) == {NEW_CLAUDE}
    assert len(env.upstream.probes) == 2


async def test_concurrent_queries_share_one_probe(env):
    email, data = await env.add("token", [NEW_CLAUDE])
    results = await asyncio.gather(*(
        manager.get_credential_model_ids(email, data) for _ in range(20)
    ))
    assert all(result == {NEW_CLAUDE} for result in results)
    assert len(env.upstream.probes) == 1


@pytest.mark.parametrize("alias", ["claude-sonnet-4-6-thinking", "claude-opus-4-6"])
async def test_alias_uses_normalized_upstream_id_for_eligibility(env, alias):
    upstream = resolve_profile(alias).upstream
    email, _ = await env.add("token", [upstream])
    assert (await manager.get_valid_credential(upstream))[0] == email


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode", ["random", "cache", "sticky"])
async def test_model_not_found_switches_once_without_retry_budget_or_auto_ban(env, stream, mode):
    first, _ = await env.add("first", [NEW_CLAUDE])
    await env.add("second", [NEW_CLAUDE])
    config._config_cache["credential_select_mode"] = mode
    body = request_body()
    manager._sticky_email = first
    await session_cache.set_session_binding(api._derive_session_key(NEW_CLAUDE, body), first)
    # random 模式显式固定首选，之后恢复真实随机池以验证排除失败凭证。
    original = env.db.get_next_available_credential

    async def first_then_random(model_name=None, excluded_emails=None):
        if first not in (excluded_emails or ()):
            return await env.db.get_available_credential_by_email(first, model_name)
        return await original(model_name, excluded_emails)

    env.db.get_next_available_credential = first_then_random
    env.upstream.outcomes["first"] = [(400, "Requested model not found.")]
    result = await call_api(stream, body)
    assert isinstance(result, list) if stream else result.status_code == 200
    assert [token for token, _ in env.upstream.generated] == ["first", "second"]
    assert [payload["project"] for _, payload in env.upstream.generated] == [
        "project-first", "project-second",
    ]
    assert all(payload["model"] == NEW_CLAUDE for _, payload in env.upstream.generated)
    assert first not in manager._model_cache
    assert (await env.db.get_credential_state(first))["disabled"] is False
    assert api.record_api_call_error.await_count == 1
    assert api.record_usage_log.await_count == 2


@pytest.mark.parametrize("stream", [False, True])
async def test_all_model_mismatches_stop_after_each_credential_once(env, stream):
    for index in range(5):
        label = f"token-{index}"
        await env.add(label, [NEW_CLAUDE])
        env.upstream.outcomes[label] = [(400, "Requested model not found.")]
    result = await call_api(stream)
    assert result.status_code == 400
    tokens = [token for token, _ in env.upstream.generated]
    assert len(tokens) == len(set(tokens)) == 5


@pytest.mark.parametrize("stream", [False, True])
async def test_ordinary_400_keeps_existing_error_handling(env, stream):
    email, _ = await env.add("token", [NEW_CLAUDE])
    env.upstream.outcomes["token"] = [(400, "Invalid thought_signature for model.")]
    result = await call_api(stream)
    assert result.status_code == 400
    assert len(env.upstream.generated) == 1
    assert email in manager._model_cache


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("succeed", [False, True])
async def test_existing_service_error_retry_budget_remains_bounded(env, stream, succeed):
    await env.add("token", [NEW_CLAUDE])
    config._config_cache.update({"retry_429_enabled": True, "retry_429_max_retries": 2})
    env.upstream.outcomes["token"] = [
        (503, "unavailable"), (503, "unavailable"),
        (200, "ok") if succeed else (503, "unavailable"),
    ]
    result = await call_api(stream)
    if succeed:
        assert isinstance(result, list) if stream else result.status_code == 200
    else:
        assert result.status_code == 503
    assert len(env.upstream.generated) == 3


@pytest.mark.parametrize("message", [
    "Requested model not found.",
    "Model 'claude-opus-5-5-high' does not exist.",
    "Model gemini-4-argon-high is not available.",
])
def test_detects_explicit_model_unavailable(message):
    assert api._is_model_not_found_error(400, json.dumps({"error": {"message": message}}))


@pytest.mark.parametrize("body", [
    json.dumps({"error": {"message": "Invalid thought_signature for model."}}),
    json.dumps({"error": {"message": "Model 'claude' does not support function calling."}}),
    json.dumps({"error": {"message": "Function call not found for requested model."}}),
    json.dumps({"error": "Requested model not found."}),
    "not JSON",
    "[]",
])
def test_other_400_errors_are_not_model_mismatches(body):
    assert not api._is_model_not_found_error(400, body)
    assert not api._is_model_not_found_error(503, body)
