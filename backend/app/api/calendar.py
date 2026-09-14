"""Loopback-only calendar read API. OAuth never returns credentials to React."""
from html import escape
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.core.config import settings
from app.integrations.calendar.auth import FLOW_TTL_SECONDS
from app.integrations.calendar.errors import CalendarError, ERRORS
from app.integrations.calendar.factory import get_calendar_auth, get_calendar_provider
from app.schemas.calendar import CalendarEventRange, CalendarMetadata, ConnectionStatus, validate_range

COOKIE = "jarvis_calendar_flow"
COOKIE_PATH = "/api/calendar/oauth"


def require_local(request: Request):
    try:
        host = urlsplit("http://" + request.headers.get("host", "")).hostname
        peer = request.client.host if request.client else None
        test_request = host == "testserver" and peer == "testclient"
        if host not in ("localhost", "127.0.0.1", "::1") and not test_request:
            raise ValueError()
        # testserver is the in-process TestClient host; real socket peers still
        # must be loopback. Do not accept Forwarded/X-Forwarded-Host as authority.
        if peer not in ("localhost", "127.0.0.1", "::1", "testclient"):
            raise ValueError()
        origin = request.headers.get("origin")
        if origin:
            parsed = urlsplit(origin)
            if (origin not in settings.cors_origin_list
                    or parsed.hostname not in ("localhost", "127.0.0.1", "::1")):
                raise ValueError()
        if request.method == "POST" and not origin:
            raise ValueError()
    except ValueError:
        raise CalendarError("CALENDAR_LOCAL_ONLY") from None


router = APIRouter(prefix="/api/calendar", tags=["calendar"], dependencies=[Depends(require_local)])


@router.get("/connection", response_model=ConnectionStatus)
def connection(provider=Depends(get_calendar_provider)):
    return provider.get_connection_status()


@router.post("/connect")
def connect(auth=Depends(get_calendar_auth)):
    # Top-level browser form navigation avoids exposing flow data to React or
    # introducing credentialed cross-origin fetch into the existing app.
    url, binding = auth.begin()
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(COOKIE, binding, max_age=FLOW_TTL_SECONDS, httponly=True,
                        samesite="lax", path=COOKIE_PATH)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.post("/authorize-write")
def authorize_write(auth=Depends(get_calendar_auth)):
    """Backend-fixed incremental consent; the frontend cannot supply scopes."""
    url, binding = auth.begin_write()
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(COOKIE, binding, max_age=FLOW_TTL_SECONDS, httponly=True,
                        samesite="lax", path=COOKIE_PATH)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/oauth/callback")
def callback(request: Request, auth=Depends(get_calendar_auth)):
    code = "connected"
    try:
        raw = request.scope.get("calendar_callback_query", b"")
        if len(raw) > 16384:
            raise CalendarError("CALENDAR_AUTH_STATE_INVALID")
        pairs = parse_qsl(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=20)
        params = dict(pairs)
        if len(params) != len(pairs):
            raise CalendarError("CALENDAR_AUTH_STATE_INVALID")
        auth.finish(params, request.cookies.get(COOKIE))
    except CalendarError as exc:
        code = exc.code
    except Exception:
        code = "CALENDAR_AUTH_FAILED"
    query = urlencode({"status": code, "request_id": getattr(request.state, "correlation_id", "")})
    response = RedirectResponse("/api/calendar/oauth/result?" + query, status_code=303)
    response.delete_cookie(COOKIE, path=COOKIE_PATH)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/oauth/result", response_class=HTMLResponse)
def result_page(request: Request):
    code = request.query_params.get("status", "")
    message = "Microsoft Outlook 已连接。请返回 JARVIS 日历页，点击刷新。" if code == "connected" else "Microsoft 授权未完成。请返回 JARVIS 重试。"
    safe_code = code if code in ERRORS else ""
    cid = request.query_params.get("request_id", "")
    cid = cid if len(cid) <= 64 and cid.isalnum() else ""
    return HTMLResponse(
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>JARVIS Calendar</title>"
        f"<h1>JARVIS Calendar</h1><p>{message}</p><p>{escape(safe_code)}</p><p>{escape(cid)}</p></html>",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                 "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"})


@router.post("/disconnect", response_model=ConnectionStatus)
def disconnect(request: Request, auth=Depends(get_calendar_auth)):
    if request.headers.get("x-jarvis-calendar") != "1":
        raise CalendarError("CALENDAR_LOCAL_ONLY")
    auth.disconnect()
    return auth.get_connection_status()


@router.get("/default", response_model=CalendarMetadata)
def default_calendar(provider=Depends(get_calendar_provider)):
    return provider.get_default_calendar()


@router.get("/events", response_model=CalendarEventRange)
def events(request: Request, provider=Depends(get_calendar_provider)):
    start, end = validate_range(request.query_params.get("start"), request.query_params.get("end"))
    return provider.list_events(start, end)


async def calendar_error_handler(request: Request, exc: CalendarError):
    return JSONResponse(status_code=exc.status, content={"error": {
        "code": exc.code, "message": exc.message,
        "correlation_id": getattr(request.state, "correlation_id", None),
    }}, headers={"Cache-Control": "no-store"})
