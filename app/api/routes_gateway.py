"""API Gateway management: gateways, routes, consumers, keys, TLS, logs, tests.

All endpoints live under ``/api/gateway`` and are principal-gated; mutations
require at least ``operator``. The public hot path (``/gw/<slug>/...``) lives
in :mod:`app.api.gateway_proxy`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, select

from .. import models, schemas
from ..deps import DB, Operator, get_principal
from ..services import gateway_service
from ..timeutil import utcnow

router = APIRouter(
    prefix="/gateway",
    tags=["gateway"],
    dependencies=[Depends(get_principal)],
)


async def _gateway_or_404(db: DB, gateway_id: int) -> models.Gateway:
    gw = await db.get(models.Gateway, gateway_id)
    if gw is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Gateway not found")
    return gw


async def _route_or_404(db: DB, route_id: int) -> models.GatewayRoute:
    route = await db.get(models.GatewayRoute, route_id)
    if route is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Route not found")
    return route


async def _consumer_or_404(db: DB, consumer_id: int) -> models.GatewayConsumer:
    consumer = await db.get(models.GatewayConsumer, consumer_id)
    if consumer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Consumer not found")
    return consumer


async def _key_or_404(db: DB, key_id: int) -> models.GatewayApiKey:
    key = await db.get(models.GatewayApiKey, key_id)
    if key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")
    return key


async def _read_gateway(db: DB, gw: models.Gateway) -> schemas.GatewayRead:
    route_count = (
        await db.scalar(
            select(func.count(models.GatewayRoute.id)).where(
                models.GatewayRoute.gateway_id == gw.id
            )
        )
        or 0
    )
    consumer_count = (
        await db.scalar(
            select(func.count(models.GatewayConsumer.id)).where(
                models.GatewayConsumer.gateway_id == gw.id
            )
        )
        or 0
    )
    return schemas.GatewayRead(
        id=gw.id,
        slug=gw.slug,
        name=gw.name,
        description=gw.description,
        enabled=gw.enabled,
        base_url=gw.base_url,
        tls_domain=gw.tls_domain,
        tls_enabled=gw.tls_enabled,
        issuer=gw.issuer,
        cert_id=gw.cert_id,
        tls_status=await gateway_service.tls_status(db, gw),
        tls_error=gw.tls_error,
        route_count=route_count,
        consumer_count=consumer_count,
        created_at=gw.created_at,
        updated_at=gw.updated_at,
    )


@router.get("", response_model=list[schemas.GatewayRead])
async def list_gateways(db: DB) -> list[schemas.GatewayRead]:
    gws = (await db.scalars(select(models.Gateway).order_by(models.Gateway.name))).all()
    return [await _read_gateway(db, gw) for gw in gws]


@router.post("", response_model=schemas.GatewayRead, status_code=status.HTTP_201_CREATED)
async def create_gateway(
    payload: schemas.GatewayCreate, db: DB, _op: Operator
) -> schemas.GatewayRead:
    existing = await gateway_service.get_gateway_by_slug(db, payload.slug)
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Slug /gw/{payload.slug} is already in use")
    gw = models.Gateway(
        slug=payload.slug,
        name=payload.name,
        description=payload.description,
        enabled=payload.enabled,
        base_url=payload.base_url,
        tls_domain=payload.tls_domain,
        tls_enabled=payload.tls_enabled,
        issuer=payload.issuer,
        tls_status="none",
    )
    db.add(gw)
    await db.flush()
    if gw.tls_enabled and gw.tls_domain.strip():
        try:
            await gateway_service.issue_tls_cert(db, gw)
        except ValueError:
            # Gateway still created; tls_status/tls_error already recorded.
            pass
    await db.commit()
    await db.refresh(gw)
    return await _read_gateway(db, gw)


@router.get("/logs", response_model=list[schemas.GatewayLogRead])
async def list_logs(
    db: DB,
    gateway_id: int | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[schemas.GatewayLogRead]:
    stmt = select(models.GatewayLog).order_by(models.GatewayLog.id.desc()).limit(limit)
    if gateway_id is not None:
        stmt = stmt.where(models.GatewayLog.gateway_id == gateway_id)
    rows = (await db.scalars(stmt)).all()
    return [schemas.GatewayLogRead.model_validate(r, from_attributes=True) for r in rows]


@router.delete("/logs")
async def clear_logs(db: DB, _op: Operator, gateway_id: int | None = None) -> dict[str, int]:
    stmt = delete(models.GatewayLog)
    if gateway_id is not None:
        stmt = stmt.where(models.GatewayLog.gateway_id == gateway_id)
    result = await db.execute(stmt)
    await db.commit()
    return {"deleted": result.rowcount or 0}


@router.post("/test", response_model=schemas.GatewayDirectTestResult)
async def run_direct_test(
    payload: schemas.GatewayDirectTest, db: DB, _op: Operator
) -> schemas.GatewayDirectTestResult:
    return await gateway_service.direct_test(payload)


@router.get("/{gateway_id}", response_model=schemas.GatewayRead)
async def get_gateway(gateway_id: int, db: DB) -> schemas.GatewayRead:
    return await _read_gateway(db, await _gateway_or_404(db, gateway_id))


@router.patch("/{gateway_id}", response_model=schemas.GatewayRead)
async def update_gateway(
    gateway_id: int, payload: schemas.GatewayUpdate, db: DB, _op: Operator
) -> schemas.GatewayRead:
    gw = await _gateway_or_404(db, gateway_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(gw, field, value)
    await db.commit()
    await db.refresh(gw)
    return await _read_gateway(db, gw)


@router.delete("/{gateway_id}")
async def delete_gateway(gateway_id: int, db: DB, _op: Operator) -> dict[str, bool]:
    gw = await _gateway_or_404(db, gateway_id)
    # SQLite by default does NOT enforce FK cascades, so children must be
    # removed explicitly — otherwise orphaned consumers/keys would remain
    # live (their hashes would still authenticate through the proxy).
    consumer_ids = (
        await db.scalars(
            select(models.GatewayConsumer.id).where(models.GatewayConsumer.gateway_id == gateway_id)
        )
    ).all()
    if consumer_ids:
        key_hashes = (
            await db.scalars(
                select(models.GatewayApiKey.key_hash).where(
                    models.GatewayApiKey.consumer_id.in_(consumer_ids)
                )
            )
        ).all()
        for kh in key_hashes:
            gateway_service.invalidate_key(kh)
        await db.execute(
            delete(models.GatewayApiKey).where(models.GatewayApiKey.consumer_id.in_(consumer_ids))
        )
        await db.execute(
            delete(models.GatewayConsumer).where(models.GatewayConsumer.gateway_id == gateway_id)
        )
    await db.execute(
        delete(models.GatewayRoute).where(models.GatewayRoute.gateway_id == gateway_id)
    )
    await db.execute(delete(models.GatewayLog).where(models.GatewayLog.gateway_id == gateway_id))
    await db.delete(gw)
    await db.commit()
    return {"ok": True}


# --- TLS actions -----------------------------------------------------------


@router.post("/{gateway_id}/issue-tls", response_model=schemas.GatewayRead)
async def issue_gateway_tls(gateway_id: int, db: DB, _op: Operator) -> schemas.GatewayRead:
    gw = await _gateway_or_404(db, gateway_id)
    if not gw.tls_domain.strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Set a TLS domain on the gateway first"
        )
    try:
        await gateway_service.issue_tls_cert(db, gw)
    except ValueError as exc:
        await db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await db.commit()
    await db.refresh(gw)
    return await _read_gateway(db, gw)


@router.post("/{gateway_id}/renew-tls", response_model=schemas.GatewayRead)
async def renew_gateway_tls(gateway_id: int, db: DB, _op: Operator) -> schemas.GatewayRead:
    gw = await _gateway_or_404(db, gateway_id)
    if not gw.tls_enabled:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "TLS is not enabled on this gateway"
        )
    try:
        await gateway_service.renew_tls_cert(db, gw)
    except ValueError as exc:
        await db.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await db.commit()
    await db.refresh(gw)
    return await _read_gateway(db, gw)


# --- Routes ----------------------------------------------------------------


@router.get("/{gateway_id}/routes", response_model=list[schemas.GatewayRouteRead])
async def list_routes(gateway_id: int, db: DB) -> list[schemas.GatewayRouteRead]:
    await _gateway_or_404(db, gateway_id)
    routes = await gateway_service.load_routes(db, gateway_id)
    return [schemas.GatewayRouteRead.model_validate(r, from_attributes=True) for r in routes]


@router.post(
    "/{gateway_id}/routes",
    response_model=schemas.GatewayRouteRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_route(
    gateway_id: int, payload: schemas.GatewayRouteCreate, db: DB, _op: Operator
) -> schemas.GatewayRouteRead:
    await _gateway_or_404(db, gateway_id)
    route = models.GatewayRoute(gateway_id=gateway_id, **payload.model_dump())
    db.add(route)
    await db.commit()
    await db.refresh(route)
    return schemas.GatewayRouteRead.model_validate(route, from_attributes=True)


@router.get("/routes/{route_id}", response_model=schemas.GatewayRouteRead)
async def get_route(route_id: int, db: DB) -> schemas.GatewayRouteRead:
    return schemas.GatewayRouteRead.model_validate(
        await _route_or_404(db, route_id), from_attributes=True
    )


@router.patch("/routes/{route_id}", response_model=schemas.GatewayRouteRead)
async def update_route(
    route_id: int, payload: schemas.GatewayRouteUpdate, db: DB, _op: Operator
) -> schemas.GatewayRouteRead:
    route = await _route_or_404(db, route_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(route, field, value)
    await db.commit()
    await db.refresh(route)
    return schemas.GatewayRouteRead.model_validate(route, from_attributes=True)


@router.delete("/routes/{route_id}")
async def delete_route(route_id: int, db: DB, _op: Operator) -> dict[str, bool]:
    route = await _route_or_404(db, route_id)
    await db.delete(route)
    await db.commit()
    return {"ok": True}


# --- Consumers -------------------------------------------------------------


@router.get("/{gateway_id}/consumers", response_model=list[schemas.GatewayConsumerRead])
async def list_consumers(gateway_id: int, db: DB) -> list[schemas.GatewayConsumerRead]:
    await _gateway_or_404(db, gateway_id)
    consumers = (
        await db.scalars(
            select(models.GatewayConsumer)
            .where(models.GatewayConsumer.gateway_id == gateway_id)
            .order_by(models.GatewayConsumer.name)
        )
    ).all()
    result: list[schemas.GatewayConsumerRead] = []
    for consumer in consumers:
        key_count = (
            await db.scalar(
                select(func.count(models.GatewayApiKey.id)).where(
                    models.GatewayApiKey.consumer_id == consumer.id
                )
            )
            or 0
        )
        result.append(
            schemas.GatewayConsumerRead(
                id=consumer.id,
                gateway_id=consumer.gateway_id,
                name=consumer.name,
                description=consumer.description,
                enabled=consumer.enabled,
                key_count=key_count,
                created_at=consumer.created_at,
            )
        )
    return result


@router.post(
    "/{gateway_id}/consumers",
    response_model=schemas.GatewayConsumerRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_consumer(
    gateway_id: int, payload: schemas.GatewayConsumerCreate, db: DB, _op: Operator
) -> schemas.GatewayConsumerRead:
    await _gateway_or_404(db, gateway_id)
    consumer = models.GatewayConsumer(gateway_id=gateway_id, **payload.model_dump())
    db.add(consumer)
    await db.commit()
    await db.refresh(consumer)
    return schemas.GatewayConsumerRead(
        id=consumer.id,
        gateway_id=consumer.gateway_id,
        name=consumer.name,
        description=consumer.description,
        enabled=consumer.enabled,
        key_count=0,
        created_at=consumer.created_at,
    )


@router.patch("/consumers/{consumer_id}", response_model=schemas.GatewayConsumerRead)
async def update_consumer(
    consumer_id: int, payload: schemas.GatewayConsumerUpdate, db: DB, _op: Operator
) -> schemas.GatewayConsumerRead:
    consumer = await _consumer_or_404(db, consumer_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(consumer, field, value)
    await db.commit()
    await db.refresh(consumer)
    return schemas.GatewayConsumerRead(
        id=consumer.id,
        gateway_id=consumer.gateway_id,
        name=consumer.name,
        description=consumer.description,
        enabled=consumer.enabled,
        key_count=(
            await db.scalar(
                select(func.count(models.GatewayApiKey.id)).where(
                    models.GatewayApiKey.consumer_id == consumer.id
                )
            )
            or 0
        ),
        created_at=consumer.created_at,
    )


@router.delete("/consumers/{consumer_id}")
async def delete_consumer(consumer_id: int, db: DB, _op: Operator) -> dict[str, bool]:
    consumer = await _consumer_or_404(db, consumer_id)
    key_hashes = (
        await db.scalars(
            select(models.GatewayApiKey.key_hash).where(
                models.GatewayApiKey.consumer_id == consumer_id
            )
        )
    ).all()
    for kh in key_hashes:
        gateway_service.invalidate_key(kh)
    # Remove key rows explicitly (SQLite FK cascade is not enforced by default;
    # leaving them would let the old keys keep authenticating via the proxy).
    await db.execute(
        delete(models.GatewayApiKey).where(models.GatewayApiKey.consumer_id == consumer_id)
    )
    await db.delete(consumer)
    await db.commit()
    return {"ok": True}


# --- API keys --------------------------------------------------------------


@router.post(
    "/consumers/{consumer_id}/keys",
    response_model=schemas.GatewayApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_key(
    consumer_id: int, payload: schemas.GatewayApiKeyCreate, db: DB, _op: Operator
) -> schemas.GatewayApiKeyCreated:
    _consumer = await _consumer_or_404(db, consumer_id)
    plain = payload.key.strip() if payload.key else gateway_service.generate_api_key()
    if len(plain) < gateway_service.MIN_KEY_LENGTH:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"API keys must be at least {gateway_service.MIN_KEY_LENGTH} characters",
        )
    key_hash = gateway_service.hash_api_key(plain)
    if (
        await db.scalar(
            select(func.count(models.GatewayApiKey.id)).where(
                models.GatewayApiKey.key_hash == key_hash
            )
        )
        or 0
    ) > 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "That API key is already registered")
    row = models.GatewayApiKey(
        consumer_id=consumer_id,
        key_hash=key_hash,
        key_prefix=gateway_service.key_prefix(plain),
        label=payload.label,
        enabled=True,
        expires_at=payload.expires_at,
        created_at=utcnow(),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    gateway_service.invalidate_key(key_hash)  # ensure no stale cache entry
    return schemas.GatewayApiKeyCreated(
        id=row.id,
        consumer_id=consumer_id,
        key=plain,
        key_prefix=row.key_prefix,
        label=row.label,
        expires_at=row.expires_at,
    )


@router.get("/consumers/{consumer_id}/keys", response_model=list[schemas.GatewayApiKeyRead])
async def list_keys(consumer_id: int, db: DB) -> list[schemas.GatewayApiKeyRead]:
    await _consumer_or_404(db, consumer_id)
    rows = (
        await db.scalars(
            select(models.GatewayApiKey)
            .where(models.GatewayApiKey.consumer_id == consumer_id)
            .order_by(models.GatewayApiKey.id.desc())
        )
    ).all()
    return [schemas.GatewayApiKeyRead.model_validate(r, from_attributes=True) for r in rows]


@router.patch("/keys/{key_id}", response_model=schemas.GatewayApiKeyRead)
async def update_key(
    key_id: int, payload: schemas.GatewayApiKeyUpdate, db: DB, _op: Operator
) -> schemas.GatewayApiKeyRead:
    key = await _key_or_404(db, key_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(key, field, value)
    await db.commit()
    await db.refresh(key)
    gateway_service.invalidate_key(key.key_hash)
    return schemas.GatewayApiKeyRead.model_validate(key, from_attributes=True)


@router.post("/keys/{key_id}/rotate", response_model=schemas.GatewayApiKeyCreated)
async def rotate_key(key_id: int, db: DB, _op: Operator) -> schemas.GatewayApiKeyCreated:
    key = await _key_or_404(db, key_id)
    gateway_service.invalidate_key(key.key_hash)
    plain = gateway_service.generate_api_key()
    key.key_hash = gateway_service.hash_api_key(plain)
    key.key_prefix = gateway_service.key_prefix(plain)
    key.enabled = True
    await db.commit()
    await db.refresh(key)
    return schemas.GatewayApiKeyCreated(
        id=key.id,
        consumer_id=key.consumer_id,
        key=plain,
        key_prefix=key.key_prefix,
        label=key.label,
        expires_at=key.expires_at,
    )


@router.delete("/keys/{key_id}")
async def delete_key(key_id: int, db: DB, _op: Operator) -> dict[str, bool]:
    key = await _key_or_404(db, key_id)
    gateway_service.invalidate_key(key.key_hash)
    await db.delete(key)
    await db.commit()
    return {"ok": True}
