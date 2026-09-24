"""SQLAlchemy models for the NetBox-style infrastructure inventory."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.dockwatch.database import Base


def utcnow() -> datetime:
    """Return the current UTC time (naive, for SQLite storage)."""
    return datetime.now(UTC).replace(tzinfo=None)


class TimestampMixin:
    """Add ``created_at`` / ``updated_at`` columns that update automatically."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class Site(Base, TimestampMixin):
    """A physical location (data center, office, colo...)."""

    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    slug: Mapped[str] = mapped_column(String(150), unique=True, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False)
    facility: Mapped[str | None] = mapped_column(String(150))
    address: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)

    racks: Mapped[list[Rack]] = relationship(back_populates="site", cascade="all, delete-orphan")
    devices: Mapped[list[Device]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )


class Rack(Base, TimestampMixin):
    """A physical rack within a site."""

    __tablename__ = "racks"
    __table_args__ = (UniqueConstraint("site_id", "name", name="uq_rack_site_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False)
    u_height: Mapped[int] = mapped_column(default=42, nullable=False)
    position: Mapped[int | None] = mapped_column()
    role: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)

    site: Mapped[Site] = relationship(back_populates="racks")
    devices: Mapped[list[Device]] = relationship(
        back_populates="rack", cascade="all, delete-orphan"
    )


class Device(Base, TimestampMixin):
    """A physical or virtual device."""

    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("name", name="uq_device_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), index=True, nullable=False)
    device_type: Mapped[str] = mapped_column(String(150), nullable=False)
    role: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False)
    vendor: Mapped[str | None] = mapped_column(String(150))
    model: Mapped[str | None] = mapped_column(String(150))
    serial: Mapped[str | None] = mapped_column(String(150), index=True)
    asset_tag: Mapped[str | None] = mapped_column(String(100), index=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), index=True, nullable=False
    )
    rack_id: Mapped[int | None] = mapped_column(ForeignKey("racks.id", ondelete="SET NULL"))
    rack_position: Mapped[int | None] = mapped_column()
    description: Mapped[str | None] = mapped_column(Text)

    site: Mapped[Site] = relationship(back_populates="devices")
    rack: Mapped[Rack | None] = relationship(back_populates="devices")
    ip_addresses: Mapped[list[IPAddress]] = relationship(
        back_populates="device", cascade="all, delete-orphan"
    )


class IPAddress(Base, TimestampMixin):
    """An IP address (stored in CIDR form, e.g. ``10.0.0.1/24``)."""

    __tablename__ = "ip_addresses"
    __table_args__ = (UniqueConstraint("address", name="uq_ip_address"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    address: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False)
    dns_name: Mapped[str | None] = mapped_column(String(255), index=True)
    role: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), index=True
    )
    interface: Mapped[str | None] = mapped_column(String(100))

    device: Mapped[Device | None] = relationship(back_populates="ip_addresses")
