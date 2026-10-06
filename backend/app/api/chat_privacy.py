"""Phase 10C: narrow privacy mechanism for the ordinary Chat messages resource.

Scope is exactly GET / POST /api/conversations/{conversation_id}/messages:
- every response (success or error) carries Cache-Control: no-store;
- request-validation failures use one fixed sanitized 422 body that never echoes
  rejected values, client-supplied field names, raw validation objects or body content.

No other resource's caching, validation behavior or error formatting is changed.
"""

import re

from fastapi.responses import JSONResponse

_MESSAGES_RESOURCE = re.compile(r"/api/conversations/[^/]+/messages")
NO_STORE = "no-store"
VALIDATION_MESSAGE = "Invalid chat message request"


def is_messages_resource(path: str) -> bool:
    return _MESSAGES_RESOURCE.fullmatch(path) is not None


def sanitized_validation_response() -> JSONResponse:
    """Fixed 422 in the existing error envelope; identifies the request only generically."""
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "VALIDATION_ERROR", "message": VALIDATION_MESSAGE}},
        headers={"Cache-Control": NO_STORE},
    )


class ChatMessagesPrivacy:
    """Sets Cache-Control: no-store on every response from the messages resource."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not is_messages_resource(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        async def no_store_send(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                message["headers"] = headers + [(b"cache-control", NO_STORE.encode("latin-1"))]
            await send(message)

        await self.app(scope, receive, no_store_send)
