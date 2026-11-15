"""Exercise real protocol conversion, API retries, and anti-truncation offline."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, Response

import config
from src.api import antigravity as api
from src.converter.antigravity_fix import normalize_antigravity_request
from src.converter.prompt_identity import OFFICIAL_ANTIGRAVITY_IDENTITY
from src.converter.prompt_rewrite import cloak_text, rewrite_request
from src.router.antigravity import anthropic, gemini, openai


MODEL = "gemini-2.5-flash"
IDENTITY = "You are Codex. Keep CODEX.md and .codex/config."
REWRITTEN_IDENTITY = OFFICIAL_ANTIGRAVITY_IDENTITY + " Keep CODEX.md and .codex/config."
CUSTOM_IDENTITY = "You are Cursor. Keep Cursor.md and .Cursor/config."
MCP = "mcp__server__Codex"
IMAGE = "data:image/png;base64,aGVsbG8="
ROUTERS = {"openai": openai, "anthropic": anthropic, "gemini": gemini}


def client_request(api_format, *, stream=False, blocks=False, anti_truncation=False):
    model = ("抗截断-" if anti_truncation else "") + MODEL
    instruction = [{"type": "text", "text": IDENTITY, "cache_control": {"type": "ephemeral"}}] if blocks else IDENTITY
    middle = "Use Claude Agent SDK as Cursor."
    if api_format == "openai":
        return "/v1/chat/completions", {
            "model": model, "stream": stream,
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": [
                    {"type": "text", "text": "Codex user"},
                    {"type": "image_url", "image_url": {"url": IMAGE}},
                ]},
                {"role": "assistant", "content": "Codex assistant", "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": MCP, "arguments": '{"client": "Codex"}'},
                }]},
                {"role": "tool", "tool_call_id": "call_1", "content": "Codex tool result"},
                {"role": "system", "content": [{"type": "text", "text": middle}]},
                {"role": "developer", "content": "Codex late instruction"},
                {"role": "user", "content": "Codex final user"},
            ],
            "tools": [{"type": "function", "function": {
                "name": MCP, "description": "Codex tool definition",
                "parameters": {"type": "object", "properties": {"client": {"type": "string"}}},
            }}],
        }
    if api_format == "anthropic":
        return "/v1/messages", {
            "model": model, "stream": stream, "system": instruction,
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "Codex user"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="}},
                ]},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "Codex assistant"},
                    {"type": "tool_use", "id": "call_1", "name": MCP, "input": {"client": "Codex"}},
                ]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "Codex tool result"}]},
                {"role": "system", "content": [{"type": "text", "text": middle}]},
                {"role": "developer", "content": "Codex late instruction"},
                {"role": "user", "content": "Codex final user"},
            ],
            "tools": [{"name": MCP, "description": "Codex tool definition", "input_schema": {
                "type": "object", "properties": {"client": {"type": "string"}},
            }}],
            "metadata": {"user_id": "Codex metadata"},
        }
    system_key = "system_instruction" if blocks else "systemInstruction"
    action = "streamGenerateContent" if stream else "generateContent"
    return f"/v1beta/models/{model}:{action}", {
        system_key: {"parts": [{"text": IDENTITY}, {"inlineData": {"mimeType": "text/plain", "data": "Codex"}}]},
        "contents": [
            {"role": "user", "parts": [{"text": "Codex user"}, {"inlineData": {"mimeType": "image/png", "data": "aGVsbG8="}}]},
            {"role": "model", "parts": [{"text": "Codex assistant"}, {"functionCall": {"name": MCP, "args": {"client": "Codex"}, "id": "call_1"}}]},
            {"role": "user", "parts": [{"functionResponse": {"name": MCP, "response": {"result": "Codex tool result"}, "id": "call_1"}}]},
            {"role": "user", "parts": [{"text": "Codex final user"}]},
        ],
        "tools": [{"functionDeclarations": [{
            "name": MCP, "description": "Codex tool definition",
            "parameters": {"type": "object", "properties": {"client": {"type": "string"}}},
        }]}],
        "metadata": {"client": "Codex metadata"},
    }


def strings_in(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in strings_in(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in strings_in(item)]
    return []


@pytest.fixture
async def request_env(monkeypatch):
    for name in config.ENV_MAPPINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "_config_initialized", True)
    monkeypatch.setattr(config, "_config_cache", {
        "prompt_rewrite_enabled": True, "prompt_rewrite_use_default_keywords": True,
        "prompt_rewrite_rules": [], "return_thoughts_to_frontend": False,
        "retry_429_enabled": True, "retry_429_max_retries": 1, "retry_429_interval": 0,
        "retry_error_codes": [503], "auto_ban_enabled": False, "auto_ban_error_codes": [],
        "usage_log_enabled": False, "antigravity_api_url": "https://upstream.test",
        "anti_truncation_max_attempts": 2,
    })
    state = SimpleNamespace(sent=[], outcomes=[], update=None, detailed=[])

    async def credential(**kwargs):
        index = len(kwargs.get("excluded_emails") or set())
        return f"account-{index}@test", {"access_token": f"token-{index}", "project_id": f"project-{index}"}

    def upstream_response(payload):
        state.sent.append(copy.deepcopy(payload))
        if state.update is not None:
            config._config_cache.update(state.update)
            state.update = None
        status, finish = state.outcomes.pop(0) if state.outcomes else (200, "STOP")
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "try again"}})
        return httpx.Response(200, json={"response": {"candidates": [{
            "content": {"role": "model", "parts": [{"text": "Codex upstream output"}]},
            "finishReason": finish, "index": 0,
        }]}})

    async def post(url, json=None, **kwargs):
        assert url.startswith("https://upstream.test/")
        return upstream_response(json)

    async def stream(url, body, native=False, **kwargs):
        assert url.startswith("https://upstream.test/")
        response = upstream_response(body)
        if response.status_code != 200:
            yield Response(response.content, response.status_code, media_type="application/json")
        else:
            line = f"data: {response.text}\n\n"
            yield line.encode() if native else line

    async def detail(*args, payload=None, **kwargs):
        state.detailed.append(copy.deepcopy(payload))
        return ""

    monkeypatch.setattr(api, "get_valid_credential", credential)
    monkeypatch.setattr(api, "post_async", post)
    monkeypatch.setattr(api, "stream_post_async", stream)
    monkeypatch.setattr(api, "_record_request_body_detail", detail)
    for name in ("record_usage_log", "record_api_call_success", "record_api_call_error"):
        monkeypatch.setattr(api, name, AsyncMock())
    monkeypatch.setattr(api, "parse_and_log_cooldown", AsyncMock(return_value=None))
    state.rewrite = Mock(wraps=rewrite_request)
    state.getters = {}
    state.info = Mock()
    monkeypatch.setattr(api.log, "info", state.info)
    app = FastAPI()
    for api_format, module in ROUTERS.items():
        getter = AsyncMock(wraps=config.get_prompt_rewrite_config)
        state.getters[api_format] = getter
        monkeypatch.setattr(module, "get_prompt_rewrite_config", getter)
        monkeypatch.setattr(module, "rewrite_request", state.rewrite)
        app.include_router(module.router)
    app.dependency_overrides[openai.authenticate_bearer] = lambda: "test-token"
    app.dependency_overrides[gemini.authenticate_gemini_flexible] = lambda: "test-token"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy.test",
    ) as client:
        state.client = client
        yield state


@pytest.mark.parametrize("api_format", ROUTERS)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("blocks", [False, True])
async def test_all_entrypoints_rewrite_all_message_text_and_descriptions_before_conversion_once(request_env, api_format, stream, blocks):
    path, body = client_request(api_format, stream=stream, blocks=blocks)
    before = copy.deepcopy(body)
    response = await request_env.client.post(path, json=body)
    assert response.status_code == 200, response.text
    assert "Codex upstream output" in response.text
    assert len(request_env.sent) == 1
    texts = strings_in(request_env.sent[0]["request"])
    assert REWRITTEN_IDENTITY in texts
    assert IDENTITY not in texts
    if api_format != "gemini":
        assert "Use Antigravity as Antigravity." in texts
        assert "Antigravity late instruction" in texts
    for ordinary in ("user", "assistant", "final user", "tool definition", "tool result"):
        assert f"Antigravity {ordinary}" in texts
        assert f"Codex {ordinary}" not in texts
    for untouched in (MCP, "Codex"):
        assert untouched in texts
    assert body == before
    assert request_env.rewrite.call_count == 1
    assert request_env.getters[api_format].await_count == 1
    rewrite_logs = [call.args[0] for call in request_env.info.call_args_list if call.args[0].startswith("[PROMPT-REWRITE]")]
    assert rewrite_logs == [f"[PROMPT-REWRITE] format={api_format} replacements={6 if api_format == 'gemini' else 9}"]


@pytest.mark.parametrize("api_format", ROUTERS)
@pytest.mark.parametrize("stream", [False, True])
async def test_api_retry_reuses_processed_request_even_after_config_changes(request_env, api_format, stream):
    config._config_cache.update({
        "prompt_rewrite_use_default_keywords": False,
        "prompt_rewrite_rules": [["Codex", "Cursor"], ["Cursor", "Final Agent"], ["Claude Agent SDK", "Agent SDK"]],
    })
    request_env.outcomes = [(503, None), (200, "STOP")]
    request_env.update = {"prompt_rewrite_rules": [["Codex", "Updated Agent"]]}
    path, body = client_request(api_format, stream=stream)
    response = await request_env.client.post(path, json=body)
    assert response.status_code == 200, response.text
    assert len(request_env.sent) == 2
    assert request_env.sent[0]["request"] == request_env.sent[1]["request"]
    assert CUSTOM_IDENTITY in strings_in(request_env.sent[1]["request"])
    assert request_env.rewrite.call_count == 1
    assert request_env.getters[api_format].await_count == 1
    assert request_env.detailed == request_env.sent
    # A later client request sees the hot update, while the retry above kept its snapshot.
    response = await request_env.client.post(path, json=body)
    assert response.status_code == 200
    assert "You are Updated Agent. Keep Updated Agent.md and .Updated Agent/config." in strings_in(request_env.sent[-1]["request"])
    assert request_env.getters[api_format].await_count == 2


@pytest.mark.parametrize("api_format", ROUTERS)
async def test_anti_truncation_and_error_retry_keep_rewritten_instructions_and_history(request_env, api_format):
    config._config_cache.update({
        "prompt_rewrite_use_default_keywords": False,
        "prompt_rewrite_rules": [["Codex", "Cursor"], ["Cursor", "Final Agent"]],
    })
    request_env.outcomes = [(503, None), (200, "MAX_TOKENS"), (200, "STOP")]
    request_env.update = {"prompt_rewrite_rules": [["Codex", "Updated Agent"], ["Cursor", "Updated Agent"]]}
    path, body = client_request(api_format, stream=True, anti_truncation=True)
    response = await request_env.client.post(path, json=body)
    assert response.status_code == 200, response.text
    assert len(request_env.sent) == 3
    assert request_env.sent[0]["request"] == request_env.sent[1]["request"]
    for sent in request_env.sent:
        assert CUSTOM_IDENTITY in strings_in(sent["request"])
        assert "Cursor user" in strings_in(sent["request"])
    continuation = request_env.sent[2]["request"]["contents"]
    assert continuation[-2]["role"] == "model"
    assert continuation[-2]["parts"][0]["text"] == "Codex upstream output"
    assert continuation[-1]["role"] == "user"
    assert "Codex upstream output" in continuation[-1]["parts"][0]["text"]
    assert request_env.rewrite.call_count == 1
    assert request_env.getters[api_format].await_count == 1
    assert request_env.detailed == request_env.sent


@pytest.mark.parametrize("api_format", ROUTERS)
@pytest.mark.parametrize("stream", [False, True])
async def test_health_checks_finish_before_config_lookup_or_rewrite(request_env, api_format, stream):
    if api_format == "gemini":
        action = "streamGenerateContent" if stream else "generateContent"
        path = f"/v1/models/{MODEL}:{action}"
        body = {"contents": [{"role": "user", "parts": [{"text": "Hi"}]}], "systemInstruction": {"parts": [{"text": "Codex"}]}}
    else:
        path = "/v1/messages" if api_format == "anthropic" else "/v1/chat/completions"
        body = {"model": MODEL, "stream": stream, "messages": [{"role": "user", "content": "Hi"}]}
        if api_format == "anthropic":
            body["system"] = "Codex"
    response = await request_env.client.post(path, json=body)
    assert response.status_code == 200
    assert request_env.rewrite.call_count == 0
    assert request_env.getters[api_format].await_count == 0
    assert request_env.sent == []


async def test_normalization_no_longer_deletes_any_instruction_or_conversation_text(request_env):
    config._config_cache.update({"sensitive_words_enabled": True, "sensitive_words": ["Codex"]})
    original = {
        "model": MODEL, "systemInstruction": {"parts": [{"text": "Codex CODEX.md"}]},
        "contents": [
            {"role": "user", "parts": [{"text": "Codex user"}]},
            {"role": "model", "parts": [{"text": "Codex assistant"}]},
            {"role": "user", "parts": [{"text": "Codex final user"}]},
        ],
    }
    before = copy.deepcopy(original)
    normalized = await normalize_antigravity_request(original)
    assert normalized["systemInstruction"] == original["systemInstruction"]
    assert normalized["contents"] == original["contents"]
    assert original == before


@pytest.mark.parametrize("api_format", ROUTERS)
@pytest.mark.parametrize("stream", [False, True])
async def test_empty_mapping_deletes_all_message_keywords_before_conversion(request_env, api_format, stream):
    config._config_cache.update({
        "prompt_rewrite_use_default_keywords": False, "prompt_rewrite_rules": [["Codex", ""]],
    })
    path, body = client_request(api_format, stream=stream)
    # The removed message must not become a conversion error; tool results still
    # carry their call IDs even when their output is emptied.
    if api_format == "gemini":
        body["contents"].insert(0, {"role": "user", "parts": [{"text": "Codex"}]})
        body["contents"][1]["parts"][0]["text"] = "Codex"
        body["contents"][3]["parts"][0]["functionResponse"]["response"]["result"] = "Codex"
    else:
        body["messages"].insert(1, {"role": "user", "content": "Codex"})
        if api_format == "openai":
            body["messages"][2]["content"][0]["text"] = "Codex"
            body["messages"][4]["content"] = "Codex"
        else:
            body["messages"][0]["content"][0]["text"] = "Codex"
            body["messages"][3]["content"][0]["content"] = "Codex"
    before = copy.deepcopy(body)
    response = await request_env.client.post(path, json=body)
    assert response.status_code == 200, response.text
    texts = strings_in(request_env.sent[0]["request"])
    assert "You are . Keep .md and ./config." in texts
    for suffix in ("assistant", "final user", "tool definition"):
        assert f" {suffix}" in texts
    assert "aGVsbG8=" in texts
    assert "" in texts
    # Function identifiers and argument values retain their protocol meaning.
    assert MCP in texts
    assert "Codex" in texts
    assert "Codex upstream output" in response.text
    assert body == before
    assert request_env.rewrite.call_count == 1


@pytest.mark.parametrize("api_format", ROUTERS)
@pytest.mark.parametrize("stream", [False, True])
async def test_cloak_without_keywords_works_on_every_route_and_retry_uses_the_snapshot(request_env, api_format, stream):
    config._config_cache.update({
        "prompt_rewrite_enabled": False, "prompt_rewrite_use_default_keywords": False,
        "prompt_rewrite_rules": [], "prompt_rewrite_cloak_enabled": True,
    })
    request_env.outcomes = [(503, None), (200, "STOP")]
    request_env.update = {"prompt_rewrite_cloak_enabled": False}
    path, body = client_request(api_format, stream=stream, blocks=True)
    before = copy.deepcopy(body)
    response = await request_env.client.post(path, json=body)
    assert response.status_code == 200, response.text
    assert len(request_env.sent) == 2
    assert request_env.sent[0]["request"] == request_env.sent[1]["request"]
    texts = strings_in(request_env.sent[1]["request"])
    assert cloak_text(IDENTITY)[0] in texts
    assert cloak_text("Codex tool definition")[0] in texts
    for suffix in ("user", "assistant", "final user", "tool result"):
        assert f"Codex {suffix}" in texts
    assert MCP in texts
    assert "Codex" in texts
    assert body == before
    assert request_env.rewrite.call_count == 1
    assert request_env.getters[api_format].await_count == 1
    assert request_env.detailed == request_env.sent
    response = await request_env.client.post(path, json=body)
    assert response.status_code == 200
    assert IDENTITY in strings_in(request_env.sent[-1]["request"])
    assert "Codex tool definition" in strings_in(request_env.sent[-1]["request"])


@pytest.mark.parametrize("api_format", ROUTERS)
async def test_combined_mapping_and_cloak_do_not_rescan_during_anti_truncation(request_env, api_format):
    config._config_cache.update({
        "prompt_rewrite_use_default_keywords": False, "prompt_rewrite_cloak_enabled": True,
        "prompt_rewrite_rules": [["Codex", "Cursor"], ["Cursor", "Final Agent"]],
    })
    request_env.outcomes = [(503, None), (200, "MAX_TOKENS"), (200, "STOP")]
    request_env.update = {"prompt_rewrite_cloak_enabled": False, "prompt_rewrite_rules": [["Codex", "Updated"]]}
    path, body = client_request(api_format, stream=True, anti_truncation=True)
    response = await request_env.client.post(path, json=body)
    assert response.status_code == 200, response.text
    assert len(request_env.sent) == 3
    for sent in request_env.sent:
        texts = strings_in(sent["request"])
        assert cloak_text(CUSTOM_IDENTITY)[0] in texts
        assert "Cursor user" in texts
        assert MCP in texts
    assert request_env.rewrite.call_count == 1
    assert request_env.getters[api_format].await_count == 1
