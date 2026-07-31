#!/usr/bin/env bash
# Prove the backup works. Run this now, not after a failure.
set -euo pipefail

if [ -n "${BACKUP_MOUNT_PATH:-}" ] && ! mountpoint -q "$BACKUP_MOUNT_PATH"; then
  echo "FAIL: $BACKUP_MOUNT_PATH not mounted — plug in the backup drive first"
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

restic restore latest --tag finoverview --target "$TMP"
DB="$(find "$TMP" -name finance.db | head -1)"
[ -n "$DB" ] || { echo "FAIL: no finance.db in the snapshot"; exit 1; }

sqlite3 "$DB" "PRAGMA integrity_check;" | grep -qx ok || { echo "FAIL: integrity"; exit 1; }

echo "accounts:  $(sqlite3 "$DB" 'SELECT COUNT(*) FROM accounts;')"
echo "balances:  $(sqlite3 "$DB" 'SELECT COUNT(*) FROM balance_snapshots;')"
echo "positions: $(sqlite3 "$DB" 'SELECT COUNT(*) FROM position_snapshots;')"
echo "latest:    $(sqlite3 "$DB" 'SELECT MAX(ts) FROM balance_snapshots;')"
echo "RESTORE OK"
