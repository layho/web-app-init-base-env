"""Bound API request bodies before FastAPI parses JSON."""
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from .feedback_attachments import MAX_FILES, MAX_TOTAL

SCREENSHOT_ROUTES = {"/api/v1/feedback", "/api/v1/releases/reject"}
SCREENSHOT_LIMIT = 4 * ((MAX_TOTAL + 2) // 3) + 4 * MAX_FILES + 64 * 1024
JSON_LIMIT = 64 * 1024
MAX_SCREENSHOT_REQUESTS = 2
TOO_LARGE = "Request body too large"


class RequestBodyLimits:
    def __init__(self, app):
        self.app = app
        self.active_uploads = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        root = scope.get("root_path", "")
        if root and path.startswith(root + "/"):
            path = path[len(root):]
        path = path.rstrip("/")
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        upload = path in SCREENSHOT_ROUTES
        limit = SCREENSHOT_LIMIT if upload else JSON_LIMIT
        ceiling = str(limit).encode("ascii")
        for key, value in scope["headers"]:
            if key == b"content-length":
                length = value.strip().lstrip(b"0") or b"0"
                if length.isdigit() and (len(length), length) > (len(ceiling), ceiling):
                    await JSONResponse({"detail": TOO_LARGE}, status_code=413)(scope, receive, send)
                    return

        if upload and self.active_uploads >= MAX_SCREENSHOT_REQUESTS:
            await JSONResponse({"detail": "Too many concurrent uploads"}, status_code=413)(scope, receive, send)
            return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(413, TOO_LARGE)
            return message

        # No await between checking and reserving a slot in the worker's event loop.
        if upload:
            self.active_uploads += 1
        try:
            await self.app(scope, limited_receive, send)
        finally:
            if upload:
                self.active_uploads -= 1
