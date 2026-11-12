"""Typed key/value settings stored in SQLite."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..models import Setting

DEFAULTS: dict[str, str] = {
    "retention_enabled": "0",
    "retention_days": "365",
    "retention_last_run": "",
    "warn_percent": "80",
    "critical_percent": "90",
    "temp_warn_c": "55",
    "active_days": "7",
}


@dataclass
class AppSettings:
    retention_enabled: bool
    retention_days: int
    retention_last_run: str
    warn_percent: int
    critical_percent: int
    temp_warn_c: int
    active_days: int


def get_raw(db: Session, key: str) -> str:
    row = db.get(Setting, key)
    return row.value if row else DEFAULTS[key]


def set_raw(db: Session, key: str, value: str) -> None:
    if key not in DEFAULTS:
        raise KeyError(key)
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()


def load(db: Session) -> AppSettings:
    g = lambda k: get_raw(db, k)  # noqa: E731
    return AppSettings(
        retention_enabled=g("retention_enabled") == "1",
        retention_days=int(g("retention_days")),
        retention_last_run=g("retention_last_run"),
        warn_percent=int(g("warn_percent")),
        critical_percent=int(g("critical_percent")),
        temp_warn_c=int(g("temp_warn_c")),
        active_days=int(g("active_days")),
    )
