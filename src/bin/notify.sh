#!/usr/bin/env bash
# notify.sh "Title" "Message" [min|low|default|high|urgent] [tag]   (tag: kept for compatibility, not sent)
# Desktop notification always; phone notification via ntfy when state/ntfy_topic exists
# and state/ntfy_off does not. Messages carry status text only, never file contents.
# shellcheck source=common.sh
. "$(dirname "$(readlink -f "$0")")/common.sh"
title="${1:-my-system}"; msg="${2:-}"; prio="${3:-default}"; tag="${4:-computer}"
case "$prio" in high|urgent) urg=critical;; min|low) urg=low;; *) urg=normal;; esac
command -v notify-send >/dev/null && notify-send -u "$urg" -a "My System" "$title" "$msg" 2>/dev/null
# Phone only for what needs you: priority PHONE_MIN_PRIO (default: default) and above, plus reports sent on purpose
# (MS_PHONE=1: status report, weekly summary). Quiet successes stay on the desktop, in the app and in the report.
rank() { case "$1" in min) echo 1;; low) echo 2;; high) echo 4;; urgent) echo 5;; *) echo 3;; esac; }
to_phone=0
[ "$(rank "$prio")" -ge "$(rank "${PHONE_MIN_PRIO:-default}")" ] && to_phone=1
[ "${MS_PHONE:-}" = 1 ] && to_phone=1
topic=$(cat "$MS/state/ntfy_topic" 2>/dev/null)
if [ "$to_phone" = 1 ] && [ -n "$topic" ] && [ ! -f "$MS/state/ntfy_off" ]; then
  # no "Tags" header: ntfy would show each tag as an emoji
  curl -s -m 10 -o /dev/null -H "Title: [$MACHINE_LABEL] $title" -H "Priority: $prio" \
    -d "$msg" "$NTFY_SERVER/$topic" || true
fi
mkdir -p "$MS/reports"
echo "$(date '+%F %T')|$prio|$title|${msg//$'\n'/ }" >> "$MS/reports/notifications.log"
exit 0
