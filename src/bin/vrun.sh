#!/usr/bin/env bash
# vrun <command...>  — run any long job and get notified when it ends (exit code + duration).
# Queue many jobs:   tsp vrun vivado -mode batch -source run.tcl
# shellcheck source=common.sh
. "$(dirname "$(readlink -f "$0")")/common.sh"
[ $# -eq 0 ] && { echo "usage: vrun <command> [args...]"; exit 1; }
start=$(date +%s)
"$@"
rc=$?
dur=$(( $(date +%s) - start ))
hms=$(printf '%dh%02dm%02ds' $((dur/3600)) $((dur%3600/60)) $((dur%60)))
name="$(basename "$1") — $(basename "$PWD")"
if [ $rc -eq 0 ]; then
  "$MS/bin/notify.sh" "Finished: $name" "OK after $hms" default white_check_mark
else
  "$MS/bin/notify.sh" "FAILED: $name" "exit code $rc after $hms in $PWD" high x
fi
"$PY" - "$PWD" "$dur" "$rc" "$*" >> "$MS/reports/runs.jsonl" <<'EOF'
import json, sys, time
cwd, dur, rc, cmd = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
print(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "cmd": cmd[:200], "cwd": cwd,
                  "seconds": dur, "rc": rc, "via": "vrun"}))
EOF
exit $rc
