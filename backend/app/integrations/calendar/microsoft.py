"""Graph v1.0 GET-only transport and bounded calendarView projection."""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import time
from urllib.parse import quote, urlsplit

import httpx

from app.schemas.calendar import CalendarCreateResult, CalendarEvent, CalendarEventRange, CalendarMetadata, validate_range
from .errors import CalendarError

MAX_PAGES = 20
MAX_EVENTS = 2000
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
EVENT_FIELDS = "id,subject,start,end,isAllDay,isCancelled,showAs,location"


def graph_time(value: dict) -> datetime:
    stamp = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        if value.get("timeZone") != "UTC":
            raise ValueError()
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def project_event(raw: dict) -> CalendarEvent:
    # Missing/ambiguous time or scheduling semantics make the whole read fail.
    if not isinstance(raw["id"], str) or not raw["id"]:
        raise ValueError()
    if type(raw["isAllDay"]) is not bool or type(raw["isCancelled"]) is not bool:
        raise ValueError()
    start, end = graph_time(raw["start"]), graph_time(raw["end"])
    if start >= end:
        raise ValueError()
    location = raw.get("location") or {}
    name = location.get("displayName")
    return CalendarEvent(
        external_event_id=raw["id"], title=raw.get("subject") or "（无标题）",
        start_at=start, end_at=end, is_all_day=raw["isAllDay"],
        cancelled=raw["isCancelled"], show_as=raw.get("showAs", "unknown"),
        location=name if isinstance(name, str) else None,
    )


class MicrosoftGraphCalendarProvider:
    def __init__(self, auth, *, transport=None, sleep=time.sleep):
        self.auth, self._transport, self._sleep = auth, transport, sleep
        self.base = auth.config.jarvis_microsoft_graph_base

    def get_connection_status(self):
        return self.auth.get_connection_status()

    def _delay(self, response, retry):
        raw = response.headers.get("Retry-After")
        if raw:
            try:
                delay = float(raw) if raw.isdigit() else (
                    parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds()
                if delay > 10:
                    # Do not sleep less than Retry-After or tie up a local worker indefinitely.
                    raise CalendarError("CALENDAR_RATE_LIMITED")
                return max(0, delay)
            except (ValueError, TypeError, OverflowError):
                pass
        return 0.5 * (2 ** retry)

    def _get(self, client, url, params=None):
        token = self.auth.access_token()
        refreshed = False
        retry = 0
        while True:
            try:
                # Never follow redirects carrying Authorization to an untrusted host.
                with client.stream("GET", url, params=params, headers={
                    "Authorization": f"Bearer {token}", "Prefer": 'outlook.timezone="UTC"',
                    "Accept": "application/json",
                }) as response:
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_RESPONSE_BYTES:
                            raise CalendarError("CALENDAR_UPSTREAM_ERROR")
                status = response.status_code
                if status == 401:
                    if refreshed:
                        self.auth.mark_auth_required()
                        raise CalendarError("CALENDAR_AUTH_FAILED")
                    token = self.auth.access_token(force_refresh=True)
                    refreshed = True
                    continue
                if status == 403:
                    raise CalendarError("CALENDAR_PERMISSION_DENIED")
                if status == 429 or status in (500, 502, 503, 504):
                    if retry >= 2:
                        raise CalendarError("CALENDAR_RATE_LIMITED" if status == 429 else "CALENDAR_UPSTREAM_ERROR")
                    self._sleep(self._delay(response, retry))
                    retry += 1
                    continue
                if status != 200:
                    raise CalendarError("CALENDAR_UPSTREAM_ERROR")
                body = json.loads(data)
                if not isinstance(body, dict):
                    raise ValueError()
                return body
            except httpx.TransportError:
                if retry >= 2:
                    raise CalendarError("CALENDAR_UPSTREAM_ERROR") from None
                self._sleep(0.5 * (2 ** retry))
                retry += 1
            except CalendarError:
                raise
            except Exception:
                raise CalendarError("CALENDAR_UPSTREAM_ERROR") from None

    def _write_request(self, client, method, url, *, body=None, expected=200):
        """Bounded Graph write transport with one refresh and stable retries."""
        token = self.auth.write_access_token()
        refreshed = False
        retry = 0
        while True:
            try:
                with client.stream(method, url, json=body, headers={
                    "Authorization": f"Bearer {token}",
                    "Prefer": 'outlook.timezone="UTC"',
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                }) as response:
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_RESPONSE_BYTES:
                            raise CalendarError("CALENDAR_CREATE_FAILED", retryable=False)
                status = response.status_code
                if status == 401:
                    if refreshed:
                        raise CalendarError("CALENDAR_WRITE_AUTH_REQUIRED")
                    token = self.auth.write_access_token(force_refresh=True)
                    refreshed = True
                    continue
                if status == 403:
                    raise CalendarError("CALENDAR_WRITE_PERMISSION_DENIED")
                if status == 429 or status in (500, 502, 503, 504):
                    if retry >= 2:
                        code = "CALENDAR_RATE_LIMITED" if status == 429 else "CALENDAR_CREATE_FAILED"
                        raise CalendarError(code, retryable=True)
                    self._sleep(self._delay(response, retry))
                    retry += 1
                    continue
                if status != expected:
                    raise CalendarError("CALENDAR_CREATE_FAILED")
                parsed = json.loads(data)
                if not isinstance(parsed, dict):
                    raise ValueError()
                return parsed
            except httpx.TransportError:
                if retry >= 2:
                    raise CalendarError("CALENDAR_CREATE_FAILED", retryable=True) from None
                self._sleep(0.5 * (2 ** retry))
                retry += 1
            except CalendarError:
                raise
            except Exception:
                raise CalendarError("CALENDAR_CREATE_FAILED") from None

    def _client(self):
        return httpx.Client(timeout=10, follow_redirects=False, trust_env=False, transport=self._transport)

    def get_default_calendar(self):
        with self._client() as client:
            body = self._get(client, self.base + "/me/calendar", {"$select": "id,name"})
        try:
            if not isinstance(body["id"], str) or not body["id"]:
                raise ValueError()
            return CalendarMetadata(external_calendar_id=body["id"], name=body["name"])
        except Exception:
            raise CalendarError("CALENDAR_UPSTREAM_ERROR") from None

    def list_events(self, start, end):
        start, end = validate_range(start, end)
        url = self.base + "/me/calendar/calendarView"
        params = {"startDateTime": start.isoformat(), "endDateTime": end.isoformat(),
                  "$select": EVENT_FIELDS, "$top": "100"}
        events = {}
        visited = set()
        with self._client() as client:
            for _ in range(MAX_PAGES):
                if url in visited:
                    raise CalendarError("CALENDAR_UPSTREAM_ERROR")
                visited.add(url)
                body = self._get(client, url, params)
                try:
                    rows = body["value"]
                    if not isinstance(rows, list):
                        raise ValueError()
                    for raw in rows:
                        event = project_event(raw)
                        previous = events.get(event.external_event_id)
                        if previous is not None and previous != event:
                            raise ValueError()
                        events[event.external_event_id] = event
                    if len(events) > MAX_EVENTS:
                        raise ValueError()
                    next_link = body.get("@odata.nextLink")
                    if not next_link:
                        return CalendarEventRange(events=sorted(events.values(), key=lambda e: (e.start_at, e.external_event_id)))
                    parsed = urlsplit(next_link)
                    expected = urlsplit(self.base)
                    if (parsed.scheme != "https" or parsed.netloc != expected.netloc
                            or parsed.path != expected.path + "/me/calendar/calendarView"
                            or parsed.fragment or parsed.username or parsed.password):
                        raise ValueError()
                    url, params = next_link, None
                except Exception:
                    raise CalendarError("CALENDAR_UPSTREAM_ERROR") from None
        raise CalendarError("CALENDAR_UPSTREAM_ERROR")

    def create_event(self, *, title, start, end, location, transaction_id):
        """Create exactly one timed event, then authoritatively read it back."""
        start, end = validate_range(start, end)
        if not title.strip() or len(title) > 200 or end - start > timedelta(days=1):
            raise CalendarError("CALENDAR_CREATE_INVALID")
        body = {
            "subject": title.strip(),
            "start": {"dateTime": start.isoformat().replace("+00:00", "Z"), "timeZone": "UTC"},
            "end": {"dateTime": end.isoformat().replace("+00:00", "Z"), "timeZone": "UTC"},
            "transactionId": transaction_id,
        }
        if location:
            body["location"] = {"displayName": location}
        with self._client() as client:
            created = self._write_request(
                client, "POST", self.base + "/me/calendar/events", body=body, expected=201
            )
            event_id = created.get("id")
            if not isinstance(event_id, str) or not event_id or len(event_id) > 500:
                raise CalendarError("CALENDAR_CREATE_FAILED")
            authoritative = self._write_request(
                client,
                "GET",
                self.base + "/me/events/" + quote(event_id, safe=""),
                expected=200,
            )
        try:
            event = project_event(authoritative)
            if (event.external_event_id != event_id or event.title != title.strip()
                    or event.start_at != start or event.end_at != end or event.is_all_day
                    or event.cancelled):
                raise ValueError()
            expected_location = location or None
            if (event.location or None) != expected_location:
                raise ValueError()
            return CalendarCreateResult(
                external_event_id=event.external_event_id,
                title=event.title,
                start_at=event.start_at,
                end_at=event.end_at,
                location=event.location,
            )
        except CalendarError:
            raise
        except Exception:
            raise CalendarError("CALENDAR_CREATE_FAILED") from None
