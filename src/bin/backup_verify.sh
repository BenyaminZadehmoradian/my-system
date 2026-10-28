#!/usr/bin/env bash
# Monthly restore test: borg check of the newest archive + restore up to 5 real files to a
# temporary folder and compare them with the originals.
# shellcheck source=common.sh
. "$(dirname "$(readlink -f "$0")")/common.sh"
# shellcheck source=/dev/null
. "$MS/config/backup.conf"
[ -z "${REPO:-}" ] && REPO=$(cat "$MS/state/backup_repo" 2>/dev/null)
export BORG_REPO="$REPO" BORG_PASSCOMMAND="secret-tool lookup $KEYRING_ATTR"
ST="$MS/state/verify_status"
LOG="$MS/reports/backup/verify_$(date +%F).log"
mkdir -p "$MS/reports/backup"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

result() {
  printf 'last_run=%s\nlast_result=%s\ndetail=%s\n' "$(date +%s)" "$1" "$2" > "$ST"
  if [ "$1" = ok ]; then "$MS/bin/notify.sh" "Backup restore test passed" "$2" low white_check_mark
  else "$MS/bin/notify.sh" "Backup restore test FAILED" "$2" high x; fi
  echo "$1: $2" | tee -a "$LOG"
}

exec 9>"$MS/state/backup.lock"
flock -w 3600 9 || { result failed "backup lock busy for 1 h"; exit 2; }
[ -d "$REPO" ] || { result failed "repository not reachable"; exit 2; }
last=$(borg list --last 1 --format '{archive}' 2>>"$LOG")
[ -n "$last" ] || { result failed "no archive found"; exit 2; }
echo "== verify $last $(date '+%F %T')" >> "$LOG"
nice -n 19 borg check --archives-only --last 1 >> "$LOG" 2>&1 || { result failed "borg check reported problems ($last)"; exit 2; }
mapfile -t files < <(borg list --format '{type} {size} {path}{NL}' "::$last" 2>>"$LOG" \
  | awk '$1=="-" && $2>1024 && $2<5242880 {sub(/^[^ ]+ [^ ]+ /,""); print}' | shuf -n 5)
[ ${#files[@]} -gt 0 ] || { result failed "no sample files in $last"; exit 2; }
( cd "$TMP" && borg extract "::$last" "${files[@]}" ) >> "$LOG" 2>&1 || { result failed "extract failed"; exit 2; }
ok=0; same=0
for f in "${files[@]}"; do
  [ -f "$TMP/$f" ] || continue
  ok=$((ok + 1))
  [ -f "/$f" ] && cmp -s "$TMP/$f" "/$f" && same=$((same + 1))
done
[ $ok -eq ${#files[@]} ] || { result failed "only $ok of ${#files[@]} files restored"; exit 2; }
result ok "$last: metadata OK, $ok files restored ($same identical to current originals)"
