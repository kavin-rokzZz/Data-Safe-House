"""Admin: issue invite codes and manage portal users and their device access."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..database import get_db, utcnow
from ..models import AdminUser, Device, Invite, PortalUser, UserDevice
from ..security import admin_deps, hash_password, require_admin
from ..services import devices as device_service
from ..services import portal as svc
from ..services.activity import log_event
from ..templating import flash, render

router = APIRouter(dependencies=admin_deps)


def _user(db: Session, user_id: int) -> PortalUser:
    u = db.get(PortalUser, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    return u


def _valid_slugs(db: Session, slugs: list[str]) -> list[str]:
    known = {d.slug for d in db.scalars(select(Device))}
    return [s for s in dict.fromkeys(slugs) if s in known or svc.assignable_slug_ok(s)]


@router.get("/users")
def users_page(request: Request, db: Session = Depends(get_db)):
    device_service.sync_devices(db)
    users = list(db.scalars(select(PortalUser).order_by(PortalUser.username)))
    links = db.execute(select(UserDevice.user_id, Device).join(Device, Device.id == UserDevice.device_id)).all()
    by_user: dict[int, list[Device]] = {}
    for uid, dev in links:
        by_user.setdefault(uid, []).append(dev)
    return render(request, "users.html", active="users", users=users, by_user=by_user,
                  devices=list(db.scalars(select(Device).order_by(Device.name))),
                  invites=list(db.scalars(select(Invite).order_by(Invite.id.desc()).limit(20))), now=utcnow(),
                  new_invite=request.session.pop("new_invite", None), temp_pw=request.session.pop("temp_pw", None),
                  expiry_choices=svc.INVITE_EXPIRY)


@router.post("/users/invites")
def create_invite(request: Request, label: str = Form(""), minutes: int = Form(1440),
                  devices: list[str] = Form(default=[]), user: AdminUser = Depends(require_admin),
                  db: Session = Depends(get_db)):
    if minutes not in svc.INVITE_EXPIRY:
        minutes = 1440
    slugs = _valid_slugs(db, devices)
    code, inv = svc.create_invite(db, label, slugs, minutes, user.username)
    log_event(db, "device", f"Invite code created '{inv.label or 'unlabelled'}' "
                            f"({', '.join(slugs) or 'new folder'}; valid {svc.INVITE_EXPIRY[minutes]})", actor=user.username)
    # Shown exactly once on the next page load; only the hash is stored.
    request.session["new_invite"] = {"code": code, "label": inv.label, "expires": inv.expires_at.isoformat(),
                                     "url": f"{str(request.base_url).rstrip('/')}/portal/signup?code={code}",
                                     "devices": slugs}
    return RedirectResponse("/users", status_code=303)


@router.post("/users/invites/{invite_id}/revoke")
def revoke_invite(request: Request, invite_id: int, user: AdminUser = Depends(require_admin),
                  db: Session = Depends(get_db)):
    inv = db.get(Invite, invite_id)
    if not inv:
        raise HTTPException(404, "Invite not found")
    inv.revoked = True
    db.commit()
    log_event(db, "device", f"Invite code revoked '{inv.label or 'unlabelled'}'", actor=user.username)
    flash(request, "Invite revoked.")
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/devices")
def set_devices(request: Request, user_id: int, devices: list[str] = Form(default=[]),
                admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    u = _user(db, user_id)
    slugs = _valid_slugs(db, devices)
    rows = []
    for slug in slugs:
        dev = db.scalar(select(Device).where(Device.slug == slug))
        if dev is None:
            dev = Device(slug=slug, name=slug)
            db.add(dev)
            db.flush()
        rows.append(UserDevice(user_id=u.id, device_id=dev.id))
    db.execute(delete(UserDevice).where(UserDevice.user_id == u.id))
    db.add_all(rows)
    db.commit()
    log_event(db, "device", f"Devices for '{u.username}' set to: {', '.join(slugs) or 'none'}", actor=admin.username)
    flash(request, f"Updated devices for {u.username}.")
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/toggle")
def toggle(request: Request, user_id: int, admin: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    u = _user(db, user_id)
    u.disabled = not u.disabled  # takes effect on the user's very next request
    db.commit()
    log_event(db, "auth", f"User '{u.username}' {'disabled' if u.disabled else 'enabled'}", level="warning",
              actor=admin.username)
    flash(request, f"{u.username} {'disabled' if u.disabled else 'enabled'}.")
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/reset-password")
def reset_password(request: Request, user_id: int, admin: AdminUser = Depends(require_admin),
                   db: Session = Depends(get_db)):
    u = _user(db, user_id)
    temp = "".join(secrets.choice(svc.CODE_ALPHABET.lower() + svc.CODE_ALPHABET) for _ in range(14)) + "a1A"
    u.password_hash = hash_password(temp)  # also invalidates every existing session of this user
    db.commit()
    log_event(db, "auth", f"Password reset for '{u.username}'", level="warning", actor=admin.username)
    request.session["temp_pw"] = {"username": u.username, "password": temp}
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/delete")
def delete_user(request: Request, user_id: int, admin: AdminUser = Depends(require_admin),
                db: Session = Depends(get_db)):
    u = _user(db, user_id)
    db.execute(delete(UserDevice).where(UserDevice.user_id == u.id))
    db.delete(u)
    db.commit()
    log_event(db, "auth", f"User '{u.username}' deleted (files kept)", level="warning", actor=admin.username)
    flash(request, f"Deleted {u.username}. Their files were not touched.")
    return RedirectResponse("/users", status_code=303)
