"""Public API Gateway hot path: ``/gw/<slug>/<path>``.

This router deliberately declares no auth dependency: enforcement is per-route
(``open`` routes proxy freely; ``api_key`` routes require a valid key). It is
registered *before* the static SPA mount so ``/gw/...`` never falls through to
the frontend.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import models
from ..deps import DB
from ..services import gateway_service
from ..timeutil import utcnow

router = APIRouter(tags=["gateway-proxy"], include_in_schema=False)

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "0.0.0.0"


@router.api_route("/gw/{slug}/{path:path}", methods=_METHODS)
async def gateway_proxy(slug: str, path: str, request: Request, db: DB):
    start = time.monotonic()
    gateway = await gateway_service.get_gateway_by_slug(db, slug)
    if gateway is None or not gateway.enabled:
        return JSONResponse({"error": "gateway not found or disabled"}, status_code=404)

    method = request.method
    if method == "OPTIONS":
        return await _handle_preflight(slug, path, request, db)

    routes = await gateway_service.load_routes(db, gateway.id)
    route, remainder = gateway_service.match_route(routes, method, path)
    if route is None:
        return JSONResponse(
            {"error": "no matching route", "method": method, "path": path},
            status_code=404,
        )

    consumer_id: int | None = None
    if route.auth_mode == "api_key":
        key = gateway_service.extract_api_key(request)
        entry = await gateway_service.lookup_key(db, key) if key else None
        if entry is None:
            return JSONResponse(
                {"error": "missing or invalid API key"},
                status_code=401,
                headers={"WWW-Authenticate": "ApiKey"},
            )
        if not entry.enabled:
            return JSONResponse({"error": "API key disabled"}, status_code=403)
        if entry.expires_at is not None and entry.expires_at <= utcnow():
            return JSONResponse({"error": "API key expired"}, status_code=403)
        consumer_id = entry.consumer_id
        await _touch_key(db, entry.key_id)

    if route.rate_limit_rpm > 0:
        ident = str(consumer_id) if consumer_id is not None else f"ip:{_client_ip(request)}"
        if not await gateway_service.limiter.check((route.id, ident), route.rate_limit_rpm):
            await gateway_service.record_log(
                db,
                gateway.id,
                route.id,
                consumer_id,
                method,
                f"/gw/{slug}/{path}",
                429,
                int((time.monotonic() - start) * 1000),
                _client_ip(request),
            )
            return JSONResponse(
                {"error": "rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": "60"},
            )

    upstream_url = gateway_service.build_upstream_url(route, path, remainder, request.url.query)
    response = await gateway_service.relay_to_upstream(request, upstream_url, route.timeout_ms)
    status_code = response.status_code if hasattr(response, "status_code") else 502
    await gateway_service.record_log(
        db,
        gateway.id,
        route.id,
        consumer_id,
        method,
        f"/gw/{slug}/{path}",
        status_code,
        int((time.monotonic() - start) * 1000),
        _client_ip(request),
    )
    return response


async def _handle_preflight(slug: str, path: str, request: Request, db: DB):
    """Best-effort OPTIONS passthrough: resolve the route and let upstream answer."""
    gateway = await gateway_service.get_gateway_by_slug(db, slug)
    if gateway is None or not gateway.enabled:
        return JSONResponse({"error": "gateway not found or disabled"}, status_code=404)
    routes = await gateway_service.load_routes(db, gateway.id)
    route, remainder = gateway_service.match_route(routes, "GET", path)
    if route is None:
        return JSONResponse(
            {"error": "no matching route", "method": "OPTIONS", "path": path},
            status_code=404,
        )
    upstream_url = gateway_service.build_upstream_url(route, path, remainder, request.url.query)
    return await gateway_service.relay_to_upstream(request, upstream_url, route.timeout_ms)


async def _touch_key(db: DB, key_id: int) -> None:
    key = await db.get(models.GatewayApiKey, key_id)
    if key is not None:
        key.last_used_at = utcnow()
        await db.commit()
