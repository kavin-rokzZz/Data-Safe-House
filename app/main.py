"""FastAPI application factory."""
from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from .config import config
from .database import SessionLocal, init_db
from .routers import auth, dashboard, devices, files, logs, portal, users, settings as settings_router
from .security import NotAuthenticated, PortalNotAuthenticated
from .services import devices as device_service
from .services import retention
from .services.activity import log_event
from .templating import render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _secret_key() -> str:
    """Persist a random signing key next to the DB (generated on first start, chmod 600)."""
    key_file = config.data_dir / "secret.key"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32))
        key_file.chmod(0o600)
    return key_file.read_text().strip()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    with SessionLocal() as db:
        log_event(db, "system", f"NAS Admin started (backup root: {config.backup_root})")
    tasks = [asyncio.create_task(device_service.scan_loop()), asyncio.create_task(retention.retention_loop())]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="NAS Admin", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(
    SessionMiddleware,
    secret_key=_secret_key(),
    session_cookie="nas_session",
    max_age=config.session_hours * 3600,
    same_site="strict",
    https_only=config.cookie_secure,
)

CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self'; "
    "frame-src 'self'; object-src 'self'; frame-ancestors 'self'; base-uri 'self'; form-action 'self'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Content-Security-Policy", CSP)
    if request.url.path.startswith(("/internal", "/login", "/portal")):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


@app.exception_handler(NotAuthenticated)
async def _not_auth(request: Request, _exc: NotAuthenticated):
    if request.url.path.startswith(("/internal", "/files/delete")) or request.headers.get("x-csrf-token"):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    nxt = quote(request.url.path + (f"?{request.url.query}" if request.url.query else ""))
    return RedirectResponse(f"/login?next={nxt}", status_code=303)


@app.exception_handler(PortalNotAuthenticated)
async def _portal_not_auth(request: Request, _exc: PortalNotAuthenticated):
    if request.headers.get("x-csrf-token"):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    nxt = quote(request.url.path + (f"?{request.url.query}" if request.url.query else ""))
    return RedirectResponse(f"/portal/login?next={nxt}", status_code=303)


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    wants_json = request.url.path.startswith("/internal") or request.headers.get("x-csrf-token") \
        or "application/json" in request.headers.get("accept", "")
    if wants_json:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
    return render(request, "error.html", status_code=exc.status_code, code=exc.status_code, detail=exc.detail)


app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
for module in (auth, dashboard, files, devices, users, logs, settings_router, portal):
    app.include_router(module.router)
