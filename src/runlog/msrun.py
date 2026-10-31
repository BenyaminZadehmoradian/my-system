"""msrun — write a my-system run log (msrun.jsonl, format v1). Standard library only.

Copy this file into a project (`ms log template python > msrun.py`) or import it from
~/My_System/runlog. Spec: ~/My_System/runlog/RUNLOG_SPEC.md

    from msrun import Run
    with Run("s18_doe3", params={"seed": 3}) as r:
        r.stage("synth")
        r.metric("lut", 41233)
        r.log("WARN", "clock uncertainty high")
        r.artifact("reports/timing.rpt", "report")
    # end ok is written on normal exit, end failed (with the error) on an exception,
    # end cancelled on Ctrl-C.
"""
import datetime as _dt
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import traceback

VERSION = 1
FILENAME = "msrun.jsonl"
LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")
KINDS = ("report", "bitstream", "checkpoint", "netlist", "plot", "data", "log", "other")


def _now():
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _git(cwd):
    def g(*a):
        try:
            r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, timeout=5)
            return r.stdout.strip() if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None
    commit = g("rev-parse", "--short", "HEAD")
    if not commit:
        return None
    return {"commit": commit, "branch": g("rev-parse", "--abbrev-ref", "HEAD"), "dirty": bool(g("status", "--porcelain"))}


def _project(cwd):
    top = None
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True, timeout=5)
        top = r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        pass
    return os.path.basename(top or cwd)


def _register(path, run_id):
    """Tell my-system where this run log is (best effort)."""
    base = os.environ.get("MY_SYSTEM") or os.path.expanduser("~/My_System")
    rep = os.path.join(base, "reports")
    if os.path.isdir(rep):
        try:
            with open(os.path.join(rep, "runlog_index.tsv"), "a") as f:
                f.write(f"{os.path.abspath(path)}\t{run_id}\t{int(time.time())}\n")
        except OSError:
            pass


class Run:
    def __init__(self, name, params=None, folder=None, project=None, cmd=None, echo=False):
        if not re.match(r"^[A-Za-z0-9_.+-]+$", name or ""):
            raise ValueError("run name: letters, digits and _ . + - only")
        self.name = name
        self.folder = os.path.abspath(folder or os.getcwd())
        os.makedirs(self.folder, exist_ok=True)
        self.path = os.path.join(self.folder, FILENAME)
        self.run = f"{name}-{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(2)}"
        self.params = params or {}
        self.project = project
        self.cmd = cmd or " ".join(sys.argv)
        self.echo = echo
        self._t0 = None
        self._stage = None
        self._ended = False
        self._f = None

    # -- low level
    def _w(self, ev, **kw):
        if self._f is None:
            raise RuntimeError("run not started")
        d = {"v": VERSION, "ts": _now(), "run": self.run, "ev": ev}
        d.update({k: v for k, v in kw.items() if v is not None})
        self._f.write(json.dumps(d, ensure_ascii=False, default=str) + "\n")
        self._f.flush()
        if self.echo:
            print(f"[msrun] {ev} " + " ".join(f"{k}={v}" for k, v in kw.items() if v is not None), file=sys.stderr)

    # -- API
    def start(self):
        self._f = open(self.path, "a", buffering=1, encoding="utf-8")
        self._t0 = time.time()
        self._w("start", name=self.name, project=self.project or _project(self.folder), cmd=self.cmd[:500],
                cwd=self.folder, host=socket.gethostname(), pid=os.getpid(), params=self.params, git=_git(self.folder))
        _register(self.path, self.run)
        return self

    def stage(self, name, status="ok"):
        """Begin stage `name`; ends the current stage (with `status`) first."""
        self.end_stage(status)
        self._stage = name
        self._w("stage", stage=name, state="begin")

    def end_stage(self, status="ok"):
        if self._stage:
            self._w("stage", stage=self._stage, state="end", status=status)
            self._stage = None

    def log(self, level, msg, src=None):
        level = level.upper()
        if level not in LEVELS:
            raise ValueError(f"level must be one of {LEVELS}")
        self._w("log", level=level, msg=str(msg), stage=self._stage, src=src)

    def info(self, msg):
        self.log("INFO", msg)

    def warn(self, msg):
        self.log("WARN", msg)

    def error(self, msg):
        self.log("ERROR", msg)

    def metric(self, name, value, unit=None):
        if not re.match(r"^[a-z][a-z0-9_]*$", name):
            raise ValueError("metric name: snake_case, e.g. wns_ns, lut, power_w")
        self._w("metric", name=name, value=value, unit=unit, stage=self._stage)

    def artifact(self, path, kind="other"):
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        p = os.path.relpath(os.path.abspath(path), self.folder) if os.path.isabs(path) else path
        self._w("artifact", path=p, kind=kind, stage=self._stage)

    def progress(self, pct, msg=None):
        self._w("progress", pct=max(0, min(100, float(pct))), msg=msg, stage=self._stage)

    def end(self, status="ok", rc=None, error=None):
        if self._ended:
            return
        if status not in ("ok", "failed", "cancelled"):
            raise ValueError("status: ok | failed | cancelled")
        self.end_stage("ok" if status == "ok" else "failed")
        if status == "failed" and not error:
            error = "failed"
        self._w("end", status=status, rc=rc if rc is not None else (0 if status == "ok" else 1),
                seconds=round(time.time() - self._t0, 1), error=error)
        self._ended = True
        self._f.close()

    # -- context manager
    def __enter__(self):
        return self.start()

    def __exit__(self, et, ev, tb):
        if et is None:
            self.end("ok")
        elif issubclass(et, KeyboardInterrupt):
            self.end("cancelled", rc=130, error="interrupted")
        else:
            last = traceback.extract_tb(tb)[-1] if tb else None
            where = f"{os.path.basename(last.filename)}:{last.lineno}" if last else None
            self.log("ERROR", f"{et.__name__}: {ev}", src=where)
            self.end("failed", rc=1, error=f"{et.__name__}: {ev}")
        return False
