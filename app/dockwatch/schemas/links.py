"""Pydantic schemas for discovered container-port hotlinks."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ContainerHotlink(BaseModel):
    """A computed hotlink on a live container row (no persistence)."""

    container_id: str = ""
    host: str = ""
    host_port: str = ""
    container_port: str = ""
    scheme: str = "http"
    url: str = ""
    label: str = ""


class ContainerLinkBase(BaseModel):
    container_id: str = ""
    short_id: str = ""
    container_name: str = ""
    image: str = ""
    state: str = "unknown"
    container_port: str = ""
    host_port: str = ""
    host: str = ""
    scheme: str = "http"
    url: str = ""
    label: str = ""
    enabled: bool = True


class ContainerLinkRead(ContainerLinkBase, ORMModel):
    id: int
    endpoint_id: int
    sort_order: int = 0
    stale: bool = False
    manual: bool = False
    gateway_route_id: int | None = None
    created_at: datetime
    updated_at: datetime


class ContainerLinkView(ContainerLinkRead):
    """A persisted link joined with its endpoint's display name."""

    endpoint_name: str = ""


class ContainerLinkUpdate(BaseModel):
    label: str | None = Field(default=None, max_length=150)
    enabled: bool | None = None
    scheme: str | None = Field(default=None, pattern=r"^(http|https)$")
    host: str | None = Field(default=None, max_length=255)
    sort_order: int | None = Field(default=None, ge=0)


class DiscoveryResult(BaseModel):
    endpoint_id: int
    endpoint_name: str
    discovered: int = 0
    total: int = 0
    stale: int = 0
    links: list[ContainerLinkRead] = Field(default_factory=list)


class MapToGatewayRequest(BaseModel):
    gateway_id: int
    path: str = "/*"
    methods: list[str] = Field(
        default_factory=lambda: ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
    )
    strip_prefix: bool = False
    auth_mode: str = Field(default="open", pattern=r"^(open|api_key)$")
    rate_limit_rpm: int = Field(default=0, ge=0)


class MapToGatewayResult(BaseModel):
    link_id: int
    url: str
    route_id: int
    gateway_slug: str
