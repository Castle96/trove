"""Trivy image vulnerability scanning service.

Runs the ``trivy`` binary (installed separately — see README) against a
Podman/Docker image ref and caches the severity summary + trimmed report in
SQLite. Scans run in a worker thread with a timeout; one scan at a time
(Trivy is I/O- and CPU-heavy).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.dockwatch.config import get_settings
from app.dockwatch.database import get_session_factory
from app.dockwatch.models.security import ImageScan

logger = logging.getLogger(__name__)

_scan_lock = asyncio.Lock()


class TrivyNotInstalledError(RuntimeError):
    """Raised when no ``trivy`` binary can be found."""


class TrivyScanError(RuntimeError):
    """Raised when a scan fails or times out."""


def resolve_trivy_bin() -> str:
    """Locate the trivy binary (PATH plus ``~/.local/bin`` fallback)."""
    settings = get_settings()
    candidates = [settings.trivy_bin, str(Path.home() / ".local" / "bin" / "trivy"), "trivy"]
    for candidate in candidates:
        found = shutil.which(candidate) if "/" not in candidate else candidate
        if found and Path(found).exists():
            return found
        if "/" not in candidate and shutil.which(candidate):
            return shutil.which(candidate) or candidate
    raise TrivyNotInstalledError(
        "trivy binary not found (set DOCKWATCH_TRIVY_BIN or install trivy)"
    )


def normalize_image_ref(ref: str) -> str:
    """Canonical cache key: ``nginx`` → ``docker.io/library/nginx:latest``.

    Prevents duplicate rows for the same image written different ways.
    """
    ref = ref.strip().lower()
    if "://" in ref:
        ref = ref.split("://", 1)[1]
    last_slash = ref.rfind("/")
    last_colon = ref.rfind(":")
    if last_colon <= last_slash:
        ref = f"{ref}:latest"
    if "/" not in ref:
        ref = f"docker.io/library/{ref}"
    elif ref.count("/") == 1:
        first = ref.split("/")[0]
        if "." not in first and ":" not in first and first != "localhost":
            ref = f"docker.io/{ref}"
    return ref


def summarize_report(report: dict[str, Any]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Count severities and flatten vulnerabilities across all targets."""
    counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0, "UNKNOWN": 0}
    flat: list[dict[str, Any]] = []
    for result in report.get("Results") or []:
        target = result.get("Target", "")
        for vuln in result.get("Vulnerabilities") or []:
            severity = str(vuln.get("Severity", "UNKNOWN")).upper()
            counts[severity] = counts.get(severity, 0) + 1
            flat.append(
                {
                    "target": target,
                    "vulnerability_id": vuln.get("VulnerabilityID"),
                    "package": vuln.get("PkgName"),
                    "installed_version": vuln.get("InstalledVersion"),
                    "fixed_version": vuln.get("FixedVersion"),
                    "severity": severity,
                    "title": vuln.get("Title"),
                    "primary_url": vuln.get("PrimaryURL"),
                }
            )
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNKNOWN": 4}
    flat.sort(key=lambda v: (order.get(str(v["severity"]), 5), str(v["vulnerability_id"])))
    return counts, flat


def _run_scan_sync(image_ref: str, image_src: str, timeout: float) -> dict[str, Any]:
    binary = resolve_trivy_bin()
    proc = subprocess.run(
        [
            binary,
            "image",
            "--image-src",
            image_src,
            "--scanners",
            "vuln",
            "--format",
            "json",
            "--quiet",
            image_ref,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise TrivyScanError((proc.stderr or proc.stdout or "trivy failed").strip()[-500:])
    try:
        parsed: dict[str, Any] = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise TrivyScanError(f"trivy returned invalid JSON: {exc}") from exc
    return parsed


async def _engine_digest(image_ref: str) -> str | None:
    """Image digest from the engine (fallback when Trivy omits it for podman)."""
    try:
        from app.dockwatch.services.docker_service import DockerService

        return await DockerService().image_digest(image_ref)
    except Exception:
        return None


async def scan_image(
    image_ref: str, endpoint_id: int | None = None, force: bool = False
) -> ImageScan:
    """Scan ``image_ref`` (using cache unless ``force`` or stale)."""
    settings = get_settings()
    key = normalize_image_ref(image_ref)
    now_ms = int(time.time() * 1000)
    stale_after_ms = int(settings.trivy_cache_hours * 3600 * 1000)

    async with get_session_factory()() as session:
        cached = (
            await session.scalars(select(ImageScan).where(ImageScan.image_ref == key))
        ).one_or_none()
        if cached is not None and not force and now_ms - cached.scanned_at_ms < stale_after_ms:
            return cached

    async with _scan_lock:
        try:
            report = await asyncio.to_thread(
                _run_scan_sync, key, settings.trivy_image_src, settings.trivy_timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise TrivyScanError(f"trivy scan timed out after {settings.trivy_timeout}s") from exc
        counts, flat = summarize_report(report)
        metadata = report.get("Metadata") or {}
        digest = metadata.get("ImageDigest") or await _engine_digest(key)

    async with get_session_factory()() as session:
        row = (
            await session.scalars(select(ImageScan).where(ImageScan.image_ref == key))
        ).one_or_none()
        if row is None:
            row = ImageScan(image_ref=key)
            session.add(row)
        row.digest = digest
        row.scanned_at_ms = int(time.time() * 1000)
        row.critical = counts.get("CRITICAL", 0)
        row.high = counts.get("HIGH", 0)
        row.medium = counts.get("MEDIUM", 0)
        row.low = counts.get("LOW", 0)
        row.unknown = counts.get("UNKNOWN", 0)
        row.report = json.dumps(flat)
        row.endpoint_id = endpoint_id
        await session.commit()
        await session.refresh(row)
        return row


async def get_cached_scan(image_ref: str) -> ImageScan | None:
    """Return the cached scan for ``image_ref`` without scanning."""
    key = normalize_image_ref(image_ref)
    async with get_session_factory()() as session:
        return (
            await session.scalars(select(ImageScan).where(ImageScan.image_ref == key))
        ).one_or_none()


async def list_scans() -> list[ImageScan]:
    """All cached scans, most severe first."""
    async with get_session_factory()() as session:
        rows = (await session.scalars(select(ImageScan))).all()
    return sorted(rows, key=lambda r: (r.critical, r.high, r.medium, r.low), reverse=True)
