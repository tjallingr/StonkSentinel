#!/usr/bin/env bash
# Encrypted backup, offsite (B2, etc.) or a locally-attached drive.
#
# What actually protects your financial history is a tested restore, not the
# storage medium. Run scripts/restore-test.sh at least once, and again after any
# change to this script — an untested backup is a guess.
#
# For a cold-storage drive that isn't always plugged in: set BACKUP_MOUNT_PATH
# to its mount point. If it isn't mounted, this exits 0 (not a failure) so the
# nightly timer doesn't alert you for an expected disconnect — only a genuine
# restic error still fails the unit and fires OnFailure=.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/finoverview}"
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
