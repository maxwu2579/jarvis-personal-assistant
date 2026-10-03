"""Phase 10B frozen V1 natural-language grammar (the authoritative language contract).

ONLY a full match of one frozen positive pattern can lead to a READY proposal.
Nothing here tries to "understand" other phrasings: every other input fails
closed as UNSUPPORTED or NEEDS_CLARIFICATION, decided deterministically so the
frozen edge cases never depend on model behaviour.

Matching rules (frozen): leading/trailing whitespace allowed; English is
case-insensitive over ASCII letters only (re.ASCII: Unicode case-fold variants
such as "ſ" U+017F, "ı" U+0131 or "K" U+212A never match); one trailing period allowed; Chinese "记住" may be followed by
one comma; optional prefixes 请 / 帮我 / 请帮我 (Chinese) and Please (English).
No substring matching, no quote stripping, no range expansion, no grammar
broadening.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Intent = Literal["remember", "replace"]
CanonicalKey = Literal["preferred_study_time", "preferred_session_duration", "usual_available_days"]
StudyTime = Literal["morning", "afternoon", "evening"]
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

WEEKDAY_ORDER: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
STUDY_TIMES: frozenset[str] = frozenset({"morning", "afternoon", "evening"})
CANONICAL_CATEGORY: dict[str, str] = {
    "preferred_study_time": "preference",
    "preferred_session_duration": "preference",
    "usual_available_days": "constraint",
}
DURATION_MIN, DURATION_MAX = 5, 180
MAX_DAY_TOKENS = 14

# ---- fixed mappings (frozen §C / §D) ----

_ZH_PERIOD = {"上午": "morning", "早上": "morning", "下午": "afternoon", "晚上": "evening", "夜晚": "evening"}
# "the {period}" / "in the {period}" slots: night has its own frozen form ("at night").
_EN_PERIOD = {"morning": "morning", "afternoon": "afternoon", "evening": "evening"}

_ZH_DAYS: dict[str, tuple[str, ...]] = {
    "工作日": ("mon", "tue", "wed", "thu", "fri"),
    "周末": ("sat", "sun"),
}
for _index, _numeral in enumerate("一二三四五六日"):
    _ZH_DAYS[f"周{_numeral}"] = (WEEKDAY_ORDER[_index],)
    _ZH_DAYS[f"星期{_numeral}"] = (WEEKDAY_ORDER[_index],)

_EN_DAYS: dict[str, tuple[str, ...]] = {
    "weekdays": ("mon", "tue", "wed", "thu", "fri"),
    "weekends": ("sat", "sun"),
}
for _full, _short in zip(
    ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"), WEEKDAY_ORDER
):
    _EN_DAYS[_full] = (_short,)
    _EN_DAYS[_short] = (_short,)


def _alt(words) -> str:
    # Longest first so "星期一" / "monday" win over shorter alternatives.
    return "(?:" + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)) + ")"


_ZH_PERIOD_RE = f"(?P<period>{_alt(_ZH_PERIOD)})"
_EN_PERIOD_RE = f"(?P<period>{_alt(_EN_PERIOD)})"
_ZH_DAY_TOKEN = _alt(_ZH_DAYS)
_EN_DAY_TOKEN = _alt(_EN_DAYS)
_ZH_DAY_SEP = "(?:，|、|和)"
_EN_DAY_SEP = "(?: ?, ?(?:and )?| and )"
_ZH_DAYS_RE = f"(?P<days>{_ZH_DAY_TOKEN}(?:{_ZH_DAY_SEP}{_ZH_DAY_TOKEN})*)"
_EN_DAYS_RE = f"(?P<days>{_EN_DAY_TOKEN}(?:{_EN_DAY_SEP}{_EN_DAY_TOKEN})*)"
_N_RE = "(?P<n>[0-9]+)"
# English matching is case-insensitive over ASCII only; the day-list split uses
# the same flags as the outer match so " AND " splits exactly like " and ".
_EN_FLAGS = re.IGNORECASE | re.ASCII
_ZH_DAY_SPLIT = re.compile(_ZH_DAY_SEP)
_EN_DAY_SPLIT = re.compile(_EN_DAY_SEP, _EN_FLAGS)

_ZH_PREFIX = "(?:请帮我|请|帮我)?"
_ZH_REMEMBER = _ZH_PREFIX + "记住[，,]?"
_EN_PREFIX = "(?:please )?"


@dataclass(frozen=True)
class _Pattern:
    language: Literal["zh", "en"]
    intent: Intent
    key: CanonicalKey
    regex: re.Pattern[str]
    fixed_period: str | None = None


def _zh(intent: Intent, key: CanonicalKey, body: str) -> _Pattern:
    return _Pattern("zh", intent, key, re.compile(body))


def _en(intent: Intent, key: CanonicalKey, body: str, fixed_period: str | None = None) -> _Pattern:
    return _Pattern("en", intent, key, re.compile(body, _EN_FLAGS), fixed_period)


# Exactly the frozen forms A1–A8 and B1–B6; nothing more.
FROZEN_PATTERNS: tuple[_Pattern, ...] = (
    # A. remember / create intent — Chinese
    _zh("remember", "preferred_study_time", _ZH_REMEMBER + "我通常在?" + _ZH_PERIOD_RE + "学习"),
    _zh("remember", "preferred_session_duration", _ZH_REMEMBER + "我喜欢每次学习" + _N_RE + "分钟"),
    _zh("remember", "preferred_session_duration", _ZH_REMEMBER + "我偏好" + _N_RE + "分钟的学习时段"),
    _zh("remember", "usual_available_days", _ZH_REMEMBER + "我通常在?" + _ZH_DAYS_RE + "有空"),
    # A. remember / create intent — English
    _en("remember", "preferred_study_time", _EN_PREFIX + "remember that i usually study in the " + _EN_PERIOD_RE),
    _en("remember", "preferred_study_time", _EN_PREFIX + "remember that i usually study at night", "evening"),
    _en("remember", "preferred_session_duration", _EN_PREFIX + "remember that i prefer " + _N_RE + "-minute study sessions"),
    _en("remember", "usual_available_days", _EN_PREFIX + "remember that i am usually available on " + _EN_DAYS_RE),
    # B. explicit replace / update intent — Chinese
    _zh("replace", "preferred_study_time", _ZH_PREFIX + "把我的偏好学习时段改为" + _ZH_PERIOD_RE),
    _zh("replace", "preferred_session_duration", _ZH_PREFIX + "把我的单次学习时长改为" + _N_RE + "分钟"),
    _zh("replace", "usual_available_days", _ZH_PREFIX + "把我的通常可用日改为" + _ZH_DAYS_RE),
    # B. explicit replace / update intent — English
    _en("replace", "preferred_study_time", _EN_PREFIX + "change my preferred study time to the " + _EN_PERIOD_RE),
    _en("replace", "preferred_session_duration", _EN_PREFIX + "change my preferred session duration to " + _N_RE + " minutes"),
    _en("replace", "usual_available_days", _EN_PREFIX + "change my usual available days to " + _EN_DAYS_RE),
)

# ---- deterministic fail-closed classification for non-matching input ----
# Checked in this order: UNSUPPORTED markers, then NEEDS_CLARIFICATION markers,
# then a Memory-topic check (topic present -> NEEDS_CLARIFICATION, else UNSUPPORTED).

_EN_UNSUPPORTED = re.compile(
    r"\b(?:don'?t|do not|doesn'?t|does not|not|never|no longer|forget|delete|remove|erase|clear|"
    r"friend|friends|mother|father|mom|dad|wife|husband|colleague|he|she|they|his|her|their|someone|"
    r"document|documents|file|says|said|meeting|appointment|calendar|event|task|tasks|todo|"
    r"remind|reminder|reminders|medicine|ignore|instruction|instructions|system|prompt|rule|rules|"
    r"password|token|secret|api)\b",
    re.IGNORECASE,
)
_ZH_UNSUPPORTED = re.compile(
    r"不|别|没|勿|忘记|忘掉|删除|删掉|清除|移除|取消|朋友|同事|家人|妈妈|爸爸|他|她|"
    r"文档|文件|资料|说|会议|开会|日程|日历|任务|提醒|待办|吃药|忽略|指令|规则|系统|提示词|密码|密钥|验证码"
)
# Quotation marks (straight apostrophes inside words, e.g. contractions, are not quotes).
_QUOTES = re.compile(r"[\"“”„‟«»「」『』‘’]|(?<![A-Za-z])'|'(?![A-Za-z])")
_EN_CLARIFY = re.compile(
    r"\b(?:used to|previously|formerly|before|might|maybe|perhaps|probably|possibly|if|unless|when|"
    r"sometimes|would|could|should|through|until|also|as well)\b",
    re.IGNORECASE,
)
_ZH_CLARIFY = re.compile(r"以前|过去|曾经|之前|原来|可能|也许|大概|或许|如果|要是|假如|有时|偶尔|到|至|同时|另外|还有|并且|以及")
_EN_TOPIC = re.compile(
    r"\b(?:study|studies|studying|session|sessions|minute|minutes|hour|hours|available|free|"
    r"morning|mornings|afternoon|afternoons|evening|evenings|night|nights|tonight|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun|"
    r"weekday|weekdays|weekend|weekends|prefer|preferred|preference|remember)\b",
    re.IGNORECASE,
)
_ZH_TOPIC = re.compile(r"学习|时段|时长|分钟|小时|有空|可用|早上|上午|下午|晚上|夜晚|周[一二三四五六日天]|星期|工作日|周末|偏好|喜欢|记住")


@dataclass(frozen=True)
class GrammarMatch:
    intent: Intent
    key: CanonicalKey
    value: object


@dataclass(frozen=True)
class GrammarRejection:
    status: Literal["UNSUPPORTED", "NEEDS_CLARIFICATION", "INVALID"]
    reason: str


def _strip_one_period(text: str) -> str:
    return text[:-1] if text.endswith((".", "。")) else text


def _parse_duration(raw: str) -> int | None:
    """Arabic integer with explicit minute unit (already matched); None when out of contract."""
    if len(raw) > 3 or (len(raw) > 1 and raw.startswith("0")):
        return None
    number = int(raw)
    return number if DURATION_MIN <= number <= DURATION_MAX else None


def _canonical_token(table: dict, token: str, language: str):
    """Explicit safe lookup: a regex match never proves dictionary membership.

    English tokens must be pure ASCII before case-folding; anything else (or any
    token missing from the frozen table) yields None and the caller fails closed.
    """
    if language == "en":
        if not token.isascii():
            return None
        token = token.lower()
    return table.get(token)


def _split_days(raw: str, language: str) -> list[str]:
    return (_ZH_DAY_SPLIT if language == "zh" else _EN_DAY_SPLIT).split(raw)


def _lookup_days(tokens: list[str], language: str) -> list[str] | None:
    table = _ZH_DAYS if language == "zh" else _EN_DAYS
    days: set[str] = set()
    for token in tokens:
        mapped = _canonical_token(table, token, language)
        if mapped is None:
            return None
        days.update(mapped)
    return normalize_days(days)


def normalize_days(days) -> list[str]:
    """Deduplicate and order Monday → Sunday."""
    return [day for day in WEEKDAY_ORDER if day in set(days)]


def classify(content: str) -> GrammarMatch | GrammarRejection:
    """Full-match the frozen grammar; otherwise fail closed deterministically."""
    text = _strip_one_period(content.strip())
    for pattern in FROZEN_PATTERNS:
        match = pattern.regex.fullmatch(text)
        if match is None:
            continue
        if pattern.key == "preferred_study_time":
            table = _ZH_PERIOD if pattern.language == "zh" else _EN_PERIOD
            value: object = pattern.fixed_period or _canonical_token(table, match.group("period"), pattern.language)
        elif pattern.key == "preferred_session_duration":
            value = _parse_duration(match.group("n"))
            if value is None:
                return GrammarRejection("INVALID", "duration_out_of_contract")
        else:
            tokens = _split_days(match.group("days"), pattern.language)
            if len(tokens) > MAX_DAY_TOKENS:
                return GrammarRejection("INVALID", "too_many_day_tokens")
            value = _lookup_days(tokens, pattern.language)
        if value is None:
            # Canonical lookup failed: not a frozen match; fail closed below.
            break
        return GrammarMatch(pattern.intent, pattern.key, value)

    if _EN_UNSUPPORTED.search(text) or _ZH_UNSUPPORTED.search(text) or _QUOTES.search(text):
        return GrammarRejection("UNSUPPORTED", "unsupported_expression")
    if _EN_CLARIFY.search(text) or _ZH_CLARIFY.search(text):
        return GrammarRejection("NEEDS_CLARIFICATION", "uncertain_expression")
    if _EN_TOPIC.search(text) or _ZH_TOPIC.search(text):
        return GrammarRejection("NEEDS_CLARIFICATION", "not_a_supported_form")
    return GrammarRejection("UNSUPPORTED", "unsupported_expression")


def is_canonical_value(key: str, value: object) -> bool:
    """Type-sensitive canonical value contract (bool never satisfies int)."""
    if key == "preferred_study_time":
        return type(value) is str and value in STUDY_TIMES
    if key == "preferred_session_duration":
        return type(value) is int and DURATION_MIN <= value <= DURATION_MAX
    if key == "usual_available_days":
        return (
            type(value) is list
            and 1 <= len(value) <= 7
            and all(type(day) is str and day in WEEKDAY_ORDER for day in value)
            and value == normalize_days(value)
        )
    return False


def same_value(left: object, right: object) -> bool:
    """Type-sensitive equality: 45 != 45.0 != True, ["mon"] != "mon"."""
    if type(left) is not type(right):
        return False
    if type(left) is list:
        return len(left) == len(right) and all(same_value(a, b) for a, b in zip(left, right))
    return left == right
