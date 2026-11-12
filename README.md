# NAS Admin

FastAPI + SQLite + Tailwind (CDN) admin panel for a Samba-based backup NAS.

## Run

```bash
cd ~/nas-admin
cp .env.example .env            # set NAS_BACKUP_ROOT etc.
.venv/bin/python -m app.cli create-admin admin     # prompts for a password
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
```
Or install `nas-admin.service` with systemd. The Tailwind CDN needs internet in the *browser*, not on the NAS.

## Layout
`app/routers/*` pages & APIs · `app/services/*` storage scan, metrics, retention, devices · `app/models.py` SQLite tables · `app/templates/*` UI.

## Security notes
- Argon2id passwords; signed `SameSite=Strict` HttpOnly session cookie; CSRF token on every POST; 5 failed logins / 15 min per IP throttle.
- Changing the password invalidates all other sessions.
- All file paths are resolved and confined to `NAS_BACKUP_ROOT` (symlinks that escape are rejected).
- Only images/video/audio/PDF are shown inline; every other file type downloads (text is served as `text/plain`) so a backed-up `.html` can't run in the admin origin.
- Serve over HTTPS (reverse proxy such as Caddy/nginx) and set `NAS_COOKIE_SECURE=true` before exposing beyond your LAN. Never expose it directly to the internet.
- `uvicorn` sees the proxy as the client IP; if you add a proxy, run with `--proxy-headers --forwarded-allow-ips=<proxy ip>` so throttling uses the real address.

## Things to know
- **Revoke** blocks the app token immediately. Samba access is separate: set `NAS_DEVICE_HOOK` to an executable called as `hook revoke|restore <slug>` (e.g. `smbpasswd -d`/`-e` for that device's Samba user).
- **Retention** uses file modified time and is off by default. Use *Preview* first. If FreeFileSync mirrors to the NAS, deleted files may be re-uploaded.
- **Disk temperature** appears when the kernel exposes it (`drivetemp` module for SATA/USB-SATA, or NVMe); otherwise CPU temp only.
- **Logs page** shows app/backup activity plus any files in `NAS_LOG_FILES` (user must have read access, e.g. add to the `adm` group for `/var/log/samba`).
- **Users & invites** (admin → *Users*): create a one-time invite code (optionally tied to existing device folders; otherwise a new private folder is created at sign-up). The person opens `/portal/signup`, enters the code and picks a username/password. The user portal (`/portal`) shows only their own device folders: browse, preview, gallery, download, upload, delete (upload limits: `NAS_MAX_UPLOAD_MB`, `NAS_MIN_FREE_MB`). Admins can disable users, reset passwords, change folder access or delete accounts (files are never deleted). Revoking a device also cuts its owners off. Portal users have web access only; no Samba account is created.
- The old iPhone-app pairing API (`/api/v1/*`) was removed.
- Device folders are auto-discovered as the first-level directories of the backup root (`/backup/<device_slug>/…`).
