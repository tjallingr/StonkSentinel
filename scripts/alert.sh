#!/usr/bin/env bash
# Failure notification, invoked by finoverview-alert@<name>.service via OnFailure=.
#
# Usage: alert.sh <unit-name>
#
# The push carries the reason, not just the fact. A notification that says only
# "backup failed" is one you cannot act on until you are next at a terminal —
# which for a 03:30 timer means the following evening, and by then it has failed
# again and you have started ignoring it.
set -uo pipefail

NAME="${1:-unknown}"
HOST="$(hostname)"
WHEN="$(date -Is)"

# Last few lines this project logged for itself. backup.sh and the collectors
# write their one-line reason here with `systemd-cat -t finoverview`, so this is
# the actual cause rather than a generic systemd exit status.
REASON="$(journalctl -t finoverview -n 4 --no-pager -o cat 2>/dev/null | tail -4)"
if [ -z "$REASON" ]; then
  REASON="$(journalctl -u "finoverview-${NAME}.service" -n 4 --no-pager -o cat 2>/dev/null | tail -4)"
fi
[ -n "$REASON" ] || REASON="(no reason in the journal — check: journalctl -u finoverview-${NAME}.service -n 60)"

MSG="finoverview ${NAME} failed on ${HOST} at ${WHEN}
${REASON}"

printf '%s\n' "$MSG" | systemd-cat -t finoverview-alert -p err

if [ -n "${NTFY_URL:-}" ]; then
  printf '%s' "$MSG" \
    | curl -fsS --max-time 20 \
        -H "Title: finoverview ${NAME} failed on ${HOST}" \
        -H "Priority: high" \
        -H "Tags: warning" \
        --data-binary @- "$NTFY_URL" >/dev/null \
    || echo "ntfy post failed" | systemd-cat -t finoverview-alert -p warning
fi

# Never fail: this runs as OnFailure=, and a failing failure-handler is noise
# on top of the problem you are actually trying to be told about.
exit 0
