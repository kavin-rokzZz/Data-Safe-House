"""Device management (rename, revoke, restore)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AdminUser, Device, PortalUser, UserDevice
from ..security import admin_deps, require_admin
from ..services import devices as device_service
from ..services.activity import log_event
from ..templating import flash, render

router = APIRouter(dependencies=admin_deps)

KINDS = {"windows", "iphone", "other"}


def _device(db: Session, device_id: int) -> Device:
    dev = db.get(Device, device_id)
    if not dev:
        raise HTTPException(404, "Device not found")
    return dev


@router.get("/devices")
def devices_page(request: Request, db: Session = Depends(get_db)):
    device_service.sync_devices(db)
    devices = list(db.scalars(select(Device).order_by(Device.name)))
    owners: dict[int, list[str]] = {}
    for dev_id, username in db.execute(select(UserDevice.device_id, PortalUser.username)
                                       .join(PortalUser, PortalUser.id == UserDevice.user_id)):
        owners.setdefault(dev_id, []).append(username)
    return render(request, "devices.html", active="devices", devices=devices, owners=owners,
                  hook_enabled=bool(device_service.config.device_hook))


@router.post("/devices/{device_id}/rename")
def rename(request: Request, device_id: int, name: str = Form(...), kind: str = Form("other"),
           user: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    dev = _device(db, device_id)
    name = name.strip()[:128]
    if not name:
        flash(request, "Name cannot be empty.", "error")
    else:
        old, dev.name = dev.name, name
        dev.kind = kind if kind in KINDS else dev.kind
        db.commit()
        log_event(db, "device", f"Renamed device '{old}' to '{name}'", actor=user.username)
        flash(request, "Device updated.")
    return RedirectResponse("/devices", status_code=303)


@router.post("/devices/{device_id}/revoke")
def revoke(request: Request, device_id: int, user: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    dev = _device(db, device_id)
    dev.revoked = True  # its owners lose web access to this folder immediately
    db.commit()
    log_event(db, "device", f"Revoked access for '{dev.name}' ({dev.slug})", level="warning", actor=user.username)
    device_service.run_hook(db, "revoke", dev.slug, user.username)
    flash(request, f"Access revoked for {dev.name}.")
    return RedirectResponse("/devices", status_code=303)


@router.post("/devices/{device_id}/restore")
def restore(request: Request, device_id: int, user: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    dev = _device(db, device_id)
    dev.revoked = False
    db.commit()
    log_event(db, "device", f"Restored access for '{dev.name}' ({dev.slug})", actor=user.username)
    device_service.run_hook(db, "restore", dev.slug, user.username)
    flash(request, f"{dev.name} restored.")
    return RedirectResponse("/devices", status_code=303)
