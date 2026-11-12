"""Settings: storage thresholds, retention rules, admin password."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AdminUser
from ..security import (admin_deps, hash_password, login_session, require_admin, validate_new_password,
                        verify_password)
from ..services import retention
from ..services import settings as svc
from ..services.activity import log_event
from ..templating import flash, render

router = APIRouter(dependencies=admin_deps)


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db)):
    return render(request, "settings.html", active="settings", cfg=svc.load(db))


@router.post("/settings/storage")
def save_storage(request: Request, warn_percent: int = Form(...), critical_percent: int = Form(...),
                 temp_warn_c: int = Form(...), active_days: int = Form(...),
                 user: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    if not (1 <= warn_percent < critical_percent <= 100) or not (20 <= temp_warn_c <= 100) or not (1 <= active_days <= 365):
        flash(request, "Invalid values: warning must be below critical (1–100%), temperature 20–100°C, active window 1–365 days.", "error")
    else:
        for k, v in dict(warn_percent=warn_percent, critical_percent=critical_percent, temp_warn_c=temp_warn_c,
                         active_days=active_days).items():
            svc.set_raw(db, k, str(v))
        log_event(db, "settings", f"Storage limits updated (warn {warn_percent}%, critical {critical_percent}%)", actor=user.username)
        flash(request, "Storage settings saved.")
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/retention")
def save_retention(request: Request, retention_days: int = Form(...), retention_enabled: str = Form(""),
                   user: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    if not (1 <= retention_days <= 36500):
        flash(request, "Retention must be between 1 and 36500 days.", "error")
    else:
        svc.set_raw(db, "retention_days", str(retention_days))
        svc.set_raw(db, "retention_enabled", "1" if retention_enabled == "on" else "0")
        log_event(db, "settings", f"Retention {'enabled' if retention_enabled == 'on' else 'disabled'}: {retention_days} days",
                  level="warning" if retention_enabled == "on" else "info", actor=user.username)
        flash(request, "Retention rules saved.")
    return RedirectResponse("/settings", status_code=303)


@router.get("/internal/retention/preview")
def retention_preview(days: int):
    days = max(1, min(days, 36500))
    p = retention.plan(days)
    return {"days": days, "count": p.count, "bytes": p.bytes,
            "sample": [{"path": r, "size": s, "mtime": m} for r, s, m in p.sample[:20]]}


@router.post("/settings/retention/run")
def run_retention(request: Request, retention_days: int = Form(...), user: AdminUser = Depends(require_admin),
                  db: Session = Depends(get_db)):
    if not (1 <= retention_days <= 36500):
        flash(request, "Invalid retention period.", "error")
    else:
        deleted, freed = retention.execute(db, retention_days, actor=user.username)
        flash(request, f"Retention run complete: {deleted} files removed, {freed / 1e6:.1f} MB freed.")
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/password")
def change_password(request: Request, current_password: str = Form(...), new_password: str = Form(...),
                    confirm_password: str = Form(...), user: AdminUser = Depends(require_admin),
                    db: Session = Depends(get_db)):
    error = None
    if not verify_password(current_password, user.password_hash):
        error = "Current password is incorrect."
    elif new_password != confirm_password:
        error = "New passwords do not match."
    else:
        error = validate_new_password(new_password)
    if error:
        flash(request, error, "error")
    else:
        user.password_hash = hash_password(new_password)
        db.commit()
        login_session(request, user)  # keep this browser signed in; every other session is invalidated
        log_event(db, "auth", "Admin password changed", level="warning", actor=user.username)
        flash(request, "Password changed. Other sessions were signed out.")
    return RedirectResponse("/settings", status_code=303)
