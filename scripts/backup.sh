#!/usr/bin/env bash
# Encrypted backup via restic. Run scripts/restore-test.sh once after setup.
#
# Every way this can fail says why, in one line, tagged `finoverview` in the
# journal — because the only thing the ntfy alert can tell you is that it broke,
# and "backup failed" at 03:30 is not something you can act on from a phone.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/finoverview}"
export PYTHONPATH="$APP_DIR/src"
# The finoverview system user's home has no writable .cache — point restic at
# a directory it does own rather than depend on RESTIC_CACHE_DIR surviving
# every possible way this script gets invoked (systemd vs. manual sudo -u).
export RESTIC_CACHE_DIR="${RESTIC_CACHE_DIR:-$APP_DIR/data/restic-cache}"
mkdir -p "$RESTIC_CACHE_DIR"

die() {
  echo "backup FAILED: $*" | systemd-cat -t finoverview -p err
  echo "backup FAILED: $*" >&2
  exit 1
}

command -v restic >/dev/null 2>&1 \
  || die "restic is not installed (sudo apt install restic)"

if [ -n "${BACKUP_MOUNT_PATH:-}" ] && ! mountpoint -q "$BACKUP_MOUNT_PATH"; then
  echo "backup skipped: $BACKUP_MOUNT_PATH not mounted (drive not attached) $(date -Is)" \
    | systemd-cat -t finoverview -p warning
  exit 0
fi

# Unset almost always means "run by hand" rather than "misconfigured":
# /etc/finoverview/env is root-owned 0600 and is read by systemd, not by this
# script, so `sudo -u finoverview backup.sh` starts with none of it. Say that,
# rather than sending you to an env file that is probably already correct.
if [ -z "${RESTIC_REPOSITORY:-}" ] || [ -z "${RESTIC_PASSWORD_FILE:-}" ]; then
  if [ -z "${INVOCATION_ID:-}" ]; then
    die "no RESTIC_* in the environment. This script gets those from "\
"/etc/finoverview/env, which only systemd can read. Run it as the unit instead: "\
"sudo systemctl start finoverview-backup.service && journalctl -u finoverview-backup -n 40"
  fi
  die "RESTIC_REPOSITORY / RESTIC_PASSWORD_FILE not set in /etc/finoverview/env"
fi
[ -r "$RESTIC_PASSWORD_FILE" ] \
  || die "cannot read RESTIC_PASSWORD_FILE=$RESTIC_PASSWORD_FILE as $(id -un)"

# A local repository must live under the mount, or an unplugged drive is not a
# skip but a silent success: restic happily creates the repo on the SD card at
# the same path and backs up to the disk you are trying to protect against.
case "$RESTIC_REPOSITORY" in
  /*)
    if [ -n "${BACKUP_MOUNT_PATH:-}" ]; then
      case "$RESTIC_REPOSITORY/" in
        "$BACKUP_MOUNT_PATH"/*) ;;
        *) die "RESTIC_REPOSITORY=$RESTIC_REPOSITORY is not under "\
"BACKUP_MOUNT_PATH=$BACKUP_MOUNT_PATH — an unplugged drive would back up to "\
"the SD card instead" ;;
      esac
    fi
    # A read-only remount is its own diagnosis with its own fix, and restic
    # reports it as the same "unable to create lock in backend" you get from a
    # permissions problem. Separate them here: ext4 flips itself to ro after an
    # I/O error, and ntfs-3g refuses rw on a volume Windows left dirty.
    if [ -n "${BACKUP_MOUNT_PATH:-}" ]; then
      # `|| true` matters: findmnt exits non-zero for a path that isn't a mount,
      # and under `set -e` the assignment would take the whole script down with
      # no message at all — a silent backup failure, which is the one outcome
      # this script must never produce.
      opts=",$(findmnt -no OPTIONS "$BACKUP_MOUNT_PATH" 2>/dev/null || true),"
      case "$opts" in
        *,ro,*) die "$BACKUP_MOUNT_PATH is mounted READ-ONLY. Usually the "\
"filesystem was flipped after an I/O error — check: sudo dmesg -T | tail -40, "\
"then unmount and fsck it before remounting rw." ;;
      esac
    fi

    parent="$(dirname "$RESTIC_REPOSITORY")"
    [ -w "$parent" ] || die "$parent is not writable by $(id -un) — the drive is "\
"probably mounted root-owned; fix with: sudo chown finoverview:finoverview $parent"
    # The repo directory itself, when it already exists. The parent being
    # writable says nothing about a repo created earlier by root — which is
    # exactly what "unable to create lock in backend" is: restic can read the
    # repo it found and cannot write a lock into it.
    if [ -d "$RESTIC_REPOSITORY" ] && [ ! -w "$RESTIC_REPOSITORY" ]; then
      die "$RESTIC_REPOSITORY exists but is not writable by $(id -un) — it was "\
"probably created by root. Fix with: sudo chown -R finoverview:finoverview $RESTIC_REPOSITORY"
    fi
    ;;
esac

# A drive yanked mid-run leaves a lock behind, and every later run then fails
# with "repository is already locked". Stale locks only: `unlock` without
# --remove-all will not touch a lock held by a process that is still alive.
restic unlock >/dev/null 2>&1 || true

# A new drive has no repository on it, and `restic backup` will not create one.
# init is idempotent in the way that matters: on an existing repo it refuses,
# which tells us the repo is there but the password is wrong — a different fault
# with a different fix, so they get different messages.
if ! restic cat config >/dev/null 2>&1; then
  if restic init >/dev/null 2>&1; then
    echo "initialised a new restic repository at $RESTIC_REPOSITORY" \
      | systemd-cat -t finoverview -p notice
  else
    die "no usable repository at $RESTIC_REPOSITORY — init failed. If the repo "\
"does exist, the password in $RESTIC_PASSWORD_FILE does not match it."
  fi
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

# VACUUM INTO takes a consistent snapshot without stopping the web process.
# Never back up a live SQLite file by copying it: you can capture a torn WAL.
"$APP_DIR/.venv/bin/python" -m finoverview.cli backup "$STAGE/finance.db" \
  || die "sqlite snapshot failed — see the journal for the python traceback"

# Config and the Enable Banking private key. Losing the key means re-registering
# the application and re-consenting at both banks, so it belongs in the backup.
cp -a "$APP_DIR/config/settings.toml" "$STAGE/" 2>/dev/null || true
cp -a "$APP_DIR/config/assets.toml"   "$STAGE/" 2>/dev/null || true
cp -a "$APP_DIR/secrets"              "$STAGE/" 2>/dev/null || true

restic backup --tag finoverview --host "$(hostname)" "$STAGE" \
  || die "restic backup failed (disk full, drive disconnected mid-run, or I/O error)"
restic forget --tag finoverview --prune \
  --keep-daily 14 --keep-weekly 8 --keep-monthly 24 \
  || die "restic forget/prune failed — snapshots were written, retention was not applied"
restic check --read-data-subset=5% \
  || die "restic check found repository damage — do NOT trust this repo, run "\
"restic check --read-data in full"

echo "backup ok $(date -Is)"
