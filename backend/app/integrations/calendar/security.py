"""Prevent library debug output and callback codes from entering logs."""
import logging


class _NoAuthDiagnostics(logging.Filter):
    def filter(self, record):
        return False


def secure_library_logging():
    # Third-party auth diagnostics can include upstream error descriptions.
    # Suppress at the source, including child loggers, even under DEBUG logging.
    names = ["msal", "msal_extensions", "httpx", "httpcore", "urllib3",
             "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy"]
    names += [name for name in logging.Logger.manager.loggerDict
              if name.startswith(("msal.", "msal_extensions.", "httpcore.", "urllib3."))]
    for name in names:
        logger = logging.getLogger(name)
        logger.setLevel(logging.CRITICAL)
        if not any(isinstance(f, _NoAuthDiagnostics) for f in logger.filters):
            logger.addFilter(_NoAuthDiagnostics())


class CalendarCallbackPrivacy:
    """Uvicorn uses scope.query_string for access logs: erase it before routing.

    Only this callback reads the private query copy, without logging it. The
    browser is immediately redirected to a fixed result page after processing.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("path") == "/api/calendar/oauth/callback":
            scope["calendar_callback_query"] = scope.get("query_string", b"")
            scope["query_string"] = b""
        async def private_send(message):
            if scope.get("path", "").startswith("/api/calendar") and message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                message["headers"] = headers + [(b"cache-control", b"no-store")]
            await send(message)
        await self.app(scope, receive, private_send)
