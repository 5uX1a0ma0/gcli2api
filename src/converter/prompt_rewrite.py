"""Rewrite message text, normalize instruction identities, and optionally cloak them."""

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Iterator

from src.converter.prompt_identity import collect_identity_edits


DEFAULT_REPLACEMENT = "Antigravity"
# DongHua3/cpa-plugin-antigravity v0.4.0 (d80e347), plus Claude Agent and MatrixC7 names.
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
    "DeepSeek Harness", "DSH", "Cascade", "KiloCode",
)


@dataclass(frozen=True)
class PromptRewriteConfig:
    enabled: bool = False
    use_default_keywords: bool = True
    rules: tuple[tuple[str, str], ...] = ()
    cloak_enabled: bool = False


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
        if not name:
            raise ValueError(f"第 {index} 条规则的客户端名称不能为空；替换文本留空表示删除")
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
    custom_groups: frozenset[str]
    custom_pattern: re.Pattern[str] | None


@lru_cache(maxsize=32)
def _compile_rules(
    rules: tuple[tuple[str, str], ...], custom_names: tuple[str, ...],
) -> CompiledPromptRules:
    # Named alternatives preserve the replacement lookup and Unicode source indices.
    custom_groups = frozenset(
        f"r{index}" for index, (name, _) in enumerate(rules) if name.casefold() in custom_names
    )
    defaults = "|".join(
        f"(?P<r{index}>{re.escape(name)})" for index, (name, _) in enumerate(rules) if f"r{index}" not in custom_groups
    )
    # Reject ASCII word interiors before trying all default alternatives. This
    # keeps very long unbroken identifiers linear and still allows Markdown _.
    pattern = re.compile(rf"(?<!(?a:[A-Za-z0-9]))(?:{defaults})", re.IGNORECASE) if defaults else None
    custom_pattern = re.compile(
        "|".join(f"(?P<r{index}>{re.escape(name)})" for index, (name, _) in enumerate(rules) if f"r{index}" in custom_groups),
        re.IGNORECASE,
    ) if custom_groups else None
    return CompiledPromptRules(rules, pattern, custom_groups, custom_pattern)


def compile_prompt_rewrite_rules(settings: PromptRewriteConfig) -> CompiledPromptRules:
    return _compile_rules(
        effective_prompt_rewrite_rules(settings), tuple(sorted({name.casefold() for name, _ in settings.rules})),
    )


_CODE_START_RE = re.compile(
    r"^[ \t]*(?:>[ \t]*)*(?:(?:[-+*]|\d{1,9}[.)])[ \t]+)?(?P<fence>`{3,}|~{3,})|(?P<inline>`+)", re.MULTILINE,
)
_INDENTED_CODE_RE = re.compile(r"^(?: {4,}|\t)[^\r\n]*(?:\r?\n|$)", re.MULTILINE)
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
                rf"^[ \t]*(?:>[ \t]*)*{re.escape(delimiter[0])}{{{len(delimiter)},}}[ \t]*\r?$",
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
    intervals.extend(match.span() for match in _INDENTED_CODE_RE.finditer(text))
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


def _default_matches(
    text: str, compiled: CompiledPromptRules, excluded: list[tuple[int, int]],
) -> Iterator[re.Match[str]]:
    """Match untouched source ranges so overlapping long defaults cannot hide names."""
    if compiled.pattern is None:
        return
    previous = 0
    for start, end in sorted(excluded):
        if previous < start:
            yield from compiled.pattern.finditer(text, previous, start)
        previous = max(previous, end)
    if previous < len(text):
        yield from compiled.pattern.finditer(text, previous)


def _rewrite_text(
    text: str, compiled: CompiledPromptRules, *, normalize_identity: bool = False,
) -> tuple[str, int]:
    if not text:
        return text, 0
    custom_spans, custom_edits = [], []
    for match in compiled.custom_pattern.finditer(text) if compiled.custom_pattern else ():
        custom_spans.append(match.span())
        replacement = compiled.rules[int(match.lastgroup[1:])][1]
        if replacement != match[0]:
            custom_edits.append((*match.span(), replacement))
    identities = collect_identity_edits(
        text, _code_intervals(text) + [match.span() for match in _INDENTED_CODE_RE.finditer(text)],
        DEFAULT_KEYWORDS, custom_spans,
    ) if normalize_identity else []
    identities.sort()
    edits = list(identities) + custom_edits
    intervals = protected_intervals(text) if compiled.pattern else []
    excluded = custom_spans + [(start, end) for start, end, _ in identities]
    interval_index = 0
    for match in _default_matches(text, compiled, excluded):
        start, end = match.span()
        # Built-in names protect technical references. An explicit custom mapping
        # is a literal find/replace, including code, paths and identifier substrings.
        while interval_index < len(intervals) and intervals[interval_index][1] <= start:
            interval_index += 1
        if interval_index < len(intervals) and intervals[interval_index][0] < end:
            continue
        if not _name_boundaries(text, start, end):
            continue
        replacement = compiled.rules[int(match.lastgroup[1:])][1]
        if replacement != match[0]:
            edits.append((start, end, replacement))
    if not edits:
        return text, 0
    pieces, previous = [], 0
    for start, end, replacement in sorted(edits):
        pieces.extend((text[previous:start], replacement))
        previous = end
    pieces.append(text[previous:])
    return "".join(pieces), len(edits)


def rewrite_prompt_text(text: str, settings: PromptRewriteConfig) -> tuple[str, int]:
    if not settings.enabled:
        return text, 0
    return _rewrite_text(text, compile_prompt_rewrite_rules(settings))


_ZWSP = "\u200b"
_CLOAK_WORD_RE = re.compile(r"[A-Za-z]+(?:\u200b[A-Za-z]+)*")
_TEXT_TYPES = frozenset({"text", "input_text", "output_text"})
_SIGNED_KEYS = ("signature", "thoughtSignature", "thought_signature")
_GEMINI_NON_TEXT_KEYS = (
    "inlineData", "inline_data", "fileData", "file_data", "functionCall",
    "function_call", "functionResponse", "function_response",
)
TextTransform = Callable[[str], tuple[str, int]]


def cloak_text(text: str) -> tuple[str, int]:
    """Split English words of at least five letters once, using a real U+200B."""
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        word = match[0]
        if len(word) < 5 or _ZWSP in word:
            return word
        count += 1
        return word[:3] + _ZWSP + word[3:]

    result = _CLOAK_WORD_RE.sub(replace, text)
    return result, count


def _rewrite_json_strings(value: Any, apply: TextTransform) -> tuple[Any, int]:
    """Tool outputs are data: replace string values without changing JSON keys."""
    if isinstance(value, str):
        return apply(value)
    if not isinstance(value, (dict, list)):
        return value, 0
    result, count = value, 0
    for key, item in (value.items() if isinstance(value, dict) else enumerate(value)):
        changed, replacements = _rewrite_json_strings(item, apply)
        if replacements:
            if result is value:
                result = value.copy()
            result[key] = changed
            count += replacements
    return result, count


def _rewrite_block(
    block: Any, apply: TextTransform, *, gemini: bool = False,
) -> tuple[Any, int]:
    if isinstance(block, str):
        return apply(block)
    if not isinstance(block, dict):
        return block, 0
    if block.get("thought") is True or any(key in block for key in _SIGNED_KEYS):
        return block, 0
    if block.get("type") in ("thinking", "redacted_thinking"):
        return block, 0
    if gemini:
        for key in ("functionResponse", "function_response"):
            response = block.get(key)
            if isinstance(response, dict) and "response" in response:
                output, count = _rewrite_json_strings(response["response"], apply)
                if count:
                    return {**block, key: {**response, "response": output}}, count
        if any(key in block for key in _GEMINI_NON_TEXT_KEYS):
            return block, 0
    elif block.get("type") == "tool_result":
        content, count = _rewrite_content(block.get("content"), apply)
        return ({**block, "content": content}, count) if count else (block, 0)
    if block.get("type") not in (_TEXT_TYPES | {None} if gemini else _TEXT_TYPES):
        return block, 0
    if not isinstance(block.get("text"), str):
        return block, 0
    text, count = apply(block["text"])
    return ({**block, "text": text}, count) if count else (block, 0)


def _rewrite_content(
    value: Any, apply: TextTransform, *, gemini: bool = False,
) -> tuple[Any, int]:
    if isinstance(value, str):
        return apply(value)
    if isinstance(value, dict):
        return _rewrite_block(value, apply, gemini=gemini)
    if not isinstance(value, list):
        return value, 0
    result, count = [], 0
    for block in value:
        changed, replacements = _rewrite_block(block, apply, gemini=gemini)
        count += replacements
        # Empty text parts can become missing-text protobuf nodes upstream.
        # Drop only parts emptied by this pass, retaining images and tool results.
        if replacements and _empty_text(changed):
            continue
        result.append(changed)
    return (result, count) if count else (value, 0)


def _empty_text(value: Any) -> bool:
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return all(_empty_text(block) for block in value)
    return (
        isinstance(value, dict) and isinstance(value.get("text"), str)
        and value.get("type") in _TEXT_TYPES | {None}
        and not any(key in value for key in (*_SIGNED_KEYS, *_GEMINI_NON_TEXT_KEYS))
        and not value.get("thought") and not value["text"].strip()
    )


def _rewrite_messages(
    messages: Any, keyword: TextTransform, instruction: TextTransform, *, gemini: bool = False,
) -> tuple[Any, int]:
    if not isinstance(messages, list):
        return messages, 0
    result, count = [], 0
    field = "parts" if gemini else "content"
    for message in messages:
        if not isinstance(message, dict):
            result.append(message)
            continue
        apply = instruction if message.get("role") in ("system", "developer") else keyword
        content, replacements = _rewrite_content(message.get(field), apply, gemini=gemini)
        changed = {**message, field: content} if replacements else message
        count += replacements
        for key in ("refusal", "reasoning_content"):
            if isinstance(message.get(key), str):
                text, extra = keyword(message[key])
                if extra:
                    changed = {**changed, key: text}
                    count += extra
        # Responses tool output items carry their result in output, not content.
        if message.get("type") == "function_call_output" and "output" in message:
            output, extra = _rewrite_json_strings(message["output"], keyword)
            if extra:
                changed = {**changed, "output": output}
                count += extra
        # A deletion may empty a text-only message. Omit it before conversion,
        # while retaining tool responses, calls, images and other typed blocks.
        if replacements and _empty_text(content) and message.get("role") in (
            "system", "developer", "user", "assistant", "model",
        ) and not any(changed.get(key) for key in ("tool_calls", "function_call", "refusal", "reasoning_content")):
            continue
        result.append(changed)
    return (result, count) if count else (messages, 0)


def _rewrite_descriptions(value: Any, apply: TextTransform) -> tuple[Any, int]:
    """Visit tool/schema descriptions, preserving names, enums, defaults and examples."""
    if not isinstance(value, (dict, list)):
        return value, 0
    result, count = value, 0
    for key, item in (value.items() if isinstance(value, dict) else enumerate(value)):
        if key in ("enum", "const", "default", "example", "examples"):
            continue
        changed, replacements = apply(item) if key == "description" and isinstance(item, str) else _rewrite_descriptions(item, apply)
        if replacements:
            if result is value:
                result = value.copy()
            result[key] = changed
            count += replacements
    return result, count


def rewrite_request(
    request: dict[str, Any], api_format: str, settings: PromptRewriteConfig,
) -> tuple[dict[str, Any], int]:
    """Replace all message text once; cloak only instructions and tool descriptions."""
    if not isinstance(request, dict) or api_format not in ("openai", "anthropic", "gemini", "responses"):
        return request, 0
    if not settings.enabled and not settings.cloak_enabled:
        return request, 0
    compiled = compile_prompt_rewrite_rules(settings) if settings.enabled else _compile_rules((), ())

    def keyword(text: str) -> tuple[str, int]:
        return _rewrite_text(text, compiled)

    def instruction(text: str) -> tuple[str, int]:
        text, count = _rewrite_text(text, compiled, normalize_identity=settings.enabled and settings.use_default_keywords)
        if settings.cloak_enabled:
            text, extra = cloak_text(text)
            count += extra
        return text, count

    def description(text: str) -> tuple[str, int]:
        text, count = keyword(text)
        if settings.cloak_enabled:
            text, extra = cloak_text(text)
            count += extra
        return text, count

    result, count = request, 0

    def set_field(key: str, value: Any, replacements: int, *, empty: bool = False):
        nonlocal result, count
        if replacements:
            result = {**result}
            if empty:
                result.pop(key, None)
            else:
                result[key] = value
            count += replacements

    if api_format in ("openai", "anthropic"):
        if api_format == "anthropic" and "system" in request:
            system, replacements = _rewrite_content(request["system"], instruction)
            set_field("system", system, replacements, empty=_empty_text(system))
        messages, replacements = _rewrite_messages(request.get("messages"), keyword, instruction)
        set_field("messages", messages, replacements)
    elif api_format == "responses":
        if isinstance(request.get("instructions"), str):
            text, replacements = instruction(request["instructions"])
            set_field("instructions", text, replacements, empty=not text.strip())
        value = request.get("input")
        changed, replacements = keyword(value) if isinstance(value, str) else _rewrite_messages(value, keyword, instruction)
        set_field("input", changed, replacements)
    elif api_format == "gemini":
        for key in ("systemInstruction", "system_instruction"):
            system = request.get(key)
            if not isinstance(system, dict):
                continue
            parts, replacements = _rewrite_content(system.get("parts"), instruction, gemini=True)
            set_field(key, {**system, "parts": parts}, replacements, empty=_empty_text(parts))
        contents, replacements = _rewrite_messages(request.get("contents"), keyword, instruction, gemini=True)
        set_field("contents", contents, replacements)
    for key in ("tools", "functions"):
        if key in request:
            definitions, replacements = _rewrite_descriptions(request[key], description)
            set_field(key, definitions, replacements)
    return result, count
