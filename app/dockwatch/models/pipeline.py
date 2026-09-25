"""ORM models for per-agent development pipelines.

``PipelineRun`` captures one pipeline execution against a repository (commit
checked out, branch, status, total latency). ``PipelineStageSample`` records
per-stage timing/status so the Fleet tab can render a live stage-flow per
code agent plus last-run history.

The workers (smolagents ``code-agent`` processes on the ray / fleet / jarvis
hosts) report runs via ``POST /api/pipeline/ingest`` — the same remote-agent
telemetry pattern as voice turns and monitor samples.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.dockwatch.database import Base
from app.dockwatch.models.inventory import TimestampMixin

#: Canonical dev-pipeline stage order in the Fleet tab stage-flow diagram.
STAGES = ("checkout", "deps", "format", "lint", "build", "test", "report")


class PipelineRun(Base, TimestampMixin):
    """One dev-pipeline execution reported by a code agent."""

    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), index=True
    )
    endpoint_id: Mapped[int | None] = mapped_column(
        ForeignKey("endpoints.id", ondelete="SET NULL"), index=True
    )
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    #: Agent language target: ``rust`` | ``go`` | ``python``.
    language: Mapped[str] = mapped_column(String(30), default="", nullable=False)
    repo_url: Mapped[str | None] = mapped_column(String(500))
    branch: Mapped[str | None] = mapped_column(String(200))
    commit: Mapped[str | None] = mapped_column(String(64))
    #: ``running`` | ``ok`` | ``error``.
    status: Mapped[str] = mapped_column(String(30), default="running", nullable=False)
    total_latency_ms: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    #: Epoch milliseconds; drives time-ordered history views.
    ts: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)


class PipelineStageSample(Base, TimestampMixin):
    """Timing/status for one pipeline stage within a run."""

    __tablename__ = "pipeline_stage_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    agent_id: Mapped[int | None] = mapped_column(index=True)
    #: One of :data:`STAGES`.
    stage: Mapped[str] = mapped_column(String(30), index=True, nullable=False)
    #: ``ok`` | ``error`` | ``skipped``.
    status: Mapped[str] = mapped_column(String(30), default="ok", nullable=False)
    latency_ms: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    #: Captured command output tail (bounded by the worker before ingest).
    output: Mapped[str | None] = mapped_column(Text)
    ts: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
