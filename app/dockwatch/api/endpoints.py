"""REST API for monitored endpoints (multi-host registry) + fleet overview."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dockwatch.api.deps import require_write
from app.dockwatch.api.inventory import conflict_handler, get_or_404
from app.dockwatch.database import get_session
from app.dockwatch.models.endpoint import Endpoint
from app.dockwatch.schemas.endpoint import (
    EndpointCreate,
    EndpointRead,
    EndpointStatus,
    EndpointUpdate,
    FleetOverview,
)
from app.dockwatch.services.activity import activity_log
from app.dockwatch.services.docker_manager import docker_manager

DB = Annotated[AsyncSession, Depends(get_session)]
router = APIRouter(prefix="/api/endpoints", tags=["endpoints"])


async def _enabled_endpoints(db: AsyncSession) -> list[Endpoint]:
    rows = await db.scalars(select(Endpoint).order_by(Endpoint.name))
    return list(rows.all())


@router.get("/fleet/overview", response_model=FleetOverview)
async def fleet_overview(db: DB) -> dict[str, Any]:
    """Local + every enabled endpoint polled concurrently (partial failures OK)."""
    endpoints = await _enabled_endpoints(db)
    statuses = await docker_manager.fleet_status(endpoints)
    reachable = sum(1 for s in statuses if s.get("available"))
    try:
        by_id = {e.id: e for e in endpoints}
        for s in statuses:
            eid = s.get("endpoint_id")
            if eid is not None and eid in by_id:
                row = by_id[eid]
                if s.get("available"):
                    row.touch("ok")
                else:
                    row.touch("unavailable", s.get("reason"))
        await db.commit()
    except Exception:
        await db.rollback()
    return {
        "endpoints": [
            {
                "endpoint_id": s.get("endpoint_id"),
                "endpoint_name": s.get("endpoint_name", "local"),
                "available": bool(s.get("available")),
                "reason": s.get("reason"),
                "engine": s.get("engine"),
                "version": s.get("version"),
                "containers_total": int(s.get("containers_total", 0) or 0),
                "containers_running": int(s.get("containers_running", 0) or 0),
                "containers_stopped": int(s.get("containers_stopped", 0) or 0),
            }
            for s in statuses
        ],
        "total_endpoints": len(statuses),
        "reachable": reachable,
        "total_containers": sum(int(s.get("containers_total", 0) or 0) for s in statuses),
        "total_running": sum(int(s.get("containers_running", 0) or 0) for s in statuses),
    }


@router.get("/fleet/containers")
async def fleet_containers(
    db: DB,
    include_stats: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List containers across local + enabled endpoints (best-effort)."""
    import asyncio

    from app.dockwatch.config import get_settings

    endpoints = await _enabled_endpoints(db)
    settings = get_settings()
    sem = asyncio.Semaphore(max(settings.fleet_poll_concurrency, 1))

    async def one(
        endpoint_id: int | None, endpoint_name: str, url: str | None
    ) -> list[dict[str, Any]]:
        async with sem:
            try:
                if url is None:
                    service = docker_manager.default
                else:
                    service = await docker_manager.for_url(url)
                timeout = settings.fleet_poll_timeout
                items = await asyncio.wait_for(
                    service.list_containers(include_stats=include_stats), timeout
                )
            except Exception:
                return []
            out: list[dict[str, Any]] = []
            for item in items:
                row = dict(item)
                row["endpoint_id"] = endpoint_id
                row["endpoint_name"] = endpoint_name
                out.append(row)
            return out

    jobs = [one(None, "local", None)]
    jobs.extend(one(e.id, e.name, e.url) for e in endpoints if e.enabled)
    batches = await asyncio.gather(*jobs)
    merged: list[dict[str, Any]] = [row for batch in batches for row in batch]
    merged = merged[:limit]
    try:
        by_name = {e.name: e for e in endpoints}
        for row in merged:
            name = row.get("endpoint_name")
            if name and name in by_name:
                by_name[name].touch("ok")
        await db.commit()
    except Exception:
        await db.rollback()
    return merged


@router.get("/count")
async def endpoints_count(db: DB) -> dict[str, int]:
    total = await db.scalar(select(func.count(Endpoint.id))) or 0
    enabled_stmt = select(func.count(Endpoint.id)).where(Endpoint.enabled.is_(True))
    enabled = await db.scalar(enabled_stmt) or 0
    return {"total": total, "enabled": enabled}


@router.get("", response_model=list[EndpointRead])
async def list_endpoints(
    db: DB,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[Endpoint]:
    rows = await db.scalars(
        select(Endpoint).order_by(Endpoint.sort_order, Endpoint.name).limit(limit).offset(offset)
    )
    return list(rows.all())


@router.post(
    "",
    response_model=EndpointRead,
    status_code=201,
    dependencies=[Depends(require_write)],
)
async def create_endpoint(payload: EndpointCreate, db: DB) -> Endpoint:
    endpoint = Endpoint(**payload.model_dump())
    db.add(endpoint)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise conflict_handler(exc) from exc
    await db.refresh(endpoint)
    activity_log.record("endpoint", "created", endpoint.name)
    return endpoint


@router.get("/{endpoint_id}", response_model=EndpointRead)
async def get_endpoint(endpoint_id: int, db: DB) -> Endpoint:
    row: Endpoint = await get_or_404(db, Endpoint, endpoint_id, "Endpoint")
    return row


@router.put(
    "/{endpoint_id}",
    response_model=EndpointRead,
    dependencies=[Depends(require_write)],
)
async def update_endpoint(endpoint_id: int, payload: EndpointUpdate, db: DB) -> Endpoint:
    endpoint: Endpoint = await get_or_404(db, Endpoint, endpoint_id, "Endpoint")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(endpoint, key, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise conflict_handler(exc) from exc
    await db.refresh(endpoint)
    activity_log.record("endpoint", "updated", endpoint.name)
    docker_manager.clear()
    return endpoint


@router.delete(
    "/{endpoint_id}",
    status_code=204,
    dependencies=[Depends(require_write)],
)
async def delete_endpoint(endpoint_id: int, db: DB) -> None:
    endpoint = await get_or_404(db, Endpoint, endpoint_id, "Endpoint")
    await db.delete(endpoint)
    await db.commit()
    activity_log.record("endpoint", "deleted", endpoint.name)
    docker_manager.clear()


@router.post(
    "/{endpoint_id}/test",
    response_model=EndpointStatus,
    dependencies=[Depends(require_write)],
)
async def test_endpoint(endpoint_id: int, db: DB) -> dict[str, Any]:
    """Probe one endpoint now and cache the result on the row."""
    endpoint: Endpoint = await get_or_404(db, Endpoint, endpoint_id, "Endpoint")
    service = await docker_manager.for_endpoint(endpoint)
    try:
        status = await service.status()
    except Exception as exc:
        endpoint.touch("unavailable", str(exc))
        await db.commit()
        return {
            "endpoint_id": endpoint.id,
            "endpoint_name": endpoint.name,
            "available": False,
            "reason": str(exc),
        }
    available = bool(status.get("available"))
    reason = None if available else status.get("reason")
    endpoint.touch("ok" if available else "unavailable", reason)
    await db.commit()
    return {
        "endpoint_id": endpoint.id,
        "endpoint_name": endpoint.name,
        "available": available,
        "reason": status.get("reason"),
        "engine": status.get("engine"),
        "version": status.get("version"),
        "containers_total": int(status.get("containers_total", 0)),
        "containers_running": int(status.get("containers_running", 0)),
        "containers_stopped": int(status.get("containers_stopped", 0)),
    }
