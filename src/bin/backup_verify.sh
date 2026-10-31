#!/usr/bin/env bash
# Monthly restore test (borgmatic-style "spot" check):
#  1. borg check of the newest archive's metadata; every BACKUP_REPO_CHECK_DAYS also the whole repository
#  2. file count of the newest archive vs. the one before (a sudden drop means sources went missing)
#  3. restore BACKUP_SPOT_FILES random files and compare them with the originals. Files not changed since the
#     backup MUST be identical (else: corruption → FAILED); files changed since are only counted.
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
conf() { "$PY" -c "import sys; sys.path.insert(0, '$MS/lib'); from mysys import config; print(int(config.num('$1')))"; }
SPOT=$(conf BACKUP_SPOT_FILES); TOL=$(conf BACKUP_COUNT_TOLERANCE_PCT); REPO_DAYS=$(conf BACKUP_REPO_CHECK_DAYS)
"$PY" "$MS/bin/ms" ping verify start >/dev/null 2>&1

result() {
  printf 'last_run=%s\nlast_result=%s\ndetail=%s\n' "$(date +%s)" "$1" "$2" > "$ST"
  if [ "$1" = ok ]; then "$MS/bin/notify.sh" "Backup restore test passed" "$2" low white_check_mark
    "$PY" "$MS/bin/ms" ping verify >/dev/null 2>&1
  else "$MS/bin/notify.sh" "Backup restore test FAILED" "$2" high x
    "$PY" "$MS/bin/ms" ping verify fail >/dev/null 2>&1; fi
  echo "$1: $2" | tee -a "$LOG"
}

exec 9>"$MS/state/backup.lock"
flock -w 3600 9 || { result failed "backup lock busy for 1 h"; exit 2; }
[ -d "$REPO" ] || { result failed "repository not reachable"; exit 2; }
mapfile -t last2 < <(borg list --last 2 --format '{archive}{NL}' 2>>"$LOG")
last=${last2[-1]:-}
prev=""; [ ${#last2[@]} -ge 2 ] && prev=${last2[0]}
[ -n "$last" ] || { result failed "no archive found"; exit 2; }
echo "== verify $last $(date '+%F %T')" >> "$LOG"
nice -n 19 borg check --archives-only --last 1 >> "$LOG" 2>&1 || { result failed "borg check reported problems ($last)"; exit 2; }
extra=""
lastrepo=$(cat "$MS/state/verify_repo_last" 2>/dev/null || echo 0)
if [ $(( $(date +%s) - lastrepo )) -ge $(( REPO_DAYS * 86400 )) ]; then
  echo "== full repository check" >> "$LOG"
  nice -n 19 ionice -c3 borg check --repository-only >> "$LOG" 2>&1 || { result failed "repository check found damage"; exit 2; }
  date +%s > "$MS/state/verify_repo_last"; extra="; full repository check OK"
fi
# archive start time (epoch) — files not modified after it must match exactly
start=$(borg info --json "::$last" 2>>"$LOG" | "$PY" -c "import json,sys,datetime
a=json.load(sys.stdin)['archives'][0]['start']; print(int(datetime.datetime.fromisoformat(a).timestamp()))" 2>>"$LOG")
borg list --format '{type} {size} {path}{NL}' "::$last" 2>>"$LOG" > "$TMP/list"
n_last=$(awk '$1=="-"' "$TMP/list" | wc -l)
if [ -n "$prev" ]; then
  n_prev=$(borg list --format '{type}{NL}' "::$prev" 2>>"$LOG" | grep -c '^-')
  if [ "$n_prev" -gt 100 ] && [ $(( (n_prev - n_last) * 100 / n_prev )) -gt "$TOL" ]; then
    result failed "$last has $n_last files, $prev had $n_prev (more than $TOL% fewer — did a source folder disappear?)"; exit 2
  fi
  extra="$extra; $n_last files (previous archive $n_prev)"
fi
mapfile -t files < <(awk '$1=="-" && $2>0 && $2<52428800 {sub(/^[^ ]+ [^ ]+ /,""); print}' "$TMP/list" | shuf -n "$SPOT")
[ ${#files[@]} -gt 0 ] || { result failed "no sample files in $last"; exit 2; }
mkdir -p "$TMP/x"
( cd "$TMP/x" && borg extract "::$last" "${files[@]}" ) >> "$LOG" 2>&1 || { result failed "extract failed"; exit 2; }
ok=0; same=0; changed=0; gone=0; bad=()
for f in "${files[@]}"; do
  [ -f "$TMP/x/$f" ] || continue
  ok=$((ok + 1))
  if [ ! -f "/$f" ]; then gone=$((gone + 1)); continue; fi
  if cmp -s "$TMP/x/$f" "/$f"; then same=$((same + 1))
  elif [ -n "$start" ] && [ "$(stat -c %Z "/$f")" -le "$start" ]; then bad+=("/$f")   # ctime: also changed by mv/cp -p/rsync -t
  else changed=$((changed + 1)); fi
done
[ $ok -eq ${#files[@]} ] || { result failed "only $ok of ${#files[@]} files restored"; exit 2; }
if [ ${#bad[@]} -gt 0 ]; then
  printf 'MISMATCH (unchanged since backup): %s\n' "${bad[@]}" >> "$LOG"
  result failed "${#bad[@]} restored file(s) differ from originals that were not changed since the backup — e.g. ${bad[0]}"; exit 2
fi
result ok "$last: metadata OK, $ok files restored: $same identical, $changed changed since, $gone deleted since$extra"
