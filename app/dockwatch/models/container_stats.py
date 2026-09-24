"""SQLAlchemy model for persisted container stats snapshots (per-contAINER resource history)."""

from __future__ import annotations

from sqlalchemy import BigInteger, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.dockwatch.database import Base


class ContainerStatsSnapshot(Base):
    """One container stats sample, persisted on each poll cycle.

    ``short_id`` + ``ts`` is the lookup key for per-container history.
    ``endpoint_id`` is NULL for the local engine.
    """

    __tablename__ = "container_stats_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    short_id: Mapped[str] = mapped_column(String(13), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    image: Mapped[str] = mapped_column(String(500), nullable=False)
    cpu_percent: Mapped[float] = mapped_column(Float, nullable=False)
    memory_percent: Mapped[float] = mapped_column(Float, nullable=False)
    memory_usage: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    memory_limit: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    network_rx: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    network_tx: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    endpoint_id: Mapped[int | None] = mapped_column(
        ForeignKey("endpoints.id", ondelete="SET NULL"), index=True, default=None
    )
