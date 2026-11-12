"""Log viewer: app activity (DB) plus tail of allow-listed system log files."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import config
from ..database import get_db
from ..models import ActivityLog
from ..security import admin_deps
from ..templating import render

router = APIRouter(dependencies=admin_deps)
PER_PAGE = 50
LEVELS = ("info", "warning", "error")
CATEGORIES = ("auth", "backup", "files", "device", "settings", "system")


def tail_lines(path: str, max_lines: int = 500, max_bytes: int = 2_000_000) -> list[str]:
    """Read the end of a (possibly huge) file without loading all of it."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read().decode("utf-8", errors="replace")
    return data.splitlines()[-max_lines:]


@router.get("/logs")
def logs_page(request: Request, source: str = "activity", level: str = "", category: str = "", q: str = "",
              page: int = Query(1, ge=1), db: Session = Depends(get_db)):
    sources = ["activity", *config.log_files]
    if source not in sources:  # allow-list: users can never request an arbitrary path
        source = "activity"
    ctx = dict(active="logs", sources=sources, source=source, q=q, level=level, category=category,
               levels=LEVELS, categories=CATEGORIES)
    if source == "activity":
        stmt = select(ActivityLog)
        if level in LEVELS:
            stmt = stmt.where(ActivityLog.level == level)
        if category in CATEGORIES:
            stmt = stmt.where(ActivityLog.category == category)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(or_(ActivityLog.message.ilike(like), ActivityLog.actor.ilike(like)))
        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = db.scalars(stmt.order_by(ActivityLog.id.desc()).offset((page - 1) * PER_PAGE).limit(PER_PAGE))
        return render(request, "logs.html", rows=list(rows), total=total, page=page, pages=max(1, -(-total // PER_PAGE)),
                      lines=None, error=None, **ctx)
    lines, error = [], None
    try:
        lines = tail_lines(source)
        if q:
            lines = [ln for ln in lines if q.lower() in ln.lower()]
        lines.reverse()  # newest first
    except OSError as exc:
        error = f"Cannot read {Path(source).name}: {exc.strerror}"
    return render(request, "logs.html", rows=None, total=len(lines), page=1, pages=1, lines=lines, error=error, **ctx)
