"""Pydantic schemas for the Docker monitoring API.

Field names mirror the Docker JSON keys where the SPA relies on them, so the
wire format of every endpoint remains unchanged.
"""

from pydantic import BaseModel, ConfigDict, Field


class DockerStatus(BaseModel):
    """Overall Docker-API engine availability + host summary."""

    available: bool = False
    reason: str | None = None
    engine: str | None = None
    version: str | None = None
    api_version: str | None = None
    os: str | None = None
    arch: str | None = None
    hostname: str | None = None
    containers_total: int = 0
    containers_running: int = 0
    containers_paused: int = 0
    containers_stopped: int = 0
    images: int = 0
    networks: int = 0
    volumes: int = 0
    endpoint_id: int | None = None
    endpoint_name: str | None = None


class ContainerStats(BaseModel):
    cpu_percent: float = 0.0
    memory_usage: int = 0
    memory_limit: int = 0
    memory_percent: float = 0.0
    network_rx: int = 0
    network_tx: int = 0
    pids: int = 0
    restart_count: int = 0


class PortBinding(BaseModel):
    """A published container port.

    The JSON keys (``ContainerPort``/``HostIp``/``HostPort``) are Docker's
    inspect-format keys; they are what the SPA renders for container details.
    """

    model_config = ConfigDict(populate_by_name=True)

    container_port: str = Field(alias="ContainerPort")
    host_ip: str | None = Field(default=None, alias="HostIp")
    host_port: str | None = Field(default=None, alias="HostPort")


class Mount(BaseModel):
    """A container mount/volume binding (inspect ``Mounts`` entry).

    Unknown fields are preserved so nothing is dropped on the wire.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    type: str | None = Field(default=None, alias="Type")
    name: str | None = Field(default=None, alias="Name")
    source: str | None = Field(default=None, alias="Source")
    destination: str | None = Field(default=None, alias="Destination")
    driver: str | None = Field(default=None, alias="Driver")
    mode: str | None = Field(default=None, alias="Mode")
    rw: bool | None = Field(default=None, alias="RW")
    propagation: str | None = Field(default=None, alias="Propagation")


class ContainerConfig(BaseModel):
    """Container inspect ``Config`` (modeled subset, extra fields preserved)."""

    model_config = ConfigDict(extra="allow")

    Hostname: str | None = None
    Domainname: str | None = None
    User: str | None = None
    Image: str | None = None
    WorkingDir: str | None = None
    Entrypoint: list[str] | None = None
    Cmd: list[str] | None = None
    Env: list[str] | None = None
    Labels: dict[str, str] | None = None
    Tty: bool | None = None
    StopSignal: str | None = None


class ContainerRead(BaseModel):
    id: str
    short_id: str
    name: str
    image: str
    image_id: str | None = None
    state: str
    status: str
    created: str | None = None
    ports: list[PortBinding] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    stack: str | None = None
    service: str | None = None
    stats: ContainerStats | None = None
    endpoint_id: int | None = None
    endpoint_name: str | None = None


class ContainerDetail(ContainerRead):
    config: ContainerConfig = Field(default_factory=ContainerConfig)
    mounts: list[Mount] = Field(default_factory=list)
    env: list[str] = Field(default_factory=list)
    started_at: str | None = None


class LogsResponse(BaseModel):
    container_id: str
    logs: str


class StackService(BaseModel):
    name: str
    image: str
    replicas: str | None = None
    state: str
    ports: list[dict[str, object]] = Field(default_factory=list)


class StackRead(BaseModel):
    project: str
    working_dir: str | None = None
    created: str | None = None
    service_count: int = 0
    containers: list[ContainerRead] = Field(default_factory=list)


class ImageRead(BaseModel):
    id: str
    short_id: str
    tags: list[str] = Field(default_factory=list)
    size: int = 0
    created: str | None = None


class SwarmNode(BaseModel):
    id: str
    hostname: str
    role: str
    state: str
    status: str
    manager: bool = False


class ServiceRead(BaseModel):
    id: str
    name: str
    image: str
    mode: str
    replicas: str | None = None
    ports: list[dict[str, object]] = Field(default_factory=list)


class ActivityEvent(BaseModel):
    timestamp: str
    actor_type: str
    action: str
    target: str
    detail: str | None = None
    endpoint_id: int | None = None
    endpoint_name: str | None = None


class ImagePullRequest(BaseModel):
    """Pull an image by ref (``nginx`` or ``nginx:1.27``)."""

    image: str = Field(min_length=1, max_length=500)


class ImagePullResponse(BaseModel):
    image: str
    status: str = "pulled"


class ContainerDeploy(BaseModel):
    """Single-container deploy spec (compose comes later)."""

    image: str = Field(min_length=1, max_length=500)
    name: str | None = Field(
        default=None, min_length=1, max_length=150, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$"
    )
    command: str | list[str] | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    #: ``{"80/tcp": 8080}`` — container port to host port (None = random host port).
    ports: dict[str, int | None] = Field(default_factory=dict)
    #: ``["/data/app:/app:ro"]`` — socket mounts are rejected.
    volumes: list[str] = Field(default_factory=list)
    network_mode: str | None = Field(default=None, max_length=100)
    restart_policy: str | None = Field(
        default=None, pattern=r"^(no|always|unless-stopped|on-failure(:\d+)?)$"
    )
    labels: dict[str, str] = Field(default_factory=dict)
    auto_start: bool = True
    pull_if_missing: bool = False


class ContainerDeployResponse(BaseModel):
    id: str
    short_id: str
    name: str
    warnings: list[str] = Field(default_factory=list)
