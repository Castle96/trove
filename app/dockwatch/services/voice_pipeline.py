"""Voice assistant (Jarvis) pipeline service.

Provides:
* :class:`StageRecorder` — async context manager that times and persists one
  pipeline stage (used by in-process pipeline code and the demo loop).
* :func:`ensure_jarvis_agent` / :func:`touch_jarvis` — keep a ``kind="voice"``
  Agent on the swarm dashboard so Jarvis shows up as a living worker.
* :func:`run_demo_turn` + :func:`start_voice_loop` — optional background loop
  that emits synthetic turns (``DOCKWATCH_ENABLE_VOICE_DEMO=true``) and prunes
  expired telemetry, so the Voice dashboard is alive even before the audio
  stack is wired in. Never synthesize in production: point the real worker at
  ``POST /api/voice/ingest`` instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.dockwatch.config import get_settings
from app.dockwatch.database import get_session_factory
from app.dockwatch.models.swarm import Agent, SwarmNotification
from app.dockwatch.models.voice import STAGES
from app.dockwatch.services.voice_persistence import insert_turn, prune_older_than

logger = logging.getLogger(__name__)

JARVIS_NAME = "jarvis"

#: (low, high) latency range in ms per stage — realistic enough for the demo
#: telemetry to look plausible on the latency dashboard.
_STAGE_LATENCY_RANGES: dict[str, tuple[int, int]] = {
    "vad": (10, 80),
    "stt": (250, 1400),
    "nlu": (30, 120),
    "agent": (400, 2500),
    "skills": (100, 700),
    "tts": (200, 900),
}

_DEMO_TRANSCRIPTS = (
    "What containers are running right now?",
    "How is the CPU load looking?",
    "Run a vulnerability scan on postgres",
    "Show me recent activity",
    "Is the dockwatch service healthy?",
    "Any anomalies in the last hour?",
)

_DEMO_RESPONSES = (
    "There are 12 containers running across the fleet.",
    "CPU load is nominal at 3.4 percent.",
    "Scanning the postgres image now — I'll report back shortly.",
    "I see three engineers active in the last ten minutes.",
    "Dockwatch is healthy; the database and Docker socket both respond.",
    "No anomalies detected in the last hour.",
)


class StageRecorder:
    """Time a single pipeline stage and append its sample to the current turn.

    Usage::

        collector: list[dict[str, Any]] = []
        async with StageRecorder(collector, "stt") as sr:
            transcript = await transcribe_audio(audio)
        # collector now holds {"stage": "stt", "status": "ok", ...}
    """

    def __init__(self, collector: list[dict[str, Any]], stage: str) -> None:
        self._collector = collector
        self._stage = stage
        self._start: float | None = None

    async def __aenter__(self) -> StageRecorder:
        self._start = time.perf_counter()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        elapsed_ms = int((time.perf_counter() - (self._start or 0)) * 1000)
        if exc is None:
            self._collector.append({"stage": self._stage, "status": "ok", "latency_ms": elapsed_ms})
            return
        self._collector.append(
            {
                "stage": self._stage,
                "status": "error",
                "latency_ms": elapsed_ms,
                "detail": str(exc)[:2000],
            }
        )
        # Returning None propagates the caller's exception (as we want).


# ------------------------------------------------------------------ Jarvis
async def ensure_jarvis_agent(db: AsyncSession) -> Agent:
    """Return the ``jarvis`` voice Agent, creating it when first seen."""
    agent = await db.scalar(select(Agent).where(Agent.name == JARVIS_NAME))
    if agent is None:
        agent = Agent(
            name=JARVIS_NAME,
            kind="voice",
            status="idle",
            metadata_json=json.dumps({"engines": {"stt": "faster-whisper", "tts": "piper-tts"}}),
        )
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        logger.info("registered %s agent (kind=voice)", JARVIS_NAME)
    return agent


async def touch_jarvis(
    db: AsyncSession,
    status: str = "idle",
    snippet: str | None = None,
) -> Agent:
    """Heartbeat Jarvis so the swarm board shows it as a live worker."""
    agent = await ensure_jarvis_agent(db)
    if snippet:
        agent.last_conversation_snippet = snippet[:500]
    agent.touch_heartbeat(status)
    await db.commit()
    return agent


# ------------------------------------------------------------------ demo
def _synthetic_stages() -> list[dict[str, Any]]:
    """Simulated per-stage timings for the demo loop (idempotent shape)."""
    stages: list[dict[str, Any]] = []
    for stage in STAGES:
        low, high = _STAGE_LATENCY_RANGES[stage]
        status = "ok"
        latency = random.randint(low, high)
        if stage == "stt" and random.random() < 0.05:
            status = "error"
            latency = random.randint(low, high)
            stages.append(
                {
                    "stage": stage,
                    "status": status,
                    "latency_ms": latency,
                    "detail": "audio too noisy to transcribe reliably",
                }
            )
            continue
        # A failed STT short-circuits the remaining stages.
        if any(s["status"] == "error" for s in stages):
            break
        stages.append({"stage": stage, "status": status, "latency_ms": latency})
    return stages


async def run_demo_turn(factory: async_sessionmaker[AsyncSession] | None = None) -> dict[str, Any]:
    """Emit one synthetic turn through the same persistence path as ingest."""
    owner = factory or get_session_factory()
    transcript = random.choice(_DEMO_TRANSCRIPTS)
    response = random.choice(_DEMO_RESPONSES)
    stages = _synthetic_stages()
    status = "error" if any(s["status"] == "error" for s in stages) else "ok"
    async with owner() as db:
        turn = await insert_turn(
            db,
            session_id=f"demo-{int(time.time())}",
            stages=stages,
            transcript=transcript,
            response_text=response,
            status=status,
        )
        await touch_jarvis(db, status="working" if status == "ok" else "idle", snippet=transcript)
        if status == "error":
            agent = await ensure_jarvis_agent(db)
            db.add(
                SwarmNotification(
                    level="error",
                    title="Jarvis turn failed",
                    body=f"STT could not transcribe: {transcript[:200]}",
                    agent_id=agent.id,
                )
            )
            await db.commit()
    logger.debug("demo voice turn %d (%s)", turn.id, status)
    return {"id": turn.id, "status": status, "stages": stages}


# ------------------------------------------------------------------ loop
async def _voice_loop(factory: async_sessionmaker[AsyncSession]) -> None:
    """Background task: optional demo turns + hourly retention pruning."""
    settings = get_settings()
    loop = asyncio.get_running_loop()
    next_prune = loop.time() + 3600
    while True:
        try:
            if settings.enable_voice_demo:
                await run_demo_turn(factory)
            if loop.time() >= next_prune:
                async with factory() as db:
                    removed = await prune_older_than(db, settings.voice_retention_days)
                logger.info("pruned %d old voice turns", removed)
                next_prune = loop.time() + 3600
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("voice loop tick failed")
        await asyncio.sleep(settings.voice_demo_interval if settings.enable_voice_demo else 60)


async def start_voice_loop() -> asyncio.Task[None]:
    """Start the voice background task (prune + optional demo)."""
    return asyncio.create_task(_voice_loop(get_session_factory()))
