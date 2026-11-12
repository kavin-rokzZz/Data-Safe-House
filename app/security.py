"""Password hashing, session auth, CSRF protection and brute-force throttling."""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import config
from .database import get_db, utcnow
from .models import AdminUser, LoginAttempt, PortalUser

_hasher = PasswordHasher()  # Argon2id with library defaults
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing")

MAX_FAILURES = 5
WINDOW = timedelta(minutes=15)
MIN_PASSWORD_LEN = 10


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Constant-ish time: always performs one Argon2 verification."""
    try:
        _hasher.verify(password_hash or _DUMMY_HASH, password)
        return password_hash is not None
    except (VerifyMismatchError, InvalidHashError):
        return False


def validate_new_password(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    if password.lower() == password or password.upper() == password or not any(c.isdigit() for c in password):
        return "Use a mix of upper/lower case letters and at least one digit."
    return None


def password_epoch(user: AdminUser | PortalUser) -> str:
    """Changes whenever the password changes; stored in the session to invalidate old sessions."""
    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:16]


# --------------------------------------------------------------- throttling
def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def is_throttled(db: Session, scope: str, ip: str) -> bool:
    since = utcnow() - WINDOW
    fails = db.scalar(
        select(func.count(LoginAttempt.id)).where(
            LoginAttempt.scope == scope,
            LoginAttempt.ip == ip,
            LoginAttempt.success.is_(False),
            LoginAttempt.ts >= since,
        )
    )
    return (fails or 0) >= MAX_FAILURES


def record_attempt(db: Session, scope: str, ip: str, success: bool) -> None:
    db.add(LoginAttempt(scope=scope, ip=ip, success=success))
    db.commit()


# ------------------------------------------------------------ session / CSRF
def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def login_session(request: Request, user: AdminUser) -> None:
    request.session.clear()  # drop anything pre-login (prevents fixation)
    request.session.update({"uid": user.id, "epoch": password_epoch(user), "csrf": secrets.token_urlsafe(32)})


class NotAuthenticated(Exception):
    """Raised by `require_admin`; main.py turns it into a redirect (HTML) or 401 (API)."""


def require_admin(request: Request, db: Session = Depends(get_db)) -> AdminUser:
    uid = request.session.get("uid")
    user = db.get(AdminUser, uid) if uid else None
    if not user or request.session.get("epoch") != password_epoch(user):
        for k in ("uid", "epoch"):  # only our own keys, so a signed-in portal user is not logged out
            request.session.pop(k, None)
        raise NotAuthenticated()
    request.state.user = user
    return user


async def verify_csrf(request: Request) -> None:
    """Dependency for state-changing routes. Accepts header (fetch) or form field."""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    sent = request.headers.get("x-csrf-token")
    if not sent:
        form = await request.form()
        sent = form.get("csrf_token")
    expected = request.session.get("csrf", "")
    if not sent or not expected or not secrets.compare_digest(str(sent), expected):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token. Reload the page and retry.")


# ------------------------------------------------------------- user portal
def portal_login_session(request: Request, user: PortalUser) -> None:
    """Portal sessions use different keys than admin ones, so one can never satisfy the other."""
    request.session.clear()
    request.session.update({"puid": user.id, "pepoch": password_epoch(user), "csrf": secrets.token_urlsafe(32)})


class PortalNotAuthenticated(Exception):
    """Raised by `require_portal_user`; main.py redirects to /portal/login."""


def require_portal_user(request: Request, db: Session = Depends(get_db)) -> PortalUser:
    uid = request.session.get("puid")
    user = db.get(PortalUser, uid) if uid else None
    if not user or user.disabled or request.session.get("pepoch") != password_epoch(user):
        for k in ("puid", "pepoch"):
            request.session.pop(k, None)
        raise PortalNotAuthenticated()
    request.state.user = user
    return user


portal_deps = [Depends(require_portal_user), Depends(verify_csrf)]

# Convenience dependency bundle for authenticated routers
admin_deps = [Depends(require_admin), Depends(verify_csrf)]


def hash_token(token: str) -> str:
    """Fast hash for high-entropy random tokens / codes (not for passwords)."""
    return hashlib.sha256(token.encode()).hexdigest()
