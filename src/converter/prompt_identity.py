"""Conservative identity normalization, adapted from MatrixC7's Magpie middleware.

Source: https://github.com/MatrixC7/magpie-middleware-antigravity-identity
Reference revision: 04c38568b2e01dc6e3ffa56a8d51794a73cf3558.
The upstream MIT notice is in docs/licenses/magpie-antigravity-identity-MIT.txt.
Edits use source offsets so custom replacements never cascade into generated text.
"""

import re
from bisect import bisect_right
from functools import lru_cache


OFFICIAL_ANTIGRAVITY_IDENTITY = (
    "You are Antigravity, a powerful agentic AI coding assistant designed by the "
    "Google Deepmind team working on Advanced Agentic Coding."
)
_IDENTITY_STEM = OFFICIAL_ANTIGRAVITY_IDENTITY[:-1]
_BILLING_RE = re.compile(r"^[ \t]*x-anthropic-billing-header:[^\r\n]*$", re.IGNORECASE)
_CONTAINER_RE = re.compile(r"[ \t]*(?:(?:>[ \t]*|#{1,6}[ \t]+|[-+*][ \t]+|\d{1,9}[.)][ \t]+)[ \t]*)*")
_SENTENCE_END_RE = re.compile(r"\.(?=[ \t]|$)|[。!?！？;；]")
_EMPHASIS_RE = re.compile(r"(?P<wrap>\*{1,3}|_{1,3})(?P<inner>.+)(?P=wrap)$")
_ROLE_PREFIX_RE = re.compile(
    r"^(?:you[ \t]+are|you're|i[ \t]+am|i'm|act[ \t]+as)[ \t]+(?:an?[ \t]+)?"
    r"(?:(?:intelligent|advanced|automated|powerful|agentic|coding|software|development|"
    r"programming|personal|ai)[ \t]+){0,6}"
    r"(?:agent|assistant|ai|tool|cli|programmer|companion|engineer|bot|harness)[ \t]+"
    r"(?P<clause>(?:(?:created|built|made|developed|designed|trained|powered|maintained|"
    r"published|released|provided|authored|written)[ \t]+(?:by|on|upon|at))|"
    r"(?:based[ \t]+on)|(?:running[ \t]+(?:inside|in|within|on))|"
    r"(?:integrated[ \t]+(?:into|in)))[ \t]+", re.IGNORECASE,
)
_SDK_RE = re.compile(
    r"^(?:you[ \t]+are|you're)[ \t]+a[ \t]+claude[ \t]+agent,[ \t]*built[ \t]+on[ \t]+"
    r"anthropic's[ \t]+claude[ \t]+agent[ \t]+sdk$", re.IGNORECASE,
)
_CLIENT_DESCRIPTIONS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r",[ \t]*an?[ \t]+interactive[ \t]+cli[ \t]+tool[ \t]+that[ \t]+helps[ \t]+users[ \t]+with[ \t]+software[ \t]+engineering[ \t]+tasks",
    r",[ \t]*(?:anthropic's[ \t]+)?official[ \t]+cli[ \t]+for[ \t]+claude(?:,[ \t]*running[ \t]+within[ \t]+(?:the[ \t]+)?claude[ \t]+agent[ \t]+sdk)?",
    r",[ \t]*(?:(?:an?[ \t]+)?(?:advanced[ \t]+)?(?:coding[ \t]+)?agent[ \t]+)?based[ \t]+on[ \t]+gpt-[a-z0-9._-]+",
    r",[ \t]*an?[ \t]+intelligent[ \t]+ai[ \t]+assistant[ \t]+(?:created|built)[ \t]+by[ \t]+nous[ \t]+research",
    r",[ \t]*an?[ \t]+automated[ \t]+software[ \t]+development[ \t]+harness(?:[ \t]+designed[ \t]+for[ \t]+agentic[ \t]+coding[ \t]+workflows)?",
))
_SELF_DESCRIPTION_RE = re.compile(
    r"^(?:(?:the[ \t]+)?codex[ \t]+cli[ \t]+is[ \t]+an?[ \t]+open[ \t-]+source[ \t]+project[ \t]+led[ \t]+by[ \t]+openai|"
    r"(?:within[ \t]+this[ \t]+context,[ \t]*)?codex[ \t]+refers[ \t]+to[ \t]+the[ \t]+open[ \t-]+source[ \t]+agentic[ \t]+coding[ \t]+interface(?:[ \t]*\([^)]*\))?)$",
    re.IGNORECASE,
)
_OWNER_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9'’_/-]*$")
_OWNER_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9'’._/-]*/[A-Za-z0-9][A-Za-z0-9'’._/-]*$")
_MODEL_RE = re.compile(r"^(?:gpt|claude|gemini|llama|qwen|mistral|deepseek|o[1-9])[\w.-]*$", re.IGNORECASE)
_NON_AUTHORSHIP_RE = re.compile(r"^(?:based|running|integrated)\b", re.IGNORECASE)
_KNOWN_RUNTIMES = frozenset((
    "openclaw", "antigravity", "codex", "claude", "cursor", "windsurf", "aider", "cline",
    "kilocode", "devin", "opencode", "dsh", "continue", "zed", "vscode", "jetbrains",
    "terminal", "cli", "editor", "ide",
))
_OWNER_STOP_WORDS = frozenset(
    "a an the to for with without using use uses via from of in on at and or but so while "
    "if when then that which who whose it they them you your me us we i must shall should "
    "will would can could may might never always only just also not no do does did please "
    "avoid ensure make keep remember note run runs execute executes help helps helping "
    "created built made developed designed trained powered".split()
)
_CN_IDENTITY_RE = re.compile(r"^(?:你是|我是)(?:由)?([^，,。！？\r\n]{1,20}?)(?:开发|制作|构建|训练)的(?:AI)?助手$")
_CN_STOP_RE = re.compile(r"[禁不勿务必请且但并若如注确同只仅需应可允拒执运修改删读写访泄透把将被让使给对从向往于]|为了|因为|作为|认为")


@lru_cache(maxsize=4)
def _client_prefix(names: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(name).replace(r"\ ", r"[ \t]+") for name in sorted(names, key=len, reverse=True))
    return re.compile(rf"^(?:you[ \t]+are|you're)[ \t]+(?:{alternatives})(?![a-z0-9_])", re.IGNORECASE)


def _simple_owner(owner: str) -> bool:
    words = re.split(r"[ \t]+", owner)
    return bool(owner) and len(owner) <= 80 and len(words) <= 6 and all(
        (_OWNER_TOKEN_RE.fullmatch(word) or _OWNER_MODEL_RE.fullmatch(word))
        and (word.lower() not in _OWNER_STOP_WORDS or index == 0 and word.lower() in ("a", "an", "the"))
        for index, word in enumerate(words)
    )


def _identity_prefix(
    sentence: str, punct: str, clients: re.Pattern[str],
) -> tuple[int, str, bool] | None:
    """Return consumed source length, replacement, and whether this is pure identity."""
    if punct in ("?", "？"):
        return None
    full_length = len(sentence) + bool(punct)
    if sentence == _IDENTITY_STEM and punct in ("", "."):
        return full_length, sentence + punct, True
    if _SDK_RE.fullmatch(sentence) or _SELF_DESCRIPTION_RE.fullmatch(sentence):
        return full_length, OFFICIAL_ANTIGRAVITY_IDENTITY, True
    known = clients.match(sentence)
    if known:
        tail = sentence[known.end():]
        if not tail or any(pattern.fullmatch(tail) for pattern in _CLIENT_DESCRIPTIONS):
            return full_length, OFFICIAL_ANTIGRAVITY_IDENTITY, True
        if tail.startswith((",", "，")):
            return known.end(), _IDENTITY_STEM, False
        return None
    comma = re.search(r"[,，]", sentence)
    head = sentence[:comma.start()] if comma else sentence
    prefix = _ROLE_PREFIX_RE.match(head)
    if prefix:
        owner = head[prefix.end():].strip()
        if not _simple_owner(owner):
            return None
        if _NON_AUTHORSHIP_RE.match(prefix["clause"]):
            runtime = re.split(r"[ \t]+", owner)[0].lower()
            model = runtime.rsplit("/", 1)[-1] if _OWNER_MODEL_RE.fullmatch(runtime) else runtime
            if runtime not in _KNOWN_RUNTIMES and not _MODEL_RE.fullmatch(model):
                return None
        if comma:
            return len(head), _IDENTITY_STEM, False
        return full_length, OFFICIAL_ANTIGRAVITY_IDENTITY, True
    chinese = _CN_IDENTITY_RE.fullmatch(head)
    if chinese and not _CN_STOP_RE.search(chinese[1]):
        if comma:
            return len(head), _IDENTITY_STEM, False
        return full_length, OFFICIAL_ANTIGRAVITY_IDENTITY, True
    return None


def collect_identity_edits(
    text: str, code_intervals: list[tuple[int, int]], names: tuple[str, ...],
    custom_spans: list[tuple[int, int]],
) -> list[tuple[int, int, str]]:
    """Normalize explicit prose identities; collapse only leading pure paragraphs."""
    if not text or len(text) > 1_000_000:
        return []
    merged: list[tuple[int, int]] = []
    for start, end in sorted(code_intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(end, merged[-1][1])
        else:
            merged.append((start, end))
    ends = [end for _, end in merged]
    custom_ends = [end for _, end in custom_spans]

    def in_code(start: int, end: int) -> bool:
        index = bisect_right(ends, start)
        return index < len(merged) and merged[index][0] < end

    def in_custom(start: int, end: int) -> bool:
        index = bisect_right(custom_ends, start)
        return index < len(custom_spans) and custom_spans[index][0] < end

    clients = _client_prefix(names)
    edits = []
    lines = []
    offset = 0
    # Remove billing metadata before deciding paragraph boundaries. Otherwise
    # a header can prevent deduplication until a second processing pass.
    for line in text.splitlines(keepends=True):
        line_start, offset = offset, offset + len(line)
        body = line.rstrip("\r\n")
        if (
            _BILLING_RE.fullmatch(body)
            and not in_code(line_start, line_start + body.index(":") + 1)
            and not in_custom(line_start, offset)
        ):
            edits.append((line_start, offset, ""))
        else:
            lines.append((line_start, line))
    removed_until = 0
    prefix_open, identity_emitted = True, False
    for index, (line_start, line) in enumerate(lines):
        offset = line_start + len(line)
        if line_start < removed_until:
            continue
        body = line.rstrip("\r\n")
        if not body.strip():
            continue
        base = _CONTAINER_RE.match(body).end()
        content = body[base:]
        emphasis = _EMPHASIS_RE.fullmatch(content.rstrip(" \t"))
        if emphasis:
            base += len(emphasis["wrap"])
            content = emphasis["inner"]
        line_edits = []
        pure = False
        position = 0
        for _ in range(64):
            position += len(content[position:]) - len(content[position:].lstrip(" \t"))
            if position >= len(content):
                break
            ending = _SENTENCE_END_RE.search(content, position)
            end = ending.start() if ending else len(content)
            sentence = content[position:end]
            punct = ending[0] if ending else ""
            identity = _identity_prefix(sentence, punct, clients) if len(sentence) <= 2048 else None
            if identity:
                consumed, replacement, is_pure = identity
                start = line_start + base + position
                source_end = start + consumed
                if not in_code(start, source_end) and not in_custom(start, source_end):
                    pure = position == 0 and is_pure and not content[position + consumed:].strip()
                    if text[start:source_end] != replacement:
                        line_edits.append((start, source_end, replacement))
            if ending is None:
                break
            position = ending.end()
        # Paragraph removal must never move constraints or code into a new block.
        pure_paragraph = (
            pure and base == 0 and not emphasis
            and (index == 0 or not lines[index - 1][1].strip())
            and (index + 1 == len(lines) or not lines[index + 1][1].strip())
        )
        if prefix_open and pure_paragraph:
            if identity_emitted:
                removed_until = offset
                for following_index in range(index + 1, len(lines)):
                    following_start, following = lines[following_index]
                    if following.strip():
                        break
                    removed_until = following_start + len(following)
                edits.append((line_start, removed_until, ""))
                continue
            identity_emitted = True
        else:
            prefix_open = False
        edits.extend(line_edits)
    merged_edits = []
    for start, end, replacement in sorted(edits):
        if merged_edits and not replacement and not merged_edits[-1][2] and start < merged_edits[-1][1]:
            previous_start, previous_end, _ = merged_edits[-1]
            merged_edits[-1] = previous_start, max(previous_end, end), ""
        else:
            merged_edits.append((start, end, replacement))
    return merged_edits
