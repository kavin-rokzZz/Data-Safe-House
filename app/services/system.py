"""Live system metrics (CPU, RAM, disk usage, temperatures, disk I/O)."""
from __future__ import annotations

import os
import shutil
import threading
import time

import psutil

from ..config import config

_io_lock = threading.Lock()
_last_io: tuple[float, object] | None = None

psutil.cpu_percent(interval=None)  # prime the counter


def disk_usage() -> dict:
    path = config.backup_root if config.backup_root.exists() else "/"
    u = shutil.disk_usage(path)
    return {"total": u.total, "used": u.used, "free": u.free, "percent": round(u.used / u.total * 100, 1) if u.total else 0.0,
            "path": str(path)}


def _temperatures() -> dict[str, float | None]:
    out: dict[str, float | None] = {"cpu": None, "disk": None}
    try:
        temps = psutil.sensors_temperatures()
    except Exception:
        temps = {}
    for name, entries in temps.items():
        if not entries:
            continue
        cur = max((e.current for e in entries if e.current is not None), default=None)
        if name in {"drivetemp", "nvme"}:
            out["disk"] = cur
        elif out["cpu"] is None and name in {"cpu_thermal", "coretemp", "k10temp", "cpu-thermal", "soc_thermal"}:
            out["cpu"] = cur
    if out["cpu"] is None:  # Raspberry Pi fallback
        try:
            out["cpu"] = int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
        except Exception:
            pass
    return out


def _disk_io() -> dict:
    """Read/write throughput (MB/s) and busy % since the previous call."""
    global _last_io
    try:
        now, cur = time.time(), psutil.disk_io_counters()
    except Exception:
        return {"read_mbps": None, "write_mbps": None, "busy": None}
    if cur is None:
        return {"read_mbps": None, "write_mbps": None, "busy": None}
    with _io_lock:
        prev, _last_io = _last_io, (now, cur)
    if not prev:
        return {"read_mbps": 0.0, "write_mbps": 0.0, "busy": 0.0}
    dt = max(now - prev[0], 0.001)
    p = prev[1]
    busy = None
    if hasattr(cur, "busy_time"):
        busy = min(100.0, max(0.0, (cur.busy_time - p.busy_time) / (dt * 1000) * 100))
    return {
        "read_mbps": round((cur.read_bytes - p.read_bytes) / dt / 1e6, 2),
        "write_mbps": round((cur.write_bytes - p.write_bytes) / dt / 1e6, 2),
        "busy": None if busy is None else round(busy, 1),
    }


def metrics() -> dict:
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:
        load1 = load5 = load15 = 0.0
    return {
        "cpu_percent": psutil.cpu_percent(interval=None),
        "cpu_count": psutil.cpu_count() or 1,
        "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
        "ram": {"total": vm.total, "used": vm.total - vm.available, "percent": vm.percent},
        "swap": {"total": sw.total, "used": sw.used, "percent": sw.percent},
        "temps": _temperatures(),
        "io": _disk_io(),
        "uptime": int(time.time() - psutil.boot_time()),
        "disk": disk_usage(),
        "backup_root_exists": config.backup_root.is_dir(),
    }
