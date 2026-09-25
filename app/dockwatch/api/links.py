"""REST API for discovered container-port hotlinks + gateway mapping.

Endpoints:

* ``GET /api/links`` — every discovered hotlink across all endpoints.
* ``GET /api/endpoints/{id}/links`` — hotlinks for one endpoint.
* ``POST /api/endpoints/{id}/discover`` — scan the endpoint's containers and
  upsert its published-port links.
* ``PATCH /api/links/{id}`` — relabel / enable / retune a hotlink.
* ``DELETE /api/links/{id}`` — forget a hotlink.
* ``POST /api/links/{id}/map-to-gateway`` — promote a hotlink into a Trove
  gateway route (``upstream_url`` = the discovered URL) so it is proxied at
  ``/gw/<slug>``.

Links live in the Dockwatch database; gateways live in the separate Trove
database, so route creation uses the Trove session.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dockwatch.api.deps import require_write
from app.dockwatch.api.inventory import get_or_404
from app.dockwatch.database import get_session as get_dockwatch_session
from app.dockwatch.models.container_link import ContainerLink
from app.dockwatch.models.endpoint import Endpoint
from app.dockwatch.schemas.links import (
    ContainerLinkRead,
    ContainerLinkUpdate,
    ContainerLinkView,
    DiscoveryResult,
    MapToGatewayRequest,
    MapToGatewayResult,
)
from app.dockwatch.services.docker_manager import docker_manager
from app.dockwatch.services.docker_service import DockerUnavailableError
from app.models import Gateway, GatewayRoute

router = APIRouter(prefix="/api", tags=["links"])

DockwatchDB = Annotated[AsyncSession, Depends(get_dockwatch_session)]
TroveDB = Annotated[AsyncSession, Depends(get_db)]


async def _link_or_404(db: AsyncSession, link_id: int) -> ContainerLink:
    return await get_or_404(db, ContainerLink, link_id, "Link")


def _to_view(rows: list[ContainerLink], endpoint_names: dict[int, str]) -> list[ContainerLinkView]:
    return [
        ContainerLinkView(
            id=r.id,
            endpoint_id=r.endpoint_id,
            endpoint_name=endpoint_names.get(r.endpoint_id, ""),
            container_id=r.container_id,
            short_id=r.short_id,
            container_name=r.container_name,
            image=r.image,
            state=r.state,
            container_port=r.container_port,
            host_port=r.host_port,
            host=r.host,
            scheme=r.scheme,
            url=r.url,
            label=r.label,
            enabled=r.enabled,
            sort_order=r.sort_order,
            stale=r.stale,
            manual=r.manual,
            gateway_route_id=r.gateway_route_id,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rows
    ]


async def _endpoint_names(db: AsyncSession) -> dict[int, str]:
    rows = (await db.scalars(select(Endpoint))).all()
    return {e.id: e.name for e in rows}


@router.get("/links", response_model=list[ContainerLinkView])
async def list_links(
    db: DockwatchDB,
    endpoint_id: int | None = Query(default=None),
    include_stale: bool = Query(default=True),
    include_disabled: bool = Query(default=True),
    limit: int = Query(default=500, ge=1, le=500),
) -> list[ContainerLinkView]:
    stmt = select(ContainerLink).order_by(
        ContainerLink.stale, ContainerLink.endpoint_id, ContainerLink.sort_order, ContainerLink.id
    )
    if endpoint_id is not None:
        stmt = stmt.where(ContainerLink.endpoint_id == endpoint_id)
    if not include_stale:
        stmt = stmt.where(ContainerLink.stale.is_(False))
    if not include_disabled:
        stmt = stmt.where(ContainerLink.enabled.is_(True))
    rows = list((await db.scalars(stmt.limit(limit))).all())
    return _to_view(rows, await _endpoint_names(db))


@router.get("/endpoints/{endpoint_id}/links", response_model=list[ContainerLinkView])
async def list_endpoint_links(
    endpoint_id: int,
    db: DockwatchDB,
    include_stale: bool = Query(default=True),
) -> list[ContainerLinkView]:
    stmt = select(ContainerLink).where(ContainerLink.endpoint_id == endpoint_id)
    if not include_stale:
        stmt = stmt.where(ContainerLink.stale.is_(False))
    rows = list(
        (
            await db.scalars(
                stmt.order_by(ContainerLink.stale, ContainerLink.sort_order, ContainerLink.id)
            )
        ).all()
    )
    return _to_view(rows, await _endpoint_names(db))


@router.post(
    "/endpoints/{endpoint_id}/discover",
    response_model=DiscoveryResult,
    dependencies=[Depends(require_write)],
)
async def discover_endpoint(endpoint_id: int, db: DockwatchDB) -> dict[str, Any]:
    endpoint: Endpoint = await get_or_404(db, Endpoint, endpoint_id, "Endpoint")
    service = await docker_manager.for_endpoint(endpoint)
    try:
        return await _discover_endpoint(db, endpoint, service)
    except DockerUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _discover_endpoint(db: AsyncSession, endpoint: Endpoint, service: Any) -> dict[str, Any]:
    """Run discovery and build the DiscoveryResult dict (shared with test probe)."""
    from app.dockwatch.services.discovery import discover_and_sync

    result = await discover_and_sync(db, service, endpoint)
    return {
        "endpoint_id": result["endpoint_id"],
        "endpoint_name": result["endpoint_name"],
        "discovered": result["discovered"],
        "total": result["total"],
        "stale": result["stale"],
        "links": result["links"],
    }


@router.get("/links/{link_id}", response_model=ContainerLinkRead)
async def get_link(link_id: int, db: DockwatchDB) -> ContainerLink:
    return await _link_or_404(db, link_id)


@router.patch(
    "/links/{link_id}",
    response_model=ContainerLinkRead,
    dependencies=[Depends(require_write)],
)
async def update_link(link_id: int, payload: ContainerLinkUpdate, db: DockwatchDB) -> ContainerLink:
    link = await _link_or_404(db, link_id)
    fields = payload.model_dump(exclude_unset=True)
    link.label = fields.get("label", link.label)
    if "enabled" in fields:
        link.enabled = fields["enabled"]
    if "sort_order" in fields:
        link.sort_order = fields["sort_order"]
    if "scheme" in fields or "host" in fields:
        scheme = fields.get("scheme", link.scheme)
        host = fields.get("host", link.host)
        link.scheme = scheme
        link.host = host
        link.url = f"{scheme}://{host}:{link.host_port}"
        link.manual = True
    await db.commit()
    await db.refresh(link)
    return link


@router.delete("/links/{link_id}", status_code=204, dependencies=[Depends(require_write)])
async def delete_link(link_id: int, db: DockwatchDB) -> None:
    link = await _link_or_404(db, link_id)
    await db.delete(link)
    await db.commit()


@router.post(
    "/links/{link_id}/map-to-gateway",
    response_model=MapToGatewayResult,
    dependencies=[Depends(require_write)],
)
async def map_link_to_gateway(
    link_id: int, payload: MapToGatewayRequest, dw_db: DockwatchDB, db: TroveDB
) -> MapToGatewayResult:
    link = await _link_or_404(dw_db, link_id)
    gateway = await db.get(Gateway, payload.gateway_id)
    if gateway is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Gateway not found")
    if not link.url.lower().startswith(("http://", "https://")):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Link URL is not an http(s) upstream: {link.url}",
        )
    if not payload.path.startswith("/"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "path must start with '/'")
    methods = [m.strip().upper() for m in payload.methods if m.strip()]
    if not methods:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "methods must not be empty")
    route = GatewayRoute(
        gateway_id=gateway.id,
        name=link.label or link.container_name or link.short_id or f"link-{link.id}",
        methods=methods,
        path=payload.path,
        upstream_url=link.url,
        strip_prefix=payload.strip_prefix,
        auth_mode=payload.auth_mode,
        rate_limit_rpm=payload.rate_limit_rpm,
        enabled=True,
        timeout_ms=30_000,
    )
    db.add(route)
    await db.commit()
    await db.refresh(route)
    link.gateway_route_id = route.id
    await dw_db.commit()
    return MapToGatewayResult(
        link_id=link.id,
        url=link.url,
        route_id=route.id,
        gateway_slug=gateway.slug,
    )
