"""API Gateway subsystem: keys, routes, rate limits, TLS, and proxying.

Exposes the building blocks used by the management routes (``routes_gateway``)
and the public catch-all proxy (``gateway_proxy``).

Security contract enforced here:

* Generated API keys are high-entropy and prefixed ``cvk_``; user-supplied keys
  must be at least 8 characters (schema-validated before reaching the service).
* Only the SHA-256 hash of each key is persisted; the plaintext is returned to
  the operator exactly once at creation/rotation time.
* ``api_key`` routes reject requests without a valid, enabled, non-expired key.
* Rate limits are per (route, consumer-or-client) sliding windows in memory.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import models, schemas
from ..deps import DB  # noqa: F401  (re-exported type alias for callers)
from ..timeutil import utcnow
from . import cert_service, providers

logger = logging.getLogger(__name__)

API_KEY_PREFIX = "cvk_"
MIN_KEY_LENGTH = 8
_MAX_BODY_RELAY = 64 * 1024 * 1024  # per-request body relay cap transferred
_MAX_TEST_BODY = 1_000_000  # chars returned by the direct test helper

#: Hop-by-hop / framework-controlled headers never forwarded upstream.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def generate_api_key() -> str:
    """Return a fresh, high-entropy API key (long, url-safe, prefixed)."""
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(24)}"


def hash_api_key(key: str) -> str:
    """SHA-256 hex digest (the only form persisted)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def key_prefix(key: str) -> str:
    """Short, non-secret display prefix, e.g. ``cvk_Ab3xK9qLmN``."""
    return key[:12]


@dataclass
class KeyEntry:
    key_id: int
    consumer_id: int
    enabled: bool
    expires_at: Any | None


#: small in-memory key lookup cache (invalidated on key mutations)
_key_cache: dict[str, KeyEntry] = {}


def invalidate_key(key_hash: str) -> None:
    _key_cache.pop(key_hash, None)


async def lookup_key(db: AsyncSession, key: str) -> KeyEntry | None:
    """Resolve a plaintext key to its record (validated at call sites)."""
    if key is None or len(key) < MIN_KEY_LENGTH:
        return None
    kh = hash_api_key(key)
    cached = _key_cache.get(kh)
    if cached is not None:
        return cached
    row = (
        await db.execute(select(models.GatewayApiKey).where(models.GatewayApiKey.key_hash == kh))
    ).scalar_one_or_none()
    if row is None:
        return None
    entry = KeyEntry(
        key_id=row.id,
        consumer_id=row.consumer_id,
        enabled=row.enabled,
        expires_at=row.expires_at,
    )
    _key_cache[kh] = entry
    return entry


def extract_api_key(request: Request) -> str | None:
    """Pull the presented API key from ``X-API-Key`` or ``Authorization: Bearer``."""
    header = (request.headers.get("x-api-key") or "").strip()
    if header:
        return header
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


# ---------------------------------------------------------------------------
# Rate limiting (in-memory sliding windows, per route x consumer/client)
# ---------------------------------------------------------------------------


class GatewayRateLimiter:
    """Async sliding-window limiter. Windows reset when the process restarts."""

    def __init__(self) -> None:
        self._windows: dict[tuple[int, str], deque[float]] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: tuple[int, str], rpm: int) -> bool:
        """Return True when the request is allowed (recording it)."""
        if rpm <= 0:
            return True
        now = time.monotonic()
        async with self._lock:
            windows = self._windows.setdefault(key, deque())
            while windows and now - windows[0] > 60.0:
                windows.popleft()
            if len(windows) >= rpm:
                return False
            windows.append(now)
            return True


limiter = GatewayRateLimiter()


# ---------------------------------------------------------------------------
# Route resolution
# ---------------------------------------------------------------------------


def match_route(
    routes: list[models.GatewayRoute], method: str, path: str
) -> tuple[models.GatewayRoute | None, str]:
    """Match a request against a gateway's routes.

    Returns ``(route, remainder)``. ``remainder`` is the sub-path left after
    the route's wildcard prefix (used when ``strip_prefix`` is enabled).
    Exact routes match with leading/trailing slash normalization; routes whose
    path ends with ``*`` match any sub-path under the prefix.
    """
    norm = path if path.startswith("/") else f"/{path}"
    norm = norm or "/"
    for route in routes:
        if not route.enabled:
            continue
        if method not in (route.methods or []):
            continue
        rp = route.path or "/"
        if rp.endswith("*"):
            prefix = rp[:-1].rstrip("/")
            if prefix == "":
                return route, norm.lstrip("/")
            if norm == prefix or norm.startswith(f"{prefix}/"):
                remainder = norm[len(prefix) :].lstrip("/") if norm != prefix else ""
                return route, remainder
        elif norm.rstrip("/") == rp.rstrip("/"):
            return route, ""
    return None, ""


async def load_routes(db: AsyncSession, gateway_id: int) -> list[models.GatewayRoute]:
    rows = await db.scalars(
        select(models.GatewayRoute)
        .where(models.GatewayRoute.gateway_id == gateway_id)
        .order_by(models.GatewayRoute.id)
    )
    return list(rows)


async def get_gateway_by_slug(db: AsyncSession, slug: str) -> models.Gateway | None:
    return (
        await db.execute(select(models.Gateway).where(models.Gateway.slug == slug))
    ).scalar_one_or_none()


def build_upstream_url(route: models.GatewayRoute, path: str, remainder: str, query: str) -> str:
    """Join the upstream base, sub-path, (optional) stripped prefix, and query."""
    base = (route.upstream_url or "").rstrip("/")
    if route.strip_prefix:
        append = remainder
    else:
        append = path if path.startswith("/") else f"/{path}"
    if append:
        base = f"{base}/{append.lstrip('/')}"
    if query:
        base = f"{base}?{query}"
    return base


# ---------------------------------------------------------------------------
# Request logging
# ---------------------------------------------------------------------------


async def record_log(
    db: AsyncSession,
    gateway_id: int | None,
    route_id: int | None,
    consumer_id: int | None,
    method: str,
    path: str,
    status: int,
    latency_ms: int,
    client: str,
) -> None:
    db.add(
        models.GatewayLog(
            gateway_id=gateway_id,
            route_id=route_id,
            consumer_id=consumer_id,
            method=method,
            path=path,
            status=status,
            latency_ms=latency_ms,
            client=client,
            created_at=utcnow(),
        )
    )
    try:
        await db.commit()
    except Exception:  # pragma: no cover - logging must never break the proxy
        logger.exception("gateway log insert failed")
        await db.rollback()


# ---------------------------------------------------------------------------
# Streaming relay (the hot path)
# ---------------------------------------------------------------------------


async def _drain_body(request: Request) -> bytes:
    """Read the inbound request body once, capping it at the relay limit.

    We forward bodies as bytes (rather than re-streaming) so httpx can set a
    correct ``Content-Length`` — hop-by-hop headers are stripped outbound, and
    many upstreams (e.g. plain HTTP servers) cannot read a body without one.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_BODY_RELAY:
            raise ValueError("request body exceeds relay limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def relay_to_upstream(
    request: Request, upstream_url: str, timeout_ms: int
) -> JSONResponse | StreamingResponse:
    """Forward the inbound request to ``upstream_url`` and stream the response.

    Returns a ``502`` when the upstream is unreachable and a ``504`` on
    timeout; otherwise a streaming passthrough that relays status + headers.
    """
    timeout_s = max(0.1, timeout_ms / 1000.0)
    connect_s = min(10.0, timeout_s / 2)

    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and not k.lower().startswith("x-dockwatch")
    }
    # Forward client identity / correlation id upward.
    if request.client is not None:
        headers.setdefault("x-forwarded-for", request.client.host)
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        headers.setdefault("x-request-id", str(request_id))
    headers.pop("x-dockwatch-token", None)

    transport = httpx.AsyncHTTPTransport(retries=0)
    client = httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        timeout=httpx.Timeout(timeout_s, connect=connect_s),
    )
    try:
        body = await _drain_body(request)
    except ValueError as exc:
        await client.aclose()
        return JSONResponse(
            {"error": "request body too large", "detail": str(exc)}, status_code=413
        )
    built = client.build_request(request.method, upstream_url, headers=headers, content=body)
    try:
        upstream = await client.send(built, stream=True)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        await client.aclose()
        return JSONResponse({"error": "upstream unreachable", "detail": str(exc)}, status_code=502)
    except httpx.RemoteProtocolError as exc:
        await client.aclose()
        return JSONResponse(
            {"error": "upstream protocol error", "detail": str(exc)}, status_code=502
        )
    except httpx.TimeoutException as exc:
        await client.aclose()
        return JSONResponse({"error": "upstream timeout", "detail": str(exc)}, status_code=504)

    resp_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "content-length"
    }

    async def body_stream():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body_stream(),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=None,
    )


# ---------------------------------------------------------------------------
# Direct upstream tester (used by the API console)
# ---------------------------------------------------------------------------


async def direct_test(payload: schemas.GatewayDirectTest) -> schemas.GatewayDirectTestResult:
    start = time.monotonic()
    timeout = httpx.Timeout(
        payload.timeout_ms / 1000.0, connect=min(10.0, payload.timeout_ms / 1000.0)
    )
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.request(
                payload.method,
                payload.url,
                headers=payload.headers or {},
                content=payload.body,
            )
            body = resp.text[:_MAX_TEST_BODY]
            return schemas.GatewayDirectTestResult(
                ok=True,
                status=resp.status_code,
                latency_ms=int((time.monotonic() - start) * 1000),
                headers=dict(resp.headers),
                body=body,
            )
    except httpx.TimeoutException as exc:
        return schemas.GatewayDirectTestResult(
            ok=False, error=f"timeout: {exc}", latency_ms=int((time.monotonic() - start) * 1000)
        )
    except httpx.HTTPError as exc:
        return schemas.GatewayDirectTestResult(
            ok=False, error=str(exc), latency_ms=int((time.monotonic() - start) * 1000)
        )


# ---------------------------------------------------------------------------
# TLS tie-in (reuses the Trove ACME engine)
# ---------------------------------------------------------------------------


async def tls_status(db: AsyncSession, gateway: models.Gateway) -> str:
    """Derive the gateway's live TLS status from its linked certificate.

    Transient markers (``issuing``/``failed``) are preserved only while no
    certificate is linked yet; once a cert exists the status is computed from
    it (``valid``/``expiring``/``expired``/``failed``).
    """
    if not gateway.tls_enabled:
        return "none"
    if gateway.cert_id is None:
        return gateway.tls_status or "none"
    cert = await db.get(models.Certificate, gateway.cert_id)
    if cert is None:
        return "failed"
    if cert.revoked:
        return "failed"
    now = await cert_service.sim_now(db)
    window = await cert_service.get_renew_window(db)
    return cert_service.cert_to_read(cert, now, window).status  # valid|expiring|expired


async def issue_tls_cert(db: AsyncSession, gateway: models.Gateway) -> str:
    """Issue (or refresh) a certificate for the gateway's TLS domain.

    Reuses the existing ACME/simulated issuance pipeline; stores the cert id
    on the gateway and returns the resulting status. Raises on failure after
    recording ``tls_status="failed"`` + error text.
    """
    domain = (gateway.tls_domain or "").strip()
    if not domain:
        raise ValueError("tls_domain is required to issue a TLS certificate")
    gateway.tls_status = "issuing"
    gateway.tls_error = ""
    try:
        payload = schemas.CertCreate(
            cn=domain,
            sans=[domain],
            issuer=gateway.issuer or "Let's Encrypt",
            auto_renew=True,
        )
        cert = await cert_service.issue_cert(db, payload)
        gateway.cert_id = cert.id
        gateway.tls_status = await tls_status(db, gateway)
        return gateway.tls_status
    except providers.IssuanceError as exc:
        gateway.tls_status = "failed"
        gateway.tls_error = str(exc)
        logger.warning("gateway %s TLS issuance failed: %s", gateway.slug, exc)
        raise ValueError(str(exc)) from exc


async def renew_tls_cert(db: AsyncSession, gateway: models.Gateway) -> str:
    """Force-renew the gateway's certificate via the existing pipeline."""
    if gateway.cert_id is None:
        return await issue_tls_cert(db, gateway)
    cert = await db.get(models.Certificate, gateway.cert_id)
    if cert is None:
        return await issue_tls_cert(db, gateway)
    try:
        await cert_service.renew_cert(db, cert, reason="gateway-tls")
        gateway.tls_status = await tls_status(db, gateway)
        return gateway.tls_status
    except providers.IssuanceError as exc:
        gateway.tls_status = "failed"
        gateway.tls_error = str(exc)
        raise ValueError(str(exc)) from exc
