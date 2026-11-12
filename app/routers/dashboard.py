"""Dashboard page and live metrics endpoints."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import ActivityLog, Device
from ..security import admin_deps
from ..services import devices as device_service
from ..services import settings as settings_service
from ..services import storage, system
from ..services.devices import _from_epoch
from ..templating import render

router = APIRouter(dependencies=admin_deps)


def _stats(db: Session, force: bool = False) -> dict:
    device_service.sync_devices(db, force=force)
    cfg = settings_service.load(db)
    scan = storage.scan()
    cutoff = time.time() - cfg.active_days * 86400
    devices = list(db.scalars(select(Device)))
    active = [d for d in devices if not d.revoked and scan.devices.get(d.slug) and scan.devices[d.slug].newest_mtime >= cutoff]
    return {
        "device_total": len(devices),
        "device_active": len(active),
        "active_days": cfg.active_days,
        "file_count": scan.file_count,
        "total_bytes": scan.total_bytes,
        "last_backup": scan.newest_mtime,
        "scan_seconds": round(scan.duration, 2),
    }


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    cfg = settings_service.load(db)
    recent = list(db.scalars(select(ActivityLog).order_by(ActivityLog.id.desc()).limit(8)))
    devices = list(db.scalars(select(Device).order_by(Device.last_backup_at.desc().nulls_last())))
    return render(request, "dashboard.html", active="dashboard", cfg=cfg, stats=_stats(db), metrics=system.metrics(),
                  recent=recent, devices=devices, last_backup_dt=_from_epoch(storage.scan().newest_mtime))


@router.get("/internal/metrics")
def metrics():
    return system.metrics()


@router.get("/internal/stats")
def stats(refresh: bool = False, db: Session = Depends(get_db)):
    return _stats(db, force=refresh)
