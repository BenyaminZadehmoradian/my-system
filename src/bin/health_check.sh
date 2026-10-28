#!/usr/bin/env bash
# Read-only health check. Writes $1 (default reports/<today>/health.txt), prints the summary.
# Exit 0 = all OK, 1 = warnings.
# shellcheck source=common.sh
. "$(dirname "$(readlink -f "$0")")/common.sh"
OUT="${1:-$MS/reports/$(date +%F)/health.txt}"
DAYDIR="$(dirname "$OUT")"
mkdir -p "$DAYDIR"
WARN=()
warn() { WARN+=("$1"); echo "  !! $1"; }
ok()   { echo "  ok $1"; }
info() { echo "  -- $1"; }
pyq() { "$PY" -c "import sys; sys.path.insert(0, '$MS/lib'); $1" 2>/dev/null; }

{
echo "# Health — $MACHINE_LABEL — $(date '+%F %T')"

echo; echo "## Disks"
# shellcheck disable=SC2086
df -h / $DATA_DISKS $NET_SHARES 2>/dev/null
pct=$(df --output=pcent / | tail -1 | tr -dc 0-9)
[ "$pct" -ge "$SSD_WARN_PCT" ] && warn "root disk ${pct}% full" || ok "root disk ${pct}%"
for m in $DATA_DISKS $NET_SHARES; do
  mountpoint -q "$m" && ok "$m mounted" || warn "$m NOT mounted"
done
echo "forecast: $("$PY" "$MS/bin/ms" forecast)" || true
"$PY" "$MS/bin/ms" forecast >/dev/null || warn "root disk forecast: $("$PY" "$MS/bin/ms" forecast)"

echo; echo "## Biggest folders (today)"
head -15 "$DAYDIR/sizes.txt" 2>/dev/null || info "not measured yet"

echo; echo "## Memory / swap"
free -h; swapon --show 2>/dev/null
avail=$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)
total=$(awk '/MemTotal/{print int($2/1048576)}' /proc/meminfo)
[ "$avail" -lt $(( total / 10 + 1 )) ] && warn "only ${avail} GB RAM available" || ok "${avail} GB RAM available"

echo; echo "## CPU"
t=$(pyq "from mysys import probes; print(probes.cpu_temp() or '')")
[ -n "$t" ] && { [ "$t" -ge "${HOT_C:-90}" ] && warn "CPU ${t}°C" || ok "CPU ${t}°C"; }
echo "load: $(cut -d' ' -f1-3 /proc/loadavg) on $(nproc) threads"

echo; echo "## Services"
ign="${IGNORE_UNITS:-}"
# units whose file no longer exists (leftovers of removed software) are only noted
stale=$(systemctl --failed --no-legend --plain 2>/dev/null | awk '$2=="not-found"{print $1}')
failed=$(systemctl --failed --no-legend --plain 2>/dev/null | awk '$2!="not-found"{print $1}' | while read -r u; do
  case " $ign " in *" $u "*) ;; *) echo "$u";; esac; done)
[ -n "$failed" ] && warn "failed system units: $(echo "$failed" | tr '\n' ' ')" || ok "no failed system units"
[ -n "$stale" ] && info "leftover failed units of removed software: $(echo "$stale" | tr '\n' ' ')(clear with: sudo systemctl reset-failed)"
ufailed=$(systemctl --user --failed --no-legend --plain 2>/dev/null | awk '{print $1}')
[ -n "$ufailed" ] && warn "failed user units: $(echo "$ufailed" | tr '\n' ' ')" || ok "no failed user units"
for tmr in my-system-watch.timer my-system-daily.timer; do
  systemctl --user is-active -q "$tmr" && ok "$tmr" || warn "$tmr inactive"
done
if command -v docker >/dev/null; then
  if systemctl is-active -q docker; then
    root=$(timeout 10 docker info --format '{{.DockerRootDir}}' 2>/dev/null)
    ok "docker${root:+ (data in $root)}"
  else
    info "docker not running"
  fi
fi
[ -f /var/run/reboot-required ] && warn "reboot required ($(uname -r) running)"

echo; echo "## Toolchains and known paths"
pyq "
import json
from mysys import config, detect
d = detect.load_state()
for l, p in d.get('tools', []) + config.pairs('paths.conf'):
    import os
    print(('  ok ' if os.path.exists(p) else '  !! MISSING ') + l + ' -> ' + p)
"

echo; echo "## Python environments"
venv_out=$(pyq "
import subprocess
from mysys import detect
for v in detect.load_state().get('venvs', []):
    py = v['path'] + '/bin/python'
    try:
        r = subprocess.run([py, '-m', 'pip', 'check'], capture_output=True, text=True, timeout=60)
        last = (r.stdout.strip().splitlines() or [''])[-1]
        state = 'ok' if r.returncode == 0 else 'issues: ' + last[:80]
    except Exception as e:
        state = 'BROKEN (' + type(e).__name__ + ')'
    print('  ' + ('ok ' if state == 'ok' else '?? ') + v['path'] + ' (' + (v.get('python') or '?') + ') ' + ('' if state == 'ok' else state))
")
echo "${venv_out:-  -- none detected}"
grep -q 'BROKEN' <<< "$venv_out" && warn "a Python environment is broken (interpreter missing)"

echo; echo "## Projects (git)"
if [ -s "$DAYDIR/git.txt" ]; then
  n=$(grep -c '^!!' "$DAYDIR/git.txt")
  grep '^!!' "$DAYDIR/git.txt" | head -15
  info "$n project(s) need attention (uncommitted / unpushed / no remote / not in backup)"
else
  info "no git projects found"
fi

echo; echo "## Watched files"
db="$MS/state/watched.sha256"
if [ -f "$db" ]; then
  while read -r sum file; do
    [ -e "$file" ] || { warn "removed: $file"; continue; }
    [ "$(sha256sum "$file" 2>/dev/null | cut -d' ' -f1)" = "$sum" ] || warn "CHANGED: $file"
  done < "$db"
  ok "fingerprints compared"
else
  info "no baseline yet (ms accept)"
fi

echo; echo "## Backup"
st="$MS/state/backup_status"
if [ -f "$st" ]; then
  ls_=$(grep -s '^last_success=' "$st" | cut -d= -f2); ls_=${ls_:-0}
  echo "last result: $(grep -s '^last_result=' "$st" | cut -d= -f2-)"
  if [ "$ls_" -gt 0 ]; then
    age=$(( ($(date +%s) - ls_) / 3600 ))
    echo "last success: $(date -d "@$ls_" '+%F %T') (${age} h ago)"
    [ "$age" -le 48 ] && ok "backup fresh" || warn "last successful backup ${age} h ago"
  else
    warn "no successful backup yet"
  fi
  vst="$MS/state/verify_status"
  if [ -f "$vst" ]; then
    vr=$(grep -s '^last_run=' "$vst" | cut -d= -f2); vres=$(grep -s '^last_result=' "$vst" | cut -d= -f2)
    vage=$(( ($(date +%s) - ${vr:-0}) / 86400 ))
    [ "$vres" = ok ] && [ "$vage" -le 40 ] && ok "restore test ($vage days ago)" || warn "restore test: $vres ($vage days ago)"
  else
    info "restore test not run yet (1st of the month)"
  fi
else
  info "backup not set up (ms backup-setup)"
fi

echo; echo "## AI activity (last 24 h)"
aud="$MS/reports/ai_audit/$(date +%Y-%m).jsonl"
if [ -f "$aud" ]; then
  since=$(date -d '24 hours ago' '+%F %T')
  recent=$(awk -v s="$since" -F'"t": "' 'NF>1 && substr($2,1,19)>=s' "$aud")
  echo "tool calls: $(printf '%s' "$recent" | grep -c .), blocked: $(printf '%s' "$recent" | grep -c '"decision": "blocked"')"
else
  info "no AI activity logged"
fi

echo; echo "## Notifications"
[ -s "$MS/state/ntfy_topic" ] && [ ! -f "$MS/state/ntfy_off" ] && ok "phone notifications on" || info "phone notifications off"

echo; echo "## Updates"
command -v apt >/dev/null && echo "apt upgradable: $(apt list --upgradable 2>/dev/null | grep -c upgradable)"

echo; echo "## Errors this boot (top sources)"
journalctl -b -p err --no-pager -o cat --output-fields=SYSLOG_IDENTIFIER 2>/dev/null | sort | uniq -c | sort -rn | head -8

echo; echo "## SUMMARY"
if [ ${#WARN[@]} -eq 0 ]; then echo "ALL OK"; else printf 'WARN: %s\n' "${WARN[@]}"; fi
} > "$OUT" 2>&1

sed -n '/## SUMMARY/,$p' "$OUT" | tail -n +2
grep -q '^WARN' "$OUT" && exit 1 || exit 0
