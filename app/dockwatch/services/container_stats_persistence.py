"""Persistence for container stats snapshots (per-container resource history).

Stores one snapshot per running container on each poll tick so the container
ranking endpoint can query average/peak CPU and memory over a trailing window.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult

from app.dockwatch.database import get_session_factory
from app.dockwatch.models.container_stats import ContainerStatsSnapshot


async def insert_container_stats_snapshot(
    short_id: str,
    name: str,
    image: str,
    stats: dict[str, Any],
    endpoint_id: int | None = None,
) -> None:
    """Persist one container stats sample (upsert on ts+short_id)."""
    async with get_session_factory()() as session:
        row = ContainerStatsSnapshot(
            ts=stats.get("stats_ts", 0),
            short_id=short_id,
            name=name,
            image=image,
            cpu_percent=float(stats.get("cpu_percent", 0) or 0),
            memory_percent=float(stats.get("memory_percent", 0) or 0),
            memory_usage=int(stats.get("memory_usage", 0) or 0),
            memory_limit=int(stats.get("memory_limit", 0) or 0),
            network_rx=int(stats.get("network_rx", 0) or 0),
            network_tx=int(stats.get("network_tx", 0) or 0),
            endpoint_id=endpoint_id,
        )
        session.add(row)
        await session.commit()


async def fetch_container_ranking(
    range_seconds: int = 300,
    limit: int = 10,
    endpoint_id: int | None = None,
) -> list[dict[str, Any]]:
    """Top containers by avg CPU (then memory) over the trailing range.

    Returns [{short_id, name, image, avg_cpu, avg_mem, peak_cpu, net_rx, net_tx}].
    """
    from datetime import UTC, datetime, timedelta

    cutoff_ms = int(
        (datetime.now(UTC) - timedelta(seconds=max(range_seconds, 60))).timestamp() * 1000
    )
    stmt = (
        select(
            ContainerStatsSnapshot.short_id,
            ContainerStatsSnapshot.name,
            ContainerStatsSnapshot.image,
            func.avg(ContainerStatsSnapshot.cpu_percent).label("avg_cpu"),
            func.avg(ContainerStatsSnapshot.memory_percent).label("avg_mem"),
            func.max(ContainerStatsSnapshot.cpu_percent).label("peak_cpu"),
            func.sum(ContainerStatsSnapshot.network_rx).label("net_rx"),
            func.sum(ContainerStatsSnapshot.network_tx).label("net_tx"),
        )
        .where(ContainerStatsSnapshot.ts >= cutoff_ms)
        .group_by(ContainerStatsSnapshot.short_id)
        .order_by(func.avg(ContainerStatsSnapshot.cpu_percent).desc())
        .limit(limit)
    )
    if endpoint_id is not None:
        stmt = stmt.where(ContainerStatsSnapshot.endpoint_id == endpoint_id)
    else:
        stmt = stmt.where(ContainerStatsSnapshot.endpoint_id.is_(None))
    async with get_session_factory()() as session:
        rows = (await session.execute(stmt)).all()
    return [
        {
            "short_id": r.short_id,
            "name": r.name,
            "image": r.image,
            "avg_cpu": round(float(r.avg_cpu or 0), 1),
            "avg_mem": round(float(r.avg_mem or 0), 1),
            "peak_cpu": round(float(r.peak_cpu or 0), 1),
            "net_rx": int(r.net_rx or 0),
            "net_tx": int(r.net_tx or 0),
        }
        for r in rows
    ]


async def prune_old_container_stats(days: int = 7, endpoint_id: int | None = None) -> int:
    """Delete container stats snapshots older than ``days``; returns rows removed."""
    from datetime import UTC, datetime, timedelta

    cutoff_ms = int((datetime.now(UTC) - timedelta(days=days, minutes=5)).timestamp() * 1000)
    stmt = delete(ContainerStatsSnapshot).where(ContainerStatsSnapshot.ts < cutoff_ms)
    if endpoint_id is not None:
        stmt = stmt.where(ContainerStatsSnapshot.endpoint_id == endpoint_id)
    async with get_session_factory()() as session:
        result = await session.execute(stmt)
        await session.commit()
    return cast("CursorResult[Any]", result).rowcount or 0
