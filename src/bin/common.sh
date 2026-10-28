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
