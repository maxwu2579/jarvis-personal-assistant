"""共享 schema 工具：UTC 时区处理集中于此，所有 API 输出必须经此序列化。"""

from datetime import datetime, timezone

# 与 SQLite 读取约定一致：naive 值视为 UTC
UTC_NAIVE_MESSAGE = (
    "due_at must include a timezone offset, e.g. 2026-08-10T09:00:00+08:00 "
    "(naive datetime is not accepted)"
)


def to_utc(value: datetime) -> datetime:
    """校验输入必须带时区并转换为 UTC；naive 输入抛 ValueError。"""
    if value.tzinfo is None:
        raise ValueError(UTC_NAIVE_MESSAGE)
    return value.astimezone(timezone.utc)


def strip_nonempty(value: str, field_name: str) -> str:
    """去除首尾空格；空值抛 ValueError（统一的消息文本）。"""
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must not be blank")
    return stripped


def serialize_utc(value: datetime | None) -> str | None:
    """统一输出为 UTC ISO 8601（'Z' 结尾），并对 SQLite 读回的 naive 值假定 UTC。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
