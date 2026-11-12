"""Runtime configuration, read once from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Tiny .env loader so we do not need another dependency. Real env vars win."""
    env_file = BASE_DIR / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    backup_root: Path
    data_dir: Path
    log_files: tuple[str, ...]
    cookie_secure: bool
    session_hours: int
    scan_interval: int
    device_hook: str

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'nas_admin.db'}"

    @property
    def thumb_dir(self) -> Path:
        return self.data_dir / "thumbs"


def load_config() -> Config:
    data_dir = Path(os.getenv("NAS_DATA_DIR", BASE_DIR / "data")).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "thumbs").mkdir(exist_ok=True)
    logs = tuple(p.strip() for p in os.getenv("NAS_LOG_FILES", "").split(",") if p.strip())
    return Config(
        backup_root=Path(os.getenv("NAS_BACKUP_ROOT", "/backup")).expanduser().resolve(),
        data_dir=data_dir,
        log_files=logs,
        cookie_secure=_bool("NAS_COOKIE_SECURE", False),
        session_hours=int(os.getenv("NAS_SESSION_HOURS", "8")),
        scan_interval=max(60, int(os.getenv("NAS_SCAN_INTERVAL", "300"))),
        device_hook=os.getenv("NAS_DEVICE_HOOK", "").strip(),
    )


config = load_config()
