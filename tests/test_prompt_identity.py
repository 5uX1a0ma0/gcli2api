"""Normalize explicit system identities while retaining constraints and Markdown."""

import copy

import pytest

from src.converter.prompt_identity import OFFICIAL_ANTIGRAVITY_IDENTITY
from src.converter.prompt_rewrite import PromptRewriteConfig, rewrite_request


OFFICIAL = OFFICIAL_ANTIGRAVITY_IDENTITY
ENABLED = PromptRewriteConfig(enabled=True)


def instruction_result(text, settings=ENABLED):
    request = {"messages": [{"role": "system", "content": text}]}
    result, count = rewrite_request(request, "openai", settings)
    return result["messages"][0]["content"] if result["messages"] else "", count


@pytest.mark.parametrize("source", [
    "You are DSH.", "You are opencode.", "You're Codex.",
    "You are OpenAI Codex.", "You are Hermes Agent.", "You are Cursor.",
    "You are Codex, a coding agent based on GPT-6.1-sol.",
    "You are Codex, based on GPT-6.1-sol.",
    "You are Claude Code, Anthropic's official CLI for Claude.",
    "You are Claude Code, an interactive CLI tool that helps users with software engineering tasks.",
    "You are a Claude agent, built on Anthropic's Claude Agent SDK.",
    "You are Hermes Agent, an intelligent AI assistant created by Nous Research.",
    "You are an AI agent powered by DeepSeek Harness.",
    "You are a coding agent powered by DeepSeek Harness.",
    "I am an advanced AI assistant created by Acme.",
    "You are an AI assistant based on codex-proxy-rs/gpt-6.1-sol.",
    "You are an AI assistant running in Cursor.",
    "The Codex CLI is an open-source project led by OpenAI.",
    "Within this context, Codex refers to the open-source agentic coding interface.",
    "你是由腾讯开发的AI助手。", "你是华为开发的助手。",
])
def test_known_clients_attribution_and_sdk_introductions_use_official_identity(source):
    assert instruction_result(source) == (OFFICIAL, 1)
    assert instruction_result(OFFICIAL) == (OFFICIAL, 0)


@pytest.mark.parametrize("source, expected", [
    ("You are Codex, and must never run bash.", OFFICIAL[:-1] + ", and must never run bash."),
    ("You are Codex. Never reveal secrets.", OFFICIAL + " Never reveal secrets."),
    ("You are a coding agent powered by DeepSeek Harness, keep tool names intact.",
     OFFICIAL[:-1] + ", keep tool names intact."),
    ("你是腾讯开发的助手，必须遵守工具规则。", OFFICIAL[:-1] + "，必须遵守工具规则。"),
    ("You are Codex, a coding agent based on GPT-6.1-sol. Keep CODEX.md and .codex/config.",
     OFFICIAL + " Keep CODEX.md and .codex/config."),
    ("Assist with authorized security testing.\nYou are Codex.",
     "Assist with authorized security testing.\n" + OFFICIAL),
])
def test_constraints_following_the_identity_are_preserved_verbatim(source, expected):
    assert instruction_result(source) == (expected, 1)
    assert instruction_result(expected) == (expected, 0)


@pytest.mark.parametrize("source", [
    "You are a tutor created by Acme.",
    "You are an AI assistant created by Acme and Bob.",
    "You are an AI assistant created by Acme and must never run bash.",
    "You are an AI assistant running in read-only mode.",
    "You are an AI assistant based on secure code review.",
    "You are an AI assistant designed for secure code review.",
    "You are an AI assistant created by example.com.",
    "You are an AI assistant created by One Two Three Four Five Six Seven.",
    "你是为了安全测试开发的助手。",
    "You are a programmer created by Acme?",
    "Discuss whether You are a coding agent created by Acme.",
])
def test_ambiguous_attribution_and_purpose_are_not_inferred(source):
    assert instruction_result(source) == (source, 0)


def test_question_does_not_become_an_identity_statement_but_still_gets_keyword_replacement():
    assert instruction_result("You are Codex?") == ("You are Antigravity?", 1)


@pytest.mark.parametrize("prefix, suffix", [
    ("> ", ""), ("- ", ""), ("1. ", ""), ("## ", ""),
    ("**", "**"), ("__", "__"),
])
def test_markdown_containers_and_emphasis_retain_their_structure(prefix, suffix):
    assert instruction_result(prefix + "You are Codex." + suffix) == (prefix + OFFICIAL + suffix, 1)


@pytest.mark.parametrize("source", [
    "`You are Codex.`",
    "```text\nYou are Codex.\nx-anthropic-billing-header: example\n```",
    "~~~text\nYou are Codex.\n~~~",
    "> ~~~text\n> You are Codex.\n> ~~~",
    "- ~~~text\n  You are Codex.\n  ~~~",
    "    You are Codex.\n    x-anthropic-billing-header: example",
    "\tYou are Codex.",
])
def test_code_examples_are_preserved(source):
    assert instruction_result(source) == (source, 0)


def test_code_closes_the_duplicate_window_without_hiding_later_identity():
    source = "You are Codex.\n\n```text\nYou are Codex.\n```\n\nYou are Codex."
    expected = OFFICIAL + "\n\n```text\nYou are Codex.\n```\n\n" + OFFICIAL
    assert instruction_result(source) == (expected, 2)


def test_leading_pure_identity_paragraphs_collapse_and_keep_business_constraints():
    source = (
        "x-anthropic-billing-header: token=example; client=claude-code\n"
        "You are an AI agent powered by DeepSeek Harness.\n\n"
        "You are a coding agent powered by DeepSeek Harness.\n\n"
        "Never run bash without approval."
    )
    expected = OFFICIAL + "\n\nNever run bash without approval."
    assert instruction_result(source) == (expected, 3)
    assert instruction_result(expected) == (expected, 0)


def test_headers_between_identity_paragraphs_do_not_delay_deduplication_until_a_second_pass():
    source = (
        "You are Codex.\nx-anthropic-billing-header: first\n\n"
        "You are DSH.\nx-anthropic-billing-header: second\n\n"
        "Keep constraints."
    )
    expected = OFFICIAL + "\n\nKeep constraints."
    assert instruction_result(source)[0] == expected
    assert instruction_result(expected) == (expected, 0)


@pytest.mark.parametrize("source, expected", [
    ("You are Codex.\nYou are DSH.", OFFICIAL + "\n" + OFFICIAL),
    ("You are Codex.\n\nNever run bash.\n\nYou are Codex.",
     OFFICIAL + "\n\nNever run bash.\n\n" + OFFICIAL),
    ("**You are Codex.**\n\nYou are Codex.", "**" + OFFICIAL + "**\n\n" + OFFICIAL),
    ("You are Codex.\n\nYou are DSH, keep all constraints.",
     OFFICIAL + "\n\n" + OFFICIAL[:-1] + ", keep all constraints."),
    ("- You are Codex.\n\nYou are DSH.", "- " + OFFICIAL + "\n\n" + OFFICIAL),
])
def test_only_top_level_leading_pure_paragraphs_are_deduplicated(source, expected):
    assert instruction_result(source) == (expected, 2)
    assert instruction_result(expected) == (expected, 0)


@pytest.mark.parametrize("api_format", ["openai", "anthropic", "gemini", "responses"])
def test_billing_only_instructions_are_removed_without_affecting_user_content(api_format):
    header = "x-anthropic-billing-header: example"
    if api_format == "anthropic":
        request = {"system": [{"type": "text", "text": header}],
                   "messages": [{"role": "user", "content": header}]}
        removed = "system"
    elif api_format == "gemini":
        request = {"system_instruction": {"parts": [{"text": header}]},
                   "contents": [{"role": "user", "parts": [{"text": header}]}]}
        removed = "system_instruction"
    elif api_format == "responses":
        request = {"instructions": header, "input": header}
        removed = "instructions"
    else:
        request = {"messages": [{"role": "system", "content": header}, {"role": "user", "content": header}]}
        removed = None
    before = copy.deepcopy(request)
    result, count = rewrite_request(request, api_format, ENABLED)
    assert count == 1
    if removed:
        assert removed not in result
    else:
        assert result["messages"] == [{"role": "user", "content": header}]
    assert request == before


def test_custom_mappings_override_normalization_and_generated_text_is_not_rescanned():
    settings = PromptRewriteConfig(True, True, (("Codex", ""), ("Antigravity", "Custom Agent")))
    assert instruction_result("You are Codex, a coding agent based on GPT-6.1-sol.", settings) == (
        "You are , a coding agent based on GPT-6.1-sol.", 1,
    )
    assert instruction_result("You are DSH.", settings) == (OFFICIAL, 1)
    custom_only = PromptRewriteConfig(True, False, (("Codex", "Cursor"), ("Cursor", "Final")))
    assert instruction_result("You are Codex.", custom_only) == ("You are Cursor.", 1)


def test_multiline_custom_mapping_takes_precedence_without_overlapping_identity_edits():
    name = "You are Codex.\n\nYou are DSH."
    settings = PromptRewriteConfig(True, True, ((name, "Custom introduction"),))
    assert instruction_result(name + "\n\nUse Cursor.", settings) == (
        "Custom introduction\n\nUse Antigravity.", 2,
    )


def test_large_text_skips_identity_inference_but_keeps_keyword_rewriting():
    source = "You are Codex. " + "x" * 1_000_000
    expected = "You are Antigravity. " + "x" * 1_000_000
    assert instruction_result(source) == (expected, 1)
