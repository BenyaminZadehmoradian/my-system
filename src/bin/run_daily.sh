#!/usr/bin/env bash
# Daily job: detection refresh, inventory, folder sizes, git status, health check,
# AI system map. Writes reports/<date>/ and points reports/latest at it.
# shellcheck source=common.sh
. "$(dirname "$(readlink -f "$0")")/common.sh"
DAY="$MS/reports/$(date +%F)"
mkdir -p "$DAY"
low() { nice -n 19 ionice -c3 "$@"; }

low "$PY" "$MS/bin/ms" detect --state > /dev/null 2>&1
low "$MS/bin/inventory.sh" "$DAY/inventory" > /dev/null 2>&1
ln -sfn "$DAY" "$MS/reports/latest"
low "$PY" "$MS/bin/ms" sizes > "$DAY/sizes.txt" 2>/dev/null
low "$PY" "$MS/bin/ms" git > "$DAY/git.txt" 2>/dev/null
low "$PY" "$MS/bin/ms" log index > /dev/null 2>&1
nice -n 19 "$MS/bin/health_check.sh" "$DAY/health.txt" > "$DAY/summary.txt"
status=$?
"$PY" "$MS/bin/ms" ai-context > /dev/null 2>&1

echo "$(date '+%F %T')  status=$status  $(head -1 "$DAY/summary.txt")" >> "$MS/reports/history.log"
find "$MS/reports" -mindepth 1 -maxdepth 1 -type d -name '20??-??-??' -mtime +"$REPORT_KEEP_DAYS" -exec rm -rf {} +
if [ "$status" -ne 0 ]; then
  "$MS/bin/notify.sh" "Daily check: $(grep -c '^WARN' "$DAY/summary.txt") warning(s)" "$(head -4 "$DAY/summary.txt")" default warning
fi
cat "$DAY/summary.txt"
