"""数据库能力刷新、过期、并发及上游路由问题的回归测试。"""

import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

import config
from src import auth
from test_model_routing import (
    FLASH, GEMINI4, NEW_CLAUDE, OLD_CLAUDE, NOT_FOUND_ERROR,
    api, call_api, env, manager, panel, panel_app, storage,
)


async def test_successful_empty_list_is_known_and_not_reprobed(env):
    email, data = await env.add("empty", [])
    assert await manager.get_credential_model_ids(email, data) == frozenset()
    state = await env.db.get_credential_state(email)
    assert state["models_checked_at"] is not None
    for _ in range(3):
        assert await manager.get_valid_credential(FLASH) is None
        assert await api.fetch_available_models() == []
    assert len(env.upstream.probes) == 1
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=panel_app()), base_url="http://panel.test") as client:
        response = await client.post(f"/creds/refresh-models/{email}")
        assert response.json()["supported_models"] == []
        assert response.json()["count"] == 0
        assert response.json()["models_checked_at"] >= state["models_checked_at"]


async def test_failure_backoff_is_shared_by_database_workers(env, monkeypatch):
    email, data = await env.add("offline", None)
    assert await manager.get_credential_model_ids(email, data) is None
    state = await env.db.get_credential_state(email)
    assert state["models_checked_at"] is None
    assert time.time() < state["models_retry_after"] <= time.time() + 30
    worker = storage.Database()
    await worker.initialize()
    monkeypatch.setattr(manager, "get_database", AsyncMock(return_value=worker))
    try:
        assert await manager.get_credential_model_ids(email, data) is None
        assert await api.fetch_available_models() == []
        assert len(env.upstream.probes) == 1
        await worker.update_credential_state(email, {"models_retry_after": time.time() - 1})
        env.upstream.models["offline"] = {FLASH: {}}
        assert await manager.get_credential_model_ids(email, data) == {FLASH}
        assert len(env.upstream.probes) == 2
    finally:
        await worker.close()


@pytest.mark.parametrize("trigger", ["models", "generation"])
async def test_account_upgrade_is_discovered_on_demand(env, trigger):
    email, data = await env.add("upgrade", [OLD_CLAUDE])
    await manager.get_credential_model_ids(email, data)
    env.upstream.models["upgrade"] = {OLD_CLAUDE: {}, NEW_CLAUDE: {}}
    assert NEW_CLAUDE not in (await env.db.get_credential_state(email))["supported_models"]
    await env.db.update_credential_state(email, {"models_checked_at": time.time() - 301})
    if trigger == "models":
        assert NEW_CLAUDE in {model["id"] for model in await api.fetch_available_models()}
    else:
        assert (await call_api(False)).status_code == 200
    state = await env.db.get_credential_state(email)
    assert state["paid"] is True
    assert len(env.upstream.probes) == 2


async def test_complete_union_beyond_twenty_credentials_has_global_probe_limit(env, monkeypatch):
    for index in range(27):
        await env.add(f"account-{index}", [f"gemini-exclusive-{index}"])
    original = api.post_async
    active = peak = 0

    async def slow_probe(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return await original(*args, **kwargs)
        finally:
            active -= 1

    monkeypatch.setattr(api, "post_async", slow_probe)
    results = await asyncio.gather(api.fetch_available_models(), api.fetch_available_models())
    expected = {f"gemini-exclusive-{index}" for index in range(27)}
    assert all({model["id"] for model in result} == expected for result in results)
    assert 1 < peak <= 8
    assert len(env.upstream.probes) == 27


@pytest.mark.parametrize("field", ["access_token", "project_id", "api_url"])
async def test_failed_new_context_does_not_route_old_capabilities_or_erase_paid(env, field):
    email, data = await env.add("old", [NEW_CLAUDE])
    await manager.get_credential_model_ids(email, data)
    before = await env.db.get_credential_state(email)
    env.upstream.models["old"] = None
    if field == "api_url":
        config._config_cache["antigravity_api_url"] = "https://changed.test"
    else:
        data[field] = "changed"
        env.upstream.models["changed"] = None
        await env.db.store_credential(email, data)
    assert await manager.get_credential_model_ids(email, data) is None
    after = await env.db.get_credential_state(email)
    assert after["supported_models"] is None
    assert after["models_checked_at"] is None
    assert after["paid"] is before["paid"] is True
    assert after["paid_checked_at"] == before["paid_checked_at"]
    assert (await env.db.get_credentials_summary())["items"][0]["supported_models"] is None
    assert await api.fetch_available_models() == []
    assert len(env.upstream.probes) == 2


@pytest.mark.parametrize("field", ["access_token", "project_id", "api_url"])
async def test_late_probe_cannot_overwrite_new_context(env, monkeypatch, field):
    email, data = await env.add("old", [OLD_CLAUDE])
    started, release = asyncio.Event(), asyncio.Event()
    original = api.post_async
    old_context = True

    async def delayed(*args, **kwargs):
        nonlocal old_context
        if old_context:
            old_context = False
            response = await original(*args, **kwargs)
            started.set()
            await release.wait()
            return response
        return await original(*args, **kwargs)

    monkeypatch.setattr(api, "post_async", delayed)
    pending = asyncio.create_task(api.refresh_credential_models(email))
    await asyncio.wait_for(started.wait(), 3)
    env.upstream.models["old"] = {NEW_CLAUDE: {}}
    if field == "api_url":
        config._config_cache["antigravity_api_url"] = "https://changed.test"
    else:
        data[field] = "changed"
        env.upstream.models["changed"] = {NEW_CLAUDE: {}}
        await env.db.store_credential(email, data)
    assert await api.refresh_credential_models(email) == [NEW_CLAUDE]
    release.set()
    assert await pending is None
    assert (await env.db.get_credential_state(email))["supported_models"] == [NEW_CLAUDE]
    assert (await env.db.get_credential_state(email))["paid"] is True


async def test_late_token_refresh_does_not_overwrite_reimported_credential(env, monkeypatch):
    email, data = await env.add("expired", [OLD_CLAUDE])
    data["expiry"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await env.db.store_credential(email, data)
    started, release = asyncio.Event(), asyncio.Event()

    async def refresh(credentials):
        started.set()
        await release.wait()
        credentials.access_token = "stale-refreshed"
        credentials.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

    monkeypatch.setattr(manager.Credentials, "refresh", refresh)
    pending = asyncio.create_task(api.refresh_credential_models(email))
    await asyncio.wait_for(started.wait(), 3)
    data.update(access_token="new-import", expiry=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    env.upstream.models["new-import"] = {NEW_CLAUDE: {}}
    await env.db.store_credential(email, data)
    assert await api.refresh_credential_models(email) == [NEW_CLAUDE]
    release.set()
    assert await pending is None
    assert (await env.db.get_credential(email))["access_token"] == "new-import"
    assert (await env.db.get_credential_state(email))["supported_models"] == [NEW_CLAUDE]


async def test_cancelled_waiter_does_not_cancel_shared_refresh(env, monkeypatch):
    email, data = await env.add("token", [FLASH])
    started, release = asyncio.Event(), asyncio.Event()
    original = api.post_async

    async def delayed(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(api, "post_async", delayed)
    first = asyncio.create_task(manager.get_credential_model_ids(email, data))
    await asyncio.wait_for(started.wait(), 3)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(manager.get_credential_model_ids(email, data))
    release.set()
    assert await second == {FLASH}
    assert len(env.upstream.probes) == 1


async def test_manual_refresh_upgrades_queued_automatic_refresh(env, monkeypatch):
    email, data = await env.add("token", [OLD_CLAUDE])
    await manager.get_credential_model_ids(email, data)
    env.upstream.models["token"] = {NEW_CLAUDE: {}}
    gate = asyncio.Semaphore(0)
    monkeypatch.setattr(api, "_model_refresh_semaphore", gate)
    automatic = asyncio.create_task(api.refresh_credential_models(email, force=False))
    manual = asyncio.create_task(api.refresh_credential_models(email, force=True))
    try:
        async with asyncio.timeout(3):
            while not api._model_refresh_forced:
                await asyncio.sleep(0.01)
        assert len(api._model_refresh_tasks) == 1
        gate.release()
        assert await automatic == [NEW_CLAUDE]
        assert await manual == [NEW_CLAUDE]
        assert len(env.upstream.probes) == 2
    finally:
        gate.release()
        await asyncio.gather(automatic, manual, return_exceptions=True)


async def test_late_permanent_token_failure_does_not_disable_new_import(env, monkeypatch):
    email, data = await env.add("expired", [OLD_CLAUDE])
    data["expiry"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await env.db.store_credential(email, data)
    started, release = asyncio.Event(), asyncio.Event()

    async def refresh(credentials):
        started.set()
        await release.wait()
        raise RuntimeError("invalid_grant")

    monkeypatch.setattr(manager.Credentials, "refresh", refresh)
    pending = asyncio.create_task(api.refresh_credential_models(email))
    await asyncio.wait_for(started.wait(), 3)
    data.update(access_token="new-token", expiry=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    await env.db.store_credential(email, data)
    release.set()
    assert await pending is None
    state = await env.db.get_credential_state(email)
    assert state["disabled"] is False and state["error_codes"] == []


async def test_failure_cannot_overwrite_another_workers_success(env):
    email, data = await env.add("token", [OLD_CLAUDE])
    await manager.get_credential_model_ids(email, data)
    earlier = await env.db.get_credential_models_info(email)
    newer = await env.db.get_credential_models_info(email)
    assert await env.db.save_credential_models(email, newer, [NEW_CLAUDE], True)
    assert not await env.db.save_credential_models(email, earlier, None, None)
    state = await env.db.get_credential_state(email)
    assert state["supported_models"] == [NEW_CLAUDE]
    assert state["paid"] is True and state["models_retry_after"] is None


@pytest.mark.parametrize("stream", [False, True])
async def test_direct_api_alias_generates_and_bans_real_upstream_id(env, stream):
    real = "claude-opus-4-6-thinking"
    email, _ = await env.add("token", [real])
    env.upstream.outcomes["token"] = [(404, NOT_FOUND_ERROR)]
    body = {"model": "claude-opus-4-6", "request": {"contents": []}}
    assert (await call_api(stream, body)).status_code == 404
    assert env.upstream.generated[0][1]["model"] == real
    assert await env.db.get_disabled_model_ids(email) == {real}
    assert await api.fetch_available_models() == []


async def test_manual_refresh_bypasses_backoff_and_updates_paid(env):
    email, data = await env.add("token", None)
    await manager.get_credential_model_ids(email, data)
    env.upstream.models["token"] = {NEW_CLAUDE: {}}
    assert await manager.get_credential_model_ids(email, data) is None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=panel_app()), base_url="http://panel.test") as client:
        result = await client.post(f"/creds/refresh-models/{email}")
        assert result.json()["paid"] is True
        assert result.json()["supported_models"] == [NEW_CLAUDE]
        assert result.json()["models_checked_at"] is not None
    assert len(env.upstream.probes) == 2


async def test_batch_refresh_handles_empty_and_failed_results_independently(env):
    good, _ = await env.add("good", [NEW_CLAUDE])
    empty, _ = await env.add("empty", [])
    failed, _ = await env.add("failed", None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=panel_app()), base_url="http://panel.test") as client:
        result = await client.post("/creds/batch-action", json={"action": "refresh_models", "emails": [good, empty, failed]})
    assert result.json()["success_count"] == 2
    assert result.json()["total_count"] == 3
    assert len(result.json()["errors"]) == 1
    assert (await env.db.get_credential_state(empty))["models_checked_at"] is not None


async def test_three_failed_token_checks_do_not_hide_fourth_candidate(env, monkeypatch):
    for index in range(3):
        email, data = await env.add(f"expired-{index}", [FLASH])
        data["expiry"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        await env.db.store_credential(email, data)
    fourth, _ = await env.add("fourth", [FLASH])
    monkeypatch.setattr(manager, "refresh_token", AsyncMock(return_value=None))
    monkeypatch.setattr(storage.random, "choice", lambda rows: rows[0])
    assert (await manager.get_valid_credential(FLASH))[0] == fourth
    assert manager.refresh_token.await_count == 3
    assert [probe[1] for probe in env.upstream.probes] == ["fourth"]


@pytest.mark.parametrize("stream", [False, True])
async def test_removing_last_model_then_refreshing_never_restores_route(env, stream):
    email, _ = await env.add("last", [NEW_CLAUDE])
    env.upstream.outcomes["last"] = [(404, NOT_FOUND_ERROR)]
    assert (await call_api(stream)).status_code == 404
    assert (await env.db.get_credential_state(email))["supported_models"] == []
    assert await api.refresh_credential_models(email) == []
    assert await manager.get_valid_credential(NEW_CLAUDE) is None
    assert await api.fetch_available_models() == []
    assert len(env.upstream.generated) == 1


async def test_separate_process_ban_is_immediately_visible(env):
    email, data = await env.add("token", [NEW_CLAUDE, FLASH])
    await manager.get_credential_model_ids(email, data)
    script = """import asyncio
import sys
from src.storage.database import Database
async def main():
    database = Database()
    await database.initialize()
    await database.disable_credential_model(sys.argv[1], sys.argv[2])
    await database.close()
asyncio.run(main())
"""
    completed = subprocess.run([sys.executable, "-c", script, email, NEW_CLAUDE], capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    assert await manager.get_credential_model_ids(email, data) == {FLASH}
    assert await manager.get_valid_credential(NEW_CLAUDE) is None
    assert {model["id"] for model in await api.fetch_available_models()} == {FLASH}
    assert len(env.upstream.probes) == 1


async def test_imported_models_are_untrusted_until_database_refresh(env):
    email, data = await env.add("token", [FLASH])
    data.update(email=email, supported_models=[NEW_CLAUDE], models_checked_at=time.time() + 3600)
    result = await panel._import_single_credential(data)
    assert result["status"] == "success"
    assert (await env.db.get_credential_state(email))["models_checked_at"] is None
    assert await manager.get_valid_credential(NEW_CLAUDE) is None
    assert await manager.get_credential_model_ids(email, data) == {FLASH}
    assert len(env.upstream.probes) == 1


async def test_oauth_probe_failure_keeps_saved_credential(env, monkeypatch):
    email, data = await env.add("oauth", None)
    monkeypatch.setattr(auth, "get_database", AsyncMock(return_value=env.db))
    monkeypatch.setattr(auth, "get_user_email", AsyncMock(return_value=email))
    monkeypatch.setattr(auth, "fetch_project_id_and_tier", AsyncMock(return_value=(data["project_id"], "pro")))
    monkeypatch.setattr(auth, "_cleanup_auth_flow_server", lambda state: None)
    result = await auth._save_antigravity_credential(manager.Credentials.from_dict(data), "test", {})
    assert result["success"] is True
    assert (await env.db.get_credential(email))["access_token"] == "oauth"
    assert (await env.db.get_credential_state(email))["models_retry_after"] > time.time()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
async def test_capability_column_migration_preserves_legacy_state(env, monkeypatch, backend):
    database = env.db
    if backend == "postgresql":
        uri = os.environ.get("GCLI_TEST_POSTGRESQL_URI")
        if not uri:
            pytest.skip("Set GCLI_TEST_POSTGRESQL_URI to an isolated test database")
        monkeypatch.setenv("POSTGRESQL_URI", uri)
        database = storage.Database()
        await database.initialize()
    try:
        email = "legacy@example.test"
        data = {"access_token": "legacy", "refresh_token": "legacy-refresh", "project_id": "legacy-project"}
        await database.store_credential(email, data)
        await database.update_credential_state(email, {
            "paid": True, "paid_checked_at": 1234, "tier": "ultra", "disabled": True,
            "error_codes": [403], "supported_models": [NEW_CLAUDE],
        })
        await database.disable_credential_model(email, NEW_CLAUDE)
        await database.set_config("legacy-option", "retained")
        await database.insert_usage_log({"credential_email": email, "status_code": 403})
        async with database._engine.begin() as connection:
            for column in ("models_context", "models_checked_at", "models_retry_after"):
                await connection.execute(storage.text(f"ALTER TABLE antigravity_credentials DROP COLUMN {column}"))
        await database.close()
        await database.initialize()
        state = await database.get_credential_state(email)
        assert state["paid"] is True and state["paid_checked_at"] == 1234
        assert state["tier"] == "ultra" and state["disabled"] is True
        assert state["error_codes"] == [403]
        assert state["models_checked_at"] is None and state["supported_models"] is None
        assert await database.get_disabled_model_ids(email) == {NEW_CLAUDE}
        assert await database.get_config("legacy-option") == "retained"
        async with database.session() as session:
            assert await session.scalar(storage.select(storage.UsageLog.credential_email).where(
                storage.UsageLog.credential_email == email,
            )) == email
        # 验证迁移后的条件能力更新、上下文过期和持久禁用在两种后端均生效。
        snapshot = await database.get_credential_models_info(email)
        assert await database.save_credential_models(email, snapshot, [NEW_CLAUDE, FLASH], True)
        assert (await database.get_credential_state(email))["supported_models"] == [FLASH]
        await database.update_credential_state(email, {"disabled": False})
        assert (await database.get_next_available_credential(FLASH))[0] == email
        assert await database.get_next_available_credential(NEW_CLAUDE) is None
        data["access_token"] = "replacement"
        await database.store_credential(email, data)
        assert (await database.get_credential_state(email))["supported_models"] is None
        assert not await database.save_credential_models(email, snapshot, [NEW_CLAUDE], True)
        assert await database.get_disabled_model_ids(email) == {NEW_CLAUDE}
    finally:
        if database is not env.db:
            await database.close()
