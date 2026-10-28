#!/usr/bin/env bash
# Read-only inventory of tools, libraries, containers and Python environments.
# Output folder: $1 (default reports/<today>/inventory)
# shellcheck source=common.sh
. "$(dirname "$(readlink -f "$0")")/common.sh"
OUT="${1:-$MS/reports/$(date +%F)/inventory}"
mkdir -p "$OUT"

TOOLS="gcc g++ clang clang++ cc rustc cargo go java javac node npm python3 pip3 conda mamba uv R julia matlab octave
cmake make ninja meson bazel gdb lldb valgrind perf git gh git-lfs docker podman apptainer singularity
yosys nextpnr-ice40 nextpnr-ecp5 iverilog vvp verilator gtkwave ghdl openroad vivado vitis_hls vitis-run quartus_sh vsim
xelatex pdflatex lualatex biber latexmk pandoc tmux screen htop btop ncdu duf rg fdfind fd jq tsp borg ssh rsync curl wget code"

ver() { # first version-looking line
  local out
  out=$(timeout 10 "$@" 2>&1 | grep -v '^Picked up' | grep -m1 -iE '[0-9]+\.[0-9]+' | head -c 110)
  echo "${out:-?}"
}

{
  echo "# Tools — $(date '+%F %T')  (name  path  version)"
  for t in $TOOLS; do
    p=$(command -v "$t" 2>/dev/null) || continue
    case "$t" in
      java) v=$(ver java -version);; yosys) v=$(ver yosys -V);; iverilog) v=$(ver iverilog -V);;
      tmux) v=$(ver tmux -V);; tsp|vsim|matlab|vivado|vitis_hls|vitis-run|quartus_sh) v="(present)";;
      gtkwave) v=$(ver gtkwave --version);; *) v=$(ver "$t" --version);;
    esac
    printf '%-14s %-50s %s\n' "$t" "$p" "$v"
  done
  echo
  echo "# Toolchains (detected installs)"
  "$PY" - "$MS" <<'EOF'
import json, sys
try:
    d = json.load(open(sys.argv[1] + "/state/detected.json"))
except Exception:
    d = {}
for label, path in d.get("tools", []):
    print(f"{label:24} {path}")
EOF
  echo
  if command -v docker >/dev/null; then
    echo "# Docker images"
    timeout 20 docker image ls --format '{{.Repository}}:{{.Tag}}  {{.Size}}' 2>/dev/null || echo "(docker not reachable)"
  fi
} > "$OUT/tools.txt"

command -v apt-mark >/dev/null && apt-mark showmanual 2>/dev/null | sort > "$OUT/apt_manual.txt"
command -v dpkg-query >/dev/null && dpkg-query -W -f='${Package}\t${Version}\n' 2>/dev/null | grep -E -- '-dev\b' | sort > "$OUT/dev_libraries.txt"
command -v rpm >/dev/null && ! command -v dpkg >/dev/null && rpm -qa --qf '%{NAME}\t%{VERSION}\n' | sort > "$OUT/rpm_packages.txt"
command -v snap >/dev/null && snap list 2>/dev/null > "$OUT/snaps.txt"
command -v flatpak >/dev/null && flatpak list --app 2>/dev/null > "$OUT/flatpaks.txt"

# one file per detected Python environment
rm -f "$OUT"/venv_*.txt
"$PY" - "$MS" "$OUT" <<'EOF'
import json, os, re, subprocess, sys
ms, out = sys.argv[1], sys.argv[2]
try:
    venvs = json.load(open(ms + "/state/detected.json")).get("venvs", [])
except Exception:
    venvs = []
home = os.path.expanduser("~")
for v in venvs:
    p = v["path"]
    py = os.path.join(p, "bin", "python")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", p.replace(home, "home"))[-60:].strip("_")
    with open(os.path.join(out, f"venv_{slug}.txt"), "w") as f:
        f.write(f"# {p} — Python {v.get('python') or '?'}\n")
        if os.path.exists(py):
            try:
                r = subprocess.run([py, "-m", "pip", "list", "--format=freeze"], capture_output=True, text=True, timeout=60)
                f.write(r.stdout or "(pip unavailable)\n")
            except Exception as e:
                f.write(f"(error: {e})\n")
        else:
            f.write("(interpreter missing)\n")
EOF
echo "inventory written to $OUT"
