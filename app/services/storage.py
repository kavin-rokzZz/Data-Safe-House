"""Filesystem access under the backup root: safe path handling, scanning, listing, thumbnails."""
from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException
from PIL import Image, ImageOps

from ..config import config

try:  # HEIC/HEIF support is optional at import time
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pillow_heif = None

# Temp/lock files written by FreeFileSync and Synology/Samba recycle bins are not "backups".
IGNORED_SUFFIXES = (".ffs_tmp", ".ffs_lock", ".ffs_db", ".part", ".tmp")
IGNORED_NAMES = {"lost+found", "@eaDir", "#recycle", ".recycle", "Thumbs.db", ".DS_Store", "desktop.ini"}

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff"}
HEIC_EXT = {".heic", ".heif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".ogg", ".flac"}
TEXT_EXT = {".txt", ".log", ".md", ".json", ".csv", ".xml", ".ini", ".yaml", ".yml", ".py", ".js", ".html", ".css"}


# ------------------------------------------------------------------ paths
def root() -> Path:
    return config.backup_root


def resolve_safe(rel: str) -> Path:
    """Map a user-supplied relative path to an absolute one, refusing any escape from the root.

    `resolve()` follows symlinks, so a link inside /backup that points elsewhere is rejected too.
    """
    base = root().resolve()
    if "\x00" in rel:
        raise HTTPException(400, "Invalid path")
    target = (base / rel.lstrip("/")).resolve()
    if target != base and base not in target.parents:
        raise HTTPException(400, "Path is outside the backup directory")
    return target


def rel_of(path: Path) -> str:
    r = path.resolve().relative_to(root().resolve())
    return "" if str(r) == "." else r.as_posix()


def is_ignored(name: str) -> bool:
    return name in IGNORED_NAMES or name.lower().endswith(IGNORED_SUFFIXES)


def kind_of(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext == ".pdf":
        return "pdf"
    if ext in TEXT_EXT:
        return "text"
    return "file"


# ---------------------------------------------------------------- listing
@dataclass
class Entry:
    name: str
    rel: str
    is_dir: bool
    size: int
    mtime: float
    kind: str


def list_dir(path: Path) -> list[Entry]:
    entries: list[Entry] = []
    with os.scandir(path) as it:
        for e in it:
            if is_ignored(e.name):
                continue
            try:
                st = e.stat()  # follows symlinks; broken links raise
                is_dir = e.is_dir()
            except OSError:
                continue
            p = Path(e.path)
            entries.append(
                Entry(e.name, rel_of(p), is_dir, 0 if is_dir else st.st_size, st.st_mtime, "dir" if is_dir else kind_of(p))
            )
    return entries


def breadcrumbs(rel: str) -> list[tuple[str, str]]:
    crumbs, acc = [], ""
    for part in [p for p in rel.split("/") if p]:
        acc = f"{acc}/{part}" if acc else part
        crumbs.append((part, acc))
    return crumbs


# --------------------------------------------------------------- scanning
@dataclass
class DeviceStat:
    slug: str
    file_count: int = 0
    total_bytes: int = 0
    newest_mtime: float = 0.0


@dataclass
class ScanResult:
    devices: dict[str, DeviceStat] = field(default_factory=dict)
    file_count: int = 0
    total_bytes: int = 0
    newest_mtime: float = 0.0
    scanned_at: float = 0.0
    duration: float = 0.0


_scan_lock = threading.Lock()
_scan_cache: ScanResult | None = None
SCAN_TTL = 60.0


def _walk(path: str, stat: DeviceStat) -> None:
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    if is_ignored(e.name):
                        continue
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            st = e.stat(follow_symlinks=False)
                            stat.file_count += 1
                            stat.total_bytes += st.st_size
                            if st.st_mtime > stat.newest_mtime:
                                stat.newest_mtime = st.st_mtime
                    except OSError:
                        continue
        except OSError:
            continue


def scan(force: bool = False) -> ScanResult:
    """Walk every top-level device folder. Cached for SCAN_TTL seconds (the tree can be large)."""
    global _scan_cache
    with _scan_lock:
        if not force and _scan_cache and time.time() - _scan_cache.scanned_at < SCAN_TTL:
            return _scan_cache
        started = time.time()
        res = ScanResult()
        base = root()
        if base.is_dir():
            with os.scandir(base) as it:
                for e in it:
                    if is_ignored(e.name) or not e.is_dir(follow_symlinks=False):
                        continue
                    ds = DeviceStat(slug=e.name)
                    _walk(e.path, ds)
                    res.devices[e.name] = ds
                    res.file_count += ds.file_count
                    res.total_bytes += ds.total_bytes
                    res.newest_mtime = max(res.newest_mtime, ds.newest_mtime)
        res.scanned_at = time.time()
        res.duration = res.scanned_at - started
        _scan_cache = res
        return res


# -------------------------------------------------------------- deletion
def delete_path(path: Path) -> tuple[int, int]:
    """Delete a file or folder. Refuses the root and device top-level folders. Returns (files, bytes)."""
    base = root().resolve()
    if path == base or path.parent == base:
        raise HTTPException(400, "Top-level device folders cannot be deleted here")
    if path.is_symlink():  # remove the link only, never the target
        path.unlink()
        return 0, 0
    if path.is_dir():
        stat = DeviceStat(slug="")
        _walk(str(path), stat)
        shutil.rmtree(path)
        return stat.file_count, stat.total_bytes
    size = path.stat().st_size
    path.unlink()
    return 1, size


# ------------------------------------------------------------ thumbnails
_img_sem = threading.BoundedSemaphore(2)  # Raspberry Pi: keep decode memory bounded


def _cache_path(src: Path, size: int, tag: str) -> Path:
    st = src.stat()
    key = hashlib.sha1(f"{src}|{st.st_mtime_ns}|{st.st_size}|{size}|{tag}".encode()).hexdigest()
    return config.thumb_dir / key[:2] / f"{key}.jpg"


def make_jpeg(src: Path, max_side: int, quality: int, tag: str) -> Path:
    """Return a cached JPEG rendition of an image (handles HEIC, EXIF orientation)."""
    out = _cache_path(src, max_side, tag)
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    with _img_sem:
        if out.exists():
            return out
        try:
            with Image.open(src) as img:
                img.draft("RGB", (max_side * 2, max_side * 2))  # fast JPEG downscale on decode
                img = ImageOps.exif_transpose(img)
                img.thumbnail((max_side, max_side))
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                tmp = out.with_suffix(".tmp")
                img.save(tmp, "JPEG", quality=quality, optimize=True)
                tmp.replace(out)
        except Exception as exc:
            raise HTTPException(415, f"Cannot render image: {exc.__class__.__name__}") from exc
    return out


def guess_mime(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"
