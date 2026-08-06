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

# Run a restic command, keeping its output. On failure the last lines go into
# the reason, because restic already knows exactly what went wrong and any
# summary this script invents on top is a guess that can be wrong — reporting
# "the password does not match" for a repository that has been backing up fine
# for a week sends you to fix the one thing that isn't broken.
run_restic() {
  local label="$1"; shift
  local out
  if out="$(restic "$@" 2>&1)"; then
    [ -n "$out" ] && printf '%s\n' "$out"
    return 0
  fi
  printf '%s\n' "$out" >&2
  die "$label: $(printf '%s' "$out" | grep -v '^$' | tail -3 | tr '\n' ' | ')"
}

# A drive yanked mid-run leaves a lock behind, and every later run then fails
# with "repository is already locked". Stale locks only: `unlock` without
# --remove-all will not touch a lock held by a process that is still alive.
# Not fatal on its own — an unwritable repo fails here too, and the probe below
# reports that far better than this would.
restic unlock >/dev/null 2>&1 || true

# A new drive has no repository on it, and `restic backup` will not create one.
# But "cat config failed" has several causes and only one of them is a missing
# repository, so branch on what restic actually said rather than assuming.
if ! probe="$(restic cat config 2>&1 >/dev/null)"; then
  # Is the repository actually absent? For a local path the filesystem answers
  # that directly, and it is the only trustworthy answer: restic says "unable to
  # open config file" both for a repo that is not there and for one it is not
  # allowed to read, and treating the second as the first sends you to create a
  # repository that already exists.
  repo_absent=0
  case "$RESTIC_REPOSITORY" in
    /*) [ -e "$RESTIC_REPOSITORY/config" ] || repo_absent=1 ;;
    *)  case "$probe" in
          *"Is there a repository"*|*"does not exist"*) repo_absent=1 ;;
        esac ;;
  esac

  if [ "$repo_absent" -eq 1 ]; then
    if init_err="$(restic init 2>&1 >/dev/null)"; then
      echo "initialised a new restic repository at $RESTIC_REPOSITORY" \
        | systemd-cat -t finoverview -p notice
    else
      die "no repository at $RESTIC_REPOSITORY and it could not be created: "\
"$(printf '%s' "$init_err" | grep -v '^$' | tail -2 | tr '\n' ' | ')"
    fi
  else
    case "$probe" in
      *"wrong password"*|*"invalid password"*)
        die "the password in $RESTIC_PASSWORD_FILE does not match the repository "\
"at $RESTIC_REPOSITORY" ;;
      *)
        # Permission denied, an I/O error, a half-written repo. Whatever it is,
        # restic already said it — pass it through untouched.
        die "the repository at $RESTIC_REPOSITORY exists but cannot be opened: "\
"$(printf '%s' "$probe" | grep -v '^$' | tail -3 | tr '\n' ' | ')" ;;
    esac
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

run_restic "restic backup failed" backup --tag finoverview --host "$(hostname)" "$STAGE"
run_restic "restic forget/prune failed (snapshots were written, retention was not applied)" \
  forget --tag finoverview --prune --keep-daily 14 --keep-weekly 8 --keep-monthly 24
run_restic "restic check found a problem — do NOT trust this repo until 'restic check --read-data' passes" \
  check --read-data-subset=5%

echo "backup ok $(date -Is)"
