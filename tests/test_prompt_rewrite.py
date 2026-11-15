"""All message text is rewritten without mutating protocol data or source objects."""

import copy

import pytest

from src.converter.prompt_rewrite import (
    DEFAULT_KEYWORDS,
    PromptRewriteConfig,
    cloak_text,
    compile_prompt_rewrite_rules,
    protected_intervals,
    rewrite_prompt_text,
    rewrite_request,
    validate_prompt_rewrite_rules,
)
from src.converter.prompt_identity import OFFICIAL_ANTIGRAVITY_IDENTITY


ENABLED = PromptRewriteConfig(enabled=True)


@pytest.mark.parametrize("name", DEFAULT_KEYWORDS)
def test_complete_default_keyword_list(name):
    assert rewrite_prompt_text(f"You are {name}.", ENABLED) == ("You are Antigravity.", 1)


@pytest.mark.parametrize("text, expected, count", [
    ("CODEX cOdEx OpenAI CODEX Codex CLI", "Antigravity Antigravity Antigravity Antigravity", 4),
    ("GitHub Copilot CLI and GitHub Copilot", "Antigravity and Antigravity", 2),
    ("Claude Agent SDK, Claude Agent, Claude Code", "Antigravity, Antigravity, Antigravity", 3),
    ("你是Codex，使用Claude Agent SDK处理任务。", "你是Antigravity，使用Antigravity处理任务。", 2),
    ("İ ß 🙂 Codex，İCursor结束", "İ ß 🙂 Antigravity，İAntigravity结束", 2),
    ("**Codex** __Codex__ _Codex_ ***Codex***", "**Antigravity** __Antigravity__ _Antigravity_ ***Antigravity***", 4),
    (r"\*\*Codex\*\* \_\_Codex\_\_", r"\*\*Antigravity\*\* \_\_Antigravity\_\_", 2),
    ("Continue.dev Refact.ai **Continue.dev** __Refact.ai__", "Antigravity Antigravity **Antigravity** __Antigravity__", 4),
    ('"You are Continue.dev" and \'Codex and Refact.ai\'', '"You are Antigravity" and \'Antigravity and Antigravity\'', 3),
    ("myCodexAgent codex_helper my_Codex __Codex__Agent Codex_helper_ 123Codex Codex123", "myCodexAgent codex_helper my_Codex __Codex__Agent Codex_helper_ 123Codex Codex123", 0),
])
def test_matching_boundaries_and_unicode_indices(text, expected, count):
    assert rewrite_prompt_text(text, ENABLED) == (expected, count)


def test_custom_overrides_duplicates_long_names_and_replacements_do_not_cascade():
    settings = PromptRewriteConfig(True, True, (
        ("Codex", "discarded"), ("cOdEx", "Cursor"), ("Cursor", "Final"),
        ("Claude Agent", "Short"), ("Claude Agent SDK", "Long"), ("A+B", r"$1\1"),
    ))
    assert rewrite_prompt_text("Codex Cursor Claude Agent SDK Claude Agent A+B AAB", settings) == (
        r"Cursor Final Long Short $1\1 AAB", 5,
    )


def test_effective_rule_cache_and_disabled_or_empty_configuration():
    one = PromptRewriteConfig(True, False, (("Codex", "Old"), ("Codex", "New"), ("Cline", "Agent")))
    two = PromptRewriteConfig(True, False, (("Cline", "Agent"), ("Codex", "New")))
    assert compile_prompt_rewrite_rules(one) is compile_prompt_rewrite_rules(two)
    assert rewrite_prompt_text("Codex Cline", PromptRewriteConfig()) == ("Codex Cline", 0)
    assert rewrite_prompt_text("Codex Cline", PromptRewriteConfig(True, False)) == ("Codex Cline", 0)
    assert rewrite_prompt_text("Codex Cline", PromptRewriteConfig(True, False, (("Cline", "Agent"),))) == ("Codex Agent", 1)
    assert rewrite_prompt_text("Codex", PromptRewriteConfig(True, False, (("Codex", "Codex"),))) == ("Codex", 0)


@pytest.mark.parametrize("reference", [
    "CODEX.md", "__CODEX.md__", "Codex.config.json", "Codex.py:42:3", "CODEX.md#L12",
    "file:CODEX.md", "--file=CODEX.md", ".codex", ".codex/config", "Codex/", "Continue.dev/", "Codex\\",
    "./Codex", "../Codex/file.py", "~/Codex/config", "/workspace/Codex/README.md",
    "docs/Codex", "docs/Continue.dev", "docs/Refact.ai",
    r"C:\work\Codex", r"C:\Users\agent\.codex\config.toml", "C:/work/Codex", "C:Codex",
    r"\\server\share\Codex", r"docs\Codex", r"Codex\_folder", r"docs\_Codex", r"Codex\*",
    r'"C:\Program Files\Codex\config.toml"', r"'\\server\shared folder\Codex'",
    '"/workspace/my Codex folder/config"', '“./my Codex folder/config”', "'relative folder/Codex'",
    "https://example.test/Codex?client=Cursor#Cline", "file:///workspace/Codex",
    "www.example.test/Codex", "Continue.dev?client=Codex", "Refact.ai/Codex",
    "mcp__Codex__move-cursor", r"mcp\_\_Codex\_\_move-cursor", "mcp__server__Cursor",
    "`Codex Cursor`", "``Use `Codex` then Cursor``",
    '```python\nCodex = "Cursor"\n```', "~~~text\nCodex and Cursor\n~~~",
    "````text\n```\nCodex\n```\n````",
])
def test_references_are_protected_but_names_in_the_same_paragraph_are_rewritten(reference):
    separator = "\n" if "\n" in reference else " "
    text = f"Codex is the client. Keep{separator}{reference}\nCodex remains the client."
    expected = f"Antigravity is the client. Keep{separator}{reference}\nAntigravity remains the client."
    assert rewrite_prompt_text(text, ENABLED) == (expected, 2)


@pytest.mark.parametrize("text, expected", [
    ("Codex before `Codex Cursor", "Antigravity before `Codex Cursor"),
    ("Codex before ``Codex ` Cursor", "Antigravity before ``Codex ` Cursor"),
    ("Codex before\n```python\nCodex and Cursor", "Antigravity before\n```python\nCodex and Cursor"),
    ("Codex before\n~~~text\nCodex and Cursor", "Antigravity before\n~~~text\nCodex and Cursor"),
])
def test_unclosed_code_conservatively_preserves_the_remainder(text, expected):
    assert rewrite_prompt_text(text, ENABLED) == (expected, 1)


def test_closed_multiline_inline_code_does_not_protect_following_prose():
    text = "Codex before ```Codex Cursor\n```\nCodex after"
    assert rewrite_prompt_text(text, ENABLED) == ("Antigravity before ```Codex Cursor\n```\nAntigravity after", 2)


@pytest.mark.parametrize("text, expected, count", [
    ("```Codex``` Codex", "```Codex``` Antigravity", 1),
    ("```Codex``` Codex `Cursor` Cursor", "```Codex``` Antigravity `Cursor` Antigravity", 2),
])
def test_triple_backtick_inline_code_at_line_start_and_following_code_spans(text, expected, count):
    assert rewrite_prompt_text(text, ENABLED) == (expected, count)


def test_protection_intervals_are_merged_and_use_source_unicode_indices():
    text = "🙂 `https://example.test/Codex` then Codex"
    intervals = protected_intervals(text)
    assert len(intervals) == 1
    assert text[intervals[0][0]:intervals[0][1]] == "`https://example.test/Codex`"
    assert rewrite_prompt_text(text, ENABLED) == ("🙂 `https://example.test/Codex` then Antigravity", 1)


def test_long_unbroken_identifiers_do_not_change_and_do_not_hide_following_names():
    identifier = "x" * 20000 + "Codex"
    assert rewrite_prompt_text(f"{identifier} Codex", ENABLED) == (f"{identifier} Antigravity", 1)
    invalid_filename = "x" * 20000 + ".invalid$"
    assert rewrite_prompt_text(f"{invalid_filename} Codex", ENABLED) == (f"{invalid_filename} Antigravity", 1)


@pytest.mark.parametrize("rules", [
    None, {}, "[]", True, ["Codex"], [["Codex"]], [["Codex", "Agent", "extra"]],
    [[1, "Agent"]], [["Codex", None]], [["", "Agent"]], [["  ", "Agent"]],
    [["Codex", "Agent"], ["", "invalid"]],
])
def test_rule_validation_rejects_the_entire_invalid_list(rules):
    with pytest.raises(ValueError):
        validate_prompt_rewrite_rules(rules)


def test_rule_validation_trims_strings_without_changing_the_input():
    rules = [[" Codex ", " Agent "]]
    assert validate_prompt_rewrite_rules(rules) == [["Codex", "Agent"]]
    assert rules == [[" Codex ", " Agent "]]
    assert validate_prompt_rewrite_rules([]) == []
    assert validate_prompt_rewrite_rules([["\u0085Codex\u001c", "\u3000Agent\u00a0"]]) == [["Codex", "Agent"]]
    assert validate_prompt_rewrite_rules([["\ufeff", "\ufeff"]]) == [["\ufeff", "\ufeff"]]
    assert validate_prompt_rewrite_rules([[" Codex ", ""], ["Cursor", " \n"]]) == [["Codex", ""], ["Cursor", ""]]


@pytest.mark.parametrize("api_format", ["openai", "anthropic"])
def test_all_message_roles_and_descriptions_are_rewritten_and_only_changed_nodes_are_copied(api_format):
    request = {
        "messages": [
            {"role": "system", "content": "You are Codex", "name": "Codex"},
            {"role": "user", "content": "Codex", "image": {"url": "Codex"}},
            {"role": "assistant", "content": "Codex", "tool_calls": [{"arguments": "Codex"}]},
            {"role": "developer", "content": [
                {"type": "text", "text": "Use Cursor", "cache_control": {"name": "Codex"}},
                {"type": "text", "text": "Keep CODEX.md"},
                {"type": "image", "text": "Codex", "source": {"data": "Codex"}},
            ]},
            {"role": "tool", "content": "Codex", "tool_call_id": "Codex"},
            {"role": "system", "content": "Cline"},
        ],
        "tools": [{"name": "Codex", "description": "Codex", "input": {"Codex": "Codex"}}],
        "metadata": {"client": "Codex"},
        "system": [{"type": "text", "text": "Claude Agent SDK"}],
    }
    before = copy.deepcopy(request)
    result, count = rewrite_request(request, api_format, ENABLED)
    assert count == (8 if api_format == "anthropic" else 7)
    assert result["messages"][0]["content"] == OFFICIAL_ANTIGRAVITY_IDENTITY
    assert result["messages"][3]["content"][0]["text"] == "Use Antigravity"
    assert result["messages"][5]["content"] == "Antigravity"
    assert result["messages"][0]["name"] == "Codex"
    for index in (1, 2, 4):
        assert result["messages"][index]["content"] == "Antigravity"
    assert result["messages"][1]["image"] is request["messages"][1]["image"]
    assert result["messages"][2]["tool_calls"] is request["messages"][2]["tool_calls"]
    assert result["messages"][4]["tool_call_id"] == "Codex"
    assert result["tools"][0]["description"] == "Antigravity"
    assert result["tools"][0]["name"] == "Codex"
    assert result["tools"][0]["input"] is request["tools"][0]["input"]
    assert result["metadata"] is request["metadata"]
    original_blocks, changed_blocks = request["messages"][3]["content"], result["messages"][3]["content"]
    assert changed_blocks[0]["cache_control"] is original_blocks[0]["cache_control"]
    assert changed_blocks[1] is original_blocks[1]
    assert changed_blocks[2] is original_blocks[2]
    assert request == before
    if api_format == "openai":
        assert result["system"] is request["system"]
    else:
        assert result["system"][0]["text"] == "Antigravity"


def test_gemini_both_system_aliases_rewrite_only_text_parts():
    request = {
        "systemInstruction": {"role": "system", "parts": [
            {"text": "Codex", "metadata": {"Codex": True}},
            {"text": "CODEX.md"}, {"inlineData": {"data": "Codex"}},
            {"functionCall": {"name": "Codex"}, "text": "Codex"},
        ]},
        "system_instruction": {"parts": [{"text": "Cursor"}]},
        "contents": [{"role": "user", "parts": [{"text": "Codex"}]}],
        "tools": [{"name": "Codex"}], "metadata": {"Codex": "Codex"},
    }
    before = copy.deepcopy(request)
    result, count = rewrite_request(request, "gemini", ENABLED)
    assert count == 3
    assert result["systemInstruction"]["parts"][0]["text"] == "Antigravity"
    assert result["system_instruction"]["parts"][0]["text"] == "Antigravity"
    for index in (1, 2, 3):
        assert result["systemInstruction"]["parts"][index] is request["systemInstruction"]["parts"][index]
    assert result["contents"][0]["parts"][0]["text"] == "Antigravity"
    assert result["tools"] is request["tools"]
    assert result["metadata"] is request["metadata"]
    assert request == before


@pytest.mark.parametrize("api_format", ["openai", "anthropic", "gemini", "responses", "unknown"])
def test_unchanged_requests_retain_the_original_object(api_format):
    request = {"messages": [{"role": "user", "content": "Already anonymous"}], "system": "CODEX.md"}
    result, count = rewrite_request(request, api_format, ENABLED)
    assert result is request
    assert count == 0


def test_custom_empty_mapping_replaces_substrings_and_protected_text_before_defaults():
    settings = PromptRewriteConfig(True, True, (("Codex", ""), ("Cursor", "Codex")))
    text = "Codex CLI OpenAI Codex my_Codex_agent `Codex` CODEX.md /Codex/file https://test/Codex Cursor"
    assert rewrite_prompt_text(text, settings) == (
        " CLI OpenAI  my__agent `` .md //file https://test/ Codex", 8,
    )


@pytest.mark.parametrize("name, source, expected", [
    ("OpenAI", "OpenAI Codex", " Antigravity"),
    ("CLI", "Codex CLI", "Antigravity "),
])
def test_partial_custom_matches_do_not_hide_default_names_in_remaining_source(name, source, expected):
    assert rewrite_prompt_text(source, PromptRewriteConfig(True, True, ((name, ""),))) == (expected, 2)


def test_empty_mapping_prunes_only_newly_empty_text_messages_and_preserves_tool_responses():
    image = {"type": "image_url", "image_url": {"url": "data:Codex"}}
    request = {"messages": [
        {"role": "system", "content": "Codex"},
        {"role": "user", "content": "Codex"},
        {"role": "assistant", "content": [{"type": "text", "text": "Codex"}]},
        {"role": "user", "content": [{"type": "text", "text": "Codex"}, image]},
        {"role": "tool", "content": "Codex", "tool_call_id": "Codex"},
        {"role": "user", "content": "Please remove Codex here."},
    ]}
    before = copy.deepcopy(request)
    result, count = rewrite_request(request, "openai", PromptRewriteConfig(True, False, (("Codex", ""),)))
    assert count == 6
    assert len(result["messages"]) == 3
    assert result["messages"][0]["content"] == [image]
    assert result["messages"][0]["content"][0] is image
    assert result["messages"][1] == {"role": "tool", "content": "", "tool_call_id": "Codex"}
    assert result["messages"][2]["content"] == "Please remove  here."
    assert request == before


@pytest.mark.parametrize("api_format", ["anthropic", "gemini"])
def test_empty_instruction_parts_are_pruned_without_removing_other_instructions(api_format):
    parts = [{"text": "Codex"}, {"text": "Keep instructions."}] if api_format == "gemini" else [
        {"type": "text", "text": "Codex"}, {"type": "text", "text": "Keep instructions."},
    ]
    request = {"systemInstruction": {"parts": parts}} if api_format == "gemini" else {"system": parts}
    before = copy.deepcopy(request)
    result, count = rewrite_request(request, api_format, PromptRewriteConfig(True, False, (("Codex", ""),)))
    assert count == 1
    updated = result["systemInstruction"]["parts"] if api_format == "gemini" else result["system"]
    assert updated == [parts[1]]
    assert updated[0] is parts[1]
    assert request == before


@pytest.mark.parametrize("api_format", ["openai", "anthropic"])
def test_nested_tool_result_text_and_string_blocks_are_rewritten_without_touching_call_data(api_format):
    call = {"type": "tool_use", "name": "Codex", "id": "Codex", "input": {"name": "Codex"}}
    image = {"type": "image", "source": {"data": "Codex"}}
    request = {"messages": [
        {"role": "assistant", "content": [{"type": "text", "text": "Codex"}, call]},
        {"role": "user", "content": [
            "Codex", {"type": "tool_result", "tool_use_id": "Codex", "content": [
                {"type": "text", "text": "Codex"}, image,
            ]},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "Codex", "content": "Codex"}]},
    ]}
    before = copy.deepcopy(request)
    result, count = rewrite_request(request, api_format, ENABLED)
    assert count == 4
    assert result["messages"][0]["content"][0]["text"] == "Antigravity"
    assert result["messages"][0]["content"][1] is call
    assert result["messages"][1]["content"][0] == "Antigravity"
    nested = result["messages"][1]["content"][1]
    assert nested["tool_use_id"] == "Codex"
    assert nested["content"][0]["text"] == "Antigravity"
    assert nested["content"][1] is image
    assert result["messages"][2]["content"][0]["content"] == "Antigravity"
    assert request == before


def test_gemini_function_response_values_are_rewritten_but_call_names_keys_and_args_are_preserved():
    request = {"contents": [
        {"role": "model", "parts": [{"functionCall": {"name": "Codex", "args": {"client": "Codex"}}}]},
        {"role": "user", "parts": [
            {"functionResponse": {"name": "Codex", "id": "Codex", "response": {
                "Codex": ["Codex", {"value": "Cursor", "number": 1}],
            }}},
            {"function_response": {"name": "Cursor", "response": {"result": "Codex"}}},
        ]},
    ]}
    before = copy.deepcopy(request)
    result, count = rewrite_request(request, "gemini", ENABLED)
    assert count == 3
    assert result["contents"][0] is request["contents"][0]
    first = result["contents"][1]["parts"][0]["functionResponse"]
    assert first["name"] == first["id"] == "Codex"
    assert first["response"] == {"Codex": ["Antigravity", {"value": "Antigravity", "number": 1}]}
    assert result["contents"][1]["parts"][1]["function_response"]["response"] == {"result": "Antigravity"}
    assert request == before


@pytest.mark.parametrize("api_format", ["openai", "anthropic", "gemini"])
def test_signed_thinking_and_non_text_blocks_remain_unchanged(api_format):
    signed = [
        {"type": "thinking", "thinking": "Codex", "signature": "Codex"},
        {"type": "text", "text": "Codex", "thoughtSignature": "Codex"},
        {"type": "text", "text": "Codex", "thought_signature": "Codex"},
        {"text": "Codex", "thought": True},
        {"type": "image", "text": "Codex", "source": {"data": "Codex"}},
    ]
    field, root = ("parts", "contents") if api_format == "gemini" else ("content", "messages")
    request = {root: [{"role": "assistant", field: signed}]}
    result, count = rewrite_request(request, api_format, PromptRewriteConfig(True, True, (("Codex", ""),), True))
    assert count == 0
    assert result is request


@pytest.mark.parametrize("source, expected, count", [
    ("", "", 0),
    ("a AI tool four test 123 中文", "a AI tool four test 123 中文", 0),
    ("Hello ABCDE abcdefghijklmnop", "Hel\u200blo ABC\u200bDE abc\u200bdefghijklmnop", 3),
    ("English, words! description\n工具描述", "Eng\u200blish, wor\u200bds! des\u200bcription\n工具描述", 3),
    ("already\u200bcloaked des\u200bcription", "already\u200bcloaked des\u200bcription", 0),
])
def test_cloak_uses_real_zero_width_space_and_is_idempotent(source, expected, count):
    assert cloak_text(source) == (expected, count)
    assert cloak_text(expected) == (expected, 0)


@pytest.mark.parametrize("api_format", ["openai", "anthropic", "gemini", "responses"])
def test_cloak_is_independent_of_keyword_rules_and_only_applies_to_instructions_and_descriptions(api_format):
    prose = "English description"
    cloaked = "Eng\u200blish des\u200bcription"
    schema = {"type": "object", "properties": {
        "description": {"type": "string", "description": prose, "enum": [prose], "default": prose},
    }, "examples": [{"description": prose}]}
    tool = {"name": "English", "description": prose, "parameters": schema}
    if api_format == "gemini":
        request = {"systemInstruction": {"parts": [{"text": prose}]},
                   "contents": [{"role": "user", "parts": [{"text": prose}]}],
                   "tools": [{"functionDeclarations": [tool]}]}
    elif api_format == "anthropic":
        request = {"system": [{"type": "text", "text": prose}],
                   "messages": [{"role": "user", "content": prose}], "tools": [tool]}
    elif api_format == "responses":
        request = {"instructions": prose, "input": [{"role": "user", "content": prose}], "tools": [tool]}
    else:
        request = {"messages": [
            {"role": "system", "content": [{"type": "text", "text": prose}]},
            {"role": "user", "content": prose},
            {"role": "developer", "content": prose},
        ], "tools": [{"type": "function", "function": tool}]}
    before = copy.deepcopy(request)
    settings = PromptRewriteConfig(False, False, (), True)
    result, count = rewrite_request(request, api_format, settings)
    assert count == (8 if api_format == "openai" else 6)
    if api_format == "gemini":
        assert result["systemInstruction"]["parts"][0]["text"] == cloaked
        assert result["contents"] is request["contents"]
        changed_tool = result["tools"][0]["functionDeclarations"][0]
    elif api_format == "anthropic":
        assert result["system"][0]["text"] == cloaked
        assert result["messages"] is request["messages"]
        changed_tool = result["tools"][0]
    elif api_format == "responses":
        assert result["instructions"] == cloaked
        assert result["input"] is request["input"]
        changed_tool = result["tools"][0]
    else:
        assert result["messages"][0]["content"][0]["text"] == cloaked
        assert result["messages"][1] is request["messages"][1]
        assert result["messages"][2]["content"] == cloaked
        changed_tool = result["tools"][0]["function"]
    assert changed_tool["name"] == "English"
    assert changed_tool["description"] == cloaked
    prop = changed_tool["parameters"]["properties"]["description"]
    assert prop["description"] == cloaked
    assert prop["enum"] is schema["properties"]["description"]["enum"]
    assert prop["default"] == prose
    assert changed_tool["parameters"]["examples"] is schema["examples"]
    assert request == before
    repeated, repeated_count = rewrite_request(result, api_format, settings)
    assert repeated is result
    assert repeated_count == 0


def test_responses_input_text_output_text_and_function_outputs_are_supported():
    request = {"instructions": "You are Codex.", "input": [
        {"role": "developer", "content": [{"type": "input_text", "text": "Use Codex"}]},
        {"role": "user", "content": [{"type": "input_text", "text": "Codex"}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "Codex"}]},
        {"type": "function_call", "name": "Codex", "arguments": '{"name": "Codex"}'},
        {"type": "function_call_output", "call_id": "Codex", "output": "Codex result"},
    ]}
    before = copy.deepcopy(request)
    result, count = rewrite_request(request, "responses", ENABLED)
    assert count == 5
    assert result["instructions"] == OFFICIAL_ANTIGRAVITY_IDENTITY
    assert result["input"][0]["content"][0]["text"] == "Use Antigravity"
    assert result["input"][1]["content"][0]["text"] == "Antigravity"
    assert result["input"][2]["content"][0]["text"] == "Antigravity"
    assert result["input"][3] is request["input"][3]
    assert result["input"][4] == {"type": "function_call_output", "call_id": "Codex", "output": "Antigravity result"}
    assert request == before
    assert rewrite_request({"input": "Codex"}, "responses", ENABLED) == ({"input": "Antigravity"}, 1)
