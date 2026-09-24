"""REST API for host monitoring (Glances-style system metrics, multi-endpoint).

Local host uses the shared :data:`monitor_service` singleton + SQLite history.
Remote hosts push samples via ``POST /api/monitor/ingest`` with their
``endpoint_id``; ``series``/``anomalies`` accept ``?endpoint_id=`` to read
per-endpoint history.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.dockwatch.api.deps import require_write
from app.dockwatch.api.rate_limit import limiter
from app.dockwatch.database import get_session
from app.dockwatch.models.endpoint import Endpoint
from app.dockwatch.services.monitor_persistence import (
    bucket_samples,
    fetch_anomalies,
    fetch_anomaly_density,
    fetch_history,
    fetch_recent,
    insert_samples,
)
from app.dockwatch.services.monitor_service import (
    MAX_HISTORY,
    MonitorSampleDict,
    MonitorUnavailableError,
    monitor_service,
)

router = APIRouter(prefix="/api/monitor", tags=["monitor"])

MAX_SERIES = 300

DB = Annotated[AsyncSession, Depends(get_session)]


def _unavailable(exc: MonitorUnavailableError) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


class IngestPayload(BaseModel):
    """One sample pushed by a remote agent (same shape as local samples)."""

    endpoint_id: int = Field(gt=0)
    sample: dict[str, Any]


def _validate_sample(raw: dict[str, Any]) -> MonitorSampleDict:
    """Minimal validation — remote agents send the full local sample shape."""
    required = ("t", "cpu", "cores", "memory", "swap", "load1", "net", "disk")
    missing = [k for k in required if k not in raw]
    if missing:
        raise HTTPException(status_code=422, detail=f"sample missing keys: {missing}")
    return raw  # type: ignore[return-value]


@router.get("/status")
async def monitor_status() -> dict[str, Any]:
    """Host metrics availability. Always 200 so the UI can react gracefully."""
    try:
        monitor_service.snapshot()
        return {"available": True, "reason": None}
    except MonitorUnavailableError as exc:
        return {"available": False, "reason": str(exc)}


@router.get("/snapshot")
async def monitor_snapshot() -> dict[str, Any]:
    """Current host metrics: system info, gauges, processes, disk usage."""
    try:
        return monitor_service.snapshot()
    except MonitorUnavailableError as exc:
        raise _unavailable(exc) from exc


@router.get("/series")
async def monitor_series(
    limit: int = Query(default=120, ge=5, le=MAX_SERIES),
    endpoint_id: int | None = Query(default=None),
) -> dict[str, Any]:
    """Rolling time-series (oldest first). Local = memory; remote = persisted."""
    if endpoint_id is not None:
        samples = await fetch_recent(limit=limit, endpoint_id=endpoint_id)
        return {"samples": samples, "count": len(samples), "endpoint_id": endpoint_id}
    samples = monitor_service.series(limit=limit)
    if not samples:
        try:
            monitor_service.sample()
            samples = monitor_service.series(limit=limit)
        except MonitorUnavailableError as exc:
            raise _unavailable(exc) from exc
    return {"samples": samples, "count": len(samples)}


@router.get("/anomalies")
async def monitor_anomalies(
    limit: int = Query(default=10, ge=1, le=100),
    endpoint_id: int | None = Query(default=None),
) -> dict[str, Any]:
    """Most recent z-score anomaly events detected on CPU or memory."""
    items = await fetch_anomalies(limit=limit, endpoint_id=endpoint_id)
    return {"anomalies": items, "count": len(items)}


@router.get("/history")
async def monitor_history(
    range: int = Query(default=900, ge=60, le=604800, description="Trailing window (s)"),
    bucket: int = Query(default=60, ge=5, le=3600, description="Bucket width (s)"),
    endpoint_id: int | None = Query(default=None),
) -> dict[str, Any]:
    """Downsampled metric history (avg/max per epoch-aligned bucket).

    Local (no ``endpoint_id``) reads persisted samples; when none are stored
    yet the in-memory rolling buffer is bucketed so long-range views still
    render on a fresh start.
    """
    buckets = await fetch_history(range, bucket, endpoint_id=endpoint_id)
    if endpoint_id is None and not buckets:
        buckets = bucket_samples(monitor_service.series(limit=MAX_HISTORY), bucket)
    return {
        "range_seconds": range,
        "bucket_seconds": bucket,
        "endpoint_id": endpoint_id,
        "series": buckets,
    }


@router.get("/anomaly-density")
async def monitor_anomaly_density(
    range: int = Query(default=86400, ge=60, le=604800, description="Trailing window (s)"),
    bucket: int = Query(default=3600, ge=5, le=86400, description="Bucket width (s)"),
    endpoint_id: int | None = Query(default=None),
) -> dict[str, Any]:
    """Per-bucket anomaly counts for the trailing window (long-range density strip)."""
    density = await fetch_anomaly_density(range, bucket, endpoint_id=endpoint_id)
    return {
        "range_seconds": range,
        "bucket_seconds": bucket,
        "endpoint_id": endpoint_id,
        "density": density,
    }


@router.post(
    "/ingest",
    status_code=202,
    dependencies=[Depends(require_write)],
)
@limiter.limit("60/minute")
async def monitor_ingest(payload: IngestPayload, request: Request, db: DB) -> dict[str, Any]:
    """Accept a sample pushed by a remote agent for ``endpoint_id``."""
    endpoint = await db.get(Endpoint, payload.endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    if not endpoint.enabled:
        raise HTTPException(status_code=400, detail="Endpoint is disabled")
    sample = _validate_sample(payload.sample)
    await insert_samples([sample], endpoint_id=endpoint.id)
    endpoint.touch("ok")
    await db.commit()
    return {"accepted": True, "endpoint_id": endpoint.id}
