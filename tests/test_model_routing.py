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
from fastapi import FastAPI, Response

import config
from src import credential_manager as manager, session_cache
from src.storage import database as storage

# src.utils 导入时会查询 CLI 版本；测试只使用固定版本，禁止外部网络请求。
with patch("httpx.get", return_value=httpx.Response(
    200, json={"tag_name": "v1.4.12"}, request=httpx.Request("GET", "https://github.test")
)):
    from src.api import antigravity as api
    from src.converter.antigravity_fix import resolve_profile
    from src.panel import creds as panel
    from src.router.antigravity.model_list import get_antigravity_models_with_features


FLASH = "gemini-3.8-flash"
OLD_CLAUDE = "claude-sonnet-4-6"
NEW_CLAUDE = "claude-opus-5-5-high"
GEMINI4 = "gemini-4-argon-high"
NOT_FOUND_ERROR = {"error": {
    "code": 404,
    "message": "Requested entity was not found.",
    "status": "NOT_FOUND",
}}


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
            return httpx.Response(status, json=message if isinstance(message, dict) else {
                "error": {"message": message}
            })
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
    monkeypatch.setattr(panel, "get_database", AsyncMock(return_value=database))
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


def panel_app(authenticated=True):
    app = FastAPI()
    app.include_router(panel.router)
    if authenticated:
        app.dependency_overrides[panel.verify_panel_token] = lambda: "test-token"
    return app


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
    # 固定候选选择顺序，确保真正走过八个不匹配的候选和数据库排除过滤。
    monkeypatch.setattr(storage.random, "choice", lambda candidates: candidates[0])
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


@pytest.mark.parametrize("model", [None, NEW_CLAUDE])
async def test_pool_selection_excludes_candidates_before_random_choice(env, monkeypatch, model):
    excluded, _ = await env.add("excluded", [NEW_CLAUDE])
    disabled, _ = await env.add("disabled", [NEW_CLAUDE])
    cooling, _ = await env.add("cooling", [NEW_CLAUDE])
    available, _ = await env.add("available", [NEW_CLAUDE])
    await env.db.update_credential_state(disabled, {"disabled": True})
    await env.db.set_model_cooldown(cooling, NEW_CLAUDE, time.time() + 3600)
    monkeypatch.setattr(storage.random, "choice", lambda candidates: candidates[0])

    result = await env.db.get_next_available_credential(model, excluded_emails={excluded})
    assert result[0] == (cooling if model is None else available)
    assert result[1]["access_token"] == ("cooling" if model is None else "available")
    assert await env.db.get_next_available_credential(
        model, excluded_emails={excluded, cooling, available}
    ) is None
    assert not env.upstream.probes and not env.upstream.generated


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
    monkeypatch.setattr(manager, "time", SimpleNamespace(monotonic=lambda: now[0], time=time.time))
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


@pytest.mark.parametrize("models,tier,paid", [
    ([OLD_CLAUDE, FLASH], "ultra", False),
    ([NEW_CLAUDE, FLASH], "free", True),
    ([GEMINI4], "free", True),
    (["claude-sonnet-5-5-low", OLD_CLAUDE], "free", True),
    ([FLASH], "pro", None),
    ([], "pro", None),
    (None, "pro", None),
])
@pytest.mark.parametrize("disabled", [False, True])
async def test_panel_paid_marker_uses_cached_capabilities_without_generating(
    env, models, tier, paid, disabled
):
    email, _ = await env.add("token", models, tier=tier)
    await env.db.update_credential_state(email, {"disabled": disabled})
    await env.db.set_model_cooldown(email, NEW_CLAUDE, time.time() + 3600)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=panel_app()), base_url="http://panel.test"
    ) as client:
        for _ in range(2):
            response = await client.get(f"/creds/model-status/{email}")
            assert response.status_code == 200
            assert response.json() == {"email": email, "paid": paid}
    assert len(env.upstream.probes) == 1
    assert not env.upstream.generated
    state = await env.db.get_credential_state(email)
    assert state["disabled"] is disabled
    assert state["tier"] == tier


async def test_panel_paid_marker_refreshes_tokens_and_preserves_credential_state(env, monkeypatch):
    email, data = await env.add("expired", [OLD_CLAUDE], tier="ultra")
    data["expiry"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await env.db.store_credential(email, data)
    await env.db.update_credential_state(email, {"disabled": True})
    env.upstream.models["renewed"] = {NEW_CLAUDE: {}}

    async def refresh(credentials):
        credentials.access_token = "renewed"
        credentials.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

    monkeypatch.setattr(panel.Credentials, "refresh", refresh)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=panel_app()), base_url="http://panel.test"
    ) as client:
        response = await client.get(f"/creds/model-status/{email}")
    assert response.json()["paid"] is True
    saved = await env.db.get_credential(email)
    assert saved["access_token"] == "renewed"
    assert panel.Credentials.from_dict(saved).access_token == "renewed"
    assert saved["project_id"] == data["project_id"]
    state = await env.db.get_credential_state(email)
    assert state["disabled"] is True and state["tier"] == "ultra"
    assert not env.upstream.generated


async def test_panel_paid_marker_keeps_failed_token_refresh_unknown(env, monkeypatch):
    email, data = await env.add("expired", [OLD_CLAUDE])
    data["expiry"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await env.db.store_credential(email, data)
    monkeypatch.setattr(panel.Credentials, "refresh", AsyncMock(side_effect=RuntimeError("offline")))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=panel_app()), base_url="http://panel.test"
    ) as client:
        response = await client.get(f"/creds/model-status/{email}")
    assert response.status_code == 200 and response.json()["paid"] is None
    assert not env.upstream.probes and not env.upstream.generated
    assert (await env.db.get_credential_state(email))["disabled"] is False


async def test_panel_paid_marker_requires_authentication_and_existing_credential(env):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=panel_app(authenticated=False)),
        base_url="http://panel.test",
    ) as client:
        response = await client.get("/creds/model-status/missing@example.test")
    assert response.status_code in (401, 403)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=panel_app()), base_url="http://panel.test"
    ) as client:
        response = await client.get("/creds/model-status/missing@example.test")
    assert response.status_code == 404
    assert not env.upstream.probes and not env.upstream.generated


@pytest.mark.parametrize("models,paid", [([NEW_CLAUDE], True), ([OLD_CLAUDE], False), ([FLASH], None)])
async def test_panel_reentry_reuses_paid_status_after_cache_expiry_and_restart(env, monkeypatch, models, paid):
    email, data = await env.add("token", models)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=panel_app()), base_url="http://panel.test"
    ) as client:
        assert (await client.get(f"/creds/model-status/{email}")).json()["paid"] is paid
        assert (await env.db.get_credential_state(email))["paid_checked_at"] is not None
        manager._model_cache[email] = (manager._model_cache[email][0], -1, frozenset(models))
        assert (await client.get(f"/creds/model-status/{email}")).json()["paid"] is paid
        data["expiry"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        await env.db.store_credential(email, data)
        refresh = AsyncMock(side_effect=RuntimeError("should not refresh a saved Paid result"))
        monkeypatch.setattr(panel.Credentials, "refresh", refresh)
        manager._model_cache.clear()
        manager._model_cache_locks.clear()
        await env.db.close()
        await env.db.initialize()
        for _ in range(2):
            summary = (await client.get("/creds/status")).json()
            assert summary["items"][0]["paid"] is paid
            assert summary["items"][0]["paid_checked_at"] is not None
            assert (await client.get(f"/creds/model-status/{email}")).json()["paid"] is paid
    refresh.assert_not_awaited()
    assert len(env.upstream.probes) == 1
    assert not env.upstream.generated


async def test_explicit_paid_refresh_updates_saved_status_and_preserves_it_on_failure(env):
    email, _ = await env.add("token", [NEW_CLAUDE])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=panel_app()), base_url="http://panel.test"
    ) as client:
        assert (await client.get(f"/creds/model-status/{email}")).json()["paid"] is True
        env.upstream.models["token"] = {OLD_CLAUDE: {}}
        assert (await client.get(f"/creds/model-status/{email}")).json()["paid"] is True
        assert (await client.get(f"/creds/model-status/{email}?refresh=true")).json()["paid"] is False
        saved = await env.db.get_credential_state(email)
        env.upstream.models["token"] = None
        assert (await client.get(f"/creds/model-status/{email}?refresh=true")).json()["paid"] is False
        assert await env.db.get_credential_state(email) == saved
    assert len(env.upstream.probes) == 3
    assert not env.upstream.generated


async def test_paid_filter_applies_before_pagination_and_combines_with_other_filters(env):
    non_paid, _ = await env.add("non-paid", [OLD_CLAUDE], tier="ultra")
    await manager.get_credential_model_ids(non_paid, await env.db.get_credential(non_paid))
    unknown, _ = await env.add("unidentified", [FLASH])
    checked_unknown, data = await env.add("shared-only", [FLASH])
    await manager.get_credential_model_ids(checked_unknown, data)
    paid_emails = []
    for index in range(22):
        email, _ = await env.add(f"paid-{index}", [NEW_CLAUDE], tier="free")
        paid_emails.append(email)
        await env.db.update_credential_state(email, {
            "paid": True, "paid_checked_at": time.time(), "disabled": index == 21,
            "error_codes": [404] if index == 20 else [],
        })
    await env.db.set_model_cooldown(paid_emails[20], NEW_CLAUDE, time.time() + 3600)
    probes_before = len(env.upstream.probes)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=panel_app()), base_url="http://panel.test"
    ) as client:
        response = await client.get("/creds/status?paid_filter=paid&offset=20&limit=20")
        assert response.status_code == 200
        summary = response.json()
        assert summary["total"] == 22 and summary["has_more"] is False
        assert [item["email"] for item in summary["items"]] == paid_emails[20:]
        assert summary["stats"] == {"total": 25, "normal": 24, "disabled": 1}
        summary = (await client.get("/creds/status?paid_filter=non_paid")).json()
        assert summary["total"] == 1 and summary["items"][0]["email"] == non_paid
        summary = (await client.get("/creds/status?paid_filter=unknown")).json()
        assert summary["total"] == 2
        assert {item["email"] for item in summary["items"]} == {unknown, checked_unknown}
        summary = (await client.get("/creds/status", params={
            "paid_filter": "paid", "status_filter": "enabled", "tier_filter": "free",
            "error_code_filter": "404", "cooldown_filter": "in_cooldown",
        })).json()
        assert summary["total"] == 1 and summary["items"][0]["email"] == paid_emails[20]
        assert (await client.get("/creds/status?paid_filter=invalid")).status_code == 400
    assert len(env.upstream.probes) == probes_before
    assert not env.upstream.generated


async def test_model_discovery_404_does_not_disable_any_model(env, monkeypatch):
    email, _ = await env.add("token", [NEW_CLAUDE])
    monkeypatch.setattr(api, "post_async", AsyncMock(return_value=httpx.Response(404, json=NOT_FOUND_ERROR)))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=panel_app()), base_url="http://panel.test"
    ) as client:
        assert (await client.get(f"/creds/model-status/{email}")).json()["paid"] is None
    assert (await env.db.get_credential_state(email))["paid_checked_at"] is None
    assert await env.db.get_disabled_model_ids(email) == set()
    assert not env.upstream.generated


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
    config._config_cache["auto_ban_error_codes"] = [404]
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
    env.upstream.outcomes["first"] = [(404, NOT_FOUND_ERROR)]
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
    assert json.loads(api.record_api_call_error.await_args.kwargs["error_message"]) == NOT_FOUND_ERROR
    assert api.record_usage_log.await_count == 2


@pytest.mark.parametrize("stream", [False, True])
async def test_all_model_mismatches_stop_after_each_credential_once(env, stream):
    for index in range(5):
        label = f"token-{index}"
        await env.add(label, [NEW_CLAUDE])
        env.upstream.outcomes[label] = [(404, NOT_FOUND_ERROR)]
    result = await call_api(stream)
    assert result.status_code == 404
    tokens = [token for token, _ in env.upstream.generated]
    assert len(tokens) == len(set(tokens)) == 5


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode", ["random", "cache", "sticky"])
@pytest.mark.parametrize("discovery_fails", [False, True])
async def test_repeated_requests_skip_a_model_rejected_by_a_credential(
    env, monkeypatch, stream, mode, discovery_fails
):
    first, _ = await env.add("first", None if discovery_fails else [NEW_CLAUDE, FLASH])
    await env.add("second", [NEW_CLAUDE])
    monkeypatch.setattr(storage.random, "choice", lambda candidates: candidates[0])
    config._config_cache["credential_select_mode"] = mode
    body = request_body()
    env.upstream.outcomes["first"] = [(404, NOT_FOUND_ERROR)] * 2

    for _ in range(2):
        manager._sticky_email = first
        await session_cache.set_session_binding(api._derive_session_key(NEW_CLAUDE, body), first)
        result = await call_api(stream, body)
        assert isinstance(result, list) if stream else result.status_code == 200

    assert [token for token, _ in env.upstream.generated] == ["first", "second", "second"]
    assert (await env.db.get_credential_state(first))["disabled"] is False
    # 只跳过被拒绝的模型，不影响该凭证的其他能力。
    assert (await manager.get_valid_credential(FLASH))[0] == first


@pytest.mark.parametrize("stream", [False, True])
async def test_model_mismatch_stays_disabled_after_cache_expiry(env, monkeypatch, stream):
    first, _ = await env.add("first", [NEW_CLAUDE])
    await env.add("second", [NEW_CLAUDE])
    monkeypatch.setattr(storage.random, "choice", lambda candidates: candidates[0])
    now = [0.0]
    monkeypatch.setattr(manager, "time", SimpleNamespace(monotonic=lambda: now[0], time=time.time))
    env.upstream.outcomes["first"] = [(404, NOT_FOUND_ERROR)]
    await call_api(stream)
    assert (await manager.get_valid_credential(NEW_CLAUDE))[0] != first
    for now[0] in (301, 86400, 86400 * 365):
        assert (await manager.get_valid_credential(NEW_CLAUDE))[0] != first


@pytest.mark.parametrize("field", ["access_token", "project_id", "api_url"])
async def test_model_mismatch_stays_disabled_after_configuration_changes(env, monkeypatch, field):
    first, data = await env.add("first", [NEW_CLAUDE])
    await env.add("second", [NEW_CLAUDE])
    monkeypatch.setattr(storage.random, "choice", lambda candidates: candidates[0])
    env.upstream.outcomes["first"] = [(404, NOT_FOUND_ERROR)]
    await call_api(False)
    assert (await manager.get_valid_credential(NEW_CLAUDE))[0] != first

    if field == "api_url":
        config._config_cache["antigravity_api_url"] = "https://new-antigravity.test"
    else:
        data[field] = "new"
        env.upstream.models["new"] = {NEW_CLAUDE: {}}
        await env.db.store_credential(first, data)
    assert (await manager.get_valid_credential(NEW_CLAUDE))[0] != first


async def test_rediscovery_and_model_lists_keep_rejected_models_excluded(env, monkeypatch):
    first, data = await env.add("first", [NEW_CLAUDE, FLASH])
    second, _ = await env.add("second", [NEW_CLAUDE])
    monkeypatch.setattr(storage.random, "choice", lambda candidates: candidates[0])
    env.upstream.outcomes["first"] = [(404, NOT_FOUND_ERROR)]
    await call_api(False)
    await env.db.update_credential_state(second, {"disabled": True})
    # 上游列表仍包含被拒绝的模型，重新查询也不能恢复它。
    assert await manager.get_credential_model_ids(first, data) == {FLASH}
    assert {model["id"] for model in await api.fetch_available_models()} == {FLASH}
    assert await manager.get_valid_credential(NEW_CLAUDE) is None
    # 重新导入同一邮箱的凭证不会解除永久禁用。
    await manager.add_credential(first, data)
    assert await manager.get_valid_credential(NEW_CLAUDE) is None


async def test_inflight_discovery_cannot_restore_a_rejected_model(env, monkeypatch):
    email, data = await env.add("token", [NEW_CLAUDE, FLASH])
    started, release = asyncio.Event(), asyncio.Event()
    original = api.post_async

    async def delayed_post(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(api, "post_async", delayed_post)
    discovery = asyncio.create_task(manager.get_credential_model_ids(email, data))
    await started.wait()
    await manager.mark_credential_model_unavailable(email, NEW_CLAUDE)
    release.set()
    assert await discovery == {FLASH}
    assert await manager.get_credential_model_ids(email, data) == {FLASH}
    assert await manager.get_valid_credential(NEW_CLAUDE) is None


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode", ["random", "cache", "sticky"])
async def test_model_disable_survives_database_and_process_restart(env, monkeypatch, stream, mode):
    first, _ = await env.add("first", [NEW_CLAUDE, FLASH])
    await env.add("second", [NEW_CLAUDE])
    monkeypatch.setattr(storage.random, "choice", lambda candidates: candidates[0])
    config._config_cache["credential_select_mode"] = mode
    body = request_body()
    env.upstream.outcomes["first"] = [(404, NOT_FOUND_ERROR)]
    await call_api(stream, body)
    await env.db.close()
    manager._model_cache.clear()
    manager._model_cache_locks.clear()
    manager._reset_sticky()
    session_cache._memory_store.clear()

    restarted = storage.Database()
    await restarted.initialize()
    monkeypatch.setattr(manager, "get_database", AsyncMock(return_value=restarted))
    try:
        manager._sticky_email = first
        await session_cache.set_session_binding(api._derive_session_key(NEW_CLAUDE, body), first)
        result = await call_api(stream, body)
        assert isinstance(result, list) if stream else result.status_code == 200
        assert [token for token, _ in env.upstream.generated] == ["first", "second", "second"]
        assert (await manager.get_valid_credential(FLASH))[0] == first
        assert await restarted.get_disabled_model_ids(first) == {NEW_CLAUDE}
    finally:
        await restarted.close()


async def test_concurrent_model_disables_are_idempotent_and_do_not_lose_models(env):
    email, _ = await env.add("token", [NEW_CLAUDE, GEMINI4, FLASH])
    await asyncio.gather(*(
        manager.mark_credential_model_unavailable(email, model)
        for model in [NEW_CLAUDE, GEMINI4] * 5
    ))
    assert await env.db.get_disabled_model_ids(email) == {NEW_CLAUDE, GEMINI4}
    assert await manager.get_valid_credential(NEW_CLAUDE) is None
    assert await manager.get_valid_credential(GEMINI4) is None
    assert (await manager.get_valid_credential(FLASH))[0] == email
    assert not env.upstream.generated


async def test_other_database_workers_apply_permanent_disables_to_cached_models(env):
    email, data = await env.add("token", [NEW_CLAUDE, FLASH])
    assert await manager.get_credential_model_ids(email, data) == {NEW_CLAUDE, FLASH}
    worker = storage.Database()
    await worker.initialize()
    try:
        await worker.disable_credential_model(email, NEW_CLAUDE)
        # 另一数据库连接写入后，已有的模型列表缓存也必须过滤该模型。
        assert await manager.get_credential_model_ids(email, data) == {FLASH}
        assert await manager.get_valid_credential(NEW_CLAUDE) is None
        assert (await manager.get_valid_credential(FLASH))[0] == email
        assert len(env.upstream.probes) == 1
    finally:
        await worker.close()


async def test_model_disable_table_upgrade_preserves_existing_data(env):
    email, data = await env.add("existing", [NEW_CLAUDE], tier="ultra")
    await env.db.update_credential_state(email, {"disabled": True, "error_codes": [403]})
    await env.db.set_config("existing-option", "keep")
    await env.db.insert_usage_log({"credential_email": email, "status_code": 403})
    before_data = await env.db.get_credential(email)
    before_state = await env.db.get_credential_state(email)
    # 模拟已有凭证、配置和日志，但尚无 Paid 字段及模型禁用表的原版数据库。
    async with env.db._engine.begin() as connection:
        await connection.run_sync(storage.DisabledCredentialModel.__table__.drop)
        await connection.execute(storage.text("ALTER TABLE antigravity_credentials DROP COLUMN paid"))
        await connection.execute(storage.text("ALTER TABLE antigravity_credentials DROP COLUMN paid_checked_at"))
    await env.db.close()
    await env.db.initialize()
    assert await env.db.get_credential(email) == before_data
    assert await env.db.get_credential_state(email) == before_state
    assert await env.db.get_config("existing-option") == "keep"
    async with env.db.session() as session:
        assert await session.scalar(storage.select(storage.UsageLog.credential_email)) == email
    await env.db.disable_credential_model(email, NEW_CLAUDE)
    assert await env.db.get_disabled_model_ids(email) == {NEW_CLAUDE}


async def test_permanently_disabled_model_does_not_refresh_an_expired_token(env, monkeypatch):
    email, data = await env.add("expired", [NEW_CLAUDE])
    data["expiry"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await env.db.store_credential(email, data)
    await manager.mark_credential_model_unavailable(email, NEW_CLAUDE)
    refresh = AsyncMock()
    monkeypatch.setattr(manager, "refresh_token", refresh)
    assert await manager.get_valid_credential(NEW_CLAUDE) is None
    refresh.assert_not_awaited()
    assert not env.upstream.probes and not env.upstream.generated


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("message", [
    "Invalid thought_signature for model.",
    "Requested model not found.",
    "Function call not found for requested model.",
])
async def test_ordinary_400_keeps_existing_error_handling(env, stream, message):
    email, _ = await env.add("token", [NEW_CLAUDE])
    env.upstream.outcomes["token"] = [(400, message)]
    result = await call_api(stream)
    assert result.status_code == 400
    assert len(env.upstream.generated) == 1
    assert email in manager._model_cache
    assert await env.db.get_disabled_model_ids(email) == set()


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


async def test_export_import_reuses_token_and_preserves_paid_and_model_disables(env, monkeypatch):
    email, data = await env.add("token", [NEW_CLAUDE, GEMINI4, FLASH])
    await manager.get_credential_model_ids(email, data)
    await env.db.disable_credential_model(email, NEW_CLAUDE)
    before_state = await env.db.get_credential_state(email)
    refresh = AsyncMock(side_effect=AssertionError("未过期的导入 access_token 应直接复用"))
    monkeypatch.setattr(panel.Credentials, "refresh", refresh)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=panel_app()), base_url="http://panel.test"
    ) as client:
        response = await client.get(f"/creds/export/{email}")
    assert response.status_code == 200
    exported = response.json()
    assert exported == {
        "email": email,
        "refresh_token": data["refresh_token"],
        "access_token": data["access_token"],
        "expiry": data["expiry"],
        "tier": data["tier"],
        "project_id": data["project_id"],
    }

    result = await panel._import_single_credential(exported)
    assert result["status"] == "success"
    refresh.assert_not_awaited()
    assert email not in manager._model_cache
    stored = await env.db.get_credential(email)
    assert stored["access_token"] == data["access_token"]
    assert stored["expiry"] == data["expiry"]
    after_state = await env.db.get_credential_state(email)
    assert after_state["paid"] is before_state["paid"] is True
    assert after_state["paid_checked_at"] == before_state["paid_checked_at"]
    assert await env.db.get_disabled_model_ids(email) == {NEW_CLAUDE}
    assert len(env.upstream.probes) == 1 and not env.upstream.generated
