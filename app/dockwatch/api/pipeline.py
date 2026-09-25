"""REST API for per-agent development pipelines.

Remote code-agent workers (``code-agent`` on the ray / fleet / jarvis hosts)
report completed pipeline runs via ``POST /api/pipeline/ingest`` — the same
remote-agent telemetry pattern as ``/api/voice/ingest``. Read endpoints feed
the Fleet tab's per-agent stage-flow panels and the run history view.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dockwatch.api.deps import require_write
from app.dockwatch.api.inventory import get_or_404
from app.dockwatch.database import get_session
from app.dockwatch.models.endpoint import Endpoint
from app.dockwatch.models.pipeline import PipelineRun
from app.dockwatch.models.swarm import Agent, Project
from app.dockwatch.schemas.pipeline import (
    LiveEntry,
    PipelineOverview,
    PipelineRunRead,
    RunIngest,
)
from app.dockwatch.services.code_agents import endpoint_names_by_id, fetch_code_agents
from app.dockwatch.services.pipeline_persistence import (
    fetch_live,
    fetch_overview,
    fetch_recent_runs,
    insert_run,
)

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])

DB = Annotated[AsyncSession, Depends(get_session)]


async def _agent_or_404(db: AsyncSession, agent_name: str) -> Agent:
    agent = await db.scalar(select(Agent).where(Agent.name == agent_name))
    if agent is None:
        raise HTTPException(status_code=404, detail=f"no swarm agent named {agent_name!r}")
    return agent


@router.post(
    "/ingest",
    status_code=201,
    dependencies=[Depends(require_write)],
)
async def pipeline_ingest(payload: RunIngest, db: DB) -> dict[str, Any]:
    """Remote code agent reports one pipeline execution with stage timings.

    The worker must know its own agent name; everything else (language, model)
    is resolved from the agent's swarm metadata. A ``running`` ingest refreshes
    the live panel immediately; the final ``ok``/``error`` ingest carries the
    complete stage list.
    """
    agent = await _agent_or_404(db, payload.agent_name)
    project_id = payload.project_id
    if project_id is not None:
        await get_or_404(db, Project, project_id, "Project")
    stages = [s.model_dump() for s in payload.stages]
    run = await insert_run(
        db,
        agent=agent,
        stages=stages,
        project_id=project_id,
        language=payload.language,
        repo_url=payload.repo_url,
        branch=payload.branch,
        commit=payload.commit,
        status=payload.status,
    )
    return {"id": run.id, "ts": run.ts, "agent": agent.name, "status": run.status}


@router.get("/overview", response_model=PipelineOverview)
async def pipeline_overview(db: DB) -> dict[str, Any]:
    """Aggregate counters plus registered code-agent names."""
    agents = await fetch_code_agents(db)
    overview = await fetch_overview(db)
    overview["agents"] = [a.name for a in agents]
    return overview


@router.get("/live", response_model=list[LiveEntry])
async def pipeline_live(db: DB) -> list[dict[str, Any]]:
    """Live stage-flow per registered code agent (Fleet tab panels)."""
    agents = await fetch_code_agents(db)
    endpoints = await db.scalars(select(Endpoint))
    names = endpoint_names_by_id(agents, list(endpoints.all()))
    return await fetch_live(db, agents, endpoint_names=names)


@router.get("/runs", response_model=list[PipelineRunRead])
async def pipeline_runs(
    db: DB,
    agent_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[Any]:
    """Recent pipeline runs (optionally filtered to one agent)."""
    return await fetch_recent_runs(db, limit=limit, agent_id=agent_id)


@router.get("/runs/{run_id}", response_model=PipelineRunRead)
async def pipeline_run_detail(run_id: int, db: DB) -> Any:
    run = await get_or_404(db, PipelineRun, run_id, "PipelineRun")
    return run
