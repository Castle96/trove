"""REST API for the voice assistant (Jarvis) pipeline dashboard.

The voice worker (faster-whisper STT / piper TTS, wherever it runs) reports
complete turns via ``POST /api/voice/ingest`` — the same remote-agent pattern
as monitor ingest. Dashboard endpoints serve the live pipeline state, latency
history buckets, and recent turns to the SPA.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dockwatch.api.deps import require_write
from app.dockwatch.config import get_settings
from app.dockwatch.database import get_session
from app.dockwatch.models.swarm import Agent, SwarmNotification
from app.dockwatch.models.voice import STAGES
from app.dockwatch.schemas.voice import TurnIngest
from app.dockwatch.services.voice_persistence import (
    fetch_latest_stage_samples,
    fetch_overview,
    fetch_recent_turns,
    fetch_stage_history,
    insert_turn,
)
from app.dockwatch.services.voice_pipeline import JARVIS_NAME, touch_jarvis

router = APIRouter(prefix="/api/voice", tags=["voice"])

DB = Annotated[AsyncSession, Depends(get_session)]


def _agent_view(agent: Agent | None) -> dict[str, Any] | None:
    """Serialize the Jarvis swarm agent without a full Pydantic dependency."""
    if agent is None:
        return None
    return {
        "name": agent.name,
        "kind": agent.kind,
        "status": agent.status,
        "last_heartbeat": agent.last_heartbeat.isoformat() if agent.last_heartbeat else None,
        "last_conversation_snippet": agent.last_conversation_snippet,
    }


def _turn_view(turn: Any) -> dict[str, Any]:
    return {
        "id": turn.id,
        "session_id": turn.session_id,
        "transcript": turn.transcript,
        "response_text": turn.response_text,
        "status": turn.status,
        "total_latency_ms": turn.total_latency_ms,
        "ts": turn.ts,
    }


@router.get("/overview")
async def voice_overview(db: DB) -> dict[str, Any]:
    """Dashboard cards: Jarvis status + aggregate counters."""
    agent = await db.scalar(select(Agent).where(Agent.name == JARVIS_NAME))
    counts = await fetch_overview(db)
    return {
        "agent": _agent_view(agent),
        "demo_enabled": bool(get_settings().enable_voice_demo),
        **counts,
    }


@router.get("/live")
async def voice_live(db: DB) -> dict[str, Any]:
    """Current pipeline state: per-stage status/latency from the last turn."""
    agent = await db.scalar(select(Agent).where(Agent.name == JARVIS_NAME))
    samples = await fetch_latest_stage_samples(db)
    by_stage = {s.stage: s for s in samples}
    stages: list[dict[str, Any]] = []
    for stage_name in STAGES:
        sample = by_stage.get(stage_name)
        if sample is None:
            stages.append({"stage": stage_name, "status": "idle", "latency_ms": None})
        else:
            stages.append(
                {
                    "stage": sample.stage,
                    "status": sample.status,
                    "latency_ms": sample.latency_ms,
                    "detail": sample.detail,
                }
            )
    last_turn: dict[str, Any] | None = None
    if samples:
        turns = await fetch_recent_turns(db, limit=1)
        if turns:
            last_turn = _turn_view(turns[0])
    return {
        "agent_status": agent.status if agent else "unknown",
        "stages": stages,
        "last_turn": last_turn,
        "demo_enabled": bool(get_settings().enable_voice_demo),
    }


@router.get("/history")
async def voice_history(
    db: DB,
    range: int = Query(default=3600, ge=60, le=2592000, description="Trailing window (s)"),
    bucket: int = Query(default=60, ge=10, le=3600, description="Bucket width (s)"),
) -> dict[str, Any]:
    """Per-stage latency buckets (p50/p95/count/errors) for the chart."""
    stages = await fetch_stage_history(db, range, bucket)
    return {"range_seconds": range, "bucket_seconds": bucket, "stages": stages}


@router.get("/turns")
async def voice_turns(
    db: DB,
    limit: int = Query(default=50, ge=1, le=500),
    session_id: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    """Recent turns (transcript/response/status/latency) for the feed table."""
    turns = await fetch_recent_turns(db, limit=limit, session_id=session_id)
    return [_turn_view(t) for t in turns]


@router.post(
    "/ingest",
    status_code=201,
    dependencies=[Depends(require_write)],
)
async def voice_ingest(payload: TurnIngest, db: DB) -> dict[str, Any]:
    """Remote worker reports one completed turn with its own stage timings.

    Jarvis heartbeats as ``idle`` (a completed turn means the worker is done);
    in-flight state is reported by the worker via the swarm heartbeat endpoint.
    An error turn additionally raises a swarm notification.
    """
    stages = [s.model_dump() for s in payload.stages]
    turn = await insert_turn(
        db,
        session_id=payload.session_id,
        stages=stages,
        transcript=payload.transcript,
        response_text=payload.response_text,
        status=payload.status,
    )
    agent = await touch_jarvis(
        db,
        status="idle",
        snippet=payload.transcript,
    )
    if payload.status != "ok":
        try:
            db.add(
                SwarmNotification(
                    level="error",
                    title="Jarvis turn failed",
                    body=f"session {payload.session_id}: {payload.status}",
                    agent_id=agent.id,
                )
            )
            await db.commit()
        except IntegrityError:
            await db.rollback()
    return {"id": turn.id, "ts": turn.ts, "agent_status": agent.status}
