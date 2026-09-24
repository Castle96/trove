"""SQLAlchemy model for monitored endpoints (multi-host registry).

An endpoint is a Docker/host target Dockwatch polls: a local unix socket,
a remote TCP Docker endpoint, or a host that pushes metrics via the ingest
API. The local engine from settings is always available as ``local``
without a DB row; rows here add *additional* remotes.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.dockwatch.database import Base
from app.dockwatch.models.inventory import TimestampMixin, utcnow


class Endpoint(Base, TimestampMixin):
    """A remotely monitored Docker/host endpoint."""

    __tablename__ = "endpoints"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), unique=True, index=True, nullable=False)
    #: Docker endpoint URL, e.g. ``unix:///var/run/docker.sock`` or ``tcp://host:2375``.
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    #: ``docker`` | ``host`` | ``combo`` — what this endpoint provides.
    kind: Mapped[str] = mapped_column(String(30), default="docker", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: Optional JSON blob for TLS certs / auth config (private key material is NOT stored here).
    credentials: Mapped[str | None] = mapped_column(Text)
    #: Ascending sort key for the picker + fleet table (0 = default position).
    sort_order: Mapped[int] = mapped_column(default=0, nullable=False)
    #: Last known status: ``ok`` | ``unavailable`` | ``never``.
    last_status: Mapped[str] = mapped_column(String(30), default="never", nullable=False)
    last_seen: Mapped[datetime | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(Text)

    def touch(self, status: str, error: str | None = None) -> None:
        """Update cached reachability state."""
        self.last_status = status
        self.last_error = error
        self.last_seen = utcnow()
