"""User-portal rules: which folders a user may touch, and how sign-up invites are created and redeemed."""
from __future__ import annotations

import re
import secrets
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import AdminUser, Device, Invite, PortalUser, UserDevice
from ..security import hash_token
from . import storage
from .devices import make_slug

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I confusion
INVITE_EXPIRY = {60: "1 hour", 1440: "24 hours", 4320: "3 days", 10080: "7 days"}


def normalize_code(code: str) -> str:
    return "".join(c for c in code.upper() if c.isalnum())


# ----------------------------------------------------------------- access
def user_devices(db: Session, user: PortalUser, include_revoked: bool = False) -> list[Device]:
    q = select(Device).join(UserDevice, UserDevice.device_id == Device.id).where(UserDevice.user_id == user.id)
    if not include_revoked:
        q = q.where(Device.revoked.is_(False))  # revoking a device also cuts off its owner's web access
    return list(db.scalars(q.order_by(Device.name)))


def resolve_for_user(slugs: set[str], rel: str) -> Path:
    """Like `storage.resolve_safe`, but the result must live inside one of the user's device folders.

    The check runs on the *resolved* path, so a symlink in one device folder pointing at another
    device's folder is refused as well.
    """
    p = storage.resolve_safe(rel)
    base = storage.root().resolve()
    if p == base:
        return p  # the virtual root; callers show only the user's devices there
    if p.relative_to(base).parts[0] not in slugs:
        raise HTTPException(404, "Not found")
    return p


def virtual_root(slugs: set[str]) -> list[storage.Entry]:
    """What the user sees at the top level: just their own device folders."""
    base = storage.root().resolve()
    out = []
    for slug in sorted(slugs):
        d = base / slug
        if d.is_dir() and not d.is_symlink():
            out.append(storage.Entry(slug, slug, True, 0, d.stat().st_mtime, "dir"))
    return out


# ---------------------------------------------------------------- invites
def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))


def create_invite(db: Session, label: str, slugs: list[str], minutes: int, admin: str) -> tuple[str, Invite]:
    raw = new_code()
    inv = Invite(code_hash=hash_token(raw), label=label.strip()[:128], device_slugs=",".join(slugs),
                 created_by=admin, expires_at=utcnow() + timedelta(minutes=minutes))
    db.add(inv)
    db.commit()
    return f"{raw[:4]}-{raw[4:]}", inv


def find_valid_invite(db: Session, code: str) -> Invite | None:
    inv = db.scalar(select(Invite).where(Invite.code_hash == hash_token(normalize_code(code))))
    if inv and not inv.used_at and not inv.revoked and inv.expires_at > utcnow():
        return inv
    return None


def username_error(db: Session, username: str) -> str | None:
    if not USERNAME_RE.match(username):
        return "Username must be 3–32 characters: lowercase letters, digits, dot, dash or underscore."
    taken = db.scalar(select(PortalUser.id).where(PortalUser.username == username)) or \
        db.scalar(select(AdminUser.id).where(AdminUser.username == username))
    return "That username is taken." if taken else None


def unique_folder_slug(db: Session, username: str) -> str:
    """A device slug for a new user folder that neither a DB row nor a directory is using."""
    slug = make_slug(db, username)
    base = storage.root()
    n = 2
    while (base / slug).exists():
        slug = f"{make_slug(db, username)[:100]}-{n}"
        n += 1
    return slug


def assignable_slug_ok(slug: str) -> bool:
    d = storage.root() / slug
    return bool(slug) and "/" not in slug and d.is_dir() and not d.is_symlink() and not storage.is_ignored(slug)
