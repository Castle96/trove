"""REST API for Trivy image vulnerability scans (cached summaries)."""

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.requests import Request

from app.dockwatch.api.deps import require_write
from app.dockwatch.api.rate_limit import limiter
from app.dockwatch.schemas.security import ImageScanDetail, ImageScanRead, ScanRequest
from app.dockwatch.services.activity import activity_log
from app.dockwatch.services.trivy_service import (
    TrivyNotInstalledError,
    TrivyScanError,
    get_cached_scan,
    list_scans,
    scan_image,
)

router = APIRouter(prefix="/api/security", tags=["security"])


def _read_model(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "image_ref": row.image_ref,
        "digest": row.digest,
        "scanned_at_ms": row.scanned_at_ms,
        "critical": row.critical,
        "high": row.high,
        "medium": row.medium,
        "low": row.low,
        "unknown": row.unknown,
        "total": row.total,
        "endpoint_id": row.endpoint_id,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


@router.get("/scans", response_model=list[ImageScanRead])
async def list_image_scans(
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """All cached scan summaries, most severe first."""
    return [_read_model(row) for row in await list_scans()][:limit]


@router.get("/scans/detail", response_model=ImageScanDetail)
async def get_scan_detail(image: str = Query(min_length=1, max_length=500)) -> dict[str, Any]:
    """Cached scan + flattened CVE list for one image ref (404 when never scanned)."""
    row = await get_cached_scan(image)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No scan cached for {image!r}")
    try:
        vulnerabilities = json.loads(row.report)
    except json.JSONDecodeError:
        vulnerabilities = []
    return {**_read_model(row), "vulnerabilities": vulnerabilities}


@router.post(
    "/scans",
    response_model=ImageScanRead,
    status_code=202,
    dependencies=[Depends(require_write)],
)
@limiter.limit("10/minute")
async def trigger_scan(
    payload: ScanRequest,
    request: Request,
    endpoint_id: int | None = Query(default=None),
    force: bool = Query(default=False),
) -> dict[str, Any]:
    """Scan an image now (uses cache unless ``force`` or stale)."""
    try:
        row = await scan_image(payload.image, endpoint_id=endpoint_id, force=force)
    except TrivyNotInstalledError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except TrivyScanError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    activity_log.record(
        "image", "scanned", payload.image, endpoint_id=endpoint_id, endpoint_name=None
    )
    return _read_model(row)
