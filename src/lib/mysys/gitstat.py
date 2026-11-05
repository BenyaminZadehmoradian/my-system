"""Git status of every detected project: uncommitted work, unpushed commits, no remote,
and whether the project is covered by the backup."""
import json
import os
import re
import subprocess
import time

from . import config, detect


def _git(path, *args):
    try:
        r = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, errors="replace", timeout=60)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def status(path):
    st = _git(path, "status", "--porcelain=v1", "-b", "--untracked-files=normal")
    lines = st.splitlines()
    head = lines[0] if lines else ""
    m = re.match(r"## (?:No commits yet on )?([^.\s]+)(?:\.\.\.(\S+))?(?: \[(.*)\])?", head)
    branch, upstream, track = (m.group(1), m.group(2), m.group(3) or "") if m else ("?", None, "")
    ahead = int(re.search(r"ahead (\d+)", track).group(1)) if "ahead" in track else 0
    behind = int(re.search(r"behind (\d+)", track).group(1)) if "behind" in track else 0
    changed = sum(1 for l in lines[1:] if not l.startswith("??"))
    untracked = sum(1 for l in lines[1:] if l.startswith("??"))
    last = _git(path, "log", "-1", "--format=%ct").strip()
    remote = bool(_git(path, "remote").strip())
    return {"path": path, "branch": branch, "upstream": upstream, "ahead": ahead, "behind": behind,
            "changed": changed, "untracked": untracked, "has_remote": remote,
            "last_commit": int(last) if last.isdigit() else None}


def covered(path, sources):
    rp = os.path.realpath(path)
    return any(rp == os.path.realpath(s) or rp.startswith(os.path.realpath(s) + "/") for s in sources)


def run():
    projects = (detect.load_state() or {}).get("projects") or detect.git_repos()
    sources = config.lst("backup_sources.txt")
    out = []
    for p in projects:
        s = status(p)
        s["backed_up"] = covered(p, sources)
        out.append(s)
    config.STATE.mkdir(parents=True, exist_ok=True)
    (config.STATE / "git_status.json").write_text(json.dumps(out, indent=1))
    return out


def problems(s):
    p = []
    if s["changed"]:
        p.append(f"{s['changed']} uncommitted change(s)")
    if s["ahead"]:
        p.append(f"{s['ahead']} commit(s) not pushed")
    if not s["has_remote"]:
        p.append("no remote (only on this machine)")
    if not s.get("backed_up"):
        p.append("not in backup")
    if s["last_commit"] and s["changed"] and time.time() - s["last_commit"] > 14 * 86400:
        p.append("uncommitted for 2+ weeks")
    return p
