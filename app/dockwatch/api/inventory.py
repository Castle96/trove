"""REST API for the NetBox-style infrastructure inventory (sites/racks/devices/IPs)."""

from collections.abc import Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dockwatch.api.deps import require_write
from app.dockwatch.database import get_session
from app.dockwatch.models import Device, IPAddress, Rack, Site
from app.dockwatch.schemas.inventory import (
    DeviceCreate,
    DeviceRead,
    DeviceUpdate,
    InventoryOverview,
    IPAddressCreate,
    IPAddressRead,
    IPAddressUpdate,
    RackCreate,
    RackRead,
    RackUpdate,
    SearchResult,
    SiteCreate,
    SiteRead,
    SiteUpdate,
)
from app.dockwatch.services.activity import activity_log

DB = Annotated[AsyncSession, Depends(get_session)]
router = APIRouter(prefix="/api/inventory", tags=["inventory"])


# ------------------------------------------------------------------ helpers
async def get_or_404(db: AsyncSession, model: type[Any], entity_id: int, label: str) -> Any:
    obj = await db.get(model, entity_id)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return obj


def conflict_handler(exc: IntegrityError) -> HTTPException:
    """Map an IntegrityError to a friendly 409 response."""
    return HTTPException(
        status_code=409, detail=exc.orig.args[0] if exc.orig and exc.orig.args else "Conflict"
    )


# ------------------------------------------------------------------ bulk aggregation helpers
async def _counts_by_parent(
    db: AsyncSession,
    parent_col: Any,
    child_id: Any,
    parent_ids: Sequence[int],
) -> dict[int, int]:
    """One GROUP BY aggregate: ``{parent_id: count}`` for the given parents."""
    if not parent_ids:
        return {}
    stmt = (
        select(parent_col, func.count(child_id))
        .where(parent_col.in_(parent_ids))
        .group_by(parent_col)
    )
    rows = (await db.execute(stmt)).all()
    return {int(row[0]): int(row[1]) for row in rows}


async def _rack_count_by_site(db: AsyncSession, site_ids: Sequence[int]) -> dict[int, int]:
    return await _counts_by_parent(db, Rack.site_id, Rack.id, site_ids)


async def _device_count_by_site(db: AsyncSession, site_ids: Sequence[int]) -> dict[int, int]:
    return await _counts_by_parent(db, Device.site_id, Device.id, site_ids)


async def _device_count_by_rack(db: AsyncSession, rack_ids: Sequence[int]) -> dict[int, int]:
    return await _counts_by_parent(db, Device.rack_id, Device.id, rack_ids)


async def _ip_count_by_device(db: AsyncSession, device_ids: Sequence[int]) -> dict[int, int]:
    return await _counts_by_parent(db, IPAddress.device_id, IPAddress.id, device_ids)


async def _name_map(
    db: AsyncSession,
    entity_ids: Sequence[int],
    model: type[Site] | type[Rack] | type[Device],
) -> dict[int, str]:
    """``{id: name}`` for one of the inventory entity types."""
    if not entity_ids:
        return {}
    stmt = select(model.id, model.name).where(model.id.in_(entity_ids))
    return {int(row[0]): str(row[1]) for row in (await db.execute(stmt)).all()}


async def _sites_read_bulk(db: AsyncSession, sites: Sequence[Site]) -> list[SiteRead]:
    ids = [s.id for s in sites]
    # Sequential: AsyncSession does not support concurrent use (asyncio.gather
    # over one session races on connection provisioning).
    rack_counts = await _rack_count_by_site(db, ids)
    device_counts = await _device_count_by_site(db, ids)
    return [
        SiteRead.model_validate(site).model_copy(
            update={
                "rack_count": rack_counts.get(site.id, 0),
                "device_count": device_counts.get(site.id, 0),
            }
        )
        for site in sites
    ]


async def _racks_read_bulk(db: AsyncSession, racks: Sequence[Rack]) -> list[RackRead]:
    ids = [r.id for r in racks]
    # Sequential: AsyncSession does not support concurrent use.
    site_names = await _name_map(db, [r.site_id for r in racks], Site)
    device_counts = await _device_count_by_rack(db, ids)
    return [
        RackRead.model_validate(rack).model_copy(
            update={
                "site_name": site_names.get(rack.site_id, ""),
                "device_count": device_counts.get(rack.id, 0),
            }
        )
        for rack in racks
    ]


async def _devices_read_bulk(db: AsyncSession, devices: Sequence[Device]) -> list[DeviceRead]:
    ids = [d.id for d in devices]
    # Sequential: AsyncSession does not support concurrent use.
    site_names = await _name_map(db, [d.site_id for d in devices], Site)
    rack_names = await _name_map(db, [d.rack_id for d in devices if d.rack_id], Rack)
    ip_counts = await _ip_count_by_device(db, ids)
    return [
        DeviceRead.model_validate(device).model_copy(
            update={
                "site_name": site_names.get(device.site_id, ""),
                "rack_name": rack_names.get(device.rack_id) if device.rack_id else None,
                "ip_count": ip_counts.get(device.id, 0),
            }
        )
        for device in devices
    ]


async def _ips_read_bulk(db: AsyncSession, ips: Sequence[IPAddress]) -> list[IPAddressRead]:
    device_names = await _name_map(db, [ip.device_id for ip in ips if ip.device_id], Device)
    return [
        IPAddressRead.model_validate(ip).model_copy(
            update={"device_name": device_names.get(ip.device_id) if ip.device_id else None}
        )
        for ip in ips
    ]


# ------------------------------------------------------------------ single-row helpers
async def _site_read(db: AsyncSession, site: Site) -> SiteRead:
    rack_count = await db.scalar(select(func.count(Rack.id)).where(Rack.site_id == site.id)) or 0
    device_count = (
        await db.scalar(select(func.count(Device.id)).where(Device.site_id == site.id)) or 0
    )
    return SiteRead.model_validate(site).model_copy(
        update={"rack_count": rack_count, "device_count": device_count}
    )


async def _rack_read(db: AsyncSession, rack: Rack) -> RackRead:
    site = await db.get(Site, rack.site_id)
    device_count = (
        await db.scalar(select(func.count(Device.id)).where(Device.rack_id == rack.id)) or 0
    )
    return RackRead.model_validate(rack).model_copy(
        update={"site_name": site.name if site else "", "device_count": device_count}
    )


async def _device_read(db: AsyncSession, device: Device) -> DeviceRead:
    site = await db.get(Site, device.site_id)
    rack = await db.get(Rack, device.rack_id) if device.rack_id else None
    ip_count = (
        await db.scalar(select(func.count(IPAddress.id)).where(IPAddress.device_id == device.id))
        or 0
    )
    return DeviceRead.model_validate(device).model_copy(
        update={
            "site_name": site.name if site else "",
            "rack_name": rack.name if rack else None,
            "ip_count": ip_count,
        }
    )


async def _ip_read(ip: IPAddress) -> IPAddressRead:
    device = await ip.awaitable_attrs.device if ip.device_id else None
    return IPAddressRead.model_validate(ip).model_copy(
        update={"device_name": device.name if device else None}
    )


# ------------------------------------------------------------------ overview
@router.get("/overview", response_model=InventoryOverview)
async def inventory_overview(db: DB) -> InventoryOverview:
    """Counts of every inventory entity (NetBox-style health summary)."""
    counts = {
        "sites": await db.scalar(select(func.count(Site.id))) or 0,
        "racks": await db.scalar(select(func.count(Rack.id))) or 0,
        "devices": await db.scalar(select(func.count(Device.id))) or 0,
        "ip_addresses": await db.scalar(select(func.count(IPAddress.id))) or 0,
        "active_devices": (
            await db.scalar(select(func.count(Device.id)).where(Device.status == "active")) or 0
        ),
    }
    return InventoryOverview(**counts)


# ------------------------------------------------------------------ sites
@router.get("/sites", response_model=list[SiteRead])
async def list_sites(
    db: DB,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[SiteRead]:
    sites = (await db.scalars(select(Site).order_by(Site.name).limit(limit).offset(offset))).all()
    return await _sites_read_bulk(db, sites)


@router.post(
    "/sites",
    response_model=SiteRead,
    status_code=201,
    dependencies=[Depends(require_write)],
)
async def create_site(payload: SiteCreate, db: DB) -> SiteRead:
    site = Site(**payload.model_dump())
    db.add(site)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise conflict_handler(exc) from exc
    await db.refresh(site)
    activity_log.record("site", "created", site.name)
    return await _site_read(db, site)


@router.get("/sites/{site_id}", response_model=SiteRead)
async def get_site(site_id: int, db: DB) -> SiteRead:
    site = await get_or_404(db, Site, site_id, "Site")
    return await _site_read(db, site)


@router.put(
    "/sites/{site_id}",
    response_model=SiteRead,
    dependencies=[Depends(require_write)],
)
async def update_site(site_id: int, payload: SiteUpdate, db: DB) -> SiteRead:
    site = await get_or_404(db, Site, site_id, "Site")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(site, key, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise conflict_handler(exc) from exc
    await db.refresh(site)
    activity_log.record("site", "updated", site.name)
    return await _site_read(db, site)


@router.delete(
    "/sites/{site_id}",
    status_code=204,
    dependencies=[Depends(require_write)],
)
async def delete_site(site_id: int, db: DB) -> None:
    site = await get_or_404(db, Site, site_id, "Site")
    await db.delete(site)
    await db.commit()
    activity_log.record("site", "deleted", site.name)


# ------------------------------------------------------------------ racks
@router.get("/racks", response_model=list[RackRead])
async def list_racks(
    db: DB,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[RackRead]:
    racks = (await db.scalars(select(Rack).order_by(Rack.name).limit(limit).offset(offset))).all()
    return await _racks_read_bulk(db, racks)


@router.post(
    "/racks",
    response_model=RackRead,
    status_code=201,
    dependencies=[Depends(require_write)],
)
async def create_rack(payload: RackCreate, db: DB) -> RackRead:
    rack = Rack(**payload.model_dump())
    db.add(rack)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise conflict_handler(exc) from exc
    await db.refresh(rack)
    activity_log.record("rack", "created", rack.name)
    return await _rack_read(db, rack)


@router.get("/racks/{rack_id}", response_model=RackRead)
async def get_rack(rack_id: int, db: DB) -> RackRead:
    rack = await get_or_404(db, Rack, rack_id, "Rack")
    return await _rack_read(db, rack)


@router.put(
    "/racks/{rack_id}",
    response_model=RackRead,
    dependencies=[Depends(require_write)],
)
async def update_rack(rack_id: int, payload: RackUpdate, db: DB) -> RackRead:
    rack = await get_or_404(db, Rack, rack_id, "Rack")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(rack, key, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise conflict_handler(exc) from exc
    await db.refresh(rack)
    activity_log.record("rack", "updated", rack.name)
    return await _rack_read(db, rack)


@router.delete(
    "/racks/{rack_id}",
    status_code=204,
    dependencies=[Depends(require_write)],
)
async def delete_rack(rack_id: int, db: DB) -> None:
    rack = await get_or_404(db, Rack, rack_id, "Rack")
    await db.delete(rack)
    await db.commit()
    activity_log.record("rack", "deleted", rack.name)


# ------------------------------------------------------------------ devices
@router.get("/devices", response_model=list[DeviceRead])
async def list_devices(
    db: DB,
    status: str | None = Query(default=None),
    site_id: int | None = Query(default=None, alias="site"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[DeviceRead]:
    stmt = select(Device)
    if status:
        stmt = stmt.where(Device.status == status)
    if site_id:
        stmt = stmt.where(Device.site_id == site_id)
    devices = (await db.scalars(stmt.order_by(Device.name).limit(limit).offset(offset))).all()
    return await _devices_read_bulk(db, devices)


@router.post(
    "/devices",
    response_model=DeviceRead,
    status_code=201,
    dependencies=[Depends(require_write)],
)
async def create_device(payload: DeviceCreate, db: DB) -> DeviceRead:
    device = Device(**payload.model_dump())
    db.add(device)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise conflict_handler(exc) from exc
    await db.refresh(device)
    activity_log.record("device", "created", device.name)
    return await _device_read(db, device)


@router.get("/devices/{device_id}", response_model=DeviceRead)
async def get_device(device_id: int, db: DB) -> DeviceRead:
    device = await get_or_404(db, Device, device_id, "Device")
    return await _device_read(db, device)


@router.put(
    "/devices/{device_id}",
    response_model=DeviceRead,
    dependencies=[Depends(require_write)],
)
async def update_device(device_id: int, payload: DeviceUpdate, db: DB) -> DeviceRead:
    device = await get_or_404(db, Device, device_id, "Device")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(device, key, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise conflict_handler(exc) from exc
    await db.refresh(device)
    activity_log.record("device", "updated", device.name)
    return await _device_read(db, device)


@router.delete(
    "/devices/{device_id}",
    status_code=204,
    dependencies=[Depends(require_write)],
)
async def delete_device(device_id: int, db: DB) -> None:
    device = await get_or_404(db, Device, device_id, "Device")
    await db.delete(device)
    await db.commit()
    activity_log.record("device", "deleted", device.name)


# ------------------------------------------------------------------ ip addresses
@router.get("/ip-addresses", response_model=list[IPAddressRead])
async def list_ip_addresses(
    db: DB,
    status: str | None = Query(default=None),
    device_id: int | None = Query(default=None, alias="device"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[IPAddressRead]:
    stmt = select(IPAddress)
    if status:
        stmt = stmt.where(IPAddress.status == status)
    if device_id:
        stmt = stmt.where(IPAddress.device_id == device_id)
    ips = (await db.scalars(stmt.order_by(IPAddress.address).limit(limit).offset(offset))).all()
    return await _ips_read_bulk(db, ips)


@router.post(
    "/ip-addresses",
    response_model=IPAddressRead,
    status_code=201,
    dependencies=[Depends(require_write)],
)
async def create_ip_address(payload: IPAddressCreate, db: DB) -> IPAddressRead:
    ip = IPAddress(**payload.model_dump())
    db.add(ip)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise conflict_handler(exc) from exc
    await db.refresh(ip)
    activity_log.record("ip-address", "created", ip.address)
    return await _ip_read(ip)


@router.get("/ip-addresses/{ip_id}", response_model=IPAddressRead)
async def get_ip_address(ip_id: int, db: DB) -> IPAddressRead:
    ip = await get_or_404(db, IPAddress, ip_id, "IP address")
    return await _ip_read(ip)


@router.put(
    "/ip-addresses/{ip_id}",
    response_model=IPAddressRead,
    dependencies=[Depends(require_write)],
)
async def update_ip_address(ip_id: int, payload: IPAddressUpdate, db: DB) -> IPAddressRead:
    ip = await get_or_404(db, IPAddress, ip_id, "IP address")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(ip, key, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise conflict_handler(exc) from exc
    await db.refresh(ip)
    activity_log.record("ip-address", "updated", ip.address)
    return await _ip_read(ip)


@router.delete(
    "/ip-addresses/{ip_id}",
    status_code=204,
    dependencies=[Depends(require_write)],
)
async def delete_ip_address(ip_id: int, db: DB) -> None:
    ip = await get_or_404(db, IPAddress, ip_id, "IP address")
    await db.delete(ip)
    await db.commit()
    activity_log.record("ip-address", "deleted", ip.address)


# ------------------------------------------------------------------ global search
@router.get("/search", response_model=SearchResult)
async def search_inventory(
    db: DB,
    q: str = Query(min_length=1, max_length=200),
) -> SearchResult:
    """NetBox-style global search across devices, IPs, sites and racks.

    The four entity queries run concurrently; relations/counts are resolved
    with bulk aggregates instead of per-row lookups.
    """
    term = f"%{q.strip()}%"

    device_stmt = (
        select(Device)
        .where(
            or_(
                Device.name.ilike(term),
                Device.serial.ilike(term),
                Device.asset_tag.ilike(term),
                Device.device_type.ilike(term),
                Device.model.ilike(term),
            )
        )
        .limit(25)
    )
    ip_stmt = (
        select(IPAddress)
        .where(
            or_(
                IPAddress.address.ilike(term),
                IPAddress.dns_name.ilike(term),
            )
        )
        .limit(25)
    )
    site_stmt = select(Site).where(or_(Site.name.ilike(term), Site.slug.ilike(term))).limit(10)
    rack_stmt = select(Rack).where(Rack.name.ilike(term)).limit(10)

    # Sequential: AsyncSession does not support concurrent use (asyncio.gather
    # over one session races on connection provisioning).
    device_rows = await db.scalars(device_stmt)
    ip_rows = await db.scalars(ip_stmt)
    site_rows = await db.scalars(site_stmt)
    rack_rows = await db.scalars(rack_stmt)

    return SearchResult(
        devices=await _devices_read_bulk(db, device_rows.all()),
        ip_addresses=await _ips_read_bulk(db, ip_rows.all()),
        sites=await _sites_read_bulk(db, site_rows.all()),
        racks=await _racks_read_bulk(db, rack_rows.all()),
    )
