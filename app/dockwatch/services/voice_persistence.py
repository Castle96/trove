"""Database operations for voice pipeline telemetry.

Mirrors the split used for host monitoring: API/worker code calls into these
pure read/write helpers instead of touching the ORM session directly.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dockwatch.models.voice import STAGES, VoiceStageSample, VoiceTurn

DAY_MS = 86_400_000


def _now_ms() -> int:
    """Epoch milliseconds; the chart bucket key used by history endpoints."""
    return int(time.time() * 1000)


# ------------------------------------------------------------------ writes
async def insert_turn(
    db: AsyncSession,
    session_id: str,
    stages: list[dict[str, Any]],
    transcript: str | None = None,
    response_text: str | None = None,
    status: str = "ok",
    ts: int | None = None,
) -> VoiceTurn:
    """Insert a turn plus its stage samples in one transaction."""
    now = _now_ms() if ts is None else ts
    total = sum(max(int(s.get("latency_ms", 0)), 0) for s in stages)
    turn = VoiceTurn(
        session_id=session_id,
        transcript=transcript,
        response_text=response_text,
        status=status,
        total_latency_ms=total,
        ts=now,
    )
    db.add(turn)
    await db.flush()
    for sample in stages:
        db.add(
            VoiceStageSample(
                turn_id=turn.id,
                session_id=session_id,
                stage=str(sample.get("stage", "unknown"))[:30],
                status=str(sample.get("status", "ok"))[:30],
                latency_ms=max(int(sample.get("latency_ms", 0)), 0),
                detail=sample.get("detail"),
                ts=now,
            )
        )
    await db.commit()
    await db.refresh(turn)
    return turn


# ------------------------------------------------------------------ reads
async def fetch_recent_turns(
    db: AsyncSession, limit: int = 50, session_id: str | None = None
) -> list[VoiceTurn]:
    """Most recent turns, newest first."""
    stmt = select(VoiceTurn).order_by(VoiceTurn.ts.desc()).limit(max(min(limit, 500), 1))
    if session_id:
        stmt = stmt.where(VoiceTurn.session_id == session_id)
    return list((await db.scalars(stmt)).all())


async def fetch_latest_stage_samples(db: AsyncSession) -> list[VoiceStageSample]:
    """Stage samples belonging to the most recent turn (empty when no turns)."""
    latest = await db.scalar(select(VoiceTurn.ts).order_by(VoiceTurn.ts.desc()).limit(1))
    if latest is None:
        return []
    rows = await db.scalars(
        select(VoiceStageSample).where(VoiceStageSample.ts == latest).order_by(VoiceStageSample.id)
    )
    return list(rows.all())


async def fetch_overview(db: AsyncSession) -> dict[str, Any]:
    """Aggregate counters for the Voice dashboard cards."""
    now = _now_ms()
    since_24h = now - DAY_MS
    counts = await db.execute(
        select(
            func.count(VoiceTurn.id),
            func.sum(VoiceTurn.total_latency_ms),
            func.count().filter(VoiceTurn.status == "error"),
        ).where(VoiceTurn.ts >= since_24h)
    )
    turns_24h, latency_sum, errors_24h = counts.one()
    total = await db.scalar(select(func.count(VoiceTurn.id))) or 0
    return {
        "turns_24h": int(turns_24h or 0),
        "turns_total": int(total or 0),
        "errors_24h": int(errors_24h or 0),
        "avg_latency_ms": round(int(latency_sum or 0) / int(turns_24h or 1)),
    }


def _buckets(
    rows: list[tuple[str, int, str, int]],
    range_seconds: int,
    bucket_seconds: int,
) -> dict[str, list[dict[str, Any]]]:
    """Aggregate raw (stage, latency_ms, status, ts_ms) rows into buckets.

    Returns one list per stage with ``ts`` (bucket start, epoch seconds) and
    p50/p95/count/errors. Bucket boundaries are aligned to epoch so charts are
    stable across polls.
    """
    out: dict[str, dict[int, list[int]]] = {s: {} for s in STAGES}
    errors: dict[str, dict[int, int]] = {s: {} for s in STAGES}
    bucket_ms = max(bucket_seconds, 1) * 1000
    for stage, latency, status, ts in rows:
        if stage not in out:
            continue
        bucket = (ts // bucket_ms) * bucket_ms
        out[stage].setdefault(bucket, []).append(int(latency))
        if status == "error":
            errors[stage][bucket] = errors[stage].get(bucket, 0) + 1

    result: dict[str, list[dict[str, Any]]] = {}
    for stage in STAGES:
        items = []
        for bucket, lats in sorted(out[stage].items()):
            s = sorted(lats)
            p50 = s[len(s) // 2]
            p95 = s[min(int(len(s) * 0.95), len(s) - 1)]
            items.append(
                {
                    "ts": bucket // 1000,
                    "p50": p50,
                    "p95": p95,
                    "count": len(lats),
                    "errors": errors[stage].get(bucket, 0),
                }
            )
        result[stage] = items
    return result


async def fetch_stage_history(
    db: AsyncSession, range_seconds: int, bucket_seconds: int
) -> dict[str, list[dict[str, Any]]]:
    """Per-stage latency buckets over the trailing window."""
    cutoff = _now_ms() - max(range_seconds, 60) * 1000
    rows = (
        await db.execute(
            select(
                VoiceStageSample.stage,
                VoiceStageSample.latency_ms,
                VoiceStageSample.status,
                VoiceStageSample.ts,
            ).where(VoiceStageSample.ts >= cutoff)
        )
    ).all()
    return _buckets([tuple(row) for row in rows], range_seconds, bucket_seconds)


# ------------------------------------------------------------------ prune
async def prune_older_than(db: AsyncSession, days: int) -> int:
    """Remove turns + stage samples older than *days*; returns turns removed."""
    cutoff = _now_ms() - max(days, 1) * DAY_MS
    n = await db.scalar(select(func.count(VoiceTurn.id)).where(VoiceTurn.ts < cutoff)) or 0
    await db.execute(delete(VoiceStageSample).where(VoiceStageSample.ts < cutoff))
    await db.execute(delete(VoiceTurn).where(VoiceTurn.ts < cutoff))
    await db.commit()
    return int(n)
