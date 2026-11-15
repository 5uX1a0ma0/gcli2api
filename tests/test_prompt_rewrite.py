"""Instruction rewriting must preserve technical references and unrelated objects."""

import copy

import pytest

from src.converter.prompt_rewrite import (
    DEFAULT_KEYWORDS,
    PromptRewriteConfig,
    compile_prompt_rewrite_rules,
    protected_intervals,
    rewrite_prompt_text,
    rewrite_request,
    validate_prompt_rewrite_rules,
)


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
    [["Codex", ""]], [["Codex", " \n"]], [["Codex", "Agent"], ["", "invalid"]],
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


@pytest.mark.parametrize("api_format", ["openai", "anthropic"])
def test_message_instruction_roles_at_all_positions_and_only_changed_nodes_are_copied(api_format):
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
    assert count == (4 if api_format == "anthropic" else 3)
    assert result["messages"][0]["content"] == "You are Antigravity"
    assert result["messages"][3]["content"][0]["text"] == "Use Antigravity"
    assert result["messages"][5]["content"] == "Antigravity"
    assert result["messages"][0]["name"] == "Codex"
    for index in (1, 2, 4):
        assert result["messages"][index] is request["messages"][index]
    assert result["tools"] is request["tools"]
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
    assert count == 2
    assert result["systemInstruction"]["parts"][0]["text"] == "Antigravity"
    assert result["system_instruction"]["parts"][0]["text"] == "Antigravity"
    for index in (1, 2, 3):
        assert result["systemInstruction"]["parts"][index] is request["systemInstruction"]["parts"][index]
    assert result["contents"] is request["contents"]
    assert result["tools"] is request["tools"]
    assert result["metadata"] is request["metadata"]
    assert request == before


@pytest.mark.parametrize("api_format", ["openai", "anthropic", "gemini", "unknown"])
def test_unchanged_requests_retain_the_original_object(api_format):
    request = {"messages": [{"role": "user", "content": "Codex"}], "system": "CODEX.md"}
    result, count = rewrite_request(request, api_format, ENABLED)
    assert result is request
    assert count == 0
