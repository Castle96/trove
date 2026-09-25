"""Database operations for dev-pipeline telemetry.

Mirrors the split used for voice telemetry: API/worker code calls these pure
read/write helpers instead of touching the ORM session directly.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dockwatch.models.pipeline import PipelineRun, PipelineStageSample
from app.dockwatch.models.swarm import Agent

DAY_MS = 86_400_000


def _now_ms() -> int:
    """Epoch milliseconds; sort key for run history."""
    return int(time.time() * 1000)


# ------------------------------------------------------------------ writes
async def insert_run(
    db: AsyncSession,
    agent: Agent,
    stages: list[dict[str, Any]],
    project_id: int | None = None,
    language: str = "",
    repo_url: str | None = None,
    branch: str | None = None,
    commit: str | None = None,
    status: str = "ok",
    ts: int | None = None,
) -> PipelineRun:
    """Insert a pipeline run plus its stage samples in one transaction.

    A worker reports a ``running`` run when it starts the pipeline and then a
    final ``ok``/``error`` run with the full stage set — the Fleet tab renders
    the newest run per agent either way.
    """
    now = _now_ms() if ts is None else ts
    total = sum(max(int(s.get("latency_ms", 0)), 0) for s in stages)
    run = PipelineRun(
        agent_id=agent.id,
        endpoint_id=agent.endpoint_id,
        project_id=project_id,
        language=language,
        repo_url=repo_url,
        branch=branch,
        commit=commit,
        status=status,
        total_latency_ms=total,
        ts=now,
    )
    db.add(run)
    await db.flush()
    for sample in stages:
        db.add(
            PipelineStageSample(
                run_id=run.id,
                agent_id=agent.id,
                stage=str(sample.get("stage", "unknown"))[:30],
                status=str(sample.get("status", "ok"))[:30],
                latency_ms=max(int(sample.get("latency_ms", 0)), 0),
                detail=sample.get("detail"),
                output=sample.get("output"),
                ts=now,
            )
        )
    await db.commit()
    await db.refresh(run)
    return run


# ------------------------------------------------------------------ reads
async def fetch_latest_run(db: AsyncSession, agent_id: int) -> PipelineRun | None:
    """Most recent run for one agent (None when it has never run)."""
    return await db.scalar(
        select(PipelineRun)
        .where(PipelineRun.agent_id == agent_id)
        .order_by(PipelineRun.ts.desc(), PipelineRun.id.desc())
        .limit(1)
    )


async def fetch_run_stages(db: AsyncSession, run_id: int) -> list[dict[str, Any]]:
    """Stage samples for a run, in insertion order."""
    if run_id is None:
        return []
    rows = await db.scalars(
        select(PipelineStageSample)
        .where(PipelineStageSample.run_id == run_id)
        .order_by(PipelineStageSample.id)
    )
    return [
        {
            "stage": s.stage,
            "status": s.status,
            "latency_ms": s.latency_ms,
            "detail": s.detail,
        }
        for s in rows
    ]


async def fetch_recent_runs(
    db: AsyncSession, limit: int = 50, agent_id: int | None = None
) -> list[PipelineRun]:
    """Most recent runs, newest first."""
    stmt = select(PipelineRun).order_by(PipelineRun.ts.desc(), PipelineRun.id.desc())
    if agent_id is not None:
        stmt = stmt.where(PipelineRun.agent_id == agent_id)
    return list((await db.scalars(stmt.limit(max(min(limit, 500), 1)))).all())


async def fetch_live(
    db: AsyncSession, code_agents: list[Agent], endpoint_names: dict[int, str] | None = None
) -> list[dict[str, Any]]:
    """Live stage-flow per code agent for the Fleet tab.

    Each entry carries the agent's latest run (None = never ran) with its
    stages, plus the agent's heartbeat status so the panel reads as a living
    pipeline.
    """
    endpoint_names = endpoint_names or {}
    live: list[dict[str, Any]] = []
    for agent in code_agents:
        run = await fetch_latest_run(db, agent.id)
        stages: list[dict[str, Any]] = []
        meta: dict[str, Any] = {}
        if agent.metadata_json:
            try:
                import json

                meta = json.loads(agent.metadata_json)
            except Exception:
                meta = {}
        language = str(meta.get("language", "") or "")
        model = str(meta.get("model", "") or None)
        if run is not None:
            stages = await fetch_run_stages(db, run.id)
            if not language:
                language = run.language
        live.append(
            {
                "agent_id": agent.id,
                "agent_name": agent.name,
                "language": language,
                "model": model,
                "endpoint_id": agent.endpoint_id,
                "endpoint_name": endpoint_names.get(agent.endpoint_id)
                if agent.endpoint_id
                else None,
                "status": agent.status,
                "last_run_status": run.status if run is not None else None,
                "last_commit": run.commit if run is not None else None,
                "branch": run.branch if run is not None else None,
                "repo_url": run.repo_url if run is not None else None,
                "total_latency_ms": run.total_latency_ms if run is not None else 0,
                "stages": stages,
                "run_id": run.id if run is not None else None,
            }
        )
    return live


async def fetch_overview(db: AsyncSession) -> dict[str, Any]:
    """Aggregate counters for dashboard cards."""
    total = await db.scalar(select(func.count(PipelineRun.id))) or 0
    ok = await db.scalar(select(func.count(PipelineRun.id)).where(PipelineRun.status == "ok")) or 0
    err = (
        await db.scalar(select(func.count(PipelineRun.id)).where(PipelineRun.status == "error"))
        or 0
    )
    running = (
        await db.scalar(select(func.count(PipelineRun.id)).where(PipelineRun.status == "running"))
        or 0
    )
    return {
        "total_runs": int(total),
        "ok_runs": int(ok),
        "error_runs": int(err),
        "running": int(running),
        "agents": [],
    }
