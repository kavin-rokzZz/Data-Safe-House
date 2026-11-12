# Installing NAS Admin (Debian package)

NAS Admin is a FastAPI admin panel and user portal for a Samba-based backup NAS.
This guide covers installing it from the `.deb` package on Debian, Ubuntu or Raspberry Pi OS.

## 1. Requirements
- Debian-based OS (Debian 12+, Ubuntu 22.04+, Raspberry Pi OS) with systemd
- Python 3.10 or newer (installed automatically as a dependency)
- A disk or folder for backups (default `/srv/nas`)
- Internet access during install, unless the package was built with `--bundle-wheels`
  and matches the target machine's architecture and Python version

## 2. Install
```bash
sudo apt install ./nas-admin_1.0.0_arm64.deb
```
`apt` pulls in Samba and the other dependencies. The installer creates a `nasadmin`
service user and a `nas` group, builds the Python environment in `/opt/nas-admin/.venv`,
and starts the `nas-admin` service.

## 3. Create the first admin
```bash
sudo nas-admin create-admin admin
```
You are prompted for a password. Use `sudo nas-admin reset-password <user>` to reset one later.

## 4. Open the panel
Browse to `http://<machine-ip>:8080` and sign in.

## 5. Point it at your storage
1. Mount your backup disk (for example at `/mnt/nas`) and add it to `/etc/fstab` with the `nofail` option.
2. Give the `nas` group write access:
   ```bash
   sudo chgrp -R nas /mnt/nas && sudo chmod -R g+rwX /mnt/nas && sudo chmod g+s /mnt/nas
   ```
3. Set `NAS_BACKUP_ROOT=/mnt/nas` in `/etc/nas-admin/nas-admin.env`.
4. If the disk is outside `/srv/nas`, add its path to `ReadWritePaths=` using
   `sudo systemctl edit nas-admin` (override file), then restart the service.
5. Restart: `sudo systemctl restart nas-admin`

## 6. Add a Samba share (optional)
```bash
sudo nas-admin-setup-samba <ShareName> <linux-user>
```
This creates the folder under `NAS_BACKUP_ROOT`, adds the share to
`/etc/samba/nas-admin-shares.conf`, and prompts for the user's Samba password.
Connect from Windows with `\\<machine>\<ShareName>`, or from macOS/Linux with `smb://<machine>/<ShareName>`.

## 7. Configuration
Edit `/etc/nas-admin/nas-admin.env`, then run `sudo systemctl restart nas-admin`.

| Setting | Purpose | Default |
|---|---|---|
| `NAS_BACKUP_ROOT` | Backup storage root | `/srv/nas` |
| `NAS_DATA_DIR` | Database, secret key, thumbnails | `/var/lib/nas-admin` |
| `NAS_LOG_FILES` | Logs the Logs page may read | Samba logs |
| `NAS_COOKIE_SECURE` | Set `true` behind HTTPS | `false` |
| `NAS_SESSION_HOURS` | Session lifetime | `8` |
| `NAS_SCAN_INTERVAL` | Seconds between storage scans | `300` |
| `NAS_DEVICE_HOOK` | Script run on device revoke/restore | empty |
| `NAS_MAX_UPLOAD_MB` / `NAS_MIN_FREE_MB` | Portal upload limits | `2048` / `1024` |

## 8. Security
- The panel serves plain HTTP. Keep it on a trusted LAN, or put it behind a reverse proxy
  with HTTPS (Caddy or nginx) and set `NAS_COOKIE_SECURE=true`. Never expose it directly to the internet.
- Restrict port 8080 with a firewall if the machine is on an untrusted network.

## 9. Operations
```bash
sudo systemctl status nas-admin      # service status
sudo journalctl -u nas-admin -f      # live logs
sudo apt install ./nas-admin_<new>.deb   # upgrade (config and data are kept)
sudo apt remove nas-admin            # uninstall, keep config and data
sudo apt purge nas-admin             # uninstall and delete config and data
```
Back up `/var/lib/nas-admin` (database and `secret.key`) to migrate or recover an install.

## 10. Building the package
From a checkout of this repository:
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
PYTHON=.venv/bin/python packaging/build-deb.sh 1.0.1 --bundle-wheels
```
The result is written to `dist/`. Omit `--bundle-wheels` for an architecture-independent
package that downloads dependencies at install time. Requires `dpkg-deb`, `fakeroot` and `rsync`.
