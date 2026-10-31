#!/usr/bin/env bash
# Make code, rules and config root-owned and read-only (nothing running as you — including
# any AI — can change them), and (re)install the Claude Code managed policy.
# Reports and state stay writable. Undo with: ms unlock
# shellcheck source=common.sh
. "$(dirname "$(readlink -f "$0")")/common.sh"
set -e
DIRS=()
for d in bin lib plugins gui ai runlog config systemd README.md; do [ -e "$MS/$d" ] && DIRS+=("$MS/$d"); done
"$PY" -c "import sys; sys.path.insert(0, '$MS/lib'); from mysys import policy; policy.write('$MS/state/managed-settings.json')"
sudo install -d -m 755 /etc/claude-code
sudo install -m 644 -o root -g root "$MS/state/managed-settings.json" /etc/claude-code/managed-settings.json
sudo chown -R root:root "${DIRS[@]}"
sudo chmod -R a-w,a+rX "${DIRS[@]}"
sudo chmod a+x "$MS"/bin/* "$MS"/ai/claude/hooks/guard.py "$MS"/gui/*.py
echo "locked. Claude Code policy: /etc/claude-code/managed-settings.json"
