"""SQLAlchemy model for discovered container-port hotlinks.

Port discovery scans an endpoint's containers, finds every published host port,
and persists one :class:`ContainerLink` per ``(endpoint, container, port)`` so
the operator can label, enable/disable, and promote each hotlink into a
gateway route. Rows are upserted by sync and never auto-deleted: a container
that disappears just gets flagged ``stale`` so management history is preserved.

``gateway_route_id`` references the Trove API-gateway table
(``gateway_routes``), which lives in a *separate* SQLite database, so it is a
plain integer with no SQL-level foreign key.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.dockwatch.database import Base
from app.dockwatch.models.inventory import TimestampMixin


class ContainerLink(Base, TimestampMixin):
    """One discovered published port (a clickable ``scheme://host:port``)."""

    __tablename__ = "container_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    endpoint_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    #: Full container id (upsert key, together with container_port).
    container_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    short_id: Mapped[str] = mapped_column(String(16), default="")
    container_name: Mapped[str] = mapped_column(String(150), default="")
    image: Mapped[str] = mapped_column(String(255), default="")
    state: Mapped[str] = mapped_column(String(30), default="unknown")
    container_port: Mapped[str] = mapped_column(String(30), default="")  # e.g. "80/tcp"
    host_port: Mapped[str] = mapped_column(String(30), default="")
    host: Mapped[str] = mapped_column(String(255), default="")
    scheme: Mapped[str] = mapped_column(String(10), default="http")  # http | https
    url: Mapped[str] = mapped_column(String(500), default="")
    #: Operator-friendly alias (defaults to the container name / label).
    label: Mapped[str] = mapped_column(String(150), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: True when the (endpoint, container, port) was absent from the last scan.
    stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: True once the operator overrides scheme/host — sync no longer overwrites
    #: those fields so a hand-tuned hotlink survives resyncs.
    manual: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Id of the GatewayRoute (Trove DB) this link was promoted into, if any.
    gateway_route_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
