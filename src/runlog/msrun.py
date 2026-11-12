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
            r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, errors="replace", timeout=5)
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
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True, errors="replace", timeout=5)
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


MARK = re.compile(r"^\s*::ms\s+(\w+)\s*(.*?)\s*$")


def marker(run, line):
    """A line a job prints to tell my-system something, in any language (echo, puts, print):

        ::ms stage synth                       a new stage begins (ends the previous one)
        ::ms metric wns_ns -0.084 [ns]         a result (number, true/false or text)
        ::ms progress 40 [placing]             percent done
        ::ms artifact reports/timing.rpt [report|bitstream|checkpoint|netlist|plot|data|log|other]
        ::ms info|warn|error TEXT              a message with its level

    Returns the kind of event written to the run log, None for an ordinary line, or "bad: why" for a marker line
    that could not be understood (it is then kept as a warning, never an exception)."""
    m = MARK.match(line)
    if not m:
        return None
    kind, rest = m.group(1).lower(), m.group(2)
    try:
        if kind == "stage" and rest:
            run.stage(re.sub(r"[^A-Za-z0-9_.+-]", "_", rest)[:60])
        elif kind == "metric":
            parts = rest.split()
            name = parts[0].replace("=", " ").split()[0] if parts else ""
            vals = rest.replace("=", " ", 1).split()[1:]
            if not name or not vals:
                raise ValueError("use: ::ms metric NAME VALUE [UNIT]")
            v = vals[0]
            try:
                val = int(v) if re.fullmatch(r"[-+]?\d+", v) else float(v)
                if val != val or val in (float("inf"), float("-inf")):
                    val = v
            except ValueError:
                val = {"true": True, "false": False}.get(v.lower(), " ".join(vals) if len(vals) > 2 else v)
            unit = vals[1] if len(vals) == 2 and not isinstance(val, str) else None
            run.metric(re.sub(r"[^a-z0-9_]", "_", name.lower()).lstrip("_0123456789") or "value", val, unit)
        elif kind == "progress":
            pm = re.match(r"(\d+(?:\.\d+)?)\s*%?\s*(.*)$", rest)
            if not pm:
                raise ValueError("use: ::ms progress PERCENT [text]")
            run.progress(float(pm.group(1)), pm.group(2) or None)
        elif kind == "artifact" and rest:
            parts = rest.rsplit(None, 1)
            if len(parts) == 2 and parts[1] in KINDS:
                run.artifact(parts[0], parts[1])
            else:
                ext = os.path.splitext(rest)[1].lower()
                run.artifact(rest, {".rpt": "report", ".bit": "bitstream", ".dcp": "checkpoint", ".png": "plot",
                                    ".pdf": "plot", ".svg": "plot", ".csv": "data", ".json": "data", ".log": "log",
                                    ".v": "netlist", ".edf": "netlist", ".md": "report"}.get(ext, "other"))
        elif kind in ("info", "warn", "warning", "error", "debug") and rest:
            run.log({"warning": "WARN"}.get(kind, kind.upper()), rest[:1000])
        else:
            raise ValueError(f"unknown or empty marker '{kind}' (stage, metric, progress, artifact, info, warn, error)")
    except (ValueError, RuntimeError, IndexError) as e:
        try:
            run.log("WARN", f"marker not understood: {line.strip()[:200]} — {e}", src="ms")
        except Exception:
            pass
        return f"bad: {e}"
    return "log" if kind in ("info", "warn", "warning", "error", "debug") else kind


class Run:
    def __init__(self, name, params=None, folder=None, project=None, cmd=None, echo=False, extra=None):
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
        self.extra = {("x_" + k if not k.startswith("x_") else k): v for k, v in (extra or {}).items()}
        self._t0 = None
        self._stage = None
        self.reported = set()            # names of the metrics written so far
        self.stages = 0
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
                cwd=self.folder, host=socket.gethostname(), pid=os.getpid(), params=self.params, git=_git(self.folder),
                **self.extra)
        _register(self.path, self.run)
        return self

    def stage(self, name, status="ok"):
        """Begin stage `name`; ends the current stage (with `status`) first."""
        self.end_stage(status)
        self._stage = name
        self.stages += 1
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
        self.reported.add(name)
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
