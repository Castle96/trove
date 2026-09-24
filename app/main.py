"""Trove — unified FastAPI entrypoint (PKI + Docker + Inventory).

Run with::

    uvicorn app.main:app --reload

This single application exposes the full Trove certificate-lifecycle API
(``/api/certs``, ``/api/settings``, ``/api/auth``, ...) together with the
Dockwatch monitoring platform (``/api/docker``, ``/api/inventory``,
``/api/monitor``, ``/api/swarm``, ``/api/voice``, ...). Both subsystems keep
their own SQLite database and are unified behind Trove's auth model
(API key / user bearer tokens / open mode).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .api import (
    routes_auth,
    routes_ca,
    routes_certs,
    routes_gateway,
    routes_ocsp,
    routes_requests,
    routes_system,
    routes_users,
)
from .api.gateway_proxy import router as gateway_proxy_router
from .config import get_settings as get_trove_settings
from .database import (
    get_engine as get_trove_engine,
)
from .database import (
    get_session_factory as get_trove_session_factory,
)
from .database import (
    init_db as init_trove_db,
)
from .deps import get_principal
from .dockwatch.api import (
    container_ranking,
    docker_router,
    endpoints_router,
    inventory_router,
    metrics_router,
    models_router,
    monitor_router,
    security_router,
    swarm_router,
    voice_router,
)
from .dockwatch.api.docker import docker_service
from .dockwatch.api.rate_limit import limiter
from .dockwatch.config import get_settings as get_dockwatch_settings
from .dockwatch.database import (
    get_engine as get_dockwatch_engine,
)
from .dockwatch.database import (
    get_session_factory as get_dockwatch_session_factory,
)
from .dockwatch.database import (
    init_db as init_dockwatch_db,
)
from .dockwatch.logging_config import request_id_var, setup_logging
from .dockwatch.middleware import (
    AccessLogMiddleware,
    RequestIDMiddleware,
    SecurityHeadersMiddleware,
)
from .dockwatch.services.metrics import MetricsMiddleware
from .dockwatch.services.monitor_loop import start_monitor_sampler
from .seed import seed_if_empty

_trove_settings = get_trove_settings()
_dockwatch_settings = get_dockwatch_settings()
setup_logging(_dockwatch_settings.log_level)

logger = logging.getLogger(__name__)

#: Background tasks owned by the unified lifespan.
_sampler_task: asyncio.Task[None] | None = None
_rescan_task: asyncio.Task[None] | None = None
_voice_task: asyncio.Task[None] | None = None
_container_stats_task: asyncio.Task[None] | None = None


def _current_request_id(request: Request | None = None) -> str | None:
    """Return the active request ID from the contextvar (or request state)."""
    request_id = request_id_var.get()
    if request_id is None and request is not None:
        state_id = getattr(request.state, "request_id", None)
        if isinstance(state_id, str):
            return state_id
    return request_id


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup: create tables in both databases + seed + start samplers."""
    from .dockwatch.services.rescan_loop import start_rescan_loop
    from .dockwatch.services.voice_pipeline import ensure_jarvis_agent, start_voice_loop

    global _sampler_task, _rescan_task, _voice_task, _container_stats_task

    # Trove subsystem (PKI, users, settings).
    await init_trove_db()
    async with get_trove_session_factory()() as db:
        await seed_if_empty(db)
    logger.info("trove database ready")

    # Dockwatch subsystem (monitoring, inventory, swarm, voice).
    await init_dockwatch_db()
    if _dockwatch_settings.enable_monitor_sampler:
        _sampler_task = await start_monitor_sampler()
        logger.info("host metrics sampler started")
    _rescan_task = None
    if _dockwatch_settings.enable_rescan_loop:
        _rescan_task = await start_rescan_loop()
        logger.info("vulnerability rescan loop started")
    _voice_task = None
    if _dockwatch_settings.enable_voice:
        if _dockwatch_settings.voice_jarvis_agent:
            async with get_dockwatch_session_factory()() as session:
                await ensure_jarvis_agent(session)
            logger.info("voice (jarvis) agent ready")
        _voice_task = await start_voice_loop()
        logger.info("voice pipeline loop started")
    _container_stats_task = None
    if _dockwatch_settings.enable_container_stats:
        from .dockwatch.services.docker_manager import docker_manager

        _container_stats_task = asyncio.create_task(
            docker_manager.default.poll_and_persist_container_stats(
                persist_every=int(_dockwatch_settings.container_stats_interval)
            ),
            name="container-stats",
        )
        logger.info("container stats collector started")
    logger.info("%s ready", _trove_settings.app_name)
    yield
    # Graceful shutdown: cancel background loops, then wait up to the grace
    # period for them (and their thread-pool work) to unwind.
    tasks = [
        t
        for t in (
            _sampler_task,
            _rescan_task,
            _voice_task,
            _container_stats_task,
        )
        if t is not None and not t.done()
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        grace = _dockwatch_settings.shutdown_grace_seconds
        _, pending = await asyncio.wait(tasks, timeout=grace)
        for task in pending:
            logger.warning(
                "background task %r still running after %.1fs shutdown grace; abandoning",
                task.get_name(),
                grace,
            )
    _sampler_task = _rescan_task = _voice_task = None
    await get_dockwatch_engine().dispose()
    await get_trove_engine().dispose()


def create_app() -> FastAPI:
    """Build the unified Trove + Dockwatch application."""
    settings = _trove_settings
    dw_settings = _dockwatch_settings

    app = FastAPI(
        title=settings.app_name,
        description=(
            "Unified homelab control plane: certificate lifecycle / PKI "
            "(Trove) plus Docker monitoring, infrastructure inventory, "
            "vulnerability scanning, model runtimes, agent swarm, and the "
            "Jarvis voice pipeline (Dockwatch)."
        ),
        version=__version__,
        contact={"name": "Trove"},
        license_info={"name": "MIT"},
        openapi_url="/api/openapi.json" if dw_settings.enable_openapi else None,
        docs_url="/docs" if dw_settings.enable_openapi else None,
        redoc_url="/redoc" if dw_settings.enable_openapi else None,
        openapi_tags=[
            {"name": "certs", "description": "Certificates, issuance, renewals, imports."},
            {"name": "system", "description": "Health, time simulation, settings, logs, CRL."},
            {"name": "auth", "description": "Login, logout, current identity."},
            {"name": "users", "description": "User and API-token administration."},
            {"name": "docker", "description": "Containers, images, networks, volumes, activity."},
            {"name": "inventory", "description": "Sites, racks, devices, and IP addresses."},
            {"name": "monitor", "description": "Host metrics history and anomaly detection."},
            {"name": "endpoints", "description": "Remote Dockwatch fleet endpoints."},
            {"name": "security", "description": "Trivy vulnerability scans and reports."},
            {"name": "models", "description": "LLM runtime node status (Ollama / llama.cpp)."},
            {"name": "swarm", "description": "Agent-swarm dashboard: agents, projects, approvals."},
            {"name": "voice", "description": "Jarvis voice pipeline: live stages, latency, turns."},
            {
                "name": "gateway",
                "description": "API gateway: routes, consumers, API keys, TLS, request logs.",
            },
            {
                "name": "gateway-proxy",
                "description": "Live API gateway hot path /gw/<slug>/<path>.",
            },
        ],
        lifespan=lifespan,
    )

    # ---- Rate limiting (Dockwatch's slowapi limiter) ----------------------
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    # ---- Exception handlers (request ID on every error response) ----------
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = _current_request_id(request)
        logger.warning(
            "validation_error path=%s request_id=%s errors=%s",
            request.url.path,
            request_id,
            exc.errors(),
        )
        return JSONResponse(
            status_code=422,
            content={
                "detail": "Validation error",
                "errors": jsonable_encoder(exc.errors()),
                "request_id": request_id,
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        request_id = _current_request_id(request)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "request_id": request_id},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = _current_request_id(request)
        logger.exception(
            "unhandled_error path=%s request_id=%s error=%r",
            request.url.path,
            request_id,
            exc,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "request_id": request_id},
        )

    # ---- Middleware stack ------------------------------------------------
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=dw_settings.allowed_hosts or ["*"],
    )
    if "*" in dw_settings.allowed_hosts and dw_settings.auth_token:
        logger.warning(
            'TrustedHostMiddleware accepts any Host header (DOCKWATCH_ALLOWED_HOSTS="*"). '
            "Set DOCKWATCH_ALLOWED_HOSTS to your domain(s) to enable host-header validation."
        )
    if dw_settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=dw_settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Dockwatch-Token", "X-Request-ID"],
        )
    app.add_middleware(RequestIDMiddleware)
    if dw_settings.security_headers:
        app.add_middleware(SecurityHeadersMiddleware, csp=dw_settings.csp_override)
    if dw_settings.enable_metrics:
        app.add_middleware(MetricsMiddleware)

    # ---- Trove API routers -------------------------------------------
    app.include_router(routes_certs.router, prefix="/api")
    app.include_router(routes_system.router, prefix="/api")
    app.include_router(routes_auth.router, prefix="/api")
    app.include_router(routes_users.router, prefix="/api")
    app.include_router(routes_gateway.router, prefix="/api")
    app.include_router(routes_ca.router, prefix="/api")
    app.include_router(routes_requests.router, prefix="/api")
    app.include_router(routes_ocsp.router, prefix="/api")

    # ---- Public API Gateway hot path (registered before the SPA mount) ----
    app.include_router(gateway_proxy_router)

    # ---- Dockwatch API routers (principal-gated; writes additionally check
    #      roles via Dockwatch's adapted ``require_write``) ------------------
    dw_auth = [Depends(get_principal)]
    app.include_router(container_ranking, dependencies=dw_auth)
    app.include_router(docker_router, dependencies=dw_auth)
    app.include_router(inventory_router, dependencies=dw_auth)
    app.include_router(monitor_router, dependencies=dw_auth)
    app.include_router(endpoints_router, dependencies=dw_auth)
    app.include_router(security_router, dependencies=dw_auth)
    app.include_router(models_router, dependencies=dw_auth)
    app.include_router(swarm_router, dependencies=dw_auth)
    app.include_router(voice_router, dependencies=dw_auth)
    # Prometheus scrape endpoint: deliberately unauthenticated.
    app.include_router(metrics_router)

    # ---- Probes -----------------------------------------------------------
    @app.get("/api/health")
    async def health() -> dict[str, str]:
        """Liveness probe (always available, no auth)."""
        return {"status": "ok", "app": settings.app_name, "version": __version__}

    @app.get("/api/ready")
    async def ready() -> JSONResponse:
        """Readiness probe: both databases must answer.

        Docker availability is reported as a check but does NOT gate
        readiness — the app keeps working as a PKI + inventory tool when the
        container socket is down (graceful degradation).
        """
        from sqlalchemy import func, select

        from .dockwatch.models.endpoint import Endpoint

        async def db_ready(engine: Any) -> tuple[bool, str | None]:
            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
            except Exception as exc:  # pragma: no cover - defensive
                return False, str(exc)
            return True, None

        cv_ok, cv_reason = await db_ready(get_trove_engine())
        dw_ok, dw_reason = await db_ready(get_dockwatch_engine())
        docker_ok = await docker_service.ping()
        checks: dict[str, str] = {
            "database": "ok" if cv_ok and dw_ok else "error: " + (cv_reason or dw_reason or ""),
            "docker": "ok" if docker_ok else "unavailable",
        }
        endpoints_total: int | None = None
        if dw_ok:
            try:
                async with get_dockwatch_session_factory()() as session:
                    endpoints_total = await session.scalar(select(func.count(Endpoint.id))) or 0
                checks["endpoints"] = str(endpoints_total)
            except Exception as exc:  # pragma: no cover - defensive
                checks["endpoints"] = f"error: {exc}"
        body: dict[str, Any] = {"ready": cv_ok and dw_ok, "checks": checks}
        return JSONResponse(content=body, status_code=200 if cv_ok and dw_ok else 503)

    # ---- Frontend (static SPA) --------------------------------------------
    frontend = settings.frontend_dir
    if frontend.exists():
        app.mount("/", StaticFiles(directory=str(frontend), html=True), name="frontend")

    return app


app = create_app()
