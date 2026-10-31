"""Software and library management: what needs updating, and exactly who uses what.

`ms software scan` (also daily) builds state/software.json:
  * updates: apt (security marked, reboot-relevant marked), snap, flatpak, pip per Python environment
  * end-of-life Python versions, programs still using deleted libraries after an update (need restart)
  * usage map, from the project folders and from real runs:
      project → Python environment(s), imported packages (with installed versions), toolchains and versions,
                command-line tools, Docker images
      Python environment → projects that use it; package → projects that import it
      system library (apt) → Python packages in which environment link against it
      apt package → processes using it right now
  * advice per update: safe now / wait until runs finish / needs reboot / test project X afterwards
`ms software` prints the summary; `ms software uses NAME` answers "who uses NAME?".
Everything here only reads; it never installs or removes anything.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import config, detect

STATE = config.STATE / "software.json"
CACHE = config.STATE / "software"
TEXT_EXT = {".py", ".sh", ".bash", ".tcl", ".mk", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".json", ".txt", ".md",
            ".do", ".xdc", ".ipynb", ".make", ".cmake", ".env"}
TEXT_NAMES = {"Makefile", "makefile", "GNUmakefile", "Dockerfile", "CMakeLists.txt", "Snakefile", "requirements.txt"}
SKIP = {".git", ".Xil", "__pycache__", "node_modules", ".venv", "venv", ".cache", ".ipynb_checkpoints", "build",
        ".runs", ".sim", ".hw", ".ip_user_files", ".gen", ".srcs", "work", "obj_dir"}
TOOLS = ["vivado", "vitis_hls", "vitis", "v++", "xsim", "xelab", "xvlog", "vivado_hls", "vitis-run", "bootgen", "xsct",
         "yosys", "nextpnr-ice40", "nextpnr-ecp5", "nextpnr", "iverilog", "vvp", "verilator", "gtkwave", "ghdl", "openroad",
         "gem5", "sniper", "matlab", "quartus_sh", "vsim", "docker", "openocd", "riscv64-unknown-elf-gcc", "arm-none-eabi-gcc",
         "cmake", "ninja", "gcc", "clang", "nvcc", "merlin", "merlincc", "latexmk", "pdflatex", "xelatex"]
PY_EOL = {"3.6": "2021-12", "3.7": "2023-06", "3.8": "2024-10", "3.9": "2025-10", "3.10": "2026-10", "3.11": "2027-10",
          "3.12": "2028-10", "3.13": "2029-10", "3.14": "2030-10"}
REBOOT_PKGS = re.compile(r"^(linux-(image|modules|headers|firmware)|libc6|systemd|dbus|nvidia-|intel-microcode|amd64-microcode|libssl)")
IMPORT_RX = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w]*)[\w.]*\s+import|import\s+([A-Za-z_][\w]*(?:\s*,\s*[A-Za-z_][\w]*)*))", re.M)
STDLIB = None


def _run(cmd, timeout=60, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _stdlib():
    global STDLIB
    if STDLIB is None:
        import sys
        STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
    return STDLIB


# ---------------------------------------------------------------- updates
def apt_updates():
    out = []
    for line in _run(["apt", "list", "--upgradable"], 120).splitlines():
        m = re.match(r"^([^/]+)/(\S+)\s+(\S+)\s+\S+\s+\[upgradable from: ([^\]]+)\]", line)
        if m:
            out.append({"kind": "apt", "name": m.group(1), "new": m.group(3), "old": m.group(4),
                        "security": "-security" in m.group(2), "reboot": bool(REBOOT_PKGS.match(m.group(1)))})
    return out


def snap_updates():
    out = []
    if shutil.which("snap"):
        for line in _run(["snap", "refresh", "--list"], 60).splitlines()[1:]:
            p = line.split()
            if len(p) >= 2:
                out.append({"kind": "snap", "name": p[0], "new": p[1], "old": ""})
    return out


def flatpak_updates():
    out = []
    if shutil.which("flatpak"):
        for line in _run(["flatpak", "remote-ls", "--updates", "--app", "--columns=application,version"], 120).splitlines():
            p = line.split("\t")
            if p and p[0]:
                out.append({"kind": "flatpak", "name": p[0], "new": p[1] if len(p) > 1 else "", "old": ""})
    return out


VENV_SCRIPT = r'''
import json, importlib.metadata as md, sys
out = {}
for d in md.distributions():
    try:
        name = d.metadata["Name"]
    except Exception:
        continue
    if not name:
        continue
    top = (d.read_text("top_level.txt") or "").split()
    if not top:
        for f in (d.files or [])[:400]:
            p = str(f).split("/")[0]
            if p.endswith(".py"):
                p = p[:-3]
            if p and not p.endswith((".dist-info", ".egg-info", ".data")) and p not in ("..", "__pycache__", "bin"):
                top.append(p)
    out[name] = {"version": d.version, "top": sorted(set(top)), "requires": d.requires or []}
print(json.dumps({"python": sys.version.split()[0], "packages": out}))
'''


def venv_info(path, outdated=True):
    py = os.path.join(path, "bin", "python")
    info = {"path": path, "python": None, "packages": {}, "outdated": [], "broken": False}
    if not os.path.exists(py):
        info["broken"] = True
        return info
    try:
        d = json.loads(_run([py, "-c", VENV_SCRIPT], 120) or "{}")
        info["python"] = d.get("python")
        info["packages"] = d.get("packages", {})
    except ValueError:
        info["broken"] = True
        return info
    if outdated:
        try:
            info["outdated"] = json.loads(_run([py, "-m", "pip", "list", "--outdated", "--format=json",
                                                "--disable-pip-version-check"], 180) or "[]")
        except ValueError:
            info["outdated"] = []
    return info


def python_eol(ver):
    if not ver:
        return None
    mm = ".".join(ver.split(".")[:2])
    eol = PY_EOL.get(mm)
    return eol if eol and eol <= time.strftime("%Y-%m") else None


# ---------------------------------------------------------------- shared libraries of compiled Python packages
def native_libs(venv_path, limit=2500):
    """{dist_top_dir: set(system library paths)} for compiled extensions in a venv."""
    res = {}
    sos = glob.glob(os.path.join(venv_path, "lib", "python*", "site-packages", "**", "*.so*"), recursive=True)[:limit]
    for so in sos:
        rel = so.split("site-packages/", 1)[-1].split("/")[0]
        for line in _run(["ldd", so], 10).splitlines():
            m = re.search(r"=>\s+(/\S+)", line)
            if m and not m.group(1).startswith(venv_path) and "site-packages" not in m.group(1):
                res.setdefault(rel, set()).add(os.path.realpath(m.group(1)))
    return res


def dpkg_owners(paths):
    owner = {}
    paths = sorted(set(paths))
    for i in range(0, len(paths), 200):
        chunk = paths[i:i + 200]
        cand = []
        for p in chunk:
            cand += [p, p.replace("/usr/lib/", "/lib/", 1)] if p.startswith("/usr/lib/") else [p]
        for line in _run(["dpkg", "-S"] + cand, 60).splitlines():
            m = re.match(r"^([^:]+)(?::\S+)?:\s+(/\S+)$", line)
            if m:
                owner[m.group(2).replace("/lib/", "/usr/lib/", 1) if m.group(2).startswith("/lib/") else m.group(2)] = m.group(1).split(",")[0]
    return owner


# ---------------------------------------------------------------- processes and libraries in use
def maps_in_use():
    """{path: [(pid, comm)]} of shared objects mapped by your processes; also deleted ones."""
    use, deleted = {}, {}
    uid = os.getuid()
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            if p.stat().st_uid != uid:
                continue
            comm = (p / "comm").read_text().strip()
            for line in (p / "maps").read_text().splitlines():
                parts = line.split(None, 5)
                if len(parts) == 6 and ".so" in parts[5]:
                    path = parts[5]
                    if path.endswith("(deleted)"):
                        deleted.setdefault(path.replace(" (deleted)", ""), set()).add((int(p.name), comm))
                    else:
                        use.setdefault(path, set()).add((int(p.name), comm))
        except (OSError, ValueError):
            continue
    return use, deleted


def package_files(pkg):
    return [l for l in _run(["dpkg", "-L", pkg], 20).splitlines() if ".so" in l]


# ---------------------------------------------------------------- project scanning
def scan_project(root, budget_files=3000, max_bytes=1_000_000):
    """What a project references: imports, requirement files, venv paths, tools, versions, docker images."""
    res = {"imports": set(), "requirements": set(), "venv_refs": set(), "tools": set(), "versions": set(),
           "docker": set(), "local_venvs": [], "files": 0}
    n = 0
    for dp, dns, fns in os.walk(root):
        for d in list(dns):
            if os.path.isfile(os.path.join(dp, d, "pyvenv.cfg")):
                res["local_venvs"].append(os.path.join(dp, d))
                dns.remove(d)
        dns[:] = [d for d in dns if d not in SKIP and not d.startswith(".")]
        for fn in fns:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in TEXT_EXT and fn not in TEXT_NAMES and not fn.startswith("requirements"):
                continue
            f = os.path.join(dp, fn)
            try:
                if os.path.getsize(f) > max_bytes:
                    continue
                txt = open(f, errors="replace").read()
            except OSError:
                continue
            n += 1
            if ext in (".py", ".ipynb"):
                for a, b in IMPORT_RX.findall(txt.replace("\\n", "\n")):
                    for mod in (a or b).split(","):
                        res["imports"].add(mod.strip().split(".")[0])
            if fn.startswith("requirements") or fn in ("pyproject.toml", "environment.yml", "setup.py", "setup.cfg"):
                for m in re.finditer(r"^\s*[-\"']?\s*([A-Za-z0-9][A-Za-z0-9_.\-]*)\s*(?:\[|[=<>!~]|$|\"|')", txt, re.M):
                    name = m.group(1)
                    if name.lower() not in ("python", "pip", "name", "version", "dependencies", "requires", "channels"):
                        res["requirements"].add(name.lower())
            for m in re.finditer(r"((?:~|/home/[^/\s]+|/opt|/mnt/[^\s\"']+)/[^\s\"'`]*?(?:\.?venvs?|envs|virtualenvs)[^\s\"'`]*)", txt):
                res["venv_refs"].add(m.group(1).split("/bin/")[0].rstrip("/"))
            low = txt.lower()
            for t in TOOLS:
                if re.search(r"(?<![\w./-])" + re.escape(t) + r"(?![\w-])", low):
                    res["tools"].add(t)
            for m in re.finditer(r"(?:Vivado|Vitis|Vitis_HLS|SDK|Xilinx|Apps|HLS)[/_ ]?(20\d\d\.\d(?:\.\d)?)|(?:ISE|14\.7)\b", txt, re.I):
                res["versions"].add(m.group(1) or "14.7")
            for m in re.finditer(r"(?:docker\s+run[^\n]*?\s|FROM\s+|image:\s*)([a-z0-9][\w./-]*:[\w.-]+)", txt):
                res["docker"].add(m.group(1))
            if n >= budget_files:
                break
        if n >= budget_files:
            break
    res["files"] = n
    return {k: sorted(v) if isinstance(v, set) else v for k, v in res.items()}


# ---------------------------------------------------------------- the whole picture
def scan(outdated=True, ldd=True, budget_s=900):
    t0 = time.time()
    det = detect.load_state() or detect.detect_all()
    home = str(config.HOME)
    data = {"time": time.time(), "updates": [], "venvs": {}, "projects": {}, "tools": {}, "docker": [], "libs": {},
            "reboot_required": os.path.exists("/var/run/reboot-required"), "restart_needed": []}
    data["updates"] = apt_updates() + snap_updates() + flatpak_updates()
    # Python environments
    venvs = [v["path"] for v in det.get("venvs", [])]
    for p in det.get("projects", []):
        for lv in glob.glob(os.path.join(p, "*/pyvenv.cfg")) + glob.glob(os.path.join(p, ".venv/pyvenv.cfg")):
            venvs.append(os.path.dirname(lv))
    for v in sorted(set(venvs)):
        if time.time() - t0 > budget_s:
            break
        info = venv_info(v, outdated)
        info["eol"] = python_eol(info.get("python"))
        if ldd and not info["broken"]:
            nl = native_libs(v)
            info["native"] = {k: sorted(s) for k, s in nl.items()}
        data["venvs"][v] = info
    # system Python
    import sys
    data["system_python"] = {"version": sys.version.split()[0], "eol": python_eol(sys.version.split()[0])}
    # shared libraries -> apt packages
    all_libs = {lib for v in data["venvs"].values() for libs in (v.get("native") or {}).values() for lib in libs}
    owners = dpkg_owners(all_libs) if all_libs and shutil.which("dpkg") else {}
    for vpath, v in data["venvs"].items():
        top_to_dist = {t: name for name, d in v["packages"].items() for t in d["top"]}
        for top, libs in (v.get("native") or {}).items():
            dist = top_to_dist.get(top, top)
            for lib in libs:
                pkg = owners.get(lib)
                if pkg:
                    data["libs"].setdefault(pkg, []).append({"venv": vpath, "package": dist, "lib": lib})
    # toolchains and command-line tools
    for label, path in det.get("tools", []):
        data["tools"][label] = {"path": path, "version": (re.search(r"(\d{4}\.\d(?:\.\d)?|\d+\.\d+)", label) or [None])[0]
                                if re.search(r"\d", label) else None}
    path_tools = {}
    for t in TOOLS:
        w = shutil.which(t)
        if w:
            path_tools[t] = os.path.realpath(w)
    data["path_tools"] = path_tools
    # Docker images
    if shutil.which("docker"):
        for line in _run(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}\t{{.CreatedSince}}\t{{.Size}}"], 30).splitlines():
            p = line.split("\t")
            if len(p) == 3 and not p[0].startswith("<none>"):
                data["docker"].append({"image": p[0], "created": p[1], "size": p[2]})
    # projects
    venv_paths = list(data["venvs"])
    for proj in det.get("projects", []):
        if time.time() - t0 > budget_s:
            break
        s = scan_project(proj)
        uses_venvs = set(s["local_venvs"])
        for ref in s["venv_refs"]:
            r = os.path.expanduser(ref.replace("~", home, 1) if ref.startswith("~") else ref)
            for vp in venv_paths:
                if r == vp or r.startswith(vp + "/") or vp.startswith(r.rstrip("/") + "/") and os.path.basename(vp) in r:
                    uses_venvs.add(vp)
        imports = [m for m in s["imports"] if m not in _stdlib()]
        pkgs = {}
        for vp in uses_venvs or []:
            v = data["venvs"].get(vp) or {}
            for name, d in (v.get("packages") or {}).items():
                if set(d["top"]) & set(imports) or name.lower() in s["requirements"]:
                    pkgs[name] = {"version": d["version"], "venv": vp}
        tool_versions = {}
        for t in s["tools"]:
            label = None
            for lab, info in data["tools"].items():
                if lab.split("_")[0] in t.replace("-", "_") or t in lab:
                    if info.get("version") and info["version"] in s["versions"]:
                        label = lab
            if not label and t in path_tools:
                label = next((lab for lab, info in data["tools"].items() if path_tools[t].startswith(info["path"].rstrip("/") + "/")),
                             None) or path_tools[t]
            tool_versions[t] = label or "(not installed)"
        data["projects"][proj] = {"venvs": sorted(uses_venvs), "imports": imports, "requirements": s["requirements"],
                                  "packages": pkgs, "tools": tool_versions, "versions": s["versions"],
                                  "docker": [d for d in s["docker"]], "files_scanned": s["files"],
                                  "missing_imports": sorted(m for m in imports if uses_venvs and not any(
                                      m in d["top"] for vp in uses_venvs for d in (data["venvs"].get(vp) or {}).get("packages", {}).values())
                                      and m not in [os.path.basename(x) for x in glob.glob(os.path.join(proj, "*"))])[:30]}
    # who uses what recently (queue runs)
    try:
        from . import queue
        runs = {}
        for j in queue.all_jobs()[-2000:]:
            exe = (j.get("cmd") or "").split()[0] if j.get("cmd") else ""
            runs.setdefault(os.path.basename(exe), set()).add(j["cwd"])
        data["recent_runs"] = {k: sorted(v)[:20] for k, v in runs.items() if k}
    except Exception:
        data["recent_runs"] = {}
    # processes still using replaced libraries
    _, deleted = maps_in_use()
    data["restart_needed"] = sorted({f"{comm} (pid {pid})" for s in deleted.values() for pid, comm in s})[:100]
    data["duration_s"] = round(time.time() - t0, 1)
    CACHE.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(data, indent=1, default=list))
    return data


def load():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- analysis
def users_of_venv(data, vp):
    return sorted(p for p, d in data["projects"].items() if vp in d["venvs"])


def users_of_package(data, name):
    """Projects that import a Python package (in any environment)."""
    out = []
    for p, d in data["projects"].items():
        if name in d["packages"]:
            out.append(f"{_short(p)} ({_short(d['packages'][name]['venv'])})")
    return out


def _short(p):
    return str(p).replace(str(config.HOME), "~")


def _major(v):
    m = re.match(r"(\d+)", str(v or ""))
    return int(m.group(1)) if m else None


def advise(data):
    """Rows: kind, name, old, new, used_by, advice, level(ok|wait|test|reboot)."""
    rows = []
    in_use, _ = maps_in_use()
    try:
        from . import watcher
        busy = bool(watcher.jobs())
    except Exception:
        busy = False
    for u in data["updates"]:
        used = []
        if u["kind"] == "apt":
            for r in data["libs"].get(u["name"], [])[:6]:
                used.append(f"{r['package']} in {_short(r['venv'])}")
            procs = set()
            for f in package_files(u["name"])[:200] if (u["reboot"] or u["name"].startswith("lib")) else []:
                for pid, comm in in_use.get(f, ()):
                    procs.add(comm)
            if procs:
                used.append("running now: " + ", ".join(sorted(procs)[:6]))
        if u.get("reboot"):
            adv, lvl = "needs a reboot afterwards — do it when no runs are active", "reboot"
        elif any(x.startswith("running now") for x in used) and busy:
            adv, lvl = "in use by running programs — update after the runs finish, then restart them", "wait"
        elif used:
            adv, lvl = "update, then re-test the Python packages listed", "test"
        else:
            adv, lvl = "safe to update now" + (" (security fix)" if u.get("security") else ""), "ok"
        rows.append({"kind": u["kind"] + (" security" if u.get("security") else ""), "name": u["name"], "old": u.get("old", ""),
                     "new": u.get("new", ""), "used_by": "; ".join(used) or "—", "advice": adv, "level": lvl})
    for vp, v in data["venvs"].items():
        for o in v.get("outdated") or []:
            users = [_short(p) for p, d in data["projects"].items() if o["name"] in d["packages"] and d["packages"][o["name"]]["venv"] == vp]
            major = _major(o.get("latest_version")) != _major(o.get("version"))
            if not users:
                adv, lvl = "no project imports it here — safe to update (or remove)", "ok"
            elif major:
                adv, lvl = "MAJOR version change — may break: update in a copy of the environment and test the projects first", "test"
            else:
                adv, lvl = "minor update — update, then run the projects' tests", "test"
            rows.append({"kind": f"pip ({_short(vp)})", "name": o["name"], "old": o.get("version", ""),
                         "new": o.get("latest_version", ""), "used_by": ", ".join(users) or "—", "advice": adv, "level": lvl})
        if v.get("eol"):
            rows.append({"kind": "python", "name": f"Python {v.get('python')} in {_short(vp)}", "old": v.get("python"),
                         "new": "newer Python", "used_by": ", ".join(_short(p) for p in users_of_venv(data, vp)) or "—",
                         "advice": f"no security fixes since {v['eol']} — recreate the environment with a current Python", "level": "test"})
        if v.get("broken"):
            rows.append({"kind": "python", "name": f"broken environment {_short(vp)}", "old": "", "new": "",
                         "used_by": ", ".join(_short(p) for p in users_of_venv(data, vp)) or "—",
                         "advice": "its Python interpreter is gone (system upgrade?) — recreate it", "level": "test"})
    for p in data.get("restart_needed") or []:
        rows.append({"kind": "restart", "name": p, "old": "", "new": "", "used_by": "—",
                     "advice": "still uses a library that an update replaced — restart it when convenient", "level": "wait"})
    order = {"reboot": 0, "wait": 1, "test": 2, "ok": 3}
    return sorted(rows, key=lambda r: (order[r["level"]], r["kind"], r["name"]))


def uses(data, name):
    """Answer 'who uses NAME?' for a project, venv, Python package, apt package, tool, toolchain or Docker image."""
    n = name.lower()
    out = []
    for p, d in data["projects"].items():
        hit = []
        for pk, info in d["packages"].items():
            if pk.lower() == n:
                hit.append(f"imports {pk} {info['version']} from {_short(info['venv'])}")
        for t, lab in d["tools"].items():
            if n in (t.lower(), str(lab).lower()) or n in str(lab).lower():
                hit.append(f"uses {t} → {_short(lab)}")
        for vp in d["venvs"]:
            if n in vp.lower():
                hit.append(f"uses environment {_short(vp)}")
        for img in d["docker"]:
            if n in img.lower():
                hit.append(f"uses docker image {img}")
        if hit:
            out.append(f"{_short(p)}: " + "; ".join(hit))
    for r in data["libs"].get(name, []):
        out.append(f"{r['package']} in {_short(r['venv'])} links {r['lib']}")
    for vp, v in data["venvs"].items():
        for pk, info in v["packages"].items():
            if pk.lower() == n:
                out.append(f"installed in {_short(vp)}: {pk} {info['version']}")
            elif any(re.match(r"^" + re.escape(n) + r"\b", r_.lower()) for r_ in info.get("requires") or []):
                out.append(f"{pk} (in {_short(vp)}) depends on {name}")
    for k, v in (data.get("recent_runs") or {}).items():
        if k.lower() == n:
            out.append(f"queued runs of {k} in: " + ", ".join(_short(x) for x in v[:8]))
    return out


def project_view(data, key):
    match = [p for p in data["projects"] if key in (p, _short(p), os.path.basename(p))]
    if not match:
        return None
    p = match[0]
    return p, data["projects"][p]


def summary_text(data):
    rows = advise(data)
    apt = [u for u in data["updates"] if u["kind"] == "apt"]
    sec = [u for u in apt if u.get("security")]
    pip = sum(len(v.get("outdated") or []) for v in data["venvs"].values())
    eol = [vp for vp, v in data["venvs"].items() if v.get("eol")]
    L = [f"Software — scanned {time.strftime('%F %H:%M', time.localtime(data['time']))} ({data.get('duration_s', '?')} s)",
         f"  apt updates      {len(apt)} ({len(sec)} security)" + ("  · reboot pending" if data.get("reboot_required") else ""),
         f"  snap / flatpak   {sum(1 for u in data['updates'] if u['kind'] == 'snap')} / {sum(1 for u in data['updates'] if u['kind'] == 'flatpak')}",
         f"  pip (outdated)   {pip} in {sum(1 for v in data['venvs'].values() if v.get('outdated'))} environment(s)",
         f"  Python EOL       {len(eol)} environment(s)" + (f": {', '.join(_short(x) for x in eol)}" if eol else ""),
         f"  restart needed   {len(data.get('restart_needed') or [])} program(s) use replaced libraries",
         f"  projects mapped  {len(data['projects'])} · environments {len(data['venvs'])} · toolchains {len(data['tools'])}", ""]
    groups = {"reboot": "Needs a reboot", "wait": "Wait until runs finish", "test": "Update, then test the users listed",
              "ok": "Safe to update now"}
    for lvl, title in groups.items():
        rs = [r for r in rows if r["level"] == lvl]
        if not rs:
            continue
        L.append(f"{title} ({len(rs)})")
        for r in rs[:40]:
            L.append(f"  {r['kind']:<22} {r['name'][:34]:<34} {str(r['old'])[:14]:>14} → {str(r['new'])[:14]:<14}  used by: {r['used_by'][:70]}")
        if len(rs) > 40:
            L.append(f"  … {len(rs) - 40} more (ms software updates)")
        L.append("")
    return "\n".join(L)


def plan_text(data):
    rows = advise(data)
    L = ["Suggested update plan (nothing is run for you):", ""]
    ok = [r for r in rows if r["level"] == "ok" and r["kind"].startswith("apt")]
    if ok:
        L.append("1. Safe now (not used by your environments or running programs):")
        L.append("   sudo apt install --only-upgrade " + " ".join(r["name"] for r in ok[:60]))
    t = [r for r in rows if r["level"] == "test" and r["kind"].startswith("apt")]
    if t:
        L.append("2. Libraries used by Python packages — update, then re-run those projects' tests:")
        for r in t[:30]:
            L.append(f"   {r['name']}: {r['used_by']}")
    w = [r for r in rows if r["level"] in ("wait", "reboot")]
    if w:
        L.append("3. After the current runs finish (ms capacity shows idle):")
        L.append("   sudo apt upgrade && sudo reboot      # kernel / libc / systemd and libraries in use")
    pipr = [r for r in rows if r["kind"].startswith("pip")]
    if pipr:
        L.append("4. Python packages, per environment (copy the environment first if a MAJOR version changes):")
        byenv = {}
        for r in pipr:
            byenv.setdefault(r["kind"][5:-1], []).append(r)
        for env, rs in byenv.items():
            safe = [r["name"] for r in rs if "safe" in r["advice"]]
            risky = [f"{r['name']} ({r['used_by']})" for r in rs if "safe" not in r["advice"]]
            if safe:
                L.append(f"   {env}/bin/pip install -U " + " ".join(safe[:40]))
            if risky:
                L.append(f"   test first in {env}: " + ", ".join(risky[:20]))
    eol = [r for r in rows if r["kind"] == "python"]
    if eol:
        L.append("5. Environments to recreate:")
        for r in eol:
            L.append(f"   {r['name']} — used by {r['used_by']}")
    if len(L) == 2:
        L.append("Everything is up to date.")
    return "\n".join(L)


def write_report(day_dir=None):
    data = load()
    if not data:
        return None
    d = Path(day_dir) if day_dir else config.REPORTS / time.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    txt = summary_text(data) + "\n\n" + plan_text(data) + "\n\nWho uses what (projects):\n"
    for p, info in sorted(data["projects"].items()):
        txt += f"\n{_short(p)}\n"
        if info["venvs"]:
            txt += "  environments: " + ", ".join(_short(v) for v in info["venvs"]) + "\n"
        if info["packages"]:
            txt += "  packages:     " + ", ".join(f"{k} {v['version']}" for k, v in sorted(info["packages"].items())) + "\n"
        if info["tools"]:
            txt += "  tools:        " + ", ".join(f"{k} → {_short(v)}" for k, v in sorted(info["tools"].items())) + "\n"
        if info["docker"]:
            txt += "  docker:       " + ", ".join(info["docker"]) + "\n"
        if info.get("missing_imports"):
            txt += "  not found in its environment: " + ", ".join(info["missing_imports"]) + "\n"
    (d / "software.txt").write_text(txt)
    return d / "software.txt"


HELP = """ms software — updates and who uses what (read-only)

  ms software                  summary: updates grouped by advice (safe / test / wait / reboot)
  ms software scan [--no-pip] [--no-ldd]   rebuild the picture (also runs daily)
  ms software updates          every update with who uses it and what to do
  ms software plan             suggested update order with the commands to run yourself
  ms software uses NAME        who uses a project / environment / package / library / tool / image
  ms software project NAME     what one project uses (environments, packages, tools, images)
"""


def cli(a):
    if a and a[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0
    if a and a[0] == "scan":
        d = scan(outdated="--no-pip" not in a, ldd="--no-ldd" not in a)
        write_report()
        print(summary_text(d))
        return 0
    data = load()
    if not data:
        print("no scan yet — running one now (a few minutes)…")
        data = scan()
        write_report()
    if not a:
        print(summary_text(data))
        return 0
    sub = a[0]
    if sub == "updates":
        for r in advise(data):
            print(f"[{r['level']:<6}] {r['kind']:<22} {r['name'][:36]:<36} {str(r['old'])[:14]:>14} → {str(r['new'])[:14]:<14}\n"
                  f"          used by: {r['used_by']}\n          {r['advice']}")
        return 0
    if sub == "plan":
        print(plan_text(data))
        return 0
    if sub == "uses" and len(a) > 1:
        res = uses(data, a[1])
        print("\n".join(res) if res else f"nothing found that uses {a[1]}")
        return 0
    if sub == "project" and len(a) > 1:
        r = project_view(data, a[1])
        if not r:
            print("unknown project (names: " + ", ".join(os.path.basename(p) for p in data["projects"]) + ")")
            return 1
        p, d = r
        print(_short(p))
        print("  environments: " + (", ".join(_short(v) for v in d["venvs"]) or "none found"))
        for k, v in sorted(d["packages"].items()):
            print(f"  package  {k:<28} {v['version']:<12} ({_short(v['venv'])})")
        for k, v in sorted(d["tools"].items()):
            print(f"  tool     {k:<28} {_short(v)}")
        for img in d["docker"]:
            print(f"  docker   {img}")
        if d.get("missing_imports"):
            print("  imports not found in its environment: " + ", ".join(d["missing_imports"]))
        return 0
    print(HELP)
    return 2
