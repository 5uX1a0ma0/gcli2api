"""Literal client-name rewriting for instruction text, without changing technical references."""

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any


DEFAULT_REPLACEMENT = "Antigravity"
# DongHua3/cpa-plugin-antigravity v0.4.0 (d80e347), plus the Claude Agent names.
DEFAULT_KEYWORDS = (
    "Claude Code", "OpenAI Codex", "Codex CLI", "Codex", "OpenCode",
    "GitHub Copilot CLI", "GitHub Copilot", "Gemini Code Assist", "Gemini CLI",
    "Cursor", "Windsurf", "Codeium", "Cline", "Roo Code", "Kilo Code", "Aider",
    "Continue.dev", "Amazon Q Developer", "Amazon CodeWhisperer",
    "JetBrains AI Assistant", "JetBrains Junie", "Kiro", "Qoder CLI", "Qoder",
    "Qwen Code", "Trae", "Tabnine", "Sourcegraph Cody", "Augment Code",
    "Replit Agent", "Replit Ghostwriter", "Devin", "OpenHands", "SWE-agent",
    "Goose", "Zed AI", "Void Editor", "PearAI", "Refact.ai", "Tabby", "GitLab Duo",
    "Visual Studio IntelliCode", "CodeBuddy", "Blackbox AI", "Pieces for Developers",
    "Qodo", "CodiumAI", "Rovo Dev CLI", "Factory Droid", "OpenClaw", "Clawdbot",
    "Moltbot", "Hermes Agent", "Hermes", "WorkBuddy", "Claude Agent", "Claude Agent SDK",
)


@dataclass(frozen=True)
class PromptRewriteConfig:
    enabled: bool = False
    use_default_keywords: bool = True
    rules: tuple[tuple[str, str], ...] = ()


def validate_prompt_rewrite_rules(value: Any) -> list[list[str]]:
    """Validate the complete list before accepting any rule; names are literals."""
    if not isinstance(value, list):
        raise ValueError("身份改写规则必须是数组（每条 [客户端名称, 替换文本]）")
    cleaned = []
    for index, rule in enumerate(value, 1):
        if not (
            isinstance(rule, (list, tuple))
            and len(rule) == 2
            and all(isinstance(item, str) for item in rule)
        ):
            raise ValueError(f"第 {index} 条身份改写规则必须是字符串二元组")
        name, replacement = (item.strip() for item in rule)
        if not name or not replacement:
            raise ValueError(f"第 {index} 条规则的客户端名称和替换文本均不能为空")
        cleaned.append([name, replacement])
    return cleaned


def effective_prompt_rewrite_rules(settings: PromptRewriteConfig) -> tuple[tuple[str, str], ...]:
    """Custom names override defaults; the last case-insensitive duplicate wins."""
    merged = {}
    if settings.use_default_keywords:
        merged.update((name.casefold(), (name, DEFAULT_REPLACEMENT)) for name in DEFAULT_KEYWORDS)
    for name, replacement in settings.rules:
        merged[name.casefold()] = (name, replacement)
    return tuple(sorted(merged.values(), key=lambda rule: (-len(rule[0]), rule[0].casefold())))


@dataclass(frozen=True)
class CompiledPromptRules:
    rules: tuple[tuple[str, str], ...]
    pattern: re.Pattern[str] | None


@lru_cache(maxsize=32)
def _compile_rules(rules: tuple[tuple[str, str], ...]) -> CompiledPromptRules:
    # Named alternatives preserve the replacement lookup and Unicode source indices.
    pattern = re.compile(
        "|".join(f"(?P<r{index}>{re.escape(name)})" for index, (name, _) in enumerate(rules)),
        re.IGNORECASE,
    ) if rules else None
    return CompiledPromptRules(rules, pattern)


def compile_prompt_rewrite_rules(settings: PromptRewriteConfig) -> CompiledPromptRules:
    return _compile_rules(effective_prompt_rewrite_rules(settings))


_CODE_START_RE = re.compile(
    r"^[ \t]*(?P<fence>`{3,}|~{3,})|(?P<inline>`+)", re.MULTILINE,
)
_URL_RE = re.compile(
    r"(?<![a-z0-9_])(?:[a-z][a-z0-9+.-]*://|www\.|mailto:|file:|"
    r"[a-z0-9.-]+\.[a-z]{2,}[/?#])[^\s<>\"'`“”‘’，。；！？、（）]+",
    re.IGNORECASE,
)
_QUOTED_RE = re.compile(r'"[^"\r\n]*"|\'[^\'\r\n]*\'|“[^”\r\n]*”|‘[^’\r\n]*’')
_TOKEN_RE = re.compile(r"[^\s`<>\[\]{}()\"'“”‘’,;!?，。；！？、：（）]+")
_MARKDOWN_WRAP_RE = re.compile(r"((?:\\?[*_~])+)(.+?)((?:\\?[*_~])+)$")
_PATH_RE = re.compile(
    r"(?:[^\s/\\]+[/\\](?:[^\s/\\]|$)|(?:^[a-z]:)?[/\\][^\s/\\]|^~[/\\]|^[a-z]:[^\s])",
    re.IGNORECASE,
)
_FILE_RE = re.compile(
    r"(?<![\w.~+-])(?:\.[\w.-]+|[\w~+-][\w.~+-]*\.[\w+-]+)"
    r"(?::\d+(?::\d+)?)?(?:#[\w:.-]+)?$",
)
_MCP_RE = re.compile(r"(?<![a-z0-9_])mcp__", re.IGNORECASE)
_DOTTED_PRODUCTS = {"continue.dev", "refact.ai"}


def _code_intervals(text: str) -> list[tuple[int, int]]:
    intervals = []
    covered_until = 0
    # Read delimiters in source order so a multiline inline code closer cannot
    # accidentally be treated as a new, unclosed fenced block.
    for match in _CODE_START_RE.finditer(text):
        if match.start() < covered_until:
            continue
        delimiter = match["fence"] or match["inline"]
        # Backticks are forbidden in a backtick fence's info string. A closed
        # triple-backtick span on one line is therefore inline code, even at column 0.
        is_fence = bool(match["fence"])
        if is_fence and delimiter[0] == "`":
            line_end = text.find("\n", match.end())
            if line_end < 0:
                line_end = len(text)
            is_fence = "`" not in text[match.end():line_end]
        if is_fence:
            closing = re.compile(
                rf"^[ \t]*{re.escape(delimiter[0])}{{{len(delimiter)},}}[ \t]*\r?$",
                re.MULTILINE,
            ).search(text, match.end())
        else:
            closing = re.compile(rf"(?<!`)`{{{len(delimiter)}}}(?!`)").search(text, match.end())
        covered_until = closing.end() if closing else len(text)
        intervals.append((match.start(), covered_until))
    return intervals


def _reference_text(token: str) -> str:
    reference = token.rstrip(".:")
    markup = _MARKDOWN_WRAP_RE.fullmatch(reference)
    if markup and markup[1].replace("\\", "")[::-1] == markup[3].replace("\\", ""):
        return markup[2]
    # Strip surrounding emphasis only. Backslashes inside a technical token,
    # e.g. Codex\_folder, still identify a path even when followed by Markdown marks.
    return reference.strip("*_~")


def _is_technical_reference(token: str) -> bool:
    reference = _reference_text(token)
    # Avoid retrying a greedy path/filename expression at every character of
    # a long, unbroken identifier in a system prompt or fenced code block.
    if ("/" in reference or "\\" in reference or reference[1:2] == ":") and _PATH_RE.search(reference):
        return True
    if "__" in reference and _MCP_RE.search(reference):
        return True
    if "." not in reference:
        return False
    filename = _FILE_RE.search(reference)
    return bool(filename and filename[0].casefold() not in _DOTTED_PRODUCTS)


def protected_intervals(text: str) -> list[tuple[int, int]]:
    """Find and merge code, URL, path, filename, dotfile, and MCP reference spans."""
    intervals = _code_intervals(text)
    intervals.extend((match.start(), match.end()) for match in _URL_RE.finditer(text))
    for match in _QUOTED_RE.finditer(text):
        if _is_technical_reference(match[0][1:-1]):
            intervals.append((match.start(), match.end()))
    for match in _TOKEN_RE.finditer(text):
        if _is_technical_reference(match[0]):
            intervals.append((match.start(), match.end()))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _ascii_identifier(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char == "_")


def _name_boundaries(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start else ""
    after = text[end] if end < len(text) else ""
    if not _ascii_identifier(before) and not _ascii_identifier(after):
        return True
    # Permit balanced Markdown underscores, including escaped underscores, while
    # keeping my_Codex, Codex_helper, and __Codex__Agent as ASCII identifiers.
    left, right, left_count, right_count = start, end, 0, 0
    while left and text[left - 1] == "_":
        left -= 1
        if left and text[left - 1] == "\\":
            left -= 1
        left_count += 1
    while right < len(text):
        if text[right] == "_":
            right += 1
        elif text.startswith("\\_", right):
            right += 2
        else:
            break
        right_count += 1
    return (
        0 < left_count == right_count <= 3
        and (left == 0 or not _ascii_identifier(text[left - 1]))
        and (right == len(text) or not _ascii_identifier(text[right]))
    )


def _rewrite_text(text: str, compiled: CompiledPromptRules) -> tuple[str, int]:
    if not text or compiled.pattern is None:
        return text, 0
    intervals = protected_intervals(text)
    interval_index, previous, count = 0, 0, 0
    pieces = []
    for match in compiled.pattern.finditer(text):
        start, end = match.span()
        while interval_index < len(intervals) and intervals[interval_index][1] <= start:
            interval_index += 1
        if interval_index < len(intervals) and intervals[interval_index][0] < end:
            continue
        if not _name_boundaries(text, start, end):
            continue
        replacement = compiled.rules[int(match.lastgroup[1:])][1]
        if replacement == match[0]:
            continue
        pieces.extend((text[previous:start], replacement))
        previous = end
        count += 1
    if not count:
        return text, 0
    pieces.append(text[previous:])
    return "".join(pieces), count


def rewrite_prompt_text(text: str, settings: PromptRewriteConfig) -> tuple[str, int]:
    if not settings.enabled:
        return text, 0
    return _rewrite_text(text, compile_prompt_rewrite_rules(settings))


def _rewrite_content(value: Any, compiled: CompiledPromptRules, *, gemini: bool = False) -> tuple[Any, int]:
    if isinstance(value, str) and not gemini:
        return _rewrite_text(value, compiled)
    if not isinstance(value, list):
        return value, 0
    result, count = None, 0
    for index, block in enumerate(value):
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            continue
        if gemini:
            if any(key in block for key in (
                "inlineData", "inline_data", "fileData", "file_data", "functionCall",
                "function_call", "functionResponse", "function_response",
            )):
                continue
        elif block.get("type") != "text":
            continue
        text, replacements = _rewrite_text(block["text"], compiled)
        if replacements:
            if result is None:
                result = list(value)
            result[index] = {**block, "text": text}
            count += replacements
    return (value if result is None else result), count


def rewrite_request(
    request: dict[str, Any], api_format: str, settings: PromptRewriteConfig,
) -> tuple[dict[str, Any], int]:
    """Copy only changed instruction nodes and ancestors; scan the source once."""
    if not settings.enabled:
        return request, 0
    compiled = compile_prompt_rewrite_rules(settings)
    if compiled.pattern is None:
        return request, 0
    result, count = request, 0
    if api_format in ("openai", "anthropic"):
        if api_format == "anthropic" and "system" in request:
            system, replacements = _rewrite_content(request["system"], compiled)
            if replacements:
                result = {**result, "system": system}
                count += replacements
        messages = request.get("messages")
        if isinstance(messages, list):
            updated = None
            for index, message in enumerate(messages):
                if not isinstance(message, dict) or message.get("role") not in ("system", "developer"):
                    continue
                content, replacements = _rewrite_content(message.get("content"), compiled)
                if replacements:
                    if updated is None:
                        updated = list(messages)
                    updated[index] = {**message, "content": content}
                    count += replacements
            if updated is not None:
                result = {**result, "messages": updated}
    elif api_format == "gemini":
        for key in ("systemInstruction", "system_instruction"):
            instruction = request.get(key)
            if not isinstance(instruction, dict):
                continue
            parts, replacements = _rewrite_content(instruction.get("parts"), compiled, gemini=True)
            if replacements:
                result = {**result, key: {**instruction, "parts": parts}}
                count += replacements
    return result, count
