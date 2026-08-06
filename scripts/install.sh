#!/usr/bin/env bash
# Install onto the Pi. Idempotent; safe to re-run after a git pull.
set -euo pipefail

APP_DIR=/opt/finoverview
REPO="$(cd "$(dirname "$0")/.." && pwd)"

# restic is what the backup timer runs. It was previously assumed to be present,
# which turns a missing package into a nightly 03:30 alert rather than a setup step.
if ! command -v restic >/dev/null 2>&1; then
  sudo apt-get install -y restic
fi

sudo useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin finoverview 2>/dev/null || true
sudo mkdir -p "$APP_DIR" /etc/finoverview
sudo rsync -a --delete \
  --exclude '.git' --exclude 'data' --exclude 'secrets' --exclude '.env' \
  --exclude '.venv' \
  --exclude 'config/settings.toml' --exclude 'config/assets.toml' \
  "$REPO/" "$APP_DIR/"

sudo mkdir -p "$APP_DIR/data" "$APP_DIR/secrets"
sudo chown -R finoverview:finoverview "$APP_DIR/data" "$APP_DIR/secrets"
sudo chmod 700 "$APP_DIR/secrets" "$APP_DIR/data"

# config/ too: the settings pages write assets.toml, so the web process must own
# it. Copying the example files in with `sudo cp` leaves them root-owned, which
# makes every save in the UI fail with a permission error. 750, not 700 — you
# still want to read these over SSH as yourself without sudo.
sudo chown -R finoverview:finoverview "$APP_DIR/config"
sudo chmod 750 "$APP_DIR/config"
# settings.toml can hold API secrets when they are not in /etc/finoverview/env.
# `if`, not `[ ... ] && ...`: under `set -e` a false test as the last command of
# an AND-list aborts the script, so a missing file would silently stop the
# install here — before any unit was deployed.
if [ -f "$APP_DIR/config/settings.toml" ]; then
  sudo chmod 640 "$APP_DIR/config/settings.toml"
fi

if [ ! -d "$APP_DIR/.venv" ]; then
  sudo python3 -m venv "$APP_DIR/.venv"
fi
sudo "$APP_DIR/.venv/bin/pip" install --upgrade pip
sudo "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

sudo cp "$REPO"/systemd/*.service "$REPO"/systemd/*.timer /etc/systemd/system/

# Per-collector schedule overrides. These were a documented manual step, which
# meant the shipped 4-hourly default silently applied to saxo — whose refresh
# token dies in about an hour. The result is a connection that breaks overnight,
# every time, and a README that explains why nobody read. Install them.
for conf in "$REPO"/systemd/drop-ins/*.conf; do
  [ -e "$conf" ] || continue
  name="$(basename "$conf" .conf)"
  sudo mkdir -p "/etc/systemd/system/finoverview-collect@${name}.timer.d"
  sudo cp "$conf" "/etc/systemd/system/finoverview-collect@${name}.timer.d/override.conf"
done

sudo systemctl daemon-reload

# daemon-reload only reloads the unit definitions; a running service keeps the
# namespace, environment and sandbox it was started with. Changing something
# like ReadWritePaths and not restarting looks exactly like the change not
# working, so restart it here rather than leaving it as a step to forget.
if systemctl is-active --quiet finoverview-web.service; then
  sudo systemctl restart finoverview-web.service
  echo "restarted finoverview-web.service"
fi

echo
echo "Installed to $APP_DIR"
echo "Next:"
# `install -o finoverview`, not `sudo cp`: cp leaves the file root-owned, and the
# settings pages then fail to save with a permission error that reads like a bug
# in the app. Re-running this script also repairs it.
echo "  1. sudo install -o finoverview -g finoverview -m 640 \\"
echo "       config/settings.example.toml $APP_DIR/config/settings.toml   # then edit"
echo "  2. sudo install -o finoverview -g finoverview -m 640 \\"
echo "       config/assets.example.toml   $APP_DIR/config/assets.toml     # then edit"
echo "  3. put the Enable Banking RSA key at $APP_DIR/secrets/enablebanking.pem (chmod 600)"
echo "  4. sudo cp .env.example /etc/finoverview/env && sudo chmod 600 /etc/finoverview/env"
echo "     then edit in your app secrets, RESTIC_* and NTFY_URL"
echo "  5. sudo -u finoverview $APP_DIR/.venv/bin/python -m finoverview.auth.eb_link --check"
echo "  6. sudo systemctl enable --now finoverview-web.service"
echo "  7. sudo systemctl enable --now finoverview-collect@{fx,manual,enablebanking,saxo}.timer"
echo "  8. backup drive, if you use one instead of B2:"
echo "       sudo mkdir -p /mnt/backup-hdd && add it to /etc/fstab, then mount it"
echo "       sudo chown finoverview:finoverview /mnt/backup-hdd   # restic runs as finoverview"
echo "       set RESTIC_REPOSITORY=/mnt/backup-hdd/restic-repo and"
echo "           BACKUP_MOUNT_PATH=/mnt/backup-hdd in /etc/finoverview/env"
echo "       backup.sh creates the repository itself on first run"
echo "  9. sudo systemctl enable --now finoverview-backup.timer"
echo " 10. sudo -u finoverview $APP_DIR/scripts/restore-test.sh   # prove it restores"
