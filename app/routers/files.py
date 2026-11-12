"""File browser, previews, thumbnails, gallery and secure delete."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import AdminUser
from ..security import admin_deps, require_admin
from ..services import storage
from ..services.activity import log_event
from ..templating import render

router = APIRouter(dependencies=admin_deps)

PAGE_SIZE = 60
# Only these are rendered inline by the browser; everything else is forced to download so a
# backed-up .html/.svg file can never execute script in the admin origin.
INLINE_SAFE = {"image", "video", "audio", "pdf"}


def _existing(rel: str):
    p = storage.resolve_safe(rel)
    if not p.exists():
        raise HTTPException(404, "Not found")
    return p


@router.get("/files")
def browse(request: Request, path: str = "", sort: str = "name", order: str = "asc"):
    p = _existing(path)
    if p.is_file():
        p = p.parent
    entries = storage.list_dir(p)
    entries = sorted_entries(entries, sort, order)
    rel = storage.rel_of(p)
    return render(request, "files.html", active="files", rel=rel, crumbs=storage.breadcrumbs(rel), entries=entries,
                  sort=sort, order=order, has_images=any(e.kind == "image" for e in entries),
                  root_missing=not storage.root().is_dir(), is_device_root=(p == storage.root().resolve()))


def serve_raw(p: Path, download: bool = False):
    """Stream a file safely (shared by the admin and user-portal routers)."""
    if not p.is_file():
        raise HTTPException(400, "Not a file")
    kind = storage.kind_of(p)
    inline = (not download) and kind in INLINE_SAFE and p.suffix.lower() not in storage.HEIC_EXT
    if kind == "text" and not download:  # plain text only, never as html/js
        return FileResponse(p, media_type="text/plain; charset=utf-8", headers={"Content-Security-Policy": "sandbox"})
    if (not download) and p.suffix.lower() in storage.HEIC_EXT:
        return FileResponse(storage.make_jpeg(p, 2400, 88, "full"), media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})
    return FileResponse(
        p, media_type=storage.guess_mime(p) if inline else "application/octet-stream",
        filename=p.name, content_disposition_type="inline" if inline else "attachment",
        # PDFs need the browser viewer (blocked by sandbox); all other types stay sandboxed.
        headers=None if kind == "pdf" and inline else {"Content-Security-Policy": "sandbox"},
    )


def serve_thumb(p: Path, size: int):
    if not p.is_file() or storage.kind_of(p) != "image":
        raise HTTPException(415, "Not an image")
    return FileResponse(storage.make_jpeg(p, size, 80, "thumb"), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


def gallery_data(p: Path, recursive: bool, virtual: list[storage.Entry] | None = None):
    """Collect (images, folders) for a gallery page.

    `virtual` is the user portal's synthetic root: a list of device folders to treat as the top level.
    """
    images, folders = [], []
    if recursive:
        starts = [storage.resolve_safe(e.rel) for e in virtual] if virtual is not None else [p]
        for start in starts:
            for dirpath, dirnames, filenames in os.walk(start):
                dirnames[:] = [d for d in dirnames if not storage.is_ignored(d)]
                for fn in filenames:
                    fp = os.path.join(dirpath, fn)
                    if not storage.is_ignored(fn) and storage.kind_of(Path(fp)) == "image":
                        try:
                            st = os.stat(fp)
                        except OSError:
                            continue
                        images.append(storage.Entry(fn, storage.rel_of(Path(fp)), False, st.st_size, st.st_mtime, "image"))
    else:
        for e in (virtual if virtual is not None else storage.list_dir(p)):
            if e.is_dir:
                folders.append(e)
            elif e.kind == "image":
                images.append(e)
    images.sort(key=lambda e: e.mtime, reverse=True)
    folders.sort(key=lambda e: e.name.lower())
    return images, folders


def paginate(images: list, page: int):
    pages = max(1, -(-len(images) // PAGE_SIZE))
    page = min(page, pages)
    return images[(page - 1) * PAGE_SIZE: page * PAGE_SIZE], page, pages


def sorted_entries(entries: list[storage.Entry], sort: str, order: str) -> list[storage.Entry]:
    keys = {"name": lambda e: e.name.lower(), "size": lambda e: e.size, "date": lambda e: e.mtime}
    entries.sort(key=keys.get(sort, keys["name"]), reverse=(order == "desc"))
    entries.sort(key=lambda e: not e.is_dir)  # folders first (stable)
    return entries


@router.get("/files/raw")
def raw(path: str, download: bool = False):
    return serve_raw(_existing(path), download)


@router.get("/files/thumb")
def thumb(path: str, size: int = Query(320, ge=64, le=800)):
    return serve_thumb(_existing(path), size)


@router.get("/gallery")
def gallery(request: Request, path: str = "", recursive: bool = False, page: int = Query(1, ge=1)):
    p = _existing(path)
    if not p.is_dir():
        p = p.parent
    images, folders = gallery_data(p, recursive)
    shown, page, pages = paginate(images, page)
    rel = storage.rel_of(p)
    return render(request, "gallery.html", active="gallery", rel=rel, crumbs=storage.breadcrumbs(rel), images=shown,
                  folders=folders, total=len(images), page=page, pages=pages, recursive=recursive)


@router.post("/files/delete")
def delete(path: str = Form(...), user: AdminUser = Depends(require_admin), db: Session = Depends(get_db)):
    p = _existing(path)
    rel = storage.rel_of(p)
    files, size = storage.delete_path(p)
    log_event(db, "files", f"Deleted '{rel}' ({files} files, {size / 1e6:.1f} MB)", level="warning", actor=user.username)
    storage.scan(force=True)
    return {"ok": True, "deleted": rel}
