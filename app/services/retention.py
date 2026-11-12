"""Retention policy: delete backup files older than N days (by modification time)."""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..database import SessionLocal
from . import settings, storage
from .activity import log_event


@dataclass
class RetentionPlan:
    days: int
    count: int = 0
    bytes: int = 0
    sample: list[tuple[str, int, float]] = field(default_factory=list)  # rel, size, mtime
    paths: list[str] = field(default_factory=list)


def plan(days: int, keep_paths: bool = False) -> RetentionPlan:
    """Find files whose mtime is older than `days`. Never touches the root level itself."""
    cutoff = time.time() - days * 86400
    p = RetentionPlan(days=days)
    base = storage.root()
    if not base.is_dir():
        return p
    stack = [str(base)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    if storage.is_ignored(e.name):
                        continue
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            st = e.stat(follow_symlinks=False)
                            if st.st_mtime < cutoff:
                                p.count += 1
                                p.bytes += st.st_size
                                if len(p.sample) < 50:
                                    p.sample.append((os.path.relpath(e.path, base), st.st_size, st.st_mtime))
                                if keep_paths:
                                    p.paths.append(e.path)
                    except OSError:
                        continue
        except OSError:
            continue
    return p


def _prune_empty_dirs() -> None:
    base = str(storage.root())
    for dirpath, _dirnames, _filenames in os.walk(base, topdown=False):
        if dirpath == base or os.path.dirname(dirpath) == base:
            continue  # keep device folders themselves
        try:
            os.rmdir(dirpath)  # fails harmlessly unless empty (children were already pruned)
        except OSError:
            pass


def execute(db: Session, days: int, actor: str) -> tuple[int, int]:
    """Delete expired files. Returns (files_deleted, bytes_freed)."""
    if days < 1:
        raise ValueError("Retention must be at least 1 day")
    p = plan(days, keep_paths=True)
    deleted = freed = 0
    for path in p.paths:
        try:
            size = os.lstat(path).st_size
            os.unlink(path)
            deleted += 1
            freed += size
        except OSError:
            continue
    _prune_empty_dirs()
    storage.scan(force=True)
    settings.set_raw(db, "retention_last_run", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    log_event(db, "files", f"Retention ({days}d) removed {deleted} files, freed {freed / 1e6:.1f} MB", actor=actor,
              level="warning" if deleted else "info")
    return deleted, freed


async def retention_loop() -> None:
    """Checks hourly; runs at most once every 24h and only if enabled."""
    while True:
        await asyncio.sleep(3600)
        try:
            def work() -> None:
                with SessionLocal() as db:
                    cfg = settings.load(db)
                    if not cfg.retention_enabled:
                        return
                    if cfg.retention_last_run:
                        last = datetime.fromisoformat(cfg.retention_last_run)
                        if datetime.now(timezone.utc) - last < timedelta(hours=24):
                            return
                    execute(db, cfg.retention_days, actor="scheduler")

            await asyncio.to_thread(work)
        except Exception as exc:
            with SessionLocal() as db:
                log_event(db, "system", f"Retention job failed: {exc}", level="error")
