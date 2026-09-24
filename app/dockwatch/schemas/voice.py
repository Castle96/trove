"""Pydantic schemas for the voice assistant (Jarvis) pipeline."""

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class StagePayload(BaseModel):
    """Timing/status of a single pipeline stage reported by the worker."""

    stage: str = Field(min_length=1, max_length=30)
    status: str = Field(default="ok", max_length=30)
    latency_ms: int = Field(default=0, ge=0, le=3_600_000)
    detail: str | None = None


class TurnIngest(BaseModel):
    """One complete turn pushed by the voice worker (remote ingest)."""

    session_id: str = Field(min_length=1, max_length=64)
    transcript: str | None = None
    response_text: str | None = None
    status: str = Field(default="ok", max_length=30)
    stages: list[StagePayload] = Field(default_factory=list)


class TurnReadBase(BaseModel):
    id: int
    session_id: str
    transcript: str | None = None
    response_text: str | None = None
    status: str
    total_latency_ms: int
    ts: int


class TurnRead(TurnReadBase, ORMModel):
    pass
