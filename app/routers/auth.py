"""Login / logout."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db, utcnow
from ..models import AdminUser
from ..security import (client_ip, is_throttled, login_session, record_attempt, require_admin, verify_csrf,
                        verify_password)
from ..services.activity import log_event
from ..templating import render

router = APIRouter()


def _safe_next(url: str) -> str:
    """Only allow same-site relative redirects."""
    return url if url.startswith("/") and not url.startswith("//") and "\\" not in url else "/"


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    if request.session.get("uid"):
        return RedirectResponse(_safe_next(next), status_code=303)
    return render(request, "login.html", next=_safe_next(next), error=None)


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/"),
          db: Session = Depends(get_db)):
    ip = client_ip(request)
    if is_throttled(db, "login", ip):
        log_event(db, "auth", f"Login blocked (too many failures) from {ip}", level="warning")
        return render(request, "login.html", status_code=429, next=_safe_next(next),
                      error="Too many failed attempts. Try again in 15 minutes.")
    user = db.scalar(select(AdminUser).where(AdminUser.username == username.strip()))
    ok = verify_password(password, user.password_hash if user else None)
    record_attempt(db, "login", ip, ok)
    if not ok or not user:
        log_event(db, "auth", f"Failed login for '{username[:64]}' from {ip}", level="warning")
        return render(request, "login.html", status_code=401, next=_safe_next(next), error="Invalid username or password.")
    user.last_login_at = utcnow()
    db.commit()
    login_session(request, user)
    log_event(db, "auth", f"Login from {ip}", actor=user.username)
    return RedirectResponse(_safe_next(next), status_code=303)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request, user: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    log_event(db, "auth", "Logout", actor=user.username)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
