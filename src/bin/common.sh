# shellcheck shell=bash
# Sourced by every my-system shell script.
MS="${MY_SYSTEM:-$HOME/My_System}"
export MY_SYSTEM="$MS"
# shellcheck source=/dev/null
[ -f "$MS/config/my_system.conf" ] && . "$MS/config/my_system.conf"
MACHINE_LABEL="${MACHINE_LABEL:-$(hostname)}"
SSD_WARN_PCT="${SSD_WARN_PCT:-93}"
NTFY_SERVER="${NTFY_SERVER:-https://ntfy.sh}"
REPORT_KEEP_DAYS="${REPORT_KEEP_DAYS:-60}"
PY=/usr/bin/python3
ms() { "$PY" "$MS/bin/ms" "$@"; }
# is_paused FEATURE: true while `ms pause FEATURE` (or `ms pause all`) is in effect
is_paused() {
  local f v now; now=$(date +%s)
  for f in "$1" all; do
    [ -f "$MS/state/paused/$f" ] || continue
    v=$(cat "$MS/state/paused/$f" 2>/dev/null); v=${v%%.*}
    { [ -z "$v" ] || [ "$v" = 0 ] || [ "$v" -gt "$now" ]; } && return 0
  done
  return 1
}
