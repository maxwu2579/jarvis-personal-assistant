"""Minimal deterministic datetime parser for Phase 8A.

This intentionally covers the authorized Chinese relative-date vocabulary. It
does not attempt general NLP and never invents a time for a date-only phrase.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
DATE_PATTERN = re.compile(
    r"(?P<absolute>\d{4}-\d{1,2}-\d{1,2})|"
    r"(?P<relative>今天|明天|后天|今晚)|"
    r"(?P<week>(?:本周|下周|周)[一二三四五六日天])"
)
COLON_TIME_PATTERN = re.compile(r"(?<!\d)(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)(?!\d)")
CHINESE_TIME_PATTERN = re.compile(
    r"(?P<daypart>上午|早上|下午|晚上|今晚|中午)?\s*"
    r"(?P<hour>\d{1,2})\s*(?:点|时)"
    r"(?:(?P<half>半)|\s*(?P<minute>\d{1,2})\s*分?)?"
)
AMBIGUOUS_DAYPARTS = ("晚上", "今晚", "下午", "上午", "早上", "中午", "早一点", "晚点", "下班后")


@dataclass(slots=True)
class ActionTimeError(ValueError):
    code: str
    public_message: str

    def __str__(self) -> str:
        return self.public_message


def parse_relative_date(text: str, current_date: date) -> date:
    """Resolve one authorized date token against the user's local date."""
    match = DATE_PATTERN.search(text)
    if match is None:
        raise ActionTimeError("DATE_MISSING", "请提供明确日期。")
    if match.group("absolute"):
        try:
            return date.fromisoformat(match.group("absolute"))
        except ValueError as exc:
            raise ActionTimeError("DATE_INVALID", "日期格式无效，请重新提供日期。") from exc

    relative = match.group("relative")
    if relative in {"今天", "今晚"}:
        return current_date
    if relative == "明天":
        return current_date + timedelta(days=1)
    if relative == "后天":
        return current_date + timedelta(days=2)

    week_text = match.group("week")
    target_weekday = WEEKDAYS[week_text[-1]]
    if week_text.startswith("下周"):
        start_next_week = current_date + timedelta(days=7 - current_date.weekday())
        return start_next_week + timedelta(days=target_weekday)
    if week_text.startswith("本周"):
        start_this_week = current_date - timedelta(days=current_date.weekday())
        return start_this_week + timedelta(days=target_weekday)
    delta = (target_weekday - current_date.weekday()) % 7
    return current_date + timedelta(days=delta)


def _parse_clock(text: str) -> time:
    colon = COLON_TIME_PATTERN.search(text)
    if colon:
        return time(hour=int(colon.group("hour")), minute=int(colon.group("minute")))

    chinese = CHINESE_TIME_PATTERN.search(text)
    if chinese is None:
        if any(word in text for word in AMBIGUOUS_DAYPARTS):
            raise ActionTimeError("TIME_AMBIGUOUS", "时间表达不够明确，请提供具体几点。")
        raise ActionTimeError("TIME_MISSING", "请提供具体时间。")

    hour = int(chinese.group("hour"))
    minute = 30 if chinese.group("half") else int(chinese.group("minute") or 0)
    if hour > 23 or minute > 59:
        raise ActionTimeError("TIME_INVALID", "时间无效，请重新提供具体时间。")

    daypart = chinese.group("daypart")
    if daypart is None and hour <= 12:
        raise ActionTimeError("TIME_AMBIGUOUS", "请说明是上午、下午，或使用 24 小时制。")
    if daypart in {"下午", "晚上", "今晚"} and hour < 12:
        hour += 12
    elif daypart in {"上午", "早上"} and hour == 12:
        hour = 0
    elif daypart == "中午" and hour < 11:
        hour += 12
    return time(hour=hour, minute=minute)


def parse_user_datetime(text: str, *, user_timezone: str, now_utc: datetime) -> datetime:
    """Parse, validate as future, and normalize one user-grounded phrase to UTC."""
    cleaned = text.strip()
    if not cleaned:
        raise ActionTimeError("TIME_MISSING", "请提供日期和具体时间。")

    # A fully explicit aware ISO value is accepted; naive ISO values are not.
    iso_candidate = None
    if "T" in cleaned or re.search(r"\s\d{1,2}:\d{2}", cleaned):
        try:
            iso_candidate = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        except ValueError:
            iso_candidate = None
    if iso_candidate is not None:
        if iso_candidate.tzinfo is None:
            raise ActionTimeError("TIMEZONE_MISSING", "时间必须包含时区。")
        result = iso_candidate.astimezone(timezone.utc)
    else:
        timezone_info = ZoneInfo(user_timezone)
        local_now = now_utc.astimezone(timezone_info)
        # Resolve clock ambiguity first: "晚上提醒我" must ask for an hour,
        # rather than obscuring that product question behind a generic date error.
        target_time = _parse_clock(cleaned)
        target_date = parse_relative_date(cleaned, local_now.date())
        result = datetime.combine(target_date, target_time, timezone_info).astimezone(timezone.utc)

    if result <= now_utc.astimezone(timezone.utc):
        raise ActionTimeError("TIME_PAST", "该时间已经过去，请提供未来时间。")
    return result


def parse_user_datetime_range(
    start_text: str,
    end_text: str,
    *,
    user_timezone: str,
    now_utc: datetime,
) -> tuple[datetime, datetime]:
    """Resolve an explicit timed range; only the start date/daypart may be inherited.

    Examples: ``明天下午3点`` + ``4点`` and ``2026-09-12 15:00+08:00`` +
    ``2026-09-12 16:00+08:00``. No duration or missing boundary is invented.
    """
    if not start_text.strip() or not end_text.strip():
        raise ActionTimeError("RANGE_MISSING", "请提供明确的开始时间和结束时间。")
    timezone_info = ZoneInfo(user_timezone)

    def checked_local(target_date: date, target_time: time) -> datetime:
        naive = datetime.combine(target_date, target_time)
        possible: set[datetime] = set()
        for fold in (0, 1):
            local = naive.replace(tzinfo=timezone_info, fold=fold)
            utc = local.astimezone(timezone.utc)
            if utc.astimezone(timezone_info).replace(tzinfo=None) == naive:
                possible.add(utc)
        if len(possible) != 1:
            raise ActionTimeError("TIME_AMBIGUOUS", "该本地时间在时区切换时不明确，请提供带时区的明确时间。")
        return possible.pop()

    def parse_boundary(text: str) -> datetime:
        cleaned = text.strip()
        try:
            iso = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        except ValueError:
            iso = None
        if iso is not None and iso.tzinfo is not None:
            result = iso.astimezone(timezone.utc)
        else:
            target_time = _parse_clock(cleaned)
            target_date = parse_relative_date(cleaned, now_utc.astimezone(timezone_info).date())
            result = checked_local(target_date, target_time)
        if result <= now_utc.astimezone(timezone.utc):
            raise ActionTimeError("TIME_PAST", "该时间已经过去，请提供未来时间。")
        return result

    start = parse_boundary(start_text)

    if DATE_PATTERN.search(end_text) or "T" in end_text or re.search(r"\s\d{1,2}:\d{2}", end_text):
        end = parse_boundary(end_text)
    else:
        inherited = end_text
        if not re.search(r"上午|早上|下午|晚上|今晚|中午", end_text):
            part = re.search(r"上午|早上|下午|晚上|今晚|中午", start_text)
            if part:
                inherited = part.group(0) + end_text
        end_clock = _parse_clock(inherited)
        end = checked_local(start.astimezone(timezone_info).date(), end_clock)

    if end <= start:
        raise ActionTimeError("RANGE_INVALID", "结束时间必须晚于开始时间。")
    if end - start > timedelta(days=1):
        raise ActionTimeError("RANGE_TOO_LONG", "单次日历事件不能超过 24 小时。")
    return start, end
