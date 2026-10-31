"""Read, validate, find and summarise my-system run logs (msrun.jsonl, format v1).

The format is defined in runlog/RUNLOG_SPEC.md. Writers: runlog/msrun.py, runlog/msrun.tcl, `ms log`.
"""
import datetime as dt
import json
import os
import re
import socket
import time
from pathlib import Path

from . import config

FILENAME = "msrun.jsonl"
INDEX = config.REPORTS / "runlog_index.tsv"
PATHS = config.STATE / "runlog_paths.txt"
EVENTS = {
    "start": ({"name"}, {"project", "cmd", "cwd", "host", "pid", "params", "git"}),
    "stage": ({"stage", "state"}, {"status"}),
    "log": ({"level", "msg"}, {"stage", "src"}),
    "metric": ({"name", "value"}, {"unit", "stage"}),
    "artifact": ({"path", "kind"}, {"stage"}),
    "progress": ({"pct"}, {"msg", "stage"}),
    "end": ({"status", "seconds"}, {"rc", "error"}),
}
BASE = {"v", "ts", "run", "ev"}
LEVELS = {"DEBUG", "INFO", "WARN", "ERROR"}
KINDS = {"report", "bitstream", "checkpoint", "netlist", "plot", "data", "log", "other"}
SKIP_DIRS = {".git", "node_modules", ".Xil", "__pycache__", ".venv", "venv", ".cache", ".hg", ".svn", ".ipynb_checkpoints"}


def parse_ts(s):
    try:
        return dt.datetime.fromisoformat(s).timestamp()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- validate
def check_line(d):
    """Problems with one decoded line (list of strings)."""
    p = []
    if not isinstance(d, dict):
        return ["not a JSON object"]
    missing = BASE - d.keys()
    if missing:
        p.append("missing " + ", ".join(sorted(missing)))
    if d.get("v") != 1:
        p.append(f"v must be 1 (got {d.get('v')!r})")
    if "ts" in d and parse_ts(d["ts"]) is None:
        p.append(f"ts is not ISO-8601: {d['ts']!r}")
    elif "ts" in d and not re.search(r"([+-]\d\d:\d\d|Z)$", d["ts"]):
        p.append("ts has no UTC offset")
    ev = d.get("ev")
    if ev not in EVENTS:
        p.append(f"unknown ev {ev!r}")
        return p
    req, opt = EVENTS[ev]
    if req - d.keys():
        p.append(f"{ev}: missing " + ", ".join(sorted(req - d.keys())))
    extra = {k for k in d.keys() - BASE - req - opt if not k.startswith("x_")}
    if extra:
        p.append(f"{ev}: unknown keys " + ", ".join(sorted(extra)) + " (custom keys must start with x_)")
    if ev == "stage" and d.get("state") not in ("begin", "end"):
        p.append("stage.state must be begin or end")
    if ev == "stage" and d.get("status") not in (None, "ok", "failed", "skipped"):
        p.append("stage.status must be ok, failed or skipped")
    if ev == "log" and d.get("level") not in LEVELS:
        p.append("log.level must be DEBUG, INFO, WARN or ERROR")
    if ev == "metric":
        if not re.match(r"^[a-z][a-z0-9_]*$", str(d.get("name", ""))):
            p.append(f"metric.name must be snake_case: {d.get('name')!r}")
        if not isinstance(d.get("value"), (int, float, str, bool)) or d.get("value") is None:
            p.append("metric.value must be a number, string or bool")
    if ev == "artifact":
        if d.get("kind") not in KINDS:
            p.append(f"artifact.kind must be one of {', '.join(sorted(KINDS))}")
        if str(d.get("path", "")).startswith("/"):
            p.append("artifact.path must be relative to the run folder")
    if ev == "progress" and not (isinstance(d.get("pct"), (int, float)) and 0 <= d["pct"] <= 100):
        p.append("progress.pct must be 0..100")
    if ev == "end":
        if d.get("status") not in ("ok", "failed", "cancelled"):
            p.append("end.status must be ok, failed or cancelled")
        if d.get("status") == "failed" and not d.get("error"):
            p.append("end.error is required when status is failed")
        if not isinstance(d.get("seconds"), (int, float)):
            p.append("end.seconds must be a number")
    if ev == "start" and "params" in d and not isinstance(d["params"], dict):
        p.append("start.params must be an object")
    return p


def validate(path):
    """[(line_no, problem)] for one msrun.jsonl; empty list = valid."""
    probs = []
    runs = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as e:
        return [(0, f"cannot read: {e}")]
    for i, line in enumerate(lines, 1):
        if not line.strip():
            probs.append((i, "empty line"))
            continue
        try:
            d = json.loads(line)
        except ValueError as e:
            probs.append((i, f"not valid JSON ({e.msg})"))
            continue
        for x in check_line(d):
            probs.append((i, x))
        rid = d.get("run") if isinstance(d, dict) else None
        if not rid:
            continue
        st = runs.setdefault(rid, {"start": 0, "end": 0, "last_ts": None, "first_ev": d.get("ev"), "ended_at": None})
        ev = d.get("ev")
        if ev == "start":
            st["start"] += 1
            if st["start"] > 1:
                probs.append((i, f"run {rid}: second start"))
        elif st["start"] == 0 and st["first_ev"] != "start":
            probs.append((i, f"run {rid}: event before start"))
            st["first_ev"] = "start"          # report once
        if st["ended_at"] is not None and ev != "end":
            probs.append((i, f"run {rid}: event after end"))
        if ev == "end":
            st["end"] += 1
            st["ended_at"] = i
            if st["end"] > 1:
                probs.append((i, f"run {rid}: second end"))
        t = parse_ts(d.get("ts"))
        if t is not None:
            if st["last_ts"] is not None and t < st["last_ts"] - 1:
                probs.append((i, f"run {rid}: ts goes backwards"))
            st["last_ts"] = t
    return probs


# ---------------------------------------------------------------- read
def _alive(pid, host):
    if not pid or (host and host != socket.gethostname()):
        return None
    return os.path.exists(f"/proc/{pid}")


def read(path):
    """{run_id: summary} for one file (invalid lines are skipped)."""
    runs = {}
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return {}
    with f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict) or "run" not in d:
                continue
            r = runs.setdefault(d["run"], {"run": d["run"], "file": str(path), "folder": str(Path(path).parent),
                                           "name": "", "project": "", "params": {}, "metrics": {}, "units": {},
                                           "stages": [], "artifacts": [], "errors": 0, "warnings": 0,
                                           "first_error": "", "status": "running", "start_ts": None, "end_ts": None,
                                           "seconds": None, "progress": None, "events": 0, "pid": None, "host": "",
                                           "cmd": "", "git": None, "last_ts": None})
            r["events"] += 1
            t = parse_ts(d.get("ts"))
            if t:
                r["last_ts"] = t
            ev = d.get("ev")
            if ev == "start":
                r.update(name=d.get("name", ""), project=d.get("project", ""), params=d.get("params") or {},
                         start_ts=t, pid=d.get("pid"), host=d.get("host", ""), cmd=d.get("cmd", ""), git=d.get("git"))
            elif ev == "stage":
                if d.get("state") == "begin":
                    r["stages"].append({"stage": d.get("stage"), "begin": t, "end": None, "status": "running"})
                else:
                    s = next((s for s in reversed(r["stages"]) if s["stage"] == d.get("stage") and s["end"] is None), None)
                    if s:
                        s["end"], s["status"] = t, d.get("status", "ok")
            elif ev == "log":
                if d.get("level") == "ERROR":
                    r["errors"] += 1
                    r["first_error"] = r["first_error"] or str(d.get("msg", ""))[:300]
                elif d.get("level") == "WARN":
                    r["warnings"] += 1
            elif ev == "metric":
                r["metrics"][d.get("name")] = d.get("value")
                if d.get("unit"):
                    r["units"][d.get("name")] = d["unit"]
            elif ev == "artifact":
                r["artifacts"].append((d.get("path"), d.get("kind")))
            elif ev == "progress":
                r["progress"] = d.get("pct")
            elif ev == "end":
                r.update(status=d.get("status", "?"), end_ts=t, seconds=d.get("seconds"))
                if d.get("error") and not r["first_error"]:
                    r["first_error"] = str(d["error"])[:300]
    for r in runs.values():
        if r["status"] == "running":
            alive = _alive(r["pid"], r["host"])
            if alive is False or (alive is None and r["last_ts"] and time.time() - r["last_ts"] > 48 * 3600):
                r["status"] = "died"
            r["seconds"] = (time.time() if r["status"] == "running" else (r["last_ts"] or time.time())) - (r["start_ts"] or time.time())
    return runs


def events(path, run_id):
    out = []
    try:
        for line in open(path, encoding="utf-8", errors="replace"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("run") == run_id:
                out.append(d)
    except OSError:
        pass
    return out


# ---------------------------------------------------------------- find
def scan_roots():
    from . import detect
    roots = set(detect.load_state().get("projects", []))
    for k in ("BIG_FOLDER_DEST",):
        v = config.get(k)
        if v:
            roots.add(os.path.expanduser(v))
    roots |= {os.path.expanduser(x) for x in config.lst("runlog_roots.txt")}
    return sorted(r for r in roots if os.path.isdir(r))


def scan(max_depth=7, budget_s=600):
    """Find msrun.jsonl files below project folders and the big-data folder; writes state/runlog_paths.txt."""
    found = set()
    t0 = time.time()
    for root in scan_roots():
        base = root.rstrip("/").count("/")
        for dp, dns, fns in os.walk(root, followlinks=False):
            if FILENAME in fns:
                found.add(os.path.join(dp, FILENAME))
            if dp.count("/") - base >= max_depth or time.time() - t0 > budget_s:
                dns[:] = []
            else:
                dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
    found |= set(indexed())
    try:
        PATHS.write_text("\n".join(sorted(found)) + "\n")
    except OSError:
        pass
    return sorted(found)


def indexed():
    out = []
    try:
        for line in INDEX.read_text().splitlines():
            p = line.split("\t")[0]
            if p and p not in out:
                out.append(p)
    except OSError:
        pass
    return out


def known_paths():
    paths = set(indexed())
    try:
        paths |= {p for p in PATHS.read_text().splitlines() if p}
    except OSError:
        pass
    return sorted(p for p in paths if os.path.exists(p))


def all_runs(limit=500):
    out = []
    for p in known_paths():
        out += read(p).values()
    out.sort(key=lambda r: r.get("start_ts") or 0, reverse=True)
    return out[:limit]


def fmt_seconds(s):
    if s is None:
        return "–"
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def timeline(path, run_id):
    """Readable text timeline of one run."""
    L = []
    for d in events(path, run_id):
        t = (d.get("ts") or "")[11:19]
        ev = d.get("ev")
        if ev == "start":
            L.append(f"{t}  START  {d.get('name')}  ({d.get('project', '')})  params={json.dumps(d.get('params', {}))}")
        elif ev == "stage":
            L.append(f"{t}  {'▶' if d.get('state') == 'begin' else '■'} {d.get('stage')}" + (f"  [{d.get('status')}]" if d.get("status") else ""))
        elif ev == "log":
            L.append(f"{t}    {d.get('level'):5} {d.get('msg')}")
        elif ev == "metric":
            L.append(f"{t}    = {d.get('name')} = {d.get('value')}{' ' + d['unit'] if d.get('unit') else ''}")
        elif ev == "artifact":
            L.append(f"{t}    + {d.get('kind')}: {d.get('path')}")
        elif ev == "progress":
            L.append(f"{t}    … {d.get('pct')}% {d.get('msg') or ''}")
        elif ev == "end":
            L.append(f"{t}  END {d.get('status', '').upper()}  rc={d.get('rc')}  {fmt_seconds(d.get('seconds'))}"
                     + (f"  {d.get('error')}" if d.get("error") else ""))
    return "\n".join(L)


# ---------------------------------------------------------------- watcher hook
def new_endings(since):
    """Runs that ended after `since` (epoch), from files modified since then."""
    out = []
    for p in known_paths():
        try:
            if os.path.getmtime(p) < since:
                continue
        except OSError:
            continue
        for r in read(p).values():
            if r["end_ts"] and r["end_ts"] >= since - 1:
                out.append(r)
    return out
