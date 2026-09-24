"""SQLAlchemy model for persisted host-monitoring samples."""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.dockwatch.database import Base


class MonitorSample(Base):
    """A single host-metrics sample persisted by the background sampler.

    Only the fields needed by the dashboards and downstream analytics are
    stored (full per-interface/per-disk detail is recomputed on demand).
    ``cores`` is a JSON-encoded list of per-core CPU percentages.
    """

    __tablename__ = "monitor_samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    cpu: Mapped[float] = mapped_column(Float, nullable=False)
    mem_percent: Mapped[float] = mapped_column(Float, nullable=False)
    mem_used: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    mem_total: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    swap_percent: Mapped[float] = mapped_column(Float, nullable=False)
    swap_used: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    swap_total: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    load1: Mapped[float] = mapped_column(Float, nullable=False)
    load5: Mapped[float] = mapped_column(Float, nullable=False)
    load15: Mapped[float] = mapped_column(Float, nullable=False)
    net_rx: Mapped[float] = mapped_column(Float, nullable=False)
    net_tx: Mapped[float] = mapped_column(Float, nullable=False)
    disk_read: Mapped[float] = mapped_column(Float, nullable=False)
    disk_write: Mapped[float] = mapped_column(Float, nullable=False)
    z_cpu: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    z_mem: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cpu_anomaly: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mem_anomaly: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: JSON-encoded list of per-core CPU percentages.
    cores: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    #: Host the sample was recorded on (useful if the dashboard DB is shared).
    host: Mapped[str] = mapped_column(String(255), default="localhost", nullable=False)
    #: Owning endpoint (NULL = local host). Remote agents push with an id.
    endpoint_id: Mapped[int | None] = mapped_column(
        ForeignKey("endpoints.id", ondelete="SET NULL"), index=True, default=None
    )
