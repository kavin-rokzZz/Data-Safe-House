#!/bin/bash
# Build nas-admin_<version>_all.deb.   Usage: packaging/build-deb.sh [version] [--bundle-wheels]
# --bundle-wheels downloads Python wheels for offline install (package becomes arch/python specific).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); SRC=$(dirname "$HERE")
VERSION=${1:-1.0.0}; BUNDLE=${2:-}
OUT=${OUT_DIR:-$SRC/dist}; STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
cp -r "$HERE/debian/DEBIAN" "$STAGE/DEBIAN"
sed "s/@VERSION@/$VERSION/" "$STAGE/DEBIAN/control.in" > "$STAGE/DEBIAN/control"; rm "$STAGE/DEBIAN/control.in"
install -d "$STAGE/opt/nas-admin" "$STAGE/etc/nas-admin" "$STAGE/lib/systemd/system" "$STAGE/usr/sbin"
# App code only: no data, secrets, venv or caches
rsync -a --exclude '__pycache__' "$SRC/app" "$STAGE/opt/nas-admin/"
cp "$SRC/requirements.txt" "$SRC/README.md" "$STAGE/opt/nas-admin/"
cp "$HERE/files/nas-admin.env" "$STAGE/etc/nas-admin/"
cp "$HERE/files/nas-admin.service" "$STAGE/lib/systemd/system/"
cp "$HERE/files/nas-admin-cli" "$STAGE/usr/sbin/nas-admin"
cp "$HERE/files/nas-admin-setup-samba" "$STAGE/usr/sbin/"
if [ "$BUNDLE" = "--bundle-wheels" ]; then
    "${PYTHON:-python3}" -m pip download -q -r "$SRC/requirements.txt" -d "$STAGE/opt/nas-admin/wheels"
    sed -i 's/^Architecture: all/Architecture: '"$(dpkg --print-architecture)"'/' "$STAGE/DEBIAN/control"
fi
chmod 755 "$STAGE"; chmod -R go-w,a+rX "$STAGE"
mkdir -p "$OUT"
fakeroot dpkg-deb --build "$STAGE" "$OUT/nas-admin_${VERSION}_$(grep ^Architecture "$STAGE/DEBIAN/control" | cut -d' ' -f2).deb"
