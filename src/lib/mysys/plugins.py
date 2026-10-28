"""Run-tracking plugins.

A plugin is a small Python file in ~/My_System/plugins/<name>.py, enabled by listing its
name in config/plugins.conf. It may define:

    MATCH      compiled regex tested against the program path (or the script name of a
               bash/python wrapper). Group 1 is used as the tool name.      (required)
    LOGS       dict tool -> list of log file names looked for in the run folder
    def log_summary(cwd, tool) -> {"errors": int, "first_error": str, "critical": int, "log": path} | None
    def results(cwd, tool)     -> {column: value} | None   (columns of reports/results.csv)
    CLEANABLE  list of glob patterns of regenerable data (shown in the Storage tab)
"""
import importlib.util
import re
from pathlib import Path

from . import config

RESULT_COLUMNS = ["time", "tool", "batch", "run", "folder", "status", "seconds",
                  "wns_ns", "tns_ns", "whs_ns", "fmax_mhz", "clock_ns", "latency_cycles",
                  "lut", "ff", "bram", "uram", "dsp", "power_w", "errors", "critical"]

_cache = None


def load():
    global _cache
    if _cache is not None:
        return _cache
    enabled = config.lst("plugins.conf") or ["vivado", "vitis_hls", "yosys", "simulators"]
    mods = []
    for name in enabled:
        f = config.PLUGINS / f"{name}.py"
        if not f.exists():
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"ms_plugin_{name}", f)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            if isinstance(getattr(m, "MATCH", None), re.Pattern):
                m.NAME = name
                mods.append(m)
        except Exception:          # a broken plugin must never stop the watcher
            continue
    _cache = mods
    return mods


def match(argv):
    """(plugin, tool) for a process argv, or (None, None)."""
    if not argv:
        return None, None
    exe = argv[0]
    if Path(exe).name in ("sh", "bash", "dash", "zsh", "python", "python3", "perl", "tclsh") and len(argv) > 1:
        cand = [a for a in argv[1:] if not a.startswith("-")]
        exe = cand[0] if cand else exe
    for p in load():
        m = p.MATCH.search(exe)
        if m:
            return p, m.group(1)
    return None, None


def count_log(path, error_rx=r"^ERROR", crit_rx=r"^CRITICAL WARNING", tail=6000):
    try:
        lines = Path(path).read_text(errors="replace").splitlines()[-tail:]
    except OSError:
        return None
    er, cr = re.compile(error_rx), re.compile(crit_rx)
    errs = [l for l in lines if er.search(l)]
    return {"errors": len(errs), "first_error": errs[0][:200] if errs else "",
            "critical": sum(1 for l in lines if cr.search(l)), "log": str(path)}


def find_log(cwd, names):
    for n in names:
        f = Path(cwd) / n
        if f.exists():
            return f
    return None


def newest(cwd, pattern, depth=4):
    """Newest file matching pattern below cwd (bounded depth, skips huge trees fast)."""
    base = Path(cwd)
    best, best_m = None, 0
    for d in range(depth + 1):
        for f in base.glob("/".join(["*"] * d + [pattern])):
            try:
                m = f.stat().st_mtime
            except OSError:
                continue
            if m > best_m:
                best, best_m = f, m
    return best
