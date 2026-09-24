"""Pydantic schemas for monitored endpoints + fleet aggregation."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class EndpointBase(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    url: str = Field(min_length=1, max_length=500)
    kind: str = Field(default="docker", max_length=30)
    enabled: bool = True
    description: str | None = None
    credentials: str | None = Field(
        default=None, max_length=4000, description="Optional JSON with TLS/auth config"
    )
    sort_order: int = Field(default=0, ge=0, description="Ascending picker/table order")


class EndpointCreate(EndpointBase):
    pass


class EndpointUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    url: str | None = Field(default=None, min_length=1, max_length=500)
    kind: str | None = Field(default=None, max_length=30)
    enabled: bool | None = None
    description: str | None = None


class EndpointRead(EndpointBase, ORMModel):
    id: int
    last_status: str = "never"
    last_seen: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class EndpointStatus(BaseModel):
    """Live reachability for one endpoint (local + remotes share the shape)."""

    endpoint_id: int | None = None
    endpoint_name: str = "local"
    available: bool = False
    reason: str | None = None
    engine: str | None = None
    version: str | None = None
    containers_total: int = 0
    containers_running: int = 0
    containers_stopped: int = 0
    kind: str = "docker"


class FleetOverview(BaseModel):
    endpoints: list[EndpointStatus] = Field(default_factory=list)
    total_endpoints: int = 0
    reachable: int = 0
    total_containers: int = 0
    total_running: int = 0
