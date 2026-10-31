#!/usr/bin/env bash
# my-system installer / updater.
#   git clone <repo> && cd my-system && ./install.sh          (interactive)
#   ./install.sh --yes                                         (accept defaults)
#   MY_SYSTEM=/other/place ./install.sh                        (install somewhere else)
# Re-running it updates the code and keeps your config, reports and state.
set -euo pipefail
YES=0; [ "${1:-}" = "--yes" ] && YES=1
REPO_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
SRC="$REPO_DIR/src"
MS="${MY_SYSTEM:-$HOME/My_System}"
PY=/usr/bin/python3
export MY_SYSTEM="$MS"
say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
ask() { [ $YES = 1 ] && return 0; read -rp "$1 [Y/n] " a; case "${a:-Y}" in n|N) return 1;; *) return 0;; esac; }

[ -x "$PY" ] || { echo "python3 is required"; exit 1; }
[ "$(id -u)" = 0 ] && [ -z "${MS_ALLOW_ROOT:-}" ] && { echo "run as your normal user (sudo is asked when needed)"; exit 1; }
say "Installing my-system $(cat "$SRC/lib/mysys/__init__.py" | sed -n 's/__version__ = "\(.*\)"/\1/p') into $MS"

# ---- 1. dependencies -------------------------------------------------------
need=()
"$PY" -c 'import PyQt6.QtWidgets' 2>/dev/null || need+=(python3-pyqt6)
for c in curl:curl git:git secret-tool:libsecret-tools borg:borgbackup tsp:task-spooler notify-send:libnotify-bin; do
  command -v "${c%%:*}" >/dev/null || need+=("${c##*:}")
done
if [ ${#need[@]} -gt 0 ]; then
  if command -v apt-get >/dev/null; then
    if ask "Install missing packages: ${need[*]}?"; then sudo apt-get install -y "${need[@]}"; fi
  else
    echo "Please install: ${need[*]} (package names may differ on your distribution)"
  fi
fi

# ---- 2. unlock an existing install, migrate older layouts -------------------
mkdir -p "$MS"
for d in bin lib plugins gui ai runlog config systemd scripts README.md; do
  if [ -e "$MS/$d" ] && [ "$(stat -c %u "$MS/$d")" = 0 ]; then
    sudo chown -R "$(id -u):$(id -g)" "$MS/$d"; chmod -R u+w "$MS/$d"
  fi
done
if [ -d "$MS/scripts" ]; then
  say "Migrating from the previous layout (scripts/ → bin/)"
  rm -rf "$MS/scripts" "$MS/ai/AI_CONTEXT.md" "$MS/ai/claude/managed-settings.json" "$MS/ai/claude/CLAUDE.md"
  [ -f "$MS/config/watched_files.conf" ] && sed -i -e "\|My_System/ai/AI_CONTEXT.md|d" \
    -e 's|~/My_System/scripts/accept_changes.sh|ms accept|' "$MS/config/watched_files.conf"
fi
mkdir -p "$MS"/{reports,state,config}

# ---- 3. code ------------------------------------------------------------------
for d in bin lib plugins gui ai runlog systemd; do
  rm -rf "${MS:?}/$d"; cp -r "$SRC/$d" "$MS/$d"
done
cp "$REPO_DIR/README.md" "$MS/README.md"
chmod +x "$MS"/bin/* "$MS"/gui/*.py "$MS"/ai/claude/hooks/guard.py

# ---- 4. config (only missing files are created) ------------------------------
say "Detecting this machine"
"$PY" "$MS/bin/ms" write-initial-config "$MS/config"
[ -f "$MS/config/backup_excludes.txt" ] || cp "$SRC/defaults/backup_excludes.txt" "$MS/config/"
grep -q "ai/AI_RULES.md" "$MS/config/watched_files.conf" 2>/dev/null || echo "$MS/ai/AI_RULES.md" >> "$MS/config/watched_files.conf"
if [ $YES = 0 ] && ask "Review/edit the main settings now (data disks, shares, alert levels)?"; then
  "${EDITOR:-nano}" "$MS/config/my_system.conf" || true
fi

# ---- 5. state -----------------------------------------------------------------
if [ ! -s "$MS/state/ntfy_topic" ]; then
  echo "$(hostname | tr -cd 'a-zA-Z0-9-' | head -c 20)-$(head -c 10 /dev/urandom | base32 | tr -d '=' | tr 'A-Z' 'a-z')" > "$MS/state/ntfy_topic"
  chmod 600 "$MS/state/ntfy_topic"
fi
"$PY" "$MS/bin/ms" detect --state > /dev/null
"$PY" "$MS/bin/ms" git > /dev/null 2>&1 || true
"$PY" "$MS/bin/ms" ai-context > /dev/null

# ---- 6. integration: timers, launcher, commands, AI instructions ---------------
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR" "$HOME/.local/share/applications" "$HOME/.local/bin" "$HOME/.claude"
for f in "$MS"/systemd/*.service "$MS"/systemd/*.timer; do
  sed "s|@MS@|$MS|g" "$f" > "$UNIT_DIR/$(basename "$f")"
done
sed "s|@MS@|$MS|g" "$MS/systemd/my-system.desktop" > "$HOME/.local/share/applications/my-system.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
ln -sfn "$MS/bin/ms" "$HOME/.local/bin/ms"
ln -sfn "$MS/bin/vrun.sh" "$HOME/.local/bin/vrun"
systemctl --user daemon-reload
systemctl --user enable --now my-system-watch.timer my-system-daily.timer my-system-digest.timer my-system-report.timer >/dev/null
repo=$("$PY" -c "import sys; sys.path.insert(0,'$MS/lib'); from mysys import config; print(config.backup_repo())")
if [ -n "$repo" ] && [ -d "$repo" ]; then
  systemctl --user enable --now my-system-backup.timer my-system-verify.timer >/dev/null
fi

# CLAUDE.md: replace our block, keep everything else the user wrote
CM="$HOME/.claude/CLAUDE.md"
touch "$CM"
"$PY" - "$CM" "$MS" <<'EOF'
import re, sys
path, ms = sys.argv[1], sys.argv[2]
text = open(path).read()
old = re.compile(r"# Global rules for Claude on this machine\n@~/My_System/ai/AI_CONTEXT\.md\n.*?stop and ask the user\.\n?", re.S)
text = old.sub("", text)
text = re.sub(r"<!-- my-system:begin -->.*?<!-- my-system:end -->\n?", "", text, flags=re.S).strip("\n")
block = open(ms + "/ai/claude/CLAUDE.md.template").read().replace("@MS@", ms)
text = text.rstrip() + ("\n\n" if text.strip() else "") + "<!-- my-system:begin -->\n" + block + "<!-- my-system:end -->\n"
open(path, "w").write(text)
EOF
ln -sfn "$MS/state/AGENTS.md" "$HOME/AGENTS.md"

# ---- 7. lock + baseline + first run ------------------------------------------
say "Locking code, rules and config (sudo)"
"$MS/bin/lock.sh"
"$MS/bin/accept_changes.sh"

# ---- 8. read-only hardware collector (root): SMART, memory modules, firmware ------
# Installed outside your home (root-owned) so nothing running as you can change what root runs.
if [ -f /etc/systemd/system/my-system-hwinfo.timer ] || ask "Install the read-only hardware collector (disk health, memory modules, firmware updates; runs daily as root)?"; then
  hwneed=()
  command -v smartctl >/dev/null || hwneed+=(smartmontools)
  command -v dmidecode >/dev/null || hwneed+=(dmidecode)
  if [ ${#hwneed[@]} -gt 0 ] && command -v apt-get >/dev/null; then sudo apt-get install -y --no-install-recommends "${hwneed[@]}"; fi
  sudo install -D -m 755 -o root -g root "$SRC/root/ms-hwinfo" /usr/local/lib/my-system/ms-hwinfo
  sudo install -d -m 755 /var/lib/my-system
  sudo install -m 644 -o root -g root "$SRC/root/my-system-hwinfo.service" "$SRC/root/my-system-hwinfo.timer" /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now my-system-hwinfo.timer >/dev/null 2>&1
  sudo systemctl start --no-block my-system-hwinfo.service
  echo "hardware collector installed (first data in about a minute: ms hw)"
fi
"$PY" "$MS/bin/ms" watch || true
systemctl --user start --no-block my-system-daily.service

echo
say "Done."
echo "  App:       'My System' in the app menu   (or: ms gui)"
echo "  Terminal:  ms            (status)   ms help   (all commands)"
echo "  Report:    ms report --open   (full system report; also on the 1st of every month)"
echo "  Phone:     install the ntfy app, subscribe to the topic shown in Notifications, then: ms notify-test"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "  Note: add ~/.local/bin to PATH to use 'ms' and 'vrun' (log out and in on Ubuntu)";; esac
if [ ! -f "$MS/state/backup_configured" ] && [ ! -f "$MS/state/backup_status" ] && [ -z "$repo" ] && [ -t 0 ]; then
  if ask "Set up the encrypted backup now?"; then "$MS/bin/backup_setup.sh"; else echo "  Later: ms backup-setup"; fi
fi
