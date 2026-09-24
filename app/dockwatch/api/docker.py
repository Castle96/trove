"""REST API for Docker monitoring + activity feed (multi-endpoint aware)."""

import asyncio
import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.dockwatch.api.deps import require_write
from app.dockwatch.api.rate_limit import limiter
from app.dockwatch.database import get_session
from app.dockwatch.models.endpoint import Endpoint
from app.dockwatch.schemas.docker import (
    ActivityEvent,
    ContainerDeploy,
    ContainerDeployResponse,
    ContainerDetail,
    ContainerRead,
    DockerStatus,
    ImagePullRequest,
    ImagePullResponse,
    ImageRead,
    LogsResponse,
    ServiceRead,
    StackRead,
)
from app.dockwatch.services.activity import activity_log
from app.dockwatch.services.docker_manager import docker_manager
from app.dockwatch.services.docker_service import (
    ContainerDetailDict,
    DockerService,
    DockerUnavailableError,
)

router = APIRouter(prefix="/api", tags=["docker"])

logger = logging.getLogger(__name__)

#: Fire-and-forget background tasks (scan-on-pull); held so they aren't GC'd.
_background_tasks: set[asyncio.Task[Any]] = set()

ContainerAction = Literal["start", "stop", "restart", "pause", "unpause", "kill"]

docker_service = DockerService()

DB = Annotated[AsyncSession, Depends(get_session)]


def _docker_unavailable(exc: DockerUnavailableError) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


async def _resolve_service(
    endpoint_id: int | None, db: AsyncSession | None
) -> tuple[DockerService, int | None, str | None]:
    """Return (service, endpoint_id, endpoint_name) for a request.

    ``None`` means the local engine (back-compat). Remote ids are looked up
    in the DB; unknown/disabled ids are 404/400.
    """
    if endpoint_id is None:
        return docker_service, None, "local"
    if db is None:  # pragma: no cover - defensive, routes always pass db
        raise HTTPException(status_code=400, detail="endpoint_id requires database")
    endpoint = await db.get(Endpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    if not endpoint.enabled:
        raise HTTPException(status_code=400, detail="Endpoint is disabled")
    service = await docker_manager.for_endpoint(endpoint)
    return service, endpoint.id, endpoint.name


def _tag_containers(
    items: list[dict[str, Any]], endpoint_id: int | None, endpoint_name: str | None
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        row = dict(item)
        row.setdefault("endpoint_id", endpoint_id)
        row.setdefault("endpoint_name", endpoint_name)
        out.append(row)
    return out


def _queue_scan_best_effort(image: str, endpoint_id: int | None) -> None:
    """Fire-and-forget Trivy scan (cached when fresh; never fails the request)."""

    async def _run() -> None:
        try:
            from app.dockwatch.services.trivy_service import scan_image

            await scan_image(image, endpoint_id=endpoint_id)
        except Exception:
            logger.exception("background scan failed for %s", image)
        finally:
            _background_tasks.discard(task)

    task = asyncio.create_task(_run())
    _background_tasks.add(task)


@router.get("/docker/status", response_model=DockerStatus)
async def docker_status(db: DB, endpoint_id: int | None = Query(default=None)) -> dict[str, Any]:
    """Engine availability + aggregate counts. Always 200, even when Docker is down."""
    service, eid, ename = await _resolve_service(endpoint_id, db)
    status = await service.status()
    return {"endpoint_id": eid, "endpoint_name": ename, **status}


@router.get("/docker/containers", response_model=list[ContainerRead])
async def list_containers(
    db: DB,
    include_stats: bool = Query(default=True),
    endpoint_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    service, eid, ename = await _resolve_service(endpoint_id, db)
    try:
        items = await service.list_containers(include_stats=include_stats)
    except DockerUnavailableError as exc:
        raise _docker_unavailable(exc) from exc
    return _tag_containers([dict(c) for c in items[:limit]], eid, ename)


@router.get("/docker/containers/{container_id}", response_model=ContainerDetail)
async def get_container(
    container_id: str, db: DB, endpoint_id: int | None = Query(default=None)
) -> ContainerDetailDict:
    service, eid, ename = await _resolve_service(endpoint_id, db)
    try:
        detail = await service.container_detail(container_id)
    except DockerUnavailableError as exc:
        raise _docker_unavailable(exc) from exc
    row = dict(detail)
    row.setdefault("endpoint_id", eid)
    row.setdefault("endpoint_name", ename)
    return row  # type: ignore[return-value]


@router.get("/docker/containers/{container_id}/logs", response_model=LogsResponse)
async def container_logs(
    container_id: str,
    db: DB,
    tail: int = Query(default=200, ge=1, le=10_000),
    endpoint_id: int | None = Query(default=None),
) -> LogsResponse:
    service, _, _ = await _resolve_service(endpoint_id, db)
    try:
        cid, logs = await service.container_logs(container_id, tail=tail)
    except DockerUnavailableError as exc:
        raise _docker_unavailable(exc) from exc
    text = logs.decode("utf-8", errors="replace") if isinstance(logs, bytes) else str(logs)
    return LogsResponse(container_id=cid, logs=text)


@router.post(
    "/docker/containers/{container_id}/action/{action}",
    status_code=204,
    dependencies=[Depends(require_write)],
)
@limiter.limit("10/minute")
async def container_action(
    container_id: str,
    action: ContainerAction,
    request: Request,
    db: DB,
    endpoint_id: int | None = Query(default=None),
) -> None:
    service, eid, ename = await _resolve_service(endpoint_id, db)
    try:
        await service.container_action(container_id, action)
    except DockerUnavailableError as exc:
        raise _docker_unavailable(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    activity_log.record("container", action, container_id, endpoint_id=eid, endpoint_name=ename)


@router.get("/docker/stacks", response_model=list[StackRead])
async def list_stacks(
    db: DB,
    endpoint_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    service, _, _ = await _resolve_service(endpoint_id, db)
    try:
        items = await service.list_stacks()
        return items[:limit]
    except DockerUnavailableError as exc:
        raise _docker_unavailable(exc) from exc


@router.get("/docker/images", response_model=list[ImageRead])
async def list_images(
    db: DB,
    endpoint_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    service, _, _ = await _resolve_service(endpoint_id, db)
    try:
        items = await service.list_images()
        return items[:limit]
    except DockerUnavailableError as exc:
        raise _docker_unavailable(exc) from exc


@router.get("/docker/services", response_model=list[ServiceRead])
async def list_services(
    db: DB,
    endpoint_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    service, _, _ = await _resolve_service(endpoint_id, db)
    try:
        items = await service.list_services()
        return items[:limit]
    except DockerUnavailableError as exc:
        raise _docker_unavailable(exc) from exc


@router.post(
    "/docker/images/pull",
    response_model=ImagePullResponse,
    status_code=202,
    dependencies=[Depends(require_write)],
)
@limiter.limit("10/minute")
async def pull_image(
    payload: ImagePullRequest,
    request: Request,
    db: DB,
    endpoint_id: int | None = Query(default=None),
) -> dict[str, str]:
    """Pull an image on the selected engine (202 when the pull completes)."""
    service, eid, ename = await _resolve_service(endpoint_id, db)
    try:
        result = await service.pull_image(payload.image)
    except DockerUnavailableError as exc:
        raise _docker_unavailable(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    activity_log.record("image", "pulled", result["image"], endpoint_id=eid, endpoint_name=ename)
    _queue_scan_best_effort(result["image"], eid)
    return result


@router.post(
    "/docker/containers",
    response_model=ContainerDeployResponse,
    status_code=201,
    dependencies=[Depends(require_write)],
)
@limiter.limit("10/minute")
async def deploy_container(
    payload: ContainerDeploy,
    request: Request,
    db: DB,
    endpoint_id: int | None = Query(default=None),
) -> dict[str, Any]:
    """Deploy a single container (no privileged/socket mounts allowed)."""
    service, eid, ename = await _resolve_service(endpoint_id, db)
    try:
        result = await service.deploy_container(
            image=payload.image,
            name=payload.name,
            command=payload.command,
            environment=payload.environment,
            ports=payload.ports,
            volumes=payload.volumes,
            network_mode=payload.network_mode,
            restart_policy=payload.restart_policy,
            labels=payload.labels,
            auto_start=payload.auto_start,
            pull_if_missing=payload.pull_if_missing,
        )
    except DockerUnavailableError as exc:
        raise _docker_unavailable(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cid: str = result["id"]
    activity_log.record(
        "container",
        "deployed",
        result.get("name") or cid[:12],
        endpoint_id=eid,
        endpoint_name=ename,
    )
    _queue_scan_best_effort(payload.image, eid)
    return {
        "id": cid,
        "short_id": cid[:12],
        "name": result.get("name", ""),
        "warnings": result.get("warnings", []),
    }


@router.delete(
    "/docker/containers/{container_id}",
    status_code=204,
    dependencies=[Depends(require_write)],
)
async def remove_container(
    container_id: str,
    db: DB,
    force: bool = Query(default=False),
    endpoint_id: int | None = Query(default=None),
) -> None:
    """Remove a container (force-remove must be explicit)."""
    service, eid, ename = await _resolve_service(endpoint_id, db)
    try:
        await service.remove_container(container_id, force=force)
    except DockerUnavailableError as exc:
        raise _docker_unavailable(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    activity_log.record(
        "container", "removed", container_id[:12], endpoint_id=eid, endpoint_name=ename
    )


@router.get("/activity", response_model=list[ActivityEvent])
async def list_activity(
    limit: int = Query(default=50, ge=1, le=200),
    endpoint_id: int | None = Query(default=None),
) -> list[dict[str, Any]]:
    """Recent dashboard activity (actions performed via this app)."""
    events = activity_log.as_list()
    if endpoint_id is not None:
        events = [e for e in events if e.get("endpoint_id") == endpoint_id]
    return events[:limit]
