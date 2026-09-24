"""Persistence for persisted host-monitoring samples (SQLite via SQLAlchemy).

The background sampler calls :func:`insert_sample` on every tick so history
survives server restarts. On startup the sampler seeds the in-memory buffer
from :func:`fetch_recent`.
"""

from __future__ import annotations

import json
import logging
import socket
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict, cast

from sqlalchemy import Integer, delete, func, select
from sqlalchemy.engine import CursorResult

from app.dockwatch.database import get_session_factory
from app.dockwatch.models.monitor import MonitorSample
from app.dockwatch.services.monitor_service import MonitorSampleDict

logger = logging.getLogger(__name__)

#: How many samples to restore into memory on startup.
MAX_RECENT = 300


class AnomalyDict(TypedDict):
    t: int
    cpu: float
    cpu_anomaly: bool
    z_cpu: float
    mem_percent: float
    mem_anomaly: bool
    z_mem: float
    endpoint_id: int | None


def _host() -> str:
    return socket.gethostname()


def _to_row(sample: MonitorSampleDict, endpoint_id: int | None = None) -> MonitorSample:
    """Flatten an in-memory sample into a database row."""
    return MonitorSample(
        ts=sample["t"],
        cpu=sample["cpu"],
        mem_percent=sample["memory"]["percent"],
        mem_used=sample["memory"]["used"],
        mem_total=sample["memory"]["total"],
        swap_percent=sample["swap"]["percent"],
        swap_used=sample["swap"]["used"],
        swap_total=sample["swap"]["total"],
        load1=sample["load1"],
        load5=sample["load5"],
        load15=sample["load15"],
        net_rx=sample["net"]["rx"],
        net_tx=sample["net"]["tx"],
        disk_read=sample["disk"]["read"],
        disk_write=sample["disk"]["write"],
        z_cpu=sample.get("z_cpu", 0.0),
        z_mem=sample.get("z_mem", 0.0),
        cpu_anomaly=sample.get("cpu_anomaly", False),
        mem_anomaly=sample.get("mem_anomaly", False),
        cores=json.dumps(sample.get("cores", [])),
        host=_host(),
        endpoint_id=endpoint_id,
    )


def _from_row(row: MonitorSample) -> MonitorSampleDict:
    """Restore an in-memory sample from a database row."""
    return {
        "t": row.ts,
        "cpu": row.cpu,
        "cores": json.loads(row.cores),
        "memory": {
            "percent": row.mem_percent,
            "used": row.mem_used,
            "total": row.mem_total,
        },
        "swap": {
            "percent": row.swap_percent,
            "used": row.swap_used,
            "total": row.swap_total,
        },
        "load1": row.load1,
        "load5": row.load5,
        "load15": row.load15,
        "net": {"rx": row.net_rx, "tx": row.net_tx},
        "disk": {"read": row.disk_read, "write": row.disk_write},
        "z_cpu": row.z_cpu,
        "z_mem": row.z_mem,
        "cpu_anomaly": row.cpu_anomaly,
        "mem_anomaly": row.mem_anomaly,
    }


async def insert_sample(sample: MonitorSampleDict, endpoint_id: int | None = None) -> None:
    """Persist one sample. Failures are logged, never raised to the caller."""
    await insert_samples([sample], endpoint_id=endpoint_id)


async def insert_samples(
    samples: Sequence[MonitorSampleDict], endpoint_id: int | None = None
) -> None:
    """Bulk-persist samples in a single session/transaction.

    Failures are logged, never raised to the caller.
    """
    if not samples:
        return
    try:
        async with get_session_factory()() as session:
            session.add_all(_to_row(sample, endpoint_id=endpoint_id) for sample in samples)
            await session.commit()
    except Exception:
        logger.exception("monitor samples bulk persistence failed")


async def fetch_recent(
    limit: int = MAX_RECENT, endpoint_id: int | None = None
) -> list[MonitorSampleDict]:
    """Most recent ``limit`` samples, oldest first (for restart recovery)."""
    stmt = select(MonitorSample).order_by(MonitorSample.id.desc()).limit(limit)
    if endpoint_id is not None:
        stmt = stmt.where(MonitorSample.endpoint_id == endpoint_id)
    else:
        stmt = stmt.where(MonitorSample.endpoint_id.is_(None))
    async with get_session_factory()() as session:
        rows = (await session.scalars(stmt)).all()
    return [_from_row(row) for row in reversed(rows)]


async def fetch_anomalies(limit: int = 10, endpoint_id: int | None = None) -> list[AnomalyDict]:
    """Most recent samples flagged as CPU or memory anomalies, oldest first."""
    stmt = (
        select(MonitorSample)
        .where(MonitorSample.cpu_anomaly.is_(True) | MonitorSample.mem_anomaly.is_(True))
        .order_by(MonitorSample.id.desc())
        .limit(limit)
    )
    if endpoint_id is not None:
        stmt = stmt.where(MonitorSample.endpoint_id == endpoint_id)
    async with get_session_factory()() as session:
        rows = (await session.scalars(stmt)).all()
    result: list[AnomalyDict] = []
    for row in reversed(rows):
        result.append(
            {
                "t": row.ts,
                "cpu": row.cpu,
                "cpu_anomaly": row.cpu_anomaly,
                "z_cpu": row.z_cpu,
                "mem_percent": row.mem_percent,
                "mem_anomaly": row.mem_anomaly,
                "z_mem": row.z_mem,
                "endpoint_id": row.endpoint_id,
            }
        )
    return result


def _bucket_key(ts: int, bucket_ms: int) -> int:
    """Epoch-aligned bucket start (ms) for ``ts`` (works for SQLite + Postgres)."""
    return (ts // bucket_ms) * bucket_ms


def _round(value: float | None, ndigits: int = 1) -> float:
    return round(value or 0.0, ndigits)


def bucket_samples(
    samples: Sequence[MonitorSampleDict], bucket_seconds: int
) -> list[dict[str, Any]]:
    """Bucket in-memory samples into the history shape (avg/max per bucket).

    Used as a lightweight fallback when no samples are persisted yet (fresh
    start) so the history view still renders from the rolling buffer.
    """
    bucket_ms = max(bucket_seconds, 1) * 1000

    def _avg(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    grouped: dict[int, list[MonitorSampleDict]] = {}
    for sample in samples:
        grouped.setdefault(_bucket_key(sample["t"], bucket_ms), []).append(sample)

    result: list[dict[str, Any]] = []
    for key in sorted(grouped):
        items = grouped[key]
        cpus = [s["cpu"] for s in items]
        mems = [s["memory"]["percent"] for s in items]
        swaps = [s["swap"]["percent"] for s in items]
        rxs = [s["net"]["rx"] for s in items]
        txs = [s["net"]["tx"] for s in items]
        reads = [s["disk"]["read"] for s in items]
        writes = [s["disk"]["write"] for s in items]
        loads = [s["load1"] for s in items]
        result.append(
            {
                "ts": key // 1000,
                "cpu": {"avg": _round(_avg(cpus)), "max": _round(max(cpus))},
                "mem": {"avg": _round(_avg(mems)), "max": _round(max(mems))},
                "swap": {"avg": _round(_avg(swaps))},
                "net": {
                    "rx_avg": _round(_avg(rxs)),
                    "rx_max": _round(max(rxs)),
                    "tx_avg": _round(_avg(txs)),
                    "tx_max": _round(max(txs)),
                },
                "disk": {
                    "read_avg": _round(_avg(reads)),
                    "read_max": _round(max(reads)),
                    "write_avg": _round(_avg(writes)),
                    "write_max": _round(max(writes)),
                },
                "load1": {"avg": _round(_avg(loads), 2), "max": _round(max(loads), 2)},
            }
        )
    return result


async def fetch_history(
    range_seconds: int,
    bucket_seconds: int,
    endpoint_id: int | None = None,
) -> list[dict[str, Any]]:
    """Downsampled metric history over the trailing ``range_seconds`` window.

    Samples are grouped into epoch-aligned ``bucket_seconds`` buckets with
    avg/max summaries for CPU, memory, swap, network, disk and load — enough
    for long-range charts and postmortem review without shipping raw samples.
    """
    bucket_ms = max(bucket_seconds, 1) * 1000
    cutoff_ms = int(
        (datetime.now(UTC) - timedelta(seconds=max(range_seconds, 60))).timestamp() * 1000
    )
    # Integer division is required for epoch-aligned buckets. SQLite treats
    # division involving a bound parameter as float division, so cast the
    # quotient back to Integer (floor for positive epoch values).
    bucket_expr = ((MonitorSample.ts / bucket_ms).cast(Integer) * bucket_ms).label("bucket")
    stmt = (
        select(
            bucket_expr,
            func.avg(MonitorSample.cpu),
            func.max(MonitorSample.cpu),
            func.avg(MonitorSample.mem_percent),
            func.max(MonitorSample.mem_percent),
            func.avg(MonitorSample.swap_percent),
            func.avg(MonitorSample.net_rx),
            func.max(MonitorSample.net_rx),
            func.avg(MonitorSample.net_tx),
            func.max(MonitorSample.net_tx),
            func.avg(MonitorSample.disk_read),
            func.max(MonitorSample.disk_read),
            func.avg(MonitorSample.disk_write),
            func.max(MonitorSample.disk_write),
            func.avg(MonitorSample.load1),
            func.max(MonitorSample.load1),
        )
        .where(MonitorSample.ts >= cutoff_ms)
        .group_by(bucket_expr)
        .order_by(bucket_expr)
    )
    if endpoint_id is not None:
        stmt = stmt.where(MonitorSample.endpoint_id == endpoint_id)
    else:
        stmt = stmt.where(MonitorSample.endpoint_id.is_(None))
    async with get_session_factory()() as session:
        rows = (await session.execute(stmt)).all()
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "ts": int(row.bucket // 1000),
                "cpu": {"avg": _round(row[1]), "max": _round(row[2])},
                "mem": {"avg": _round(row[3]), "max": _round(row[4])},
                "swap": {"avg": _round(row[5])},
                "net": {
                    "rx_avg": _round(row[6]),
                    "rx_max": _round(row[7]),
                    "tx_avg": _round(row[8]),
                    "tx_max": _round(row[9]),
                },
                "disk": {
                    "read_avg": _round(row[10]),
                    "read_max": _round(row[11]),
                    "write_avg": _round(row[12]),
                    "write_max": _round(row[13]),
                },
                "load1": {"avg": _round(row[14], 2), "max": _round(row[15], 2)},
            }
        )
    return result


async def fetch_anomaly_density(
    range_seconds: int,
    bucket_seconds: int,
    endpoint_id: int | None = None,
) -> list[dict[str, Any]]:
    """Per-bucket anomaly counts (CPU/mem/total) over the trailing window."""
    bucket_ms = max(bucket_seconds, 1) * 1000
    cutoff_ms = int(
        (datetime.now(UTC) - timedelta(seconds=max(range_seconds, 60))).timestamp() * 1000
    )
    # Integer division is required for epoch-aligned buckets. SQLite treats
    # division involving a bound parameter as float division, so cast the
    # quotient back to Integer (floor for positive epoch values).
    bucket_expr = ((MonitorSample.ts / bucket_ms).cast(Integer) * bucket_ms).label("bucket")
    stmt = (
        select(
            bucket_expr,
            func.sum(MonitorSample.cpu_anomaly.cast(Integer)),
            func.sum(MonitorSample.mem_anomaly.cast(Integer)),
            func.count(MonitorSample.id),
        )
        .where(
            MonitorSample.ts >= cutoff_ms,
            MonitorSample.cpu_anomaly.is_(True) | MonitorSample.mem_anomaly.is_(True),
        )
        .group_by(bucket_expr)
        .order_by(bucket_expr)
    )
    if endpoint_id is not None:
        stmt = stmt.where(MonitorSample.endpoint_id == endpoint_id)
    else:
        stmt = stmt.where(MonitorSample.endpoint_id.is_(None))
    async with get_session_factory()() as session:
        rows = (await session.execute(stmt)).all()
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "ts": int(row.bucket // 1000),
                "cpu": int(row[1] or 0),
                "mem": int(row[2] or 0),
                "total": int(row[3] or 0),
            }
        )
    return result


async def prune_older_than(days: int = 7, endpoint_id: int | None = None) -> int:
    """Delete samples older than ``days`` days; returns the row count removed."""
    cutoff_ms = int((datetime.now(UTC) - timedelta(days=days, minutes=5)).timestamp() * 1000)
    stmt = delete(MonitorSample).where(MonitorSample.ts < cutoff_ms)
    if endpoint_id is not None:
        stmt = stmt.where(MonitorSample.endpoint_id == endpoint_id)
    async with get_session_factory()() as session:
        result = await session.execute(stmt)
        await session.commit()
        count = cast("CursorResult[Any]", result).rowcount or 0
    return count
