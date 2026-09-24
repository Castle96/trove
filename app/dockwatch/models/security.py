"""SQLAlchemy model for cached Trivy image-scan results."""

from __future__ import annotations

from sqlalchemy import BigInteger, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.dockwatch.database import Base
from app.dockwatch.models.inventory import TimestampMixin


class ImageScan(Base, TimestampMixin):
    """Cached vulnerability summary for one image ref (``repo:tag``)."""

    __tablename__ = "image_scans"

    id: Mapped[int] = mapped_column(primary_key=True)
    image_ref: Mapped[str] = mapped_column(String(500), unique=True, index=True, nullable=False)
    digest: Mapped[str | None] = mapped_column(String(255))
    scanned_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    critical: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    high: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    medium: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    low: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unknown: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Full Trivy JSON report (compressed by omission: targets with no vulns dropped).
    report: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    endpoint_id: Mapped[int | None] = mapped_column(default=None)

    @property
    def total(self) -> int:
        return self.critical + self.high + self.medium + self.low + self.unknown
