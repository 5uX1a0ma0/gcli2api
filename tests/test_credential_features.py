"""实际邮箱去重与错误码配置的离线回归测试。"""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Delete

import config
from src.api import utils as api_utils
from src.error_codes import parse_error_codes
from src.panel import config_routes, credential_deduplication as dedup, creds
from src.storage import database as storage


CODE_KEYS = ("auto_ban_error_codes", "retry_error_codes", "request_log_error_codes")


@pytest.fixture
async def feature_env(monkeypatch, tmp_path):
    for name in config.ENV_MAPPINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("POSTGRESQL_URI", raising=False)
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "_config_initialized", True)
    monkeypatch.setattr(config, "_config_cache", {})
    db = storage.Database()
    await db.initialize()
    monkeypatch.setattr(config_routes, "get_database", AsyncMock(return_value=db))
    monkeypatch.setattr(dedup, "get_database", AsyncMock(return_value=db))
    owners = {}
    probes = []
    active = 0
    peak = 0

    async def reload_config():
        config._config_cache = await db.get_all_config()

    async def refresh(credentials):
        if credentials.refresh_token not in owners:
            raise ValueError("invalid refresh token")
        credentials.access_token = f"verified:{credentials.refresh_token}"
        credentials.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

    async def lookup(credentials):
        nonlocal active, peak
        assert credentials.access_token == f"verified:{credentials.refresh_token}"
        probes.append(credentials.refresh_token)
        peak = max(peak, active + 1)
        active += 1
        try:
            await asyncio.sleep(0.01)
            owner = owners[credentials.refresh_token]
            if isinstance(owner, Exception):
                raise owner
            return owner
        finally:
            active -= 1

    monkeypatch.setattr(config, "reload_config", reload_config)
    monkeypatch.setattr(dedup.Credentials, "refresh", refresh)
    monkeypatch.setattr(dedup, "get_user_email", lookup)

    async def add(email, owner, disabled=False):
        data = {
            "access_token": f"imported:{email}",
            "refresh_token": f"refresh:{email}",
            "client_id": "test-client",
            "client_secret": "test-client-secret",
            "project_id": f"project:{email}",
            "expiry": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "tier": "pro",
        }
        owners[data["refresh_token"]] = owner
        await db.store_credential(email, data)
        if disabled:
            await db.update_credential_state(email, {"disabled": True})
        return data

    app = FastAPI()
    app.include_router(creds.router)
    app.include_router(config_routes.router)
    app.dependency_overrides[creds.verify_panel_token] = lambda: "test-token"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://panel.test"
    ) as client:
        yield SimpleNamespace(
            db=db, app=app, client=client, add=add, owners=owners, probes=probes,
            lookup=lookup, peak=lambda: peak,
        )
    await db.close()


@pytest.mark.parametrize("value, expected", [
    ([403, 429, 403], [403, 429]),
    ("429, 403, 429", [403, 429]),
    (" [429, 403, 429] ", [403, 429]),
    ("403", [403]),
    ([100, 599], [100, 599]),
    ("", []),
    ("[]", []),
    ([], []),
])
def test_error_codes_supported_formats(value, expected):
    assert parse_error_codes(value) == expected


@pytest.mark.parametrize("value", [
    None, True, 403, {}, [True], ["403"], [403.0], [99], [600],
    "403abc,429", "403,", ",403", "403,,429", "403 429", "403，429",
    "[403,429", "403,429]", "[403,]", ' ["403"] ', "[403.5]", "[4e2]",
    "[403, false]", "[[403]]", "[null]", "99,429", "600", "0x193", "403.0",
])
def test_error_codes_reject_partial_or_non_integer_values(value):
    with pytest.raises(ValueError):
        parse_error_codes(value)


@pytest.mark.parametrize("key", CODE_KEYS)
@pytest.mark.parametrize("value", ["429,403,429", "[429,403,429]", [429, 403, 429]])
async def test_config_save_normalizes_every_code_field(feature_env, key, value):
    response = await feature_env.client.post("/config/save", json={"config": {key: value}})
    assert response.status_code == 200
    assert response.json()["saved_config"][key] == [403, 429]
    assert await feature_env.db.get_config(key) == [403, 429]
    response = await feature_env.client.get("/config/get")
    assert response.json()["config"][key] == [403, 429]


@pytest.mark.parametrize("key", CODE_KEYS)
@pytest.mark.parametrize("value", ["403oops,429", "[403,429", [True, 403], [600]])
async def test_invalid_code_field_rejects_entire_config_save(feature_env, key, value):
    before = await feature_env.db.get_all_config()
    response = await feature_env.client.post("/config/save", json={
        "config": {"host": "new-host", key: value},
    })
    assert response.status_code == 400
    assert "无效" in response.json()["detail"]
    assert await feature_env.db.get_all_config() == before


@pytest.mark.parametrize("key, env_var, getter, default", [
    ("auto_ban_error_codes", "AUTO_BAN_ERROR_CODES", config.get_auto_ban_error_codes, [403]),
    ("retry_error_codes", "RETRY_ERROR_CODES", config.get_retry_error_codes, [400, 403, 429, 500, 503]),
    ("request_log_error_codes", "REQUEST_LOG_ERROR_CODES", config.get_request_log_error_codes, [400]),
])
async def test_code_getters_environment_priority_and_fallback(
    feature_env, monkeypatch, key, env_var, getter, default
):
    assert await getter() == default
    config._config_cache[key] = [502]
    monkeypatch.setenv(env_var, "[429, 403]")
    assert await getter() == [403, 429]
    response = await feature_env.client.get("/config/get")
    assert key in response.json()["env_locked"]
    assert response.json()["config"][key] == [403, 429]
    monkeypatch.setenv(env_var, "403oops,429")
    assert await getter() == [502]
    monkeypatch.delenv(env_var)
    config._config_cache[key] = []
    assert await getter() == ([400] if key == "request_log_error_codes" else [])
    config._config_cache[key] = [True]
    assert await getter() == default


@pytest.mark.parametrize("status, codes, enabled, banned, expected", [
    (502, [502], True, False, True),
    (403, [429], True, True, False),
    (403, [403], True, False, True),
    (403, [403], False, True, False),
    (500, [], True, False, False),
])
async def test_retry_and_auto_ban_are_independent(
    feature_env, monkeypatch, status, codes, enabled, banned, expected
):
    config._config_cache.update({
        "retry_error_codes": codes, "auto_ban_enabled": banned,
        "auto_ban_error_codes": [status],
    })
    disable = AsyncMock()
    monkeypatch.setattr(api_utils, "set_cred_disabled", disable)
    assert await api_utils.handle_error_with_retry(status, "owner@example.test", enabled, 0, 2, 0) is expected
    assert disable.await_count == int(banned)
    assert await api_utils.handle_error_with_retry(status, "owner@example.test", enabled, 2, 2, 0) is False


async def test_deduplication_preview_uses_actual_owner_and_keeps_database_unchanged(feature_env):
    await feature_env.add("label-one@example.test", " OWNER@Example.Test ")
    await feature_env.add("label-two@example.test", "owner@example.test")
    await feature_env.add("unique@example.test", "different@example.test")
    before = await feature_env.db.get_credential_deduplication_snapshots()
    response = await feature_env.client.post("/creds/deduplicate", json={})
    assert response.status_code == 200
    data = response.json()
    assert data["dry_run"] is True and data["removed_count"] == 0
    assert data["verified_count"] == 3 and data["duplicate_count"] == 1
    assert data["groups"] == [{
        "actual_email": "owner@example.test", "keep_email": "label-one@example.test",
        "duplicate_emails": ["label-two@example.test"],
    }]
    assert await feature_env.db.get_credential_deduplication_snapshots() == before
    assert "refresh:" not in response.text and "test-client-secret" not in response.text


async def test_deduplication_preserves_winner_state_and_merges_account_restrictions(feature_env):
    alias = "alias@example.test"
    owner = "owner@example.test"
    await feature_env.add(alias, owner)
    await feature_env.add(owner, owner)
    await feature_env.add("other@example.test", "other@example.test")
    until = time.time() + 3600
    await feature_env.db.update_credential_state(owner, {
        "error_codes": [429], "paid": False, "paid_checked_at": 12345,
        "model_cooldowns": {"gemini": until}, "tier": "ultra",
    })
    await feature_env.db.update_credential_state(alias, {
        "model_cooldowns": {"gemini": until + 500, "claude_gpt": until + 1000},
    })
    for email, model in ((owner, "gemini-winner"), (alias, "gemini-other-token")):
        snapshot = await feature_env.db.get_credential_models_info(email)
        await feature_env.db.update_credential_state(email, {
            "supported_models": [model], "models_checked_at": time.time(),
            "models_context": snapshot["models_context"],
        })
    await feature_env.db.disable_credential_model(owner, "model-one")
    await feature_env.db.disable_credential_model(alias, "model-two")
    await feature_env.db.insert_usage_log({"credential_email": alias, "status_code": 200})
    before = await feature_env.db.get_credential_state(owner)
    scan = (await feature_env.client.post("/creds/deduplicate", json={})).json()
    assert scan["groups"][0]["keep_email"] == owner
    response = await feature_env.client.post("/creds/deduplicate", json={
        "dry_run": False, "preview": scan["preview"],
    })
    assert response.status_code == 200 and response.json()["removed_count"] == 1
    assert await feature_env.db.list_credentials() == [owner, "other@example.test"]
    after = await feature_env.db.get_credential_state(owner)
    for key in ("disabled", "error_codes", "paid", "paid_checked_at", "tier", "project_id"):
        assert after[key] == before[key]
    assert after["model_cooldowns"] == {"gemini": until + 500, "claude_gpt": until + 1000}
    assert await feature_env.db.get_disabled_model_ids(owner) == {"model-one", "model-two"}
    assert (await feature_env.db.get_credential(owner))["access_token"] == f"verified:refresh:{owner}"
    assert after["models_checked_at"] is None and after["supported_models"] is None
    async with feature_env.db.session() as session:
        raw_models = await session.scalar(storage.select(storage.AntigravityCredential.supported_models).where(
            storage.AntigravityCredential.email == owner,
        ))
    assert json.loads(raw_models) == ["gemini-winner"]
    logs, total = await feature_env.db.query_usage_logs()
    assert total == 1 and logs[0]["credential_email"] == alias


async def test_deduplication_prefers_enabled_record_over_disabled_canonical_label(feature_env):
    await feature_env.add("owner@example.test", "owner@example.test", disabled=True)
    await feature_env.add("enabled-alias@example.test", "owner@example.test")
    scan = (await feature_env.client.post("/creds/deduplicate", json={})).json()
    assert scan["groups"][0]["keep_email"] == "enabled-alias@example.test"


async def test_deduplication_does_not_enable_disabled_credentials(feature_env):
    await feature_env.add("alias@example.test", "owner@example.test", disabled=True)
    await feature_env.add("owner@example.test", "owner@example.test", disabled=True)
    scan = (await feature_env.client.post("/creds/deduplicate", json={})).json()
    response = await feature_env.client.post("/creds/deduplicate", json={
        "dry_run": False, "preview": scan["preview"],
    })
    assert response.status_code == 200 and response.json()["removed_count"] == 1
    assert (await feature_env.db.get_credential_state("owner@example.test"))["disabled"] is True


async def test_deduplication_never_deletes_unverified_credentials(feature_env):
    await feature_env.add("valid@example.test", "owner@example.test")
    await feature_env.add("invalid@example.test", RuntimeError("userinfo unavailable"))
    await feature_env.add("no-email@example.test", None)
    await feature_env.add("bad-email@example.test", "bad@")
    await feature_env.add("invalid-refresh@example.test", "owner@example.test")
    feature_env.owners.pop("refresh:invalid-refresh@example.test")
    before = await feature_env.db.list_credentials()
    scan = (await feature_env.client.post("/creds/deduplicate", json={})).json()
    assert scan["verified_count"] == 1 and scan["duplicate_count"] == 0
    assert len(scan["errors"]) == 4
    response = await feature_env.client.post("/creds/deduplicate", json={
        "dry_run": False, "preview": scan["preview"],
    })
    assert response.status_code == 200 and response.json()["removed_count"] == 0
    assert await feature_env.db.list_credentials() == before


async def test_selected_deduplication_does_not_expand_to_unselected_credentials(feature_env):
    emails = ["one@example.test", "two@example.test", "unselected@example.test"]
    for email in emails:
        await feature_env.add(email, "actual@example.test")
    selected = emails[:2] + [emails[0]]
    scan = (await feature_env.client.post("/creds/deduplicate", json={"emails": selected})).json()
    assert scan["total_count"] == 2 and scan["verified_count"] == 2
    response = await feature_env.client.post("/creds/deduplicate", json={
        "emails": selected, "dry_run": False, "preview": scan["preview"],
    })
    assert response.status_code == 200
    assert await feature_env.db.list_credentials() == [emails[0], emails[2]]
    assert "refresh:unselected@example.test" not in feature_env.probes


async def test_deduplication_requires_authentication_and_explicit_preview(feature_env):
    await feature_env.add("one@example.test", "owner@example.test")
    assert (await feature_env.client.post("/creds/deduplicate", json={"emails": []})).status_code == 400
    assert (await feature_env.client.post("/creds/deduplicate", json={"dry_run": False})).status_code == 400
    assert not feature_env.probes
    feature_env.app.dependency_overrides.clear()
    assert (await feature_env.client.post("/creds/deduplicate", json={})).status_code == 401
    assert not feature_env.probes


@pytest.mark.parametrize("change", ["credential", "disabled", "owner", "new_record"])
async def test_stale_deduplication_preview_is_rejected_without_cleanup(feature_env, change):
    one, two = "one@example.test", "two@example.test"
    original = await feature_env.add(one, "owner@example.test")
    await feature_env.add(two, "owner@example.test")
    scan = (await feature_env.client.post("/creds/deduplicate", json={})).json()
    if change == "credential":
        await feature_env.db.store_credential(one, {**original, "access_token": "updated-token"})
    elif change == "disabled":
        await feature_env.db.update_credential_state(one, {"disabled": True})
    elif change == "owner":
        feature_env.owners[original["refresh_token"]] = "changed-owner@example.test"
    else:
        await feature_env.add("new@example.test", "owner@example.test")
    before = await feature_env.db.list_credentials()
    response = await feature_env.client.post("/creds/deduplicate", json={
        "dry_run": False, "preview": scan["preview"],
    })
    assert response.status_code == 409 and "重新扫描" in response.json()["detail"]
    assert await feature_env.db.list_credentials() == before


async def test_database_rechecks_snapshots_after_remote_verification(feature_env, monkeypatch):
    for email, owner in [("one@example.test", "a@example.test"), ("two@example.test", "a@example.test"),
                         ("three@example.test", "b@example.test"), ("four@example.test", "b@example.test")]:
        await feature_env.add(email, owner)
    scan = (await feature_env.client.post("/creds/deduplicate", json={})).json()
    apply = feature_env.db.apply_credential_deduplication

    async def mutate_before_transaction(groups, verified):
        await feature_env.db.update_credential_state("four@example.test", {"disabled": True})
        return await apply(groups, verified)

    monkeypatch.setattr(feature_env.db, "apply_credential_deduplication", mutate_before_transaction)
    before = await feature_env.db.list_credentials()
    response = await feature_env.client.post("/creds/deduplicate", json={
        "dry_run": False, "preview": scan["preview"],
    })
    assert response.status_code == 409
    assert await feature_env.db.list_credentials() == before


async def test_failed_cleanup_rolls_back_prior_deletions_and_token_updates(feature_env, monkeypatch):
    for index in range(3):
        await feature_env.add(f"alias-{index}@example.test", "owner@example.test")
    scan = (await feature_env.client.post("/creds/deduplicate", json={})).json()
    before = await feature_env.db.get_credential_deduplication_snapshots()
    execute = AsyncSession.execute
    deletions = 0

    async def fail_second_delete(session, statement, *args, **kwargs):
        nonlocal deletions
        if isinstance(statement, Delete):
            deletions += 1
            if deletions == 2:
                raise RuntimeError("injected database failure")
        return await execute(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", fail_second_delete)
    response = await feature_env.client.post("/creds/deduplicate", json={
        "dry_run": False, "preview": scan["preview"],
    })
    assert response.status_code == 500 and deletions == 2
    assert await feature_env.db.get_credential_deduplication_snapshots() == before


async def test_actual_email_queries_are_bounded_to_four_workers(feature_env):
    for index in range(11):
        await feature_env.add(f"account-{index}@example.test", f"actual-{index}@example.test")
    response = await feature_env.client.post("/creds/deduplicate", json={})
    assert response.status_code == 200 and response.json()["verified_count"] == 11
    assert feature_env.peak() == 4


async def test_actual_email_timeout_preserves_credentials_and_reports_failure(feature_env, monkeypatch):
    await feature_env.add("one@example.test", "owner@example.test")
    await feature_env.add("two@example.test", "owner@example.test")
    monkeypatch.setattr(dedup, "CREDENTIAL_LOOKUP_TIMEOUT", 0.001)
    response = await feature_env.client.post("/creds/deduplicate", json={})
    assert response.status_code == 200
    assert response.json()["verified_count"] == 0 and response.json()["duplicate_count"] == 0
    assert all("超时" in error["message"] for error in response.json()["errors"])
    assert len(await feature_env.db.list_credentials()) == 2


async def test_disconnect_stops_queued_queries_and_never_applies_cleanup(feature_env, monkeypatch):
    for index in range(12):
        await feature_env.add(f"alias-{index}@example.test", "owner@example.test")

    async def disconnected(request):
        return len(feature_env.probes) >= 4

    monkeypatch.setattr(Request, "is_disconnected", disconnected)
    response = await feature_env.client.post("/creds/deduplicate", json={})
    assert response.status_code == 499
    assert len(feature_env.probes) == 4
    assert len(await feature_env.db.list_credentials()) == 12
