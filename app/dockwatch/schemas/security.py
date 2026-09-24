"""Pydantic schemas for Trivy image vulnerability scans."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ScanRequest(BaseModel):
    image: str = Field(min_length=1, max_length=500)


class ImageScanRead(ORMModel):
    id: int
    image_ref: str
    digest: str | None = None
    scanned_at_ms: int = 0
    critical: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0
    unknown: int = 0
    total: int = 0
    endpoint_id: int | None = None
    created_at: datetime
    updated_at: datetime


class ImageScanDetail(ImageScanRead):
    vulnerabilities: list[dict[str, object]] = Field(default_factory=list)
