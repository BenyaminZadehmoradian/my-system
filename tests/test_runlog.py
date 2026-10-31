"""Standard run log: writers (Python, Tcl, ms log), validator, reader. Run: python3 tests/test_runlog.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" if (ROOT / "src").is_dir() else ROOT


def _ct(src, dst, **kw):
    """copytree that leaves the copy writable (the installed source is read-only)."""
    import shutil as _sh
    _sh.copytree(src, dst, **kw)
    for dp, dns, fns in __import__("os").walk(dst):
        for n in [dp] + [__import__("os").path.join(dp, f) for f in fns]:
            try:
                __import__("os").chmod(n, __import__("os").stat(n).st_mode | 0o200)
            except OSError:
                pass

sys.path.insert(0, str(SRC / "runlog"))
sys.path.insert(0, str(SRC / "lib"))


def fake_ms():
    tmp = Path(tempfile.mkdtemp())
    ms = tmp / "My_System"
    for d in ("bin", "lib", "plugins", "runlog"):
        _ct(SRC / d, ms / d)
    for d in ("config", "state", "reports"):
        (ms / d).mkdir()
    (ms / "bin/notify.sh").write_text(f'#!/bin/sh\necho "$1|$2" >> {tmp}/notes\n')
    return tmp, ms


def test_python_writer():
    tmp, ms = fake_ms()
    os.environ["MY_SYSTEM"] = str(ms)
    import msrun
    from mysys import runlog
    d = tmp / "runs" / "doe_1"
    with msrun.Run("doe_1", params={"seed": 3, "strategy": "Explore"}, folder=d) as r:
        r.stage("synth")
        r.metric("lut", 41233)
        r.warn("clock uncertainty high")
        r.stage("route")
        r.metric("wns_ns", -0.084, "ns")
        r.progress(100)
        r.artifact(str(d / "reports/timing.rpt"), "report")
    try:
        with msrun.Run("doe_2", folder=d) as r:
            r.stage("synth")
            raise RuntimeError("placement failed")
    except RuntimeError:
        pass
    f = d / "msrun.jsonl"
    assert runlog.validate(f) == [], runlog.validate(f)
    runs = sorted(runlog.read(f).values(), key=lambda r: r["name"])
    a, b = runs
    assert a["status"] == "ok" and a["metrics"] == {"lut": 41233, "wns_ns": -0.084} and a["warnings"] == 1
    assert a["params"]["seed"] == 3 and a["artifacts"] == [("reports/timing.rpt", "report")]
    assert [s["stage"] for s in a["stages"]] == ["synth", "route"] and all(s["status"] == "ok" for s in a["stages"])
    assert b["status"] == "failed" and b["errors"] == 1 and "placement failed" in b["first_error"]
    assert b["stages"][0]["status"] == "failed"
    # registered for my-system
    assert str(f) in (ms / "reports/runlog_index.tsv").read_text()


def test_validator_catches():
    from mysys import runlog
    tmp = Path(tempfile.mkdtemp())
    f = tmp / "msrun.jsonl"
    lines = [
        {"v": 1, "ts": "2026-10-28T10:00:00+01:00", "run": "a", "ev": "metric", "name": "x", "value": 1},   # before start
        {"v": 1, "ts": "2026-10-28T10:00:01+01:00", "run": "a", "ev": "start", "name": "a"},
        {"v": 1, "ts": "2026-10-28 10:00:02", "run": "a", "ev": "log", "level": "OOPS", "msg": "x"},          # no offset, bad level
        {"v": 1, "ts": "2026-10-28T10:00:03+01:00", "run": "a", "ev": "metric", "name": "WNS", "value": 1, "foo": 2},
        {"v": 1, "ts": "2026-10-28T09:00:00+01:00", "run": "a", "ev": "artifact", "path": "/abs/x", "kind": "pic"},
        {"v": 1, "ts": "2026-10-28T10:00:05+01:00", "run": "a", "ev": "end", "status": "failed", "seconds": 5},
    ]
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    msgs = " | ".join(m for _, m in runlog.validate(f))
    for want in ("event before start", "no UTC offset", "log.level", "snake_case", "unknown keys foo", "backwards",
                 "artifact.kind", "relative", "end.error is required", "not valid JSON"):
        assert want in msgs, (want, msgs)


def test_ms_log_cli():
    tmp, ms = fake_ms()
    env = dict(os.environ, MY_SYSTEM=str(ms))
    d = tmp / "proj" / "run7"
    d.mkdir(parents=True)

    def ms_(*a, cwd=d):
        return subprocess.run([sys.executable, str(ms / "bin/ms"), "log", *a], cwd=cwd, env=env, capture_output=True, text=True)
    assert ms_("start", "sweep7", "seed=7", "mode=fast").returncode == 0
    ms_("stage", "sim")
    ms_("metric", "cycles", "12345")
    ms_("msg", "WARN", "slow", "memory")
    ms_("artifact", "out/wave.vcd", "data")
    assert ms_("end", "ok").returncode == 0
    assert not (d / ".msrun_current").exists()
    r = ms_("run", "--name", "bad", "--", "sh", "-c", "echo hello; echo 'ERROR: [Place 30-99] boom'; exit 3")
    assert r.returncode == 3 and "hello" in r.stdout
    chk = ms_("check", str(d))
    assert chk.returncode == 0, chk.stdout
    from mysys import runlog
    runs = {x["name"]: x for x in runlog.read(d / "msrun.jsonl").values()}
    assert runs["sweep7"]["status"] == "ok" and runs["sweep7"]["metrics"] == {"cycles": 12345}
    assert runs["sweep7"]["params"] == {"seed": 7, "mode": "fast"}
    assert runs["bad"]["status"] == "failed" and runs["bad"]["errors"] == 1 and "Place 30-99" in runs["bad"]["first_error"]
    assert "hello" in (d / "msrun.out").read_text()
    show = ms_("show", str(d)).stdout
    assert "END OK" in show and "END FAILED" in show
    # bad file -> check fails
    (d / "msrun.jsonl").open("a").write('{"v":1}\n')
    assert ms_("check", str(d)).returncode == 1
    # index + list
    (ms / "state/detected.json").write_text(json.dumps({"projects": [str(tmp / "proj")]}))
    assert "file(s) known" in ms_("index").stdout
    lst = ms_("list").stdout
    assert "sweep7" in lst and "bad" in lst
    tpl = ms_("template", "python").stdout
    assert "class Run" in tpl


def test_tcl_writer():
    tclsh = shutil.which("tclsh") or shutil.which("tclsh8.6")
    if not tclsh:
        print("   (tclsh not installed — skipped)")
        return
    from mysys import runlog
    tmp, ms = fake_ms()
    d = tmp / "viv"
    d.mkdir()
    script = f"""
source {SRC / 'runlog/msrun.tcl'}
msrun::start s18_doe3 {{seed 3 strategy Performance_Explore note "a \\"quoted\\" text"}}
msrun::stage synth
msrun::metric lut 41233
msrun::log WARN {{Timing 38-282 "x"}}
msrun::stage route
msrun::metric wns_ns -0.084 ns
msrun::artifact reports/timing.rpt report
msrun::progress 100
msrun::end ok
msrun::start s18_doe4
msrun::stage place
msrun::fail {{ERROR: [Place 30-640] Place Check}}
"""
    (d / "flow.tcl").write_text(script)
    r = subprocess.run([tclsh, "flow.tcl"], cwd=d, capture_output=True, text=True, env=dict(os.environ, MY_SYSTEM=str(ms)))
    assert r.returncode == 0, r.stderr
    f = d / "msrun.jsonl"
    assert runlog.validate(f) == [], (runlog.validate(f), f.read_text())
    runs = {x["name"]: x for x in runlog.read(f).values()}
    a, b = runs["s18_doe3"], runs["s18_doe4"]
    assert a["status"] == "ok" and a["metrics"] == {"lut": 41233, "wns_ns": -0.084}
    assert a["params"] == {"seed": 3, "strategy": "Performance_Explore", "note": 'a "quoted" text'}, a["params"]
    assert b["status"] == "failed" and "Place 30-640" in b["first_error"]
    assert str(f) in (ms / "reports/runlog_index.tsv").read_text()


def test_watcher_alert():
    tmp, ms = fake_ms()
    env = dict(os.environ, MY_SYSTEM=str(ms))
    code = f"""
import sys, time
sys.path.insert(0, {str(ms / 'lib')!r}); sys.path.insert(0, {str(ms / 'plugins')!r}); sys.path.insert(0, {str(ms / 'runlog')!r})
from mysys import watcher
watcher.jobs = lambda: []
watcher.tick()
import msrun
try:
    with msrun.Run("hls_k3", folder={str(tmp / 'r')!r}) as r:
        r.error("csynth failed")
        raise SystemError("csynth failed")
except SystemError:
    pass
watcher.tick()
watcher.tick()          # told only once
"""
    subprocess.run([sys.executable, "-c", code], env=env, check=True)
    notes = (tmp / "notes").read_text().splitlines()
    assert len([n for n in notes if n.startswith("Run failed: hls_k3")]) == 1, notes


if __name__ == "__main__":
    t0 = time.time()
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            fn()
            print("ok", k)
    print(f"all passed in {time.time() - t0:.1f}s")
