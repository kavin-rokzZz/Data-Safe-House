"""Device discovery (from backup folders), hook calls and background scanning."""
from __future__ import annotations

import asyncio
import re
import subprocess
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import config
from ..database import SessionLocal
from ..models import Device
from . import storage
from .activity import log_event

SLUG_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def guess_kind(slug: str) -> str:
    s = slug.lower()
    if any(k in s for k in ("iphone", "ipad", "photosync", "ios")):
        return "iphone"
    if any(k in s for k in ("pc", "win", "desktop", "laptop", "windows")):
        return "windows"
    return "other"


def make_slug(db: Session, name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.").lower() or "device"
    slug, n = base[:100], 2
    while db.scalar(select(Device).where(Device.slug == slug)):
        slug = f"{base[:100]}-{n}"
        n += 1
    return slug


def _from_epoch(ts: float) -> datetime | None:
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None) if ts else None


def sync_devices(db: Session, force: bool = False) -> None:
    """Upsert a Device row per top-level backup folder and log new backup activity."""
    result = storage.scan(force=force)
    known = {d.slug: d for d in db.scalars(select(Device))}
    for slug, st in result.devices.items():
        dev = known.get(slug)
        if dev is None:
            dev = Device(slug=slug, name=slug, kind=guess_kind(slug))
            db.add(dev)
            log_event(db, "device", f"New device folder discovered: {slug}")
        if dev.file_count and (st.file_count != dev.file_count or st.total_bytes != dev.total_bytes):
            diff = st.file_count - dev.file_count
            delta = st.total_bytes - dev.total_bytes
            sign = "+" if diff >= 0 else ""
            log_event(db, "backup", f"{dev.name}: {sign}{diff} files ({delta / 1e6:+.1f} MB) since last scan")
        dev.file_count, dev.total_bytes = st.file_count, st.total_bytes
        dev.last_backup_at = _from_epoch(st.newest_mtime)
    db.commit()


def run_hook(db: Session, action: str, slug: str, actor: str) -> None:
    """Optional admin-supplied executable (e.g. disables a Samba user). Never uses a shell."""
    if not config.device_hook or not SLUG_RE.match(slug):
        return
    try:
        proc = subprocess.run([config.device_hook, action, slug], capture_output=True, text=True, timeout=15)
        level = "info" if proc.returncode == 0 else "error"
        log_event(db, "device", f"Hook '{action} {slug}' exited {proc.returncode} {proc.stderr.strip()[:300]}", level=level, actor=actor)
    except Exception as exc:
        log_event(db, "device", f"Hook '{action} {slug}' failed: {exc}", level="error", actor=actor)


async def scan_loop() -> None:
    """Periodic background scan so activity logs and device stats stay current."""
    while True:
        try:
            def work() -> None:
                with SessionLocal() as db:
                    sync_devices(db, force=True)

            await asyncio.to_thread(work)
        except Exception as exc:  # keep the loop alive
            with SessionLocal() as db:
                log_event(db, "system", f"Background scan failed: {exc}", level="error")
        await asyncio.sleep(config.scan_interval)
