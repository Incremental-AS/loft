"""A byte cap on every request body, counted as the bytes arrive.

Starlette reads a body whole before any handler can look at it, and a chunked body has
no Content-Length to check first, so without this anyone could make the gateway hold
hundreds of MiB per request (review of 51991cc, B1). A declared size over the cap
is answered at once; a chunked body is cut off at the first byte over it. Either way the
answer is 413 with the same error code the route itself would give.
"""
from __future__ import annotations

import json


class _TooLarge(Exception):
    pass


async def _refuse(send, code: str):
    body = json.dumps({"error": code}).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                            (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": body})


class BodyLimit:
    """cap_for(method, path) -> (max bytes, error code)."""

    def __init__(self, app, cap_for):
        self.app, self.cap_for = app, cap_for

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        cap, code = self.cap_for(scope["method"], scope["path"])
        declared = [v for k, v in scope["headers"] if k == b"content-length"]
        if declared and (not declared[-1].isdigit() or int(declared[-1]) > cap):
            return await _refuse(send, code)
        seen, over, started = 0, False, False

        async def counted():
            nonlocal seen, over
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > cap:
                    over = True
                    raise _TooLarge()
            return msg

        async def guarded(msg):
            nonlocal started
            if over:        # whatever the app made of the cut-off read, the answer is 413
                return
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        try:
            await self.app(scope, counted, guarded)
        except Exception:
            if not over:
                raise
        if over and not started:
            await _refuse(send, code)
