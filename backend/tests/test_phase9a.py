"""Offline Phase 9A contract/security tests; no real credentials or Graph writes."""
import json
import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from datetime import datetime, timezone

import httpx
import msal
import pytest

from app.main import app
from app.core.config import Settings, PROJECT_ROOT
from app.integrations.calendar.auth import MicrosoftAuth, SCOPES
from app.integrations.calendar.credentials import cache_path, encrypted_cache
from app.integrations.calendar.errors import CalendarError
from app.integrations.calendar.factory import get_calendar_auth, get_calendar_provider
from app.integrations.calendar.microsoft import MicrosoftGraphCalendarProvider
from app.integrations.calendar.security import CalendarCallbackPrivacy, secure_library_logging
from app.schemas.calendar import validate_range

CLIENT_ID = "01234567-89ab-cdef-0123-456789abcdef"
START, END = "2026-09-08T00:00:00Z", "2026-09-15T00:00:00Z"
ORIGIN = {"Origin": "http://localhost:5173"}
SENTINEL = "test-only-token-must-never-escape"


class MemoryEncryptedCache(msal.SerializableTokenCache):
    """Test double only; production always requires real DPAPI."""
    is_encrypted = True


class StubMSAL:
    def __init__(self, client_id, **kwargs):
        self.cache = kwargs["token_cache"]
        self.kwargs = kwargs
        self.silent_calls = []
        self.exchange_calls = 0
        self.outcome = {"access_token": SENTINEL}
        self.flow_number = 0

    def initiate_auth_code_flow(self, **kwargs):
        self.flow_number += 1
        state = f"fake-unpredictability-tested-with-real-msal-{self.flow_number}"
        self.flow_kwargs = kwargs
        return {"state": state, "code_verifier": "test-pkce-verifier", "auth_uri":
                f"https://login.microsoftonline.com/common/oauth2/v2.0/authorize?state={state}"}

    def acquire_token_by_auth_code_flow(self, flow, response):
        self.exchange_calls += 1
        if response.get("error"):
            return {"error": "access_denied", "error_description": SENTINEL}
        if isinstance(self.outcome, Exception):
            raise self.outcome
        account = {
            "home_account_id": "account-id", "environment": "login.microsoftonline.com",
            "realm": "tenant", "username": "test@example.invalid", "local_account_id": "local-id"}
        self.cache.modify(msal.TokenCache.CredentialType.ACCOUNT, account, account)
        return self.outcome

    def get_accounts(self):
        return list(self.cache.search(msal.TokenCache.CredentialType.ACCOUNT))

    def remove_account(self, account):
        self.cache.modify(msal.TokenCache.CredentialType.ACCOUNT, account)

    def acquire_token_silent(self, scopes, **kwargs):
        self.silent_calls.append((scopes, kwargs))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def auth():
    config = Settings(_env_file=None, jarvis_microsoft_client_id=CLIENT_ID)
    return MicrosoftAuth(config, cache_factory=lambda _: MemoryEncryptedCache(), app_factory=StubMSAL)


def connect_auth(auth):
    url, binding = auth.begin()
    state = parse_qs(urlsplit(url).query)["state"][0]
    auth.finish({"state": state, "code": "test-only-code"}, binding)


def event(**updates):
    raw = {"id": "event-1", "subject": "Study", "start": {"dateTime": "2026-09-08T10:00:00.0000000", "timeZone": "UTC"},
           "end": {"dateTime": "2026-09-08T11:00:00.0000000", "timeZone": "UTC"},
           "isAllDay": False, "isCancelled": False, "showAs": "busy",
           "location": {"displayName": "Library"}, "body": {"content": SENTINEL},
           "attendees": [{"emailAddress": SENTINEL}], "extensions": [SENTINEL]}
    raw.update(updates)
    return raw


def provider(auth, handler, sleeps=None):
    connect_auth(auth)
    return MicrosoftGraphCalendarProvider(auth, transport=httpx.MockTransport(handler),
                                          sleep=(sleeps if sleeps is not None else []).append)


def test_missing_config_safe_status_and_auth_required(auth):
    auth.config.jarvis_microsoft_client_id = ""
    assert auth.get_connection_status().model_dump()["configured"] is False
    with pytest.raises(CalendarError, match="not configured"):
        auth.begin()


@pytest.mark.parametrize(("field", "value"), [
    ("jarvis_microsoft_client_id", "../../escape"),
    ("jarvis_microsoft_authority", "https://attacker.invalid/common"),
    ("jarvis_microsoft_graph_base", "http://graph.microsoft.com/v1.0"),
    ("jarvis_microsoft_redirect_uri", "http://attacker.invalid/callback"),
    ("jarvis_microsoft_redirect_uri", "http://localhost:8000/api/calendar/oauth/callback?next=evil"),
])
def test_invalid_config_fails_closed(auth, field, value):
    setattr(auth.config, field, value)
    with pytest.raises(CalendarError) as exc:
        auth.begin()
    assert exc.value.code == "CALENDAR_CONFIG_INVALID"


def test_real_msal_manages_pkce_state_and_rejects_mismatch():
    class OfflineDiscovery:
        def get(self, url, **kwargs):
            return httpx.Response(200, json={"authorization_endpoint": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
                "token_endpoint": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
                "issuer": "https://login.microsoftonline.com/common/v2.0"})

        def post(self, *args, **kwargs):
            raise AssertionError("Invalid state must not reach token exchange")

    real = msal.PublicClientApplication(CLIENT_ID, authority="https://login.microsoftonline.com/common",
                                       instance_discovery=False, http_client=OfflineDiscovery(), enable_broker_on_windows=False)
    flow = real.initiate_auth_code_flow(SCOPES, redirect_uri="http://localhost:8000/api/calendar/oauth/callback")
    another = real.initiate_auth_code_flow(SCOPES, redirect_uri="http://localhost:8000/api/calendar/oauth/callback")
    query = parse_qs(urlsplit(flow["auth_uri"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert flow["code_verifier"] and flow["state"] != another["state"]
    assert "client_secret" not in query
    assert "Calendars.Read" in query["scope"][0]
    assert not any(s in query["scope"][0] for s in ("ReadWrite", "User.Read", "Mail.", "Contacts."))
    with pytest.raises(ValueError):
        real.acquire_token_by_auth_code_flow(flow, {"state": "wrong", "code": "fake"})


@pytest.mark.parametrize("case", ["missing", "mismatch", "binding", "expired", "replay", "restart"])
def test_oauth_state_rejection(auth, case):
    now = [0]
    auth._clock = lambda: now[0]
    url, binding = auth.begin()
    response = {"state": parse_qs(urlsplit(url).query)["state"][0], "code": "fake"}
    if case == "missing": response.pop("state")
    if case == "mismatch": response["state"] = "wrong"
    if case == "binding": binding = "wrong"
    if case == "expired": now[0] = 601
    if case == "replay": auth.finish(response, binding)
    if case == "restart": auth._flow = None
    before = auth._app.exchange_calls
    with pytest.raises(CalendarError) as exc:
        auth.finish(response, binding)
    assert exc.value.code == "CALENDAR_AUTH_STATE_INVALID"
    assert auth._app.exchange_calls == before


def test_callback_error_and_token_exchange_exception_are_safe(auth, caplog):
    url, binding = auth.begin()
    state = parse_qs(urlsplit(url).query)["state"][0]
    with pytest.raises(CalendarError) as exc:
        auth.finish({"state": state, "error": "access_denied", "error_description": SENTINEL}, binding)
    assert SENTINEL not in str(exc.value)
    url, binding = auth.begin()
    auth._app.outcome = RuntimeError(SENTINEL)
    with pytest.raises(CalendarError) as exc:
        auth.finish({"state": parse_qs(urlsplit(url).query)["state"][0], "code": "fake"}, binding)
    assert SENTINEL not in str(exc.value) + caplog.text


def test_silent_refresh_and_auth_required(auth):
    with pytest.raises(CalendarError) as exc:
        auth.access_token()
    assert exc.value.code == "CALENDAR_NOT_CONNECTED"
    connect_auth(auth)
    assert auth.access_token() == SENTINEL
    assert auth.access_token(force_refresh=True) == SENTINEL
    assert auth._app.silent_calls[-1][1]["force_refresh"] is True
    assert auth._app.silent_calls[-1][0] == ["Calendars.Read"]
    auth._app.outcome = None
    with pytest.raises(CalendarError): auth.access_token()
    assert not auth.get_connection_status().connected


def test_cache_path_outside_repo(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="jarvis-cache-audit-") as outside:
        monkeypatch.setenv("LOCALAPPDATA", outside)
        assert not cache_path(CLIENT_ID).is_relative_to(PROJECT_ROOT)
    monkeypatch.setenv("LOCALAPPDATA", str(PROJECT_ROOT))
    with pytest.raises(CalendarError): cache_path(CLIENT_ID)


@pytest.mark.parametrize("kind", ["plaintext", "unavailable"])
def test_encrypted_persistence_required_no_plaintext_fallback(monkeypatch, kind):
    attempts = []
    def factory(*args):
        attempts.append(True)
        if kind == "unavailable": raise OSError(SENTINEL)
        return SimpleNamespace(is_encrypted=False)
    monkeypatch.setattr("app.integrations.calendar.credentials.FilePersistenceWithDataProtection", factory)
    with tempfile.TemporaryDirectory(prefix="jarvis-cache-denial-") as outside:
        monkeypatch.setenv("LOCALAPPDATA", outside)
        with pytest.raises(CalendarError) as exc: encrypted_cache(CLIENT_ID)
        assert exc.value.code == "CALENDAR_AUTH_STORAGE_UNAVAILABLE"
        assert SENTINEL not in str(exc.value)
        assert attempts == [True]
        assert not list(Path(outside).rglob("*.bin"))


def test_actual_windows_dpapi_roundtrip_only_synthetic_data(monkeypatch):
    import sys
    if sys.platform != "win32": pytest.skip("Windows DPAPI smoke")
    with tempfile.TemporaryDirectory(prefix="jarvis-dpapi-audit-") as outside:
        monkeypatch.setenv("LOCALAPPDATA", outside)
        cache = encrypted_cache(CLIENT_ID)
        assert cache.is_encrypted is True
        account = {"home_account_id": "fake-id", "environment": "fake", "realm": "fake", "username": SENTINEL}
        cache.modify(msal.TokenCache.CredentialType.ACCOUNT, account, account)
        assert SENTINEL.encode() not in cache_path(CLIENT_ID).read_bytes()
        reopened = encrypted_cache(CLIENT_ID)
        assert list(reopened.search(msal.TokenCache.CredentialType.ACCOUNT))[0]["username"] == SENTINEL


@pytest.mark.parametrize("show_as", ["free", "tentative", "busy", "oof", "workingElsewhere", "unknown"])
def test_safe_event_projection_and_show_as(auth, show_as):
    seen = []
    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"value": [event(showAs=show_as)]})
    output = provider(auth, handler).list_events(START, END).model_dump_json()
    assert SENTINEL not in output and "attendees" not in output and '"body"' not in output
    row = json.loads(output)["events"][0]
    assert row["start_at"] == "2026-09-08T10:00:00Z" and row["show_as"] == show_as
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/v1.0/me/calendar/calendarView"
    assert seen[0].headers["Prefer"] == 'outlook.timezone="UTC"'
    assert "body" not in seen[0].url.params["$select"]


def test_all_day_cancelled_and_recurrence_instances(auth):
    rows = [event(id="occurrence-1", type="occurrence"), event(id="exception-1", type="exception", isCancelled=True),
            event(id="all-day", isAllDay=True, start={"dateTime": "2026-09-08T00:00:00", "timeZone": "UTC"},
                  end={"dateTime": "2026-09-09T00:00:00", "timeZone": "UTC"})]
    result = provider(auth, lambda _: httpx.Response(200, json={"value": rows})).list_events(START, END)
    assert len(result.events) == 3
    assert result.events[0].is_all_day
    assert any(e.cancelled for e in result.events)
    assert not result.busy_projection_available


def test_offset_times_and_non_utc_naive_rejected(auth):
    rows = [event(start={"dateTime": "2026-09-08T18:00:00+08:00", "timeZone": "Other"})]
    p = provider(auth, lambda _: httpx.Response(200, json={"value": rows}))
    assert p.list_events(START, END).events[0].start_at.hour == 10
    rows[0]["start"] = {"dateTime": "2026-09-08T10:00:00", "timeZone": "Pacific Standard Time"}
    with pytest.raises(CalendarError): p.list_events(START, END)


def test_pagination_complete_and_empty(auth):
    seen = []
    def handler(req):
        seen.append(req)
        if len(seen) == 1:
            return httpx.Response(200, json={"value": [event()], "@odata.nextLink":
                "https://graph.microsoft.com/v1.0/me/calendar/calendarView?$skiptoken=next"})
        return httpx.Response(200, json={"value": [event(id="event-2")]})
    p = provider(auth, handler)
    assert len(p.list_events(START, END).events) == 2
    assert seen[1].url.params["$skiptoken"] == "next"
    assert all(r.method == "GET" for r in seen)
    p._transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"value": []}))
    assert p.list_events(START, END).events == []


@pytest.mark.parametrize("link", ["https://attacker.invalid/steal", "http://graph.microsoft.com/v1.0/me/calendar/calendarView",
    "https://graph.microsoft.com/v1.0/me/events", "https://graph.microsoft.com/v1.0/me/calendar/calendarView"])
def test_unsafe_or_cyclic_nextlink_fails_closed(auth, link):
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"value": [], "@odata.nextLink": link})
    with pytest.raises(CalendarError): provider(auth, handler).list_events(START, END)
    assert len(calls) == 1


@pytest.mark.parametrize(("start", "end"), [(None, END), (START, None), ("bad", END),
    ("2026-09-08T00:00:00", END), (END, START), (START, START), (START, "2027-01-01T00:00:00Z")])
def test_invalid_ranges(start, end):
    with pytest.raises(CalendarError) as exc: validate_range(start, end)
    assert exc.value.code == "CALENDAR_INVALID_RANGE"


@pytest.mark.parametrize(("status", "code", "count"), [(403, "CALENDAR_PERMISSION_DENIED", 1),
    (429, "CALENDAR_RATE_LIMITED", 3), (500, "CALENDAR_UPSTREAM_ERROR", 3),
    (401, "CALENDAR_AUTH_FAILED", 2), (302, "CALENDAR_UPSTREAM_ERROR", 1)])
def test_safe_http_errors_bounded_retry(auth, status, code, count):
    requests, sleeps = [], []
    def handler(req):
        requests.append(req)
        return httpx.Response(status, json={"error": SENTINEL}, headers={"Retry-After": "1", "Location": "https://attacker.invalid"})
    with pytest.raises(CalendarError) as exc: provider(auth, handler, sleeps).list_events(START, END)
    assert exc.value.code == code and SENTINEL not in str(exc.value)
    assert len(requests) == count and all(r.method == "GET" for r in requests)
    if status in (429, 500): assert sleeps == [1, 1]
    if status == 401: assert not auth.get_connection_status().connected


def test_429_long_retry_after_returns_without_early_retry(auth):
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(429, headers={"Retry-After": "120"})
    with pytest.raises(CalendarError): provider(auth, handler).list_events(START, END)
    assert len(calls) == 1


@pytest.mark.parametrize("body", [{}, {"value": {}}, {"value": [{"id": "broken"}]}, {"value": [event(showAs="nonsense")]}])
def test_malformed_graph_response_fails_closed(auth, body):
    with pytest.raises(CalendarError) as exc:
        provider(auth, lambda _: httpx.Response(200, json=body)).list_events(START, END)
    assert exc.value.code == "CALENDAR_UPSTREAM_ERROR"


def test_api_safe_status_metadata_events_and_no_db_side_effects(client, auth, worker_env):
    from app.models.execution import Execution
    from app.models.task import Task
    from app.models.reminder import Reminder
    def handler(req):
        if req.url.path.endswith("calendar"):
            return httpx.Response(200, json={"id": "calendar-1", "name": "Calendar", "owner": SENTINEL})
        return httpx.Response(200, json={"value": [event()]})
    p = provider(auth, handler)
    app.dependency_overrides[get_calendar_provider] = lambda: p
    for path in ("/connection", "/default", f"/events?start={START}&end={END}"):
        response = client.get("/api/calendar" + path)
        assert response.status_code == 200
        assert SENTINEL not in response.text
    response = client.get("/api/calendar/events")
    assert response.status_code == 422 and response.json()["error"]["correlation_id"]
    with worker_env[1]() as db:
        assert all(db.query(model).count() == 0 for model in (Execution, Task, Reminder))


def test_api_oauth_callback_cookie_and_redirect_secrecy(client, auth):
    app.dependency_overrides[get_calendar_auth] = lambda: auth
    app.dependency_overrides[get_calendar_provider] = lambda: MicrosoftGraphCalendarProvider(auth)
    started = client.post("/api/calendar/connect", headers=ORIGIN, follow_redirects=False)
    assert started.status_code == 303
    assert "HttpOnly" in started.headers["set-cookie"] and "SameSite=lax" in started.headers["set-cookie"]
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    result = client.get("/api/calendar/oauth/callback", params={"state": state, "code": SENTINEL}, follow_redirects=False)
    assert result.status_code == 303 and "status=connected" in result.headers["location"]
    assert SENTINEL not in result.text + str(result.headers)
    replay = client.get("/api/calendar/oauth/callback", params={"state": state, "code": SENTINEL}, follow_redirects=False)
    assert "CALENDAR_AUTH_STATE_INVALID" in replay.headers["location"]
    status = client.get("/api/calendar/connection")
    assert status.json()["connected"] and SENTINEL not in status.text
    disconnected = client.post("/api/calendar/disconnect", headers={**ORIGIN, "X-Jarvis-Calendar": "1"})
    assert disconnected.status_code == 200 and not disconnected.json()["connected"]


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://attacker.invalid"}, {**ORIGIN, "Host": "attacker.invalid"}])
def test_local_auth_mutation_rejects_untrusted_browser(client, headers):
    result = client.post("/api/calendar/connect", headers=headers, follow_redirects=False)
    assert result.status_code == 403


def test_callback_query_removed_from_access_log_scope():
    import asyncio
    scope = {"path": "/api/calendar/oauth/callback", "query_string": b"code=private&state=private"}
    async def next_app(scope, receive, send):
        assert scope["query_string"] == b""
        assert scope["calendar_callback_query"]
    asyncio.run(CalendarCallbackPrivacy(next_app)(scope, None, None))
    assert scope["query_string"] == b""


def test_library_diagnostics_cannot_log_tokens(caplog):
    secure_library_logging()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("msal.application").error(SENTINEL)
        logging.getLogger("msal.token_cache").debug(SENTINEL)
        logging.getLogger("httpx").info(SENTINEL)
    assert SENTINEL not in caplog.text


def test_offline_disconnect_clears_synthetic_access_and_refresh_tokens(auth):
    connect_auth(auth)
    for kind in (msal.TokenCache.CredentialType.ACCESS_TOKEN, msal.TokenCache.CredentialType.REFRESH_TOKEN):
        entry = {"home_account_id": "account-id", "environment": "login.microsoftonline.com", "client_id": CLIENT_ID,
                 "secret": SENTINEL, "target": "Calendars.Read", "credential_type": kind,
                 "expires_on": "4102444800", "realm": "tenant", "token_type": "Bearer"}
        auth._cache.modify(kind, entry, entry)
    def no_network(*args, **kwargs): raise AssertionError("Disconnect must work offline")
    auth._app_factory = no_network
    auth._app = None
    auth.disconnect()
    assert not auth.get_connection_status().connected
    assert SENTINEL not in auth._cache.serialize()


def test_partial_pagination_failure_does_not_return_partial_events(auth, monkeypatch):
    monkeypatch.setattr("app.integrations.calendar.microsoft.MAX_PAGES", 2)
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"value": [event(id=str(len(calls)))], "@odata.nextLink":
            f"https://graph.microsoft.com/v1.0/me/calendar/calendarView?$skiptoken={len(calls)}"})
    with pytest.raises(CalendarError) as exc: provider(auth, handler).list_events(START, END)
    assert exc.value.code == "CALENDAR_UPSTREAM_ERROR" and len(calls) == 2


def test_retry_then_success_and_401_refresh_then_success(auth):
    statuses = iter([429, 401, 200])
    sleeps = []
    def handler(req):
        status = next(statuses)
        return httpx.Response(status, json={"value": []}, headers={"Retry-After": "2"})
    result = provider(auth, handler, sleeps).list_events(START, END)
    assert result.complete and result.events == [] and sleeps == [2]
    assert any(kwargs["force_refresh"] for _, kwargs in auth._app.silent_calls)


def test_transport_timeout_bounded_and_oversized_response(auth, monkeypatch):
    calls = []
    def timeout(req):
        calls.append(req)
        raise httpx.ReadTimeout(SENTINEL)
    p = provider(auth, timeout)
    with pytest.raises(CalendarError) as exc: p.list_events(START, END)
    assert SENTINEL not in str(exc.value) and len(calls) == 3
    monkeypatch.setattr("app.integrations.calendar.microsoft.MAX_RESPONSE_BYTES", 10)
    p._transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 11))
    with pytest.raises(CalendarError): p.list_events(START, END)


def test_no_credentials_in_protected_files_or_frontend():
    for relative in (".env", ".env.example", "backend/jarvis.db"):
        assert SENTINEL.encode() not in (PROJECT_ROOT / relative).read_bytes()
    for path in (PROJECT_ROOT / "frontend" / "src").rglob("*"):
        if path.is_file():
            content = path.read_text(encoding="utf-8")
            assert SENTINEL not in content
            assert "refresh_token" not in content and "access_token" not in content


def test_connection_required_before_graph_transport(auth):
    def no_request(req): raise AssertionError("Unconnected provider must not call Graph")
    p = MicrosoftGraphCalendarProvider(auth, transport=httpx.MockTransport(no_request))
    with pytest.raises(CalendarError) as exc: p.list_events(START, END)
    assert exc.value.code == "CALENDAR_NOT_CONNECTED"


def test_testserver_hostname_not_allowed_for_real_socket_peer():
    from fastapi.testclient import TestClient
    with TestClient(app, client=("127.0.0.1", 50000)) as local:
        result = local.get("/api/calendar/connection", headers={"Host": "testserver"})
    assert result.status_code == 403


def test_connect_form_preserves_origin_and_disables_opener():
    source = (PROJECT_ROOT / "frontend/src/components/calendar/CalendarPanel.tsx").read_text(encoding="utf-8")
    form = source.split('<form action={calendarConnectUrl}', 1)[1].split('>', 1)[0]
    assert 'rel="noopener"' in form and 'noreferrer' not in form
