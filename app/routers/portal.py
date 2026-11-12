"""User portal: sign-up with an invite code, login, and file access limited to the user's own devices."""
from __future__ import annotations

import os
import re
import shutil

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db, utcnow
from ..models import Device, Invite, PortalUser, UserDevice
from ..security import (client_ip, hash_password, is_throttled, portal_deps, portal_login_session, record_attempt,
                        require_portal_user, validate_new_password, verify_csrf, verify_password)
from ..services import portal as svc
from ..services import storage
from ..services.activity import log_event
from ..templating import flash, render
from .auth import _safe_next
from .files import gallery_data, paginate, serve_raw, serve_thumb, sorted_entries

router = APIRouter(prefix="/portal")
authed = APIRouter(dependencies=portal_deps)


# ------------------------------------------------------------ sign-up / login
@router.get("")
def home(request: Request):
    return RedirectResponse("/portal/files", status_code=303)


@router.get("/login")
def login_page(request: Request, next: str = "/portal/files"):
    if request.session.get("puid"):
        return RedirectResponse("/portal/files", status_code=303)
    return render(request, "portal_login.html", next=_safe_next(next), error=None)


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/portal/files"),
          db: Session = Depends(get_db)):
    ip = client_ip(request)
    nxt = _safe_next(next)
    if not nxt.startswith("/portal"):
        nxt = "/portal/files"
    if is_throttled(db, "portal", ip):
        return render(request, "portal_login.html", status_code=429, next=nxt,
                      error="Too many failed attempts. Try again in 15 minutes.")
    user = db.scalar(select(PortalUser).where(PortalUser.username == username.strip().lower()))
    ok = verify_password(password, user.password_hash if user else None) and bool(user) and not user.disabled
    record_attempt(db, "portal", ip, ok)
    if not ok:
        log_event(db, "auth", f"Failed portal login for '{username[:32]}' from {ip}", level="warning")
        return render(request, "portal_login.html", status_code=401, next=nxt, error="Invalid username or password.")
    user.last_login_at = utcnow()
    db.commit()
    portal_login_session(request, user)
    log_event(db, "auth", f"Portal login from {ip}", actor=user.username)
    return RedirectResponse(nxt, status_code=303)


@router.get("/signup")
def signup_page(request: Request, code: str = ""):
    return render(request, "portal_signup.html", error=None, code=code[:20], username="")


@router.post("/signup")
def signup(request: Request, code: str = Form(...), username: str = Form(...), password: str = Form(...),
           confirm: str = Form(...), db: Session = Depends(get_db)):
    ip = client_ip(request)
    username = username.strip().lower()

    def fail(msg: str, status: int = 400):
        return render(request, "portal_signup.html", status_code=status, error=msg, code=code[:20], username=username)

    if is_throttled(db, "signup", ip):
        log_event(db, "auth", f"Sign-up blocked (too many bad codes) from {ip}", level="warning")
        return fail("Too many failed attempts. Try again in 15 minutes.", 429)
    invite = svc.find_valid_invite(db, code)
    record_attempt(db, "signup", ip, invite is not None)
    if not invite:
        log_event(db, "auth", f"Invalid sign-up code from {ip}", level="warning")
        return fail("That code is invalid, expired or already used.")
    err = svc.username_error(db, username) or (None if password == confirm else "Passwords do not match.") \
        or validate_new_password(password)
    if err:
        return fail(err)

    # Resolve which device folders the new user gets (admin-chosen, or a fresh private folder).
    devices: list[Device] = []
    created_dir = None
    try:
        for slug in [s for s in invite.device_slugs.split(",") if s]:
            dev = db.scalar(select(Device).where(Device.slug == slug))
            if dev is None and svc.assignable_slug_ok(slug):
                dev = Device(slug=slug, name=slug)
                db.add(dev)
                db.flush()
            if dev is not None:
                devices.append(dev)
        if not devices:
            slug = svc.unique_folder_slug(db, username)
            created_dir = storage.root() / slug
            created_dir.mkdir(parents=True, exist_ok=False)
            dev = Device(slug=slug, name=f"{username}'s files", kind="other")
            db.add(dev)
            db.flush()
            devices.append(dev)
        user = PortalUser(username=username, password_hash=hash_password(password), invite_label=invite.label)
        db.add(user)
        db.flush()
        # Atomic claim: if two people submit the same code at once, only one UPDATE matches.
        claimed = db.execute(update(Invite).where(Invite.id == invite.id, Invite.used_at.is_(None))
                             .values(used_at=utcnow(), used_by=username)).rowcount
        if claimed != 1:
            raise ValueError("code already used")
        db.add_all(UserDevice(user_id=user.id, device_id=d.id) for d in devices)
        db.commit()
    except (IntegrityError, ValueError, OSError) as exc:
        db.rollback()
        if created_dir is not None:
            shutil.rmtree(created_dir, ignore_errors=True)
        log_event(db, "auth", f"Sign-up failed for '{username}': {exc.__class__.__name__}", level="error")
        return fail("Could not create the account. The code may have just been used; ask your admin for a new one.")

    log_event(db, "auth", f"New user '{username}' signed up (invite '{invite.label or 'unlabelled'}', "
                          f"devices: {', '.join(d.slug for d in devices)})", actor=username)
    storage.scan(force=True)
    portal_login_session(request, user)
    flash(request, "Welcome! Your account is ready.")
    return RedirectResponse("/portal/files", status_code=303)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/portal/login", status_code=303)


# ---------------------------------------------------------------- user area
def _slugs(db: Session, user: PortalUser) -> set[str]:
    return {d.slug for d in svc.user_devices(db, user)}


@authed.get("/files")
def browse(request: Request, path: str = "", sort: str = "name", order: str = "asc",
           user: PortalUser = Depends(require_portal_user), db: Session = Depends(get_db)):
    slugs = _slugs(db, user)
    p = svc.resolve_for_user(slugs, path)
    if not p.exists():
        raise HTTPException(404, "Not found")
    is_root = p == storage.root().resolve()
    if p.is_file():
        p, is_root = p.parent, False
    entries = svc.virtual_root(slugs) if is_root else storage.list_dir(p)
    entries = sorted_entries(entries, sort, order)
    rel = "" if is_root else storage.rel_of(p)
    return render(request, "files.html", active="files", rel=rel, crumbs=storage.breadcrumbs(rel), entries=entries,
                  sort=sort, order=order, has_images=any(e.kind == "image" for e in entries),
                  root_missing=False, is_device_root=is_root, no_devices=is_root and not entries)


@authed.get("/files/raw")
def raw(path: str, download: bool = False, user: PortalUser = Depends(require_portal_user),
        db: Session = Depends(get_db)):
    p = svc.resolve_for_user(_slugs(db, user), path)
    if not p.exists():
        raise HTTPException(404, "Not found")
    return serve_raw(p, download)


@authed.get("/files/thumb")
def thumb(path: str, size: int = Query(320, ge=64, le=800), user: PortalUser = Depends(require_portal_user),
          db: Session = Depends(get_db)):
    p = svc.resolve_for_user(_slugs(db, user), path)
    if not p.exists():
        raise HTTPException(404, "Not found")
    return serve_thumb(p, size)


@authed.get("/gallery")
def gallery(request: Request, path: str = "", recursive: bool = False, page: int = Query(1, ge=1),
            user: PortalUser = Depends(require_portal_user), db: Session = Depends(get_db)):
    slugs = _slugs(db, user)
    p = svc.resolve_for_user(slugs, path)
    if not p.exists():
        raise HTTPException(404, "Not found")
    if not p.is_dir():
        p = p.parent
    is_root = p == storage.root().resolve()
    images, folders = gallery_data(p, recursive, virtual=svc.virtual_root(slugs) if is_root else None)
    shown, page, pages = paginate(images, page)
    rel = "" if is_root else storage.rel_of(p)
    return render(request, "gallery.html", active="gallery", rel=rel, crumbs=storage.breadcrumbs(rel), images=shown,
                  folders=folders, total=len(images), page=page, pages=pages, recursive=recursive)


@authed.post("/files/delete")
def delete(path: str = Form(...), user: PortalUser = Depends(require_portal_user), db: Session = Depends(get_db)):
    p = svc.resolve_for_user(_slugs(db, user), path)
    if not p.exists():
        raise HTTPException(404, "Not found")
    rel = storage.rel_of(p)
    files, size = storage.delete_path(p)  # refuses device top-level folders
    log_event(db, "files", f"Deleted '{rel}' ({files} files, {size / 1e6:.1f} MB)", level="warning", actor=user.username)
    storage.scan(force=True)
    return {"ok": True, "deleted": rel}


MAX_UPLOAD = int(os.getenv("NAS_MAX_UPLOAD_MB", "2048")) * 1024 * 1024  # per file
MIN_FREE = int(os.getenv("NAS_MIN_FREE_MB", "1024")) * 1024 * 1024  # keep this much disk free
_BAD_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


def _clean_name(raw: str) -> str:
    name = _BAD_CHARS.sub("_", os.path.basename((raw or "").replace("\\", "/"))).strip(" .")[:180]
    if not name or storage.is_ignored(name):
        raise HTTPException(400, f"File name not allowed: {raw!r}")
    return name


def _store(dest_dir, upload: UploadFile) -> tuple[str, int]:
    """Stream an upload to disk (never overwriting) and return (final name, bytes)."""
    name = _clean_name(upload.filename or "")
    tmp = dest_dir / f".{os.urandom(6).hex()}.part"  # .part files are ignored by the scanner
    size, next_check = 0, 0
    try:
        with open(tmp, "xb") as out:
            while chunk := upload.file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise HTTPException(413, f"'{name}' is larger than {MAX_UPLOAD // 1024 ** 2} MB")
                if size >= next_check:  # check free space up front, then every 64 MB
                    next_check = size + 64 * 1024 ** 2
                    if shutil.disk_usage(dest_dir).free < MIN_FREE:
                        raise HTTPException(507, "The NAS is almost full")
                out.write(chunk)
        stem, dot, ext = name.rpartition(".") if "." in name.strip(".") else (name, "", "")
        n = 0
        while True:
            final = name if n == 0 else f"{stem} ({n}){dot}{ext}"
            try:
                os.link(tmp, dest_dir / final)  # atomic, fails instead of overwriting
                break
            except FileExistsError:
                n += 1
        return final, size
    finally:
        tmp.unlink(missing_ok=True)


@authed.post("/files/upload")
def upload(path: str = Form(""), files: list[UploadFile] = File(...),
           user: PortalUser = Depends(require_portal_user), db: Session = Depends(get_db)):
    p = svc.resolve_for_user(_slugs(db, user), path)
    if p == storage.root().resolve() or not p.is_dir():
        raise HTTPException(400, "Open one of your device folders first, then upload")
    saved = []
    for f in files:
        name, size = _store(p, f)
        saved.append(name)
        log_event(db, "files", f"Uploaded '{storage.rel_of(p / name)}' ({size / 1e6:.1f} MB)", actor=user.username)
    storage.scan(force=True)
    return {"ok": True, "saved": saved}


@authed.get("/account")
def account(request: Request, user: PortalUser = Depends(require_portal_user), db: Session = Depends(get_db)):
    return render(request, "portal_account.html", active="account", devices=svc.user_devices(db, user, True))


@authed.post("/account/password")
def change_password(request: Request, current: str = Form(...), new: str = Form(...), confirm: str = Form(...),
                    user: PortalUser = Depends(require_portal_user), db: Session = Depends(get_db)):
    if not verify_password(current, user.password_hash):
        flash(request, "Current password is incorrect.", "error")
    elif new != confirm:
        flash(request, "New passwords do not match.", "error")
    elif (err := validate_new_password(new)):
        flash(request, err, "error")
    else:
        user.password_hash = hash_password(new)
        db.commit()
        portal_login_session(request, user)  # old sessions on other browsers become invalid
        log_event(db, "auth", "Password changed", actor=user.username)
        flash(request, "Password updated.")
    return RedirectResponse("/portal/account", status_code=303)


router.include_router(authed)
