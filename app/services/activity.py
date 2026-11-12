"""Write helpers for the activity log."""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..models import ActivityLog

logger = logging.getLogger("nas_admin")


def log_event(db: Session, category: str, message: str, *, level: str = "info", actor: str = "") -> None:
    db.add(ActivityLog(category=category, message=message[:2000], level=level, actor=actor))
    db.commit()
    logger.log(logging.WARNING if level != "info" else logging.INFO, "[%s] %s (%s)", category, message, actor)
