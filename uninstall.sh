#!/usr/bin/env bash
# Remove timers, launcher, commands and the Claude Code policy. Your data in ~/My_System
# (reports, state, config) is kept; delete the folder yourself if you want it gone.
set -u
MS="${MY_SYSTEM:-$HOME/My_System}"
for t in watch daily backup verify digest report; do
  systemctl --user disable --now "my-system-$t.timer" 2>/dev/null
  rm -f "$HOME/.config/systemd/user/my-system-$t".{service,timer}
done
systemctl --user disable --now my-system-queue.service 2>/dev/null
rm -f "$HOME/.config/systemd/user/my-system-queue.service"
command -v claude >/dev/null && claude mcp remove --scope user my-system >/dev/null 2>&1
systemctl --user daemon-reload
rm -f "$HOME/.local/share/applications/my-system.desktop" "$HOME/.local/bin/ms" "$HOME/.local/bin/vrun"
[ -L "$HOME/AGENTS.md" ] && rm -f "$HOME/AGENTS.md"
if [ -f "$HOME/.claude/CLAUDE.md" ]; then
  /usr/bin/python3 - "$HOME/.claude/CLAUDE.md" <<'EOF'
import re, sys
p = sys.argv[1]; t = open(p).read()
open(p, "w").write(re.sub(r"\n*<!-- my-system:begin -->.*?<!-- my-system:end -->\n?", "\n", t, flags=re.S))
EOF
fi
if [ -f /etc/claude-code/managed-settings.json ] && grep -q "my-system\|guard.py" /etc/claude-code/managed-settings.json; then
  sudo rm -f /etc/claude-code/managed-settings.json
fi
if [ -f /etc/systemd/system/my-system-hwinfo.timer ]; then
  sudo systemctl disable --now my-system-hwinfo.timer 2>/dev/null
  sudo rm -f /etc/systemd/system/my-system-hwinfo.{service,timer} /usr/local/lib/my-system/ms-hwinfo
  sudo rmdir /usr/local/lib/my-system 2>/dev/null
  sudo systemctl daemon-reload
  echo "hardware collector removed (its data stays in /var/lib/my-system)"
fi
for d in bin lib plugins gui ai runlog root tests config systemd README.md; do
  [ -e "$MS/$d" ] && sudo chown -R "$(id -u):$(id -g)" "$MS/$d"
done
echo "my-system removed. Data kept in $MS (rm -rf \"$MS\" to delete it)."
