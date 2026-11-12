"""Jinja2 environment, filters and the shared `render` helper."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request
from markupsafe import Markup, escape
from starlette.templating import Jinja2Templates

from .security import csrf_token

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def human_size(n: int | float | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "—"


def ts(value: datetime | None) -> Markup:
    """Naive-UTC datetime -> <time> tag; browser JS localises it."""
    if not value:
        return Markup("—")
    iso = value.replace(tzinfo=timezone.utc).isoformat()
    return Markup(f'<time class="ts" datetime="{escape(iso)}">{value:%Y-%m-%d %H:%M} UTC</time>')


def epoch(value: float | None) -> Markup:
    if not value:
        return Markup("—")
    return ts(datetime.fromtimestamp(value, timezone.utc).replace(tzinfo=None))


def uptime(seconds: int) -> str:
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    return f"{d}d {h}h" if d else f"{h}h {rem // 60}m"


templates.env.filters.update(size=human_size, ts=ts, epoch=epoch, uptime=uptime)


def render(request: Request, name: str, status_code: int = 200, **ctx):
    flashes = request.session.pop("flash", []) if "session" in request.scope else []
    portal = request.url.path.startswith("/portal")
    ctx.update(
        portal=portal,
        prefix="/portal" if portal else "",
        home="/portal" if portal else "/",
        root_label="/my devices" if portal else "/backup",
        user=getattr(request.state, "user", None),
        csrf=csrf_token(request),
        flashes=flashes,
        active=ctx.get("active", ""),
    )
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def flash(request: Request, message: str, level: str = "success") -> None:
    request.session.setdefault("flash", []).append({"level": level, "message": message})
