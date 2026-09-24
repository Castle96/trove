"""Host monitoring service (Glances-style).

Built on ``psutil`` (the same library Glances uses underneath). The singleton
``monitor_service`` is fed by a background sampler (see ``monitor_loop.py``),
which persists every sample to SQLite so the start-page graphs survive server
restarts.

Anomaly detection: each new CPU/memory value is compared against a rolling
window of its own recent history using a z-score. Values that deviate by more
than the configured threshold are flagged (``cpu_anomaly`` / ``mem_anomaly``).

Rates (network RX/TX, disk read/write) are computed from counter deltas
between consecutive samples. If a gap is too large (page idle), the rates are
reported as zero and the baseline resets, so the graphs don't spike.
"""

from __future__ import annotations

import math
import socket
import threading
import time
from collections import deque
from collections.abc import Sequence
from platform import machine, platform, processor, release, system
from typing import Any, TypedDict, cast

import psutil  # type: ignore[import-untyped]

from app.dockwatch.config import get_settings

MAX_HISTORY = 300  # 10 minutes at a 2s poll interval
ZSCORE_WINDOW = 90  # samples prior to the current one used for baseline


class MemoryDict(TypedDict):
    percent: float
    used: int
    total: int


class SwapDict(TypedDict):
    percent: float
    used: int
    total: int


class RateDict(TypedDict):
    rx: float
    tx: float


class DiskRateDict(TypedDict):
    read: float
    write: float


class MonitorSampleDict(TypedDict):
    """Shape of one in-memory / persisted monitor sample."""

    t: int
    cpu: float
    cores: list[float]
    memory: MemoryDict
    swap: SwapDict
    load1: float
    load5: float
    load15: float
    net: RateDict
    disk: DiskRateDict
    z_cpu: float
    z_mem: float
    cpu_anomaly: bool
    mem_anomaly: bool


class SystemInfoDict(TypedDict):
    hostname: str
    platform: str
    os_icon: str
    cpu_model: str
    cores: int
    uptime_seconds: int
    cpu_freq_mhz: float | None


class ProcessDict(TypedDict):
    pid: int | None
    name: str
    user: str | None
    cpu: float
    mem: float
    cmdline: str


class DiskUsageDict(TypedDict):
    mountpoint: str
    device: str
    fs_type: str
    percent: float
    used: int
    total: int


class MonitorUnavailableError(RuntimeError):
    """Raised when host metrics cannot be read."""


def zscore(value: float, baseline: Sequence[float]) -> float:
    """Sample z-score relative to ``baseline`` (its own recent history).

    Returns 0.0 when there is no usable baseline (std ≈ 0 and the value
    matches the mean renders no deviation; a different value saturates).
    """
    n = len(baseline)
    if n == 0:
        return 0.0
    mean = sum(baseline) / n
    variance = 0.0 if n == 1 else sum((x - mean) ** 2 for x in baseline) / (n - 1)
    std = math.sqrt(variance)
    if std < 1e-9:
        return 0.0 if abs(value - mean) < 1e-9 else 50.0
    return (value - mean) / std


def is_anomaly(z: float, threshold: float) -> bool:
    """True when ``|z|`` exceeds ``threshold``."""
    return abs(z) >= threshold


class MonitorService:
    """Collects and buffers host metrics."""

    def __init__(self, history: int = MAX_HISTORY) -> None:
        self._history: deque[MonitorSampleDict] = deque(maxlen=history)
        self._lock = threading.Lock()
        self._settings = get_settings()

        # Baseline counters for rate computation.
        self._last_time: float | None = None
        self._last_rx = 0
        self._last_tx = 0
        self._last_read = 0
        self._last_write = 0

    # ------------------------------------------------------------------ public
    def snapshot(self) -> dict[str, Any]:
        """Return a full snapshot: latest sample + system info + detail.

        If no sample has been recorded yet (e.g. brand-new deployment), a
        sample is forced so the overview works immediately.
        """
        sample = self.latest()
        if sample is None:
            try:
                sample = self.sample()
            except (psutil.Error, OSError, RuntimeError) as exc:
                raise MonitorUnavailableError(f"Failed to read host metrics: {exc}") from exc

        processes = self._top_processes(limit=8)
        disks = self._disk_usage()
        net_detail = self._network_interfaces()
        disk_detail = self._disk_io_detail()
        return {
            "available": True,
            "reason": None,
            "system": self._system_info(),
            "processes": processes,
            "fs_usage": disks,
            **sample,
            "net": {**sample["net"], "interfaces": net_detail},
            "disk": {**sample["disk"], "disks": disk_detail},
        }

    def sample(self) -> MonitorSampleDict:
        """Read current metrics, flag anomalies, append to rolling history.

        The returned dict is the same shape stored in the series: t, cpu,
        cores, mem, swap, load1/5/15, rx, tx, read, write plus anomaly flags.
        """
        with self._lock:
            now = time.monotonic()
            delta = 0.0
            if self._last_time is not None:
                delta = now - self._last_time

            cpu = max(float(psutil.cpu_percent(interval=None) or 0.0), 0.0)
            cores = [
                max(float(v), 0.0) for v in (psutil.cpu_percent(interval=None, percpu=True) or [])
            ]
            mem = psutil.virtual_memory()
            swap = psutil.swap_memory()
            load = psutil.getloadavg() if hasattr(psutil, "getloadavg") else (0.0, 0.0, 0.0)

            net = psutil.net_io_counters()
            disk = psutil.disk_io_counters()

            # Rates: clamp short gaps only; reset baselines on long gaps.
            if self._last_time is None or not (0.01 < delta <= 10.0):
                rx_rate = tx_rate = read_rate = write_rate = 0.0
            else:
                rx_rate = max((net.bytes_recv - self._last_rx) / delta, 0.0)
                tx_rate = max((net.bytes_sent - self._last_tx) / delta, 0.0)
                read_rate = max((disk.read_bytes - self._last_read) / delta, 0.0)
                write_rate = max((disk.write_bytes - self._last_write) / delta, 0.0)

            self._last_time = now
            self._last_rx = net.bytes_recv
            self._last_tx = net.bytes_sent
            self._last_read = disk.read_bytes
            self._last_write = disk.write_bytes

            threshold = self._settings.monitor_anomaly_zscore
            baseline_cpu = [s["cpu"] for s in list(self._history)[-ZSCORE_WINDOW:]]
            baseline_mem = [s["memory"]["percent"] for s in list(self._history)[-ZSCORE_WINDOW:]]
            z_cpu = zscore(cpu, baseline_cpu)
            z_mem = zscore(mem.percent, baseline_mem)

            sample: MonitorSampleDict = {
                "t": int(time.time() * 1000),
                "cpu": round(cpu, 1),
                "cores": [round(c, 1) for c in cores],
                "memory": {
                    "percent": round(mem.percent, 1),
                    "used": mem.used,
                    "total": mem.total,
                },
                "swap": {
                    "percent": round(swap.percent, 1),
                    "used": swap.used,
                    "total": swap.total,
                },
                "load1": round(load[0], 2),
                "load5": round(load[1], 2),
                "load15": round(load[2], 2),
                "net": {
                    "rx": round(rx_rate, 2),
                    "tx": round(tx_rate, 2),
                },
                "disk": {
                    "read": round(read_rate, 2),
                    "write": round(write_rate, 2),
                },
                "z_cpu": round(z_cpu, 2) if z_cpu else 0.0,
                "z_mem": round(z_mem, 2) if z_mem else 0.0,
                "cpu_anomaly": is_anomaly(z_cpu, threshold),
                "mem_anomaly": is_anomaly(z_mem, threshold),
            }
            self._history.append(sample)
            return sample

    def latest(self) -> MonitorSampleDict | None:
        """Most recent sample, or ``None`` if none has been recorded."""
        with self._lock:
            if not self._history:
                return None
            return cast(MonitorSampleDict, dict(self._history[-1]))

    def set_history(self, samples: Sequence[MonitorSampleDict]) -> None:
        """Seed the in-memory history from persisted samples (restart recovery)."""
        with self._lock:
            self._history.clear()
            for sample in samples:
                self._history.append(sample)

    def series(self, limit: int) -> list[MonitorSampleDict]:
        """Return up to ``limit`` most recent samples, oldest first."""
        with self._lock:
            return [cast(MonitorSampleDict, dict(s)) for s in list(self._history)[-limit:]]

    def reset(self) -> None:
        """Clear history and baselines (used by tests)."""
        with self._lock:
            self._history.clear()
            self._last_time = None
            self._last_rx = self._last_tx = self._last_read = self._last_write = 0

    # ------------------------------------------------------------------ internals
    def _system_info(self) -> SystemInfoDict:
        boot = getattr(psutil, "boot_time", lambda: time.time())()
        freq: float | None = None
        try:
            if hasattr(psutil, "cpu_freq"):
                f = psutil.cpu_freq()
                freq = round(float(f.current), 0) if f else None
        except (psutil.Error, OSError):
            freq = None
        return {
            "hostname": socket.gethostname(),
            "platform": f"{system()} {release()} ({machine()})",
            "os_icon": platform(),
            "cpu_model": processor() or "unknown",
            "cores": int(psutil.cpu_count(logical=True) or 1),
            "uptime_seconds": max(int(time.time() - boot), 0),
            "cpu_freq_mhz": freq,
        }

    def _network_interfaces(self) -> dict[str, dict[str, int]]:
        """Per-interface cumulative byte counters (detail view only)."""
        try:
            counters = psutil.net_io_counters(pernic=True)
        except (psutil.Error, OSError):
            return {}
        return {
            str(name): {"rx_bytes": int(c.bytes_recv), "tx_bytes": int(c.bytes_sent)}
            for name, c in sorted(counters.items())
        }

    def _disk_io_detail(self) -> dict[str, dict[str, int]]:
        try:
            counters = psutil.disk_io_counters(perdisk=True) or {}
        except (psutil.Error, OSError):
            return {}
        return {
            str(name): {"read_bytes": int(c.read_bytes), "write_bytes": int(c.write_bytes)}
            for name, c in sorted(counters.items())
        }

    def _disk_usage(self) -> list[DiskUsageDict]:
        """Filesystem usage percentages for real mounts."""
        result: list[DiskUsageDict] = []
        seen: set[str] = set()
        for part in psutil.disk_partitions(all=False):
            if part.fstype in {"squashfs", "proc", "sysfs", "devtmpfs", "tmpfs", "overlay"}:
                continue
            if part.device.startswith("/dev/loop"):
                continue
            if part.mountpoint in seen:
                continue
            seen.add(part.mountpoint)
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (psutil.Error, OSError, PermissionError):
                continue
            result.append(
                {
                    "mountpoint": part.mountpoint,
                    "device": part.device,
                    "fs_type": part.fstype,
                    "percent": round(usage.percent, 1),
                    "used": int(usage.used),
                    "total": int(usage.total),
                }
            )
        return sorted(result, key=lambda d: d["percent"], reverse=True)

    def _top_processes(self, limit: int) -> list[ProcessDict]:
        """Top ``limit`` processes by CPU, then by memory."""
        procs: list[ProcessDict] = []
        for proc in psutil.process_iter(
            ["pid", "name", "username", "cpu_percent", "memory_percent", "cmdline"]
        ):
            try:
                info = proc.info
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
            cmd = info.get("cmdline") or []
            cmdline = " ".join(str(c) for c in cmd[:3]) if cmd else info.get("name", "")
            procs.append(
                {
                    "pid": int(info.get("pid")) if info.get("pid") is not None else None,
                    "name": str(info.get("name", "?")),
                    "user": str(info.get("username")) if info.get("username") is not None else None,
                    "cpu": max(float(info.get("cpu_percent") or 0.0), 0.0),
                    "mem": max(float(info.get("memory_percent") or 0.0), 0.0),
                    "cmdline": str(cmdline),
                }
            )
        procs.sort(key=lambda p: (p["cpu"], p["mem"]), reverse=True)
        return procs[:limit]


monitor_service = MonitorService()
