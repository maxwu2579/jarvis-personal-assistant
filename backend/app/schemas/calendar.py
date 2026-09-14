"""Safe calendar projections. No Graph body, attendees, or credential fields."""

from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_serializer

from app.schemas.common import serialize_utc
from app.integrations.calendar.errors import CalendarError


def validate_range(start: str | datetime, end: str | datetime) -> tuple[datetime, datetime]:
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00")) if isinstance(start, str) else start
        b = datetime.fromisoformat(end.replace("Z", "+00:00")) if isinstance(end, str) else end
        if a.utcoffset() is None or b.utcoffset() is None:
            raise ValueError()
        a, b = a.astimezone(timezone.utc), b.astimezone(timezone.utc)
        if not timedelta(0) < b - a <= timedelta(days=31):
            raise ValueError()
        return a, b
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise CalendarError("CALENDAR_INVALID_RANGE") from None


class ConnectionStatus(BaseModel):
    connected: bool
    configured: bool = True
    provider: Literal["microsoft_global"] = "microsoft_global"
    account_hint: str | None = None
    read_authorized: bool = False
    write_authorized: bool = False


class CalendarCreateResult(BaseModel):
    """Safe authoritative projection returned after a Graph create + readback."""

    model_config = ConfigDict(extra="forbid")

    external_event_id: str
    title: str
    start_at: datetime
    end_at: datetime
    location: str | None = None

    @field_serializer("start_at", "end_at")
    def serialize_times(self, value: datetime):
        return serialize_utc(value)


class CalendarMetadata(BaseModel):
    external_calendar_id: str
    name: str


class CalendarEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_event_id: str
    title: str
    start_at: datetime
    end_at: datetime
    is_all_day: bool
    cancelled: bool
    show_as: Literal["free", "tentative", "busy", "oof", "workingElsewhere", "unknown"]
    location: str | None = None

    @field_serializer("start_at", "end_at")
    def serialize_times(self, value: datetime):
        return serialize_utc(value)


class CalendarEventRange(BaseModel):
    events: list[CalendarEvent]
    complete: Literal[True] = True
    # No planner/free-slot claim: showAs is displayed faithfully to the user.
    busy_projection_available: Literal[False] = False
