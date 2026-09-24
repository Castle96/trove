"""ORM models for the voice assistant (Jarvis) pipeline.

``VoiceTurn`` captures one end-to-end utterance (transcript, response,
total latency). ``VoiceStageSample`` records per-stage timing/status so the
Voice dashboard can render a live pipeline flow plus latency history.

The audio engines themselves (faster-whisper for STT, piper for TTS) run
wherever the assistant lives; these tables are the telemetry they report
into, mirroring how remote agents push monitor samples via the ingest API.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.dockwatch.database import Base
from app.dockwatch.models.inventory import TimestampMixin

#: Canonical pipeline stage order in the dashboard flow diagram.
STAGES = ("vad", "stt", "nlu", "agent", "skills", "tts")


class VoiceTurn(Base, TimestampMixin):
    """One assistant turn: user utterance -> assistant response."""

    __tablename__ = "voice_turns"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    transcript: Mapped[str | None] = mapped_column(Text)
    response_text: Mapped[str | None] = mapped_column(Text)
    #: ``ok`` | ``error``.
    status: Mapped[str] = mapped_column(String(30), default="ok", nullable=False)
    total_latency_ms: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    #: Epoch milliseconds (drives time-bucketed history charts).
    ts: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)


class VoiceStageSample(Base, TimestampMixin):
    """Timing/status for one pipeline stage within a turn."""

    __tablename__ = "voice_stage_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    turn_id: Mapped[int] = mapped_column(
        ForeignKey("voice_turns.id", ondelete="CASCADE"), index=True, nullable=False
    )
    session_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    #: One of :data:`STAGES`.
    stage: Mapped[str] = mapped_column(String(30), index=True, nullable=False)
    #: ``ok`` | ``error`` | ``skipped``.
    status: Mapped[str] = mapped_column(String(30), default="ok", nullable=False)
    latency_ms: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    ts: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
