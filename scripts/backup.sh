#!/usr/bin/env bash
# Encrypted backup via restic. Run scripts/restore-test.sh once after setup.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/finoverview}"
export PYTHONPATH="$APP_DIR/src"
# The finoverview system user's home has no writable .cache — point restic at
# a directory it does own rather than depend on RESTIC_CACHE_DIR surviving
# every possible way this script gets invoked (systemd vs. manual sudo -u).
export RESTIC_CACHE_DIR="${RESTIC_CACHE_DIR:-$APP_DIR/data/restic-cache}"
mkdir -p "$RESTIC_CACHE_DIR"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

if [ -n "${BACKUP_MOUNT_PATH:-}" ] && ! mountpoint -q "$BACKUP_MOUNT_PATH"; then
  echo "backup skipped: $BACKUP_MOUNT_PATH not mounted (drive not attached) $(date -Is)" \
    | systemd-cat -t finoverview -p warning
  exit 0
fi

: "${RESTIC_REPOSITORY:?set RESTIC_REPOSITORY in /etc/finoverview/env}"
: "${RESTIC_PASSWORD_FILE:?set RESTIC_PASSWORD_FILE in /etc/finoverview/env}"

# VACUUM INTO takes a consistent snapshot without stopping the web process.
# Never back up a live SQLite file by copying it: you can capture a torn WAL.
"$APP_DIR/.venv/bin/python" -m finoverview.cli backup "$STAGE/finance.db"

# Config and the Enable Banking private key. Losing the key means re-registering
# the application and re-consenting at both banks, so it belongs in the backup.
cp -a "$APP_DIR/config/settings.toml" "$STAGE/" 2>/dev/null || true
cp -a "$APP_DIR/config/assets.toml"   "$STAGE/" 2>/dev/null || true
cp -a "$APP_DIR/secrets"              "$STAGE/" 2>/dev/null || true

restic backup --tag finoverview --host "$(hostname)" "$STAGE"
restic forget --tag finoverview --prune \
  --keep-daily 14 --keep-weekly 8 --keep-monthly 24
restic check --read-data-subset=5%

echo "backup ok $(date -Is)"
