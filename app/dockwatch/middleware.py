"""ASGI middleware stack extracted from the original Dockwatch entrypoint.

These are pure-ASGI middlewares (no ``BaseHTTPMiddleware``) so every response
— including CORS preflights, 504s and error pages — carries the same
correlation ID and hardening headers.

Imported by the unified Trove app entrypoint (``app.main``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from starlette.datastructures import MutableHeaders

from app.dockwatch.config import get_settings
from app.dockwatch.logging_config import request_id_var

logger = logging.getLogger(__name__)

#: Content-Security-Policy tuned for the bundled SPA: same-origin default,
#: inline scripts/styles (theme bootstrap + generated gauges), Google Fonts.
#: Override via ``DOCKWATCH_CSP_OVERRIDE``; set to "" to disable CSP entirely.
DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)


class RequestIDMiddleware:
    """Assign a per-request correlation ID and expose it via header/logs.

    The ID is stored on ``scope["state"]`` (backing ``request.state``) and in a
    contextvar for log correlation.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_var.set(request_id)

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start" and b"x-request-id" not in {
                k.lower() for k, _ in message.get("headers", [])
            }:
                headers = MutableHeaders(scope=message)
                headers.append("X-Request-ID", request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            request_id_var.reset(token)


class SecurityHeadersMiddleware:
    """Harden responses with CSP, frame/embedding and MIME-sniffing headers.

    ``csp=None`` falls back to :data:`DEFAULT_CSP`; an empty string disables
    the CSP header entirely (the other headers are still applied). Pure-ASGI.
    """

    def __init__(self, app: Any, csp: str | None = DEFAULT_CSP) -> None:
        self.app = app
        self._csp = DEFAULT_CSP if csp is None else csp

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
                headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
                if self._csp:
                    headers.setdefault("Content-Security-Policy", self._csp)
            await send(message)

        await self.app(scope, receive, send_wrapper)


class AccessLogMiddleware:
    """Log method/path/status/duration for every request with a soft timeout.

    Requests exceeding ``settings.request_timeout`` receive a 504 response;
    the in-flight handler is given ``settings.shutdown_grace_seconds`` more to
    complete before it is cancelled and abandoned. Pure-ASGI.
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        settings = get_settings()
        self._timeout = settings.request_timeout
        self._grace = settings.shutdown_grace_seconds

    async def _request_id_from_scope(self, scope: dict[str, Any]) -> str | None:
        state = scope.get("state")
        if isinstance(state, dict):
            request_id = state.get("request_id")
            if isinstance(request_id, str):
                return request_id
        return None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        request_id = await self._request_id_from_scope(scope)
        method = str(scope.get("method", ""))
        path = str(scope.get("path", ""))
        status_holder: dict[str, int] = {"status": 500}

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message.get("status") or 500)
            await send(message)

        async def run_app() -> None:
            await self.app(scope, receive, send_wrapper)

        task = asyncio.ensure_future(run_app())
        try:
            done, _ = await asyncio.wait({task}, timeout=self._timeout)
            if task in done:
                task.result()  # propagate (re-raise) unhandled handler errors
        except Exception:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.exception(
                "http_request method=%s path=%s duration_ms=%.2f request_id=%s",
                method,
                path,
                duration_ms,
                request_id,
            )
            if not task.done():
                task.cancel()
            raise
        else:
            duration_ms = (time.perf_counter() - start) * 1000
            if task in done:
                logger.info(
                    "http_request method=%s path=%s status=%d duration_ms=%.2f request_id=%s",
                    method,
                    path,
                    status_holder["status"],
                    duration_ms,
                    request_id,
                )
                return

            logger.warning(
                "http_timeout method=%s path=%s duration_ms=%.2f request_id=%s",
                method,
                path,
                duration_ms,
                request_id,
            )
            # Grace window: let a write already in progress land before we abort.
            if not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=self._grace)
                except TimeoutError:
                    task.cancel()
            if task.done():
                return
            # Handler never responded; emit 504 and abandon the task.
            if status_holder["status"] == 500:
                body = json.dumps(
                    {"detail": "Request timed out", "request_id": request_id}
                ).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 504,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": body})
            if not task.done():
                task.cancel()
