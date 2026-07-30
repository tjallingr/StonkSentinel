#!/usr/bin/env bash
# Install onto the Pi. Idempotent; safe to re-run after a git pull.
set -euo pipefail

APP_DIR=/opt/finoverview
REPO="$(cd "$(dirname "$0")/.." && pwd)"

sudo useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin finoverview 2>/dev/null || true
sudo mkdir -p "$APP_DIR" /etc/finoverview
sudo rsync -a --delete \
  --exclude '.git' --exclude 'data' --exclude 'secrets' \
  --exclude 'config/settings.toml' --exclude 'config/assets.toml' \
  "$REPO/" "$APP_DIR/"

sudo mkdir -p "$APP_DIR/data" "$APP_DIR/secrets"
sudo chown -R finoverview:finoverview "$APP_DIR/data" "$APP_DIR/secrets"
sudo chmod 700 "$APP_DIR/secrets" "$APP_DIR/data"

if [ ! -d "$APP_DIR/.venv" ]; then
  sudo python3 -m venv "$APP_DIR/.venv"
fi
sudo "$APP_DIR/.venv/bin/pip" install --upgrade pip
sudo "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

sudo cp "$REPO"/systemd/*.service "$REPO"/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload

echo
echo "Installed to $APP_DIR"
echo "Next:"
echo "  1. sudo cp config/settings.example.toml $APP_DIR/config/settings.toml  # then edit"
echo "  2. sudo cp config/assets.example.toml   $APP_DIR/config/assets.toml    # then edit"
echo "  3. put the Enable Banking RSA key at $APP_DIR/secrets/enablebanking.pem (chmod 600)"
echo "  4. sudo -u finoverview $APP_DIR/.venv/bin/python -m finoverview.auth.eb_link --check"
echo "  5. sudo systemctl enable --now finoverview-web.service"
echo "  6. sudo systemctl enable --now finoverview-collect@{fx,manual,enablebanking,saxo}.timer"
