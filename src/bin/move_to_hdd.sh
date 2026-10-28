#!/usr/bin/env bash
# move_to_hdd.sh <folder> [--yes]
# Moves a folder from the root disk to $BIG_FOLDER_DEST/<name> and leaves a symlink, so every
# program keeps working. Refuses if any of your processes is using the folder.
# shellcheck source=common.sh
. "$(dirname "$(readlink -f "$0")")/common.sh"
SRC="${1:-}"; YES="${2:-}"
die() { echo "STOP: $*"; exit 1; }
[ -n "$SRC" ] || die "usage: ms move <folder> [--yes]"
[ -n "${BIG_FOLDER_DEST:-}" ] || die "BIG_FOLDER_DEST is not set in config/my_system.conf"
SRC=$(realpath -s "$SRC")
[ -d "$SRC" ] || die "$SRC is not a folder"
[ -L "$SRC" ] && die "$SRC is already a link (→ $(readlink "$SRC"))"
case "$SRC" in "$HOME"/*) ;; *) die "only folders inside your home can be moved";; esac
case "$SRC" in "$MS"*|"$HOME/.config"*|"$HOME/.local"*|"$HOME/snap"*|"$HOME/.ssh"*) die "$SRC must stay where it is";; esac
"$PY" - "$SRC" <<'EOF' || die "a protected path (venv/toolchain) — not moved"
import json, os, sys
src = os.path.realpath(sys.argv[1])
try:
    d = json.load(open(os.path.expanduser(os.environ["MY_SYSTEM"] + "/state/detected.json")))
except Exception:
    d = {}
for p in [v["path"] for v in d.get("venvs", [])] + [p for _, p in d.get("tools", [])]:
    p = os.path.realpath(p)
    if p == src or p.startswith(src + "/") or src.startswith(p + "/"):
        sys.exit(1)
EOF
DEST="$BIG_FOLDER_DEST/$(basename "$SRC")"
[ -e "$DEST" ] && die "$DEST already exists"
dmount=$(df --output=target "$(dirname "$BIG_FOLDER_DEST")" 2>/dev/null | tail -1)
[ "$dmount" = "/" ] && die "$BIG_FOLDER_DEST is on the root disk — nothing would be freed"

echo "== checking that nothing uses $SRC …"
users=$("$PY" - "$SRC" <<'EOF'
import os, sys
root = os.path.realpath(sys.argv[1]) + "/"
hits = set()
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        paths = [os.readlink(f"/proc/{pid}/cwd")]
        fd = f"/proc/{pid}/fd"
        paths += [os.readlink(f"{fd}/{x}") for x in os.listdir(fd)]
        if any((p + "/").startswith(root) for p in paths):
            hits.add(f"{open(f'/proc/{pid}/comm').read().strip()} (pid {pid})")
    except OSError:
        pass
print(", ".join(sorted(hits)))
EOF
)
[ -n "$users" ] && die "in use by: $users — wait until these finish"

size=$(du -sh "$SRC" | cut -f1)
echo "   $SRC ($size) → $DEST   [free there: $(df -h --output=avail "$dmount" | tail -1 | tr -d ' ')]"
if [ "$YES" != "--yes" ]; then
  read -rp "Move it? [y/N] " a; case "$a" in y|Y) ;; *) die "cancelled";; esac
fi
mkdir -p "$BIG_FOLDER_DEST"
echo "== copying …"
nice -n 10 rsync -aHAX --info=progress2 "$SRC/" "$DEST/" || die "copy failed — original untouched, partial copy in $DEST"
echo "== verifying …"
n1=$(find "$SRC" | wc -l); n2=$(find "$DEST" | wc -l)
s1=$(du -sb --apparent-size "$SRC" | cut -f1); s2=$(du -sb --apparent-size "$DEST" | cut -f1)
{ [ "$n1" = "$n2" ] && [ "$s1" = "$s2" ]; } || die "verification failed ($n1/$n2 files, $s1/$s2 bytes) — original untouched"
echo "== switching to the copy …"
mv "$SRC" "$SRC.moving-$$" || die "could not rename original"
ln -s "$DEST" "$SRC" || { mv "$SRC.moving-$$" "$SRC"; die "could not create link — original restored"; }
rm -rf "$SRC.moving-$$"
echo "DONE: $SRC → $DEST ($size freed)"
"$MS/bin/notify.sh" "Moved to data disk" "$(basename "$SRC") ($size) now lives in $BIG_FOLDER_DEST" low package
