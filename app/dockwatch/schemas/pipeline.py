"""Pydantic schemas for per-agent development pipelines."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class StageIngest(BaseModel):
    """Timing/status of a single pipeline stage reported by a worker."""

    stage: str = Field(min_length=1, max_length=30)
    status: str = Field(default="ok", max_length=30)
    latency_ms: int = Field(default=0, ge=0, le=86_400_000)
    detail: str | None = None
    output: str | None = None


class RunIngest(BaseModel):
    """One pipeline execution pushed by a remote code agent.

    ``agent_name`` resolves to an ``Agent`` record (``kind="code"``), so
    workers only need to know their own name — language/model come from that
    agent's metadata in the swarm dashboard.
    """

    agent_name: str = Field(min_length=1, max_length=150)
    project_id: int | None = None
    language: str = Field(default="", max_length=30)
    repo_url: str | None = Field(default=None, max_length=500)
    branch: str | None = Field(default=None, max_length=200)
    commit: str | None = Field(default=None, max_length=64)
    status: str = Field(default="ok", max_length=30)
    stages: list[StageIngest] = Field(default_factory=list)


class StageRead(BaseModel):
    stage: str
    status: str
    latency_ms: int
    detail: str | None = None


class PipelineRunRead(ORMModel):
    id: int
    agent_id: int | None = None
    agent_name: str | None = None
    project_id: int | None = None
    language: str
    repo_url: str | None = None
    branch: str | None = None
    commit: str | None = None
    status: str
    total_latency_ms: int
    ts: int
    created_at: datetime
    updated_at: datetime


class LiveEntry(BaseModel):
    """Live stage-flow for one code agent (Fleet tab panel)."""

    agent_id: int | None = None
    agent_name: str
    language: str = ""
    model: str | None = None
    endpoint_id: int | None = None
    endpoint_name: str | None = None
    status: str = "idle"  # agent heartbeat status
    last_run_status: str | None = None
    last_commit: str | None = None
    branch: str | None = None
    repo_url: str | None = None
    total_latency_ms: int = 0
    stages: list[StageRead] = Field(default_factory=list)
    run_id: int | None = None


class PipelineOverview(BaseModel):
    total_runs: int = 0
    ok_runs: int = 0
    error_runs: int = 0
    running: int = 0
    agents: list[str] = Field(default_factory=list)
