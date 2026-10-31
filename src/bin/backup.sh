#!/usr/bin/env bash
# Encrypted, deduplicated backup with borg (nightly timer, or "Back up now" in the app).
# Passphrase comes from the desktop keyring (secret-tool).
# shellcheck source=common.sh
. "$(dirname "$(readlink -f "$0")")/common.sh"
# paused: only the nightly timer is skipped; "Back up now" / ms backup (MS_FORCE or a one-time flag) still run
if [ -f "$MS/state/backup_force" ]; then rm -f "$MS/state/backup_force"; MS_FORCE=1; fi
if is_paused backup && [ "${MS_FORCE:-}" != 1 ]; then echo "backup paused (ms resume backup)"; exit 0; fi
# shellcheck source=/dev/null
. "$MS/config/backup.conf"
[ -z "${REPO:-}" ] && REPO=$(cat "$MS/state/backup_repo" 2>/dev/null)
STATE="$MS/state/backup_status"
LOGDIR="$MS/reports/backup"
mkdir -p "$LOGDIR" "$MS/state"
LOG="$LOGDIR/$(date +%F_%H%M).log"
export BORG_REPO="$REPO" BORG_PASSCOMMAND="secret-tool lookup $KEYRING_ATTR"

prev_success=$(grep -s '^last_success=' "$STATE" | cut -d= -f2)
write_state() {
  printf 'last_attempt=%s\nlast_result=%s\nlast_success=%s\nlast_archive=%s\nlast_size=%s\nlast_log=%s\n' \
    "$(date +%s)" "$1" "${prev_success:-0}" "$2" "$3" "$LOG" > "$STATE"
}
fail() {
  echo "ERROR: $1" | tee -a "$LOG"
  write_state "failed: $1" "" ""
  "$MS/bin/notify.sh" "Backup FAILED" "$1" high x
  "$PY" "$MS/bin/ms" ping backup fail >/dev/null 2>&1
  exit 2
}

exec 9>"$MS/state/backup.lock"
flock -n 9 || { echo "a backup is already running"; exit 0; }
echo "== backup started $(date '+%F %T')" > "$LOG"
"$PY" "$MS/bin/ms" ping backup start >/dev/null 2>&1
[ -n "$REPO" ] || fail "no repository configured (ms backup-setup)"
mnt=$(df --output=target "$(dirname "$REPO")" 2>/dev/null | tail -1)
[ -d "$REPO" ] || fail "repository $REPO not reachable (is ${mnt:-its disk} mounted?)"
secret-tool lookup $KEYRING_ATTR >/dev/null 2>&1 || fail "passphrase not in keyring (ms backup-setup)"

mapfile -t SRC < <(grep -v '^\s*#' "$MS/config/backup_sources.txt" | sed '/^\s*$/d' | sed "s|^~|$HOME|")
EXIST=()
for s in "${SRC[@]}"; do
  if [ -e "$s" ]; then EXIST+=("$s"); else echo "skip (missing): $s" >> "$LOG"; fi
done
[ ${#EXIST[@]} -gt 0 ] || fail "none of the source folders exist"

ARCHIVE="$(hostname)-$(date +%Y-%m-%dT%H%M)"
nice -n 19 ionice -c3 borg create --stats --compression "$COMPRESSION" --exclude-caches \
  --exclude-from "$MS/config/backup_excludes.txt" "::$ARCHIVE" "${EXIST[@]}" >> "$LOG" 2>&1
rc=$?
[ $rc -ge 2 ] && fail "borg create exit $rc — see $LOG"
borg prune --keep-daily "$KEEP_DAILY" --keep-weekly "$KEEP_WEEKLY" --keep-monthly "$KEEP_MONTHLY" >> "$LOG" 2>&1
borg compact >> "$LOG" 2>&1

size=$(grep -m1 '^This archive:' "$LOG" | awk '{print $(NF-1), $NF}')
prev_success=$(date +%s)
result=ok; [ $rc -eq 1 ] && result="ok (with warnings)"
write_state "$result" "$ARCHIVE" "$size"
echo "== done $(date '+%F %T') — $result, new data: $size" | tee -a "$LOG"
"$MS/bin/notify.sh" "Backup done" "$ARCHIVE — new data: ${size:-?}" min floppy_disk
"$PY" "$MS/bin/ms" ping backup >/dev/null 2>&1
find "$LOGDIR" -name '*.log' -mtime +90 -delete
