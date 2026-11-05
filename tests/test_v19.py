"""1.19: ms q add checks the command; AMD_SETUP; reminder for paused jobs; ms thermal; cached run logs; path_map with
~/; ms remote licenses; the device-license diagnosis; a GUI that never blocks.
Run: python3 tests/test_v19.py   (or: bash tests/run_tests.sh v19)"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_features import fake_ms, py  # noqa: E402
from test_remote import run  # noqa: E402
from test_v18 import _fake_install, _hermetic, CONF, cluster  # noqa: E402

SRC = Path(__file__).resolve().parent.parent / "src"


def _ms(ms, tmp, args, cwd, extra_env=None):
    env = dict(os.environ, MY_SYSTEM=str(ms), MS_NO_CGROUP="1", HOME=str(tmp))
    env.update(extra_env or {})
    return subprocess.run([sys.executable, str(ms / "bin/ms"), *args], env=env, cwd=cwd, capture_output=True,
                          text=True, timeout=120)


# ---------------------------------------------------------------- ms q add checks
def test_add_refuses_missing_files_and_wrong_folders():
    tmp, ms = fake_ms()
    proj = tmp / "work" / "proj"
    proj.mkdir(parents=True)
    (proj / "build.tcl").write_text("synth_design\n")
    r = _ms(ms, tmp, ["q", "add", "--", "vivado", "-mode", "batch", "-source", "missing.tcl"], proj)
    assert r.returncode == 2 and "missing.tcl does not exist" in r.stderr and "--anyway" in r.stderr, r.stderr
    r = _ms(ms, tmp, ["q", "add", "--", "vivado -mode batch -source build.tcl"], proj)
    assert r.returncode == 0 and r.stdout.strip().isdigit(), r.stdout + r.stderr
    r = _ms(ms, tmp, ["q", "add", "--", "echo", "hi"], tmp)                         # home folder
    assert r.returncode == 2 and "home folder" in r.stderr, r.stderr
    r = _ms(ms, tmp, ["q", "add", "--", "echo", "hi"], ms)                          # ~/My_System
    assert r.returncode == 2 and "My_System" in r.stderr, r.stderr
    src = tmp / "work" / "my-system"                                                 # a my-system checkout
    (src / "src" / "lib" / "mysys").mkdir(parents=True)
    (src / "install.sh").write_text("#!/bin/sh\n")
    r = _ms(ms, tmp, ["q", "add", "--", "echo", "hi"], src)
    assert r.returncode == 2 and "my-system source" in r.stderr, r.stderr
    r = _ms(ms, tmp, ["q", "add", "--anyway", "--", "bash", "nothere.sh"], proj)
    assert r.returncode == 0, r.stderr
    r = _ms(ms, tmp, ["q", "add", "--", "source /no/such/settings64.sh && vitis-run --mode hls --tcl hls.tcl"], proj)
    assert "/no/such/settings64.sh does not exist" in r.stderr and "hls.tcl does not exist" in r.stderr, r.stderr


def test_referenced_files():
    out, *_ = py(r"""
from mysys import queue as Q
c = Q.referenced_files
assert c("vivado -mode batch -source build.tcl") == ["build.tcl"]
assert c("vitis_hls -f a.tcl && vitis-run --mode hls --tcl b.tcl") == ["a.tcl", "b.tcl"]
assert c("FOO=1 bash run.sh arg") == ["run.sh"]
assert c("./go.sh; python3 x.py; python3 -c 'print(1)'") == ["./go.sh", "x.py"]
assert c("make -j8 all") == ["Makefile"] and c("make -C hw") == []
assert c("vivado -source $HOME/a.tcl") == [] and c("echo hi") == []
print("ok")
""")
    assert "ok" in out


# ---------------------------------------------------------------- AMD_SETUP
def test_amd_setup_is_put_in_front_of_amd_jobs():
    conf = 'PHONE_REPORT_TIMES=""\nPHONE_REQUESTS=0\nAI_PROCESS_NAMES=""\nAMD_SETUP="source /opt/amd/Vivado/settings64.sh"\n'
    out, *_ = py(r"""
os.makedirs(os.path.join(W, "a"), exist_ok=True)
open(os.path.join(W, "a", "run.sh"), "w").write('"$XILINX_VIVADO/bin/vivado" -mode batch -source x.tcl\n')
open(os.path.join(W, "a", "plain.sh"), "w").write('echo nothing to do with AMD\n')
open(os.path.join(W, "a", "Makefile"), "w").write('all:\n\tvitis-run --mode hls --tcl h.tcl\n')
S_ = "source /opt/amd/Vivado/settings64.sh && "
def sub(c):
    return queue.submit(c, cwd=os.path.join(W, "a"), state="held")
v = sub("vivado -mode batch -source b.tcl")
assert v["cmd"] == S_ + "vivado -mode batch -source b.tcl" and v["cmd_typed"] == "vivado -mode batch -source b.tcl", v
assert v["name"] == "vivado", v["name"]
assert sub("bash run.sh")["cmd"].startswith(S_), "a script that calls Vivado"
assert sub("make all")["cmd"].startswith(S_), "a Makefile that calls vitis-run"
assert sub("bash plain.sh")["cmd"] == "bash plain.sh"
assert sub("echo hi")["cmd"] == "echo hi"
own = "source /mnt/x/Vivado/settings64.sh && vivado -version"
assert sub(own)["cmd"] == own, "it sets itself up already"
print("ok")
""", extra_conf=conf)
    assert "ok" in out


def test_amd_setup_off_by_default():
    out, *_ = py(r"""
j = queue.submit("vivado -version", cwd=W, state="held")
assert j["cmd"] == "vivado -version" and not j.get("cmd_typed"), j
print("ok")
""")
    assert "ok" in out


# ---------------------------------------------------------------- paused jobs
def test_reminder_for_a_job_paused_too_long():
    out, notes, _ = py(r"""
os.makedirs(os.path.join(W, "s"), exist_ok=True)
a = queue.submit("sleep 60", cwd=os.path.join(W, "s"), name="longsleep", **small)
assert until(lambda: st(a["id"]) == "running", 20)
assert queue.suspend(a["id"])
j = queue.load(a["id"])
assert j["suspended_at"] and j["reason"] == "paused by user", j
S.remind_paused(queue.all_jobs(), time.time())          # just paused: nothing yet
queue.update(a["id"], suspended_at=time.time() - 25 * 3600)
S.remind_paused(queue.all_jobs(), time.time())
S.remind_paused(queue.all_jobs(), time.time())          # not twice in a day
assert queue.load(a["id"])["reminded_at"]
queue.cancel(a["id"])
until(lambda: st(a["id"]) in queue.TERMINAL, 20)
print("ok")
""")
    assert "ok" in out
    assert notes.count("Job still paused") == 1 and "ms q resume" in notes, notes


def test_old_paused_job_counts_from_now():
    out, notes, _ = py(r"""
a = queue.submit("sleep 1", cwd=W, name="old", state="held")
queue.update(a["id"], state="suspended", reason="paused by user")      # paused by an older version
S.remind_paused(queue.all_jobs(), time.time())
assert queue.load(a["id"])["suspended_at"], "starts counting now"
print("ok")
""")
    assert "ok" in out and "Job still paused" not in notes


# ---------------------------------------------------------------- thermal
THERMAL = r"""
import tempfile
from mysys import thermal as T
d = tempfile.mkdtemp()
T.COOL_SH, T.THERMALD_CONF = os.path.join(d, "cpu-cool.sh"), os.path.join(d, "thermal-conf.xml")
"""


def test_thermal_files_and_check():
    out, *_ = py(THERMAL + r"""
assert T.wanted() is None and T.trip_c() is None
open(T.COOL_SH, "w").write(T.cool_sh(85, 80))
open(T.THERMALD_CONF, "w").write(T.thermald_xml(72))
assert T.wanted() == {"max_perf_pct": 85, "pl2_w": 80}, T.wanted()
assert T.trip_c() == 72.0
assert T.MARK in open(T.THERMALD_CONF).read()
base = {"temp": 70, "max_24h": 74, "limit": 80.0, "thermald": "active", "thermald_ours": True, "trip_c": 72.0,
        "service": "enabled", "current": {"max_perf_pct": 85, "pl2_w": 80}, "wanted": {"max_perf_pct": 85, "pl2_w": 80}}
lv = lambda st: [l for l, _ in T.check(st)]
assert "!!" not in lv(base), T.check(base)
lost = dict(base, current={"max_perf_pct": 100, "pl2_w": 224})
assert any("not in force" in t and "100 instead of 85" in t for _, t in T.check(lost)), T.check(lost)
assert any("disabled" in t for _, t in T.check(dict(base, service="disabled")))
assert any("thermald is inactive" in t for _, t in T.check(dict(base, thermald="inactive")))
hot = dict(base, max_24h=86)
assert any("reached 86" in t for _, t in T.check(hot))
nolimits = dict(hot, wanted=None, thermald_ours=False)
assert any("ms thermal setup 80" in t for _, t in T.check(nolimits)), T.check(nolimits)
assert "!!" in T.show(lost) and "now max 100%" in T.show(lost)
print("ok")
""")
    assert "ok" in out


def test_thermal_max_from_history_and_setup_steps():
    out, *_ = py(THERMAL + r"""
from mysys import history as H
H.DIR.mkdir(parents=True, exist_ok=True)
now = time.time()
day = time.strftime("%Y-%m-%d", time.localtime(now))
with open(H.DIR / f"{day}.jsonl", "w") as f:
    for k, t in enumerate((61, 79, 72)):
        f.write(json.dumps({"t": int(now - 600 + k * 60), "temp": t}) + "\n")
    f.write(json.dumps({"t": int(now - 30 * 3600), "temp": 95}) + "\n")   # older than 24 h
assert T.max_temp(24, now) == 79, T.max_temp(24, now)
calls = []
def fake(cmd):
    calls.append(cmd[:2] if cmd[0] == "write" else cmd)
    return True
assert T.setup(80, run=fake)
assert ["write", T.THERMALD_CONF] in calls and ["write", T.COOL_SH] in calls, calls
assert ["systemctl", "enable", "--now", "cpu-cool.service"] in calls, calls
calls.clear()
assert T.off(run=fake)
assert ["systemctl", "disable", "--now", "cpu-cool.service"] in calls, calls
print("ok")
""")
    assert "ok" in out


def test_ms_thermal_runs_here():
    tmp, ms = fake_ms()
    r = _ms(ms, tmp, ["thermal"], tmp)
    assert r.returncode == 0 and "MAX_TEMP_C" in r.stdout, r.stdout + r.stderr
    r = _ms(ms, tmp, ["thermal", "check"], tmp)
    assert r.returncode in (0, 1), r.stderr


# ---------------------------------------------------------------- run-log cache
def test_runlog_parsed_once_until_it_changes():
    out, *_ = py(r"""
import socket
from mysys import runlog as RL
p = os.path.join(W, "msrun.jsonl")
def ev(**k):
    with open(p, "a") as f:
        f.write(json.dumps(k) + "\n")
ev(ev="start", run="a", name="ok-run", ts="2026-11-05T10:00:00")
ev(ev="end", run="a", status="ok", ts="2026-11-05T10:05:00")
ev(ev="start", run="b", name="ghost", pid=999999, host=socket.gethostname(), ts="2026-11-05T10:00:00")
n = {"parse": 0}
orig = RL._parse
def counting(path):
    n["parse"] += 1
    return orig(path)
RL._parse = counting
r1 = RL.read(p)
r2 = RL.read(p)
assert n["parse"] == 1, n
assert r1["a"]["status"] == "ok" and r1["b"]["status"] == "died", (r1["a"]["status"], r1["b"]["status"])
r2["a"]["status"] = "changed-by-caller"
assert RL.read(p)["a"]["status"] == "ok", "the cache is not changed through a returned run"
time.sleep(0.01)
ev(ev="start", run="c", name="new", ts="2026-11-05T11:00:00")
r3 = RL.read(p)
assert n["parse"] == 2 and "c" in r3, n
print("ok")
""")
    assert "ok" in out


# ---------------------------------------------------------------- path_map with ~/
def test_path_map_with_tilde_on_the_left():
    out, *_ = run(r"""
os.makedirs(os.path.join(W, "t"), exist_ok=True)
open(os.path.join(W, "t", "run.sh"), "w").write("~/.local/bin/vitis_hls -f a.tcl\n")
a = queue.submit("grep -c 'bzm/bin/vitis_hls -f' run.sh > n.txt; grep -c '[.]local/bin' run.sh > m.txt; true",
                 cwd=os.path.join(W, "t"), name="tilde", remote="titan", **small)
assert until(lambda: st(a["id"]) in queue.TERMINAL, 60)
log = open(queue.LOGS / f"{a['id']}.log").read()
assert st(a["id"]) == "done", log
assert open(os.path.join(W, "t", "n.txt")).read().strip() == "1", log
assert open(os.path.join(W, "t", "m.txt")).read().strip() == "0", log
assert open(os.path.join(W, "t", "run.sh")).read() == "~/.local/bin/vitis_hls -f a.tcl\n", "your file is unchanged"
print("ok")
""", extra="path_map = ~/.local/bin/vitis_hls => ~/bzm/bin/vitis_hls\n")
    assert "ok" in out


def test_path_map_still_refuses_bad_entries():
    out, *_ = py(r"""
from mysys import remote as RM
for bad in ("relative/x => /y", "~/a => b", "/a b => /c", "~ => /x"):
    try:
        RM.parse_path_map({"name": "t", "path_map": bad})
        raise SystemExit("accepted: " + bad)
    except KeyError:
        pass
assert RM.parse_path_map({"name": "t", "path_map": "~/.local/bin/x => ~/a/x"}) == [("~/.local/bin/x", "~/a/x")]
print("ok")
""")
    assert "ok" in out


# ---------------------------------------------------------------- ms remote licenses
FAKE_VIVADO_LIC = r"""#!/bin/bash
case "$*" in *-source*) ;; *) echo 'vivado v2025.2.1 (64-bit)'; exit 0;; esac
parts=$(sed -n 's/^set parts {\(.*\)}$/\1/p' lic.tcl)
for p in $parts; do
  echo "L begin $p"
  case "$p" in
    xc9*) echo "L missing $p"; continue;;
    xc7vx*) # what Vivado really prints when synth_design runs inside catch (Titan, 5 Nov 2026): only the warning
            echo "WARNING: [Common 17-348] Failed to get the license for feature 'Synthesis' and/or device '${p%%ffg*}'. Explanation: The license feature Synthesis could not be found."
            echo "synth_design failed"
            echo "L msg $p ERROR: [Common 17-69] Command failed: synth_design failed"
            echo "L end $p 1";;
    xc7vh*) echo "ERROR: [Common 17-345] A valid license was not found for feature 'Synthesis' and/or device 'xc7vh580t'."
            echo "L end $p 1";;
    xc7k*) echo "ERROR: [Synth 8-439] module 'top' not found"; echo "L end $p 1";;
    *) echo "Got license"; echo "L end $p 0";;
  esac
done
"""


def test_remote_licenses():
    tmp, ms = fake_ms()
    env = dict(os.environ, MY_SYSTEM=str(ms), MS_NO_CGROUP="1", HOME=str(tmp))
    env.update(cluster(tmp, shared=True))
    _hermetic(env)
    root = _fake_install(env["FAKE_HEAD"])
    (root / "Vivado/bin/vivado").write_text(FAKE_VIVADO_LIC)
    (ms / "config/remotes.conf").write_text(CONF.format(offload="never") + f"setup = source {root}/Vitis/settings64.sh\n")
    r = subprocess.run([sys.executable, str(ms / "bin/ms"), "remote", "licenses", "titan", "xc7vx330tffg1157-1",
                        "xc7z020clg400-1", "xc9zz1-a1", "xc7k70tfbg676-1", "xc7vh580thcg1155-2"], env=env,
                       capture_output=True, text=True,
                       timeout=180)
    o = r.stdout
    assert r.returncode == 0, o + r.stderr
    assert "NO  xc7vx330tffg1157-1: no license" in o and "ok  xc7z020clg400-1: licensed" in o, o
    assert "xc9zz1-a1: not installed there" in o and "xc7k70tfbg676-1: failed (not a license problem)" in o, o
    st = json.loads((ms / "state/remote/titan-unlicensed.json").read_text())
    assert "xc7vh580thcg1155-2: no license" in o, o
    assert sorted(st["devices"]) == ["xc7vh580t", "xc7vx330t"], st
    assert "Synthesizing" not in o and "Report Cell Usage" not in o, "only the result lines are shown"


def test_license_parts_from_recent_jobs():
    out, *_ = py(r"""
from mysys import remote as RM
for d, txt in (("p1", "set_part xc7vx330tffg1157-1\nset b xc7vx330tffg1157-2\n"), ("p2", "create_project -part xc7z020clg400-1\n"),
               ("p3", "# board xc7z020 only\n")):
    os.makedirs(os.path.join(W, d), exist_ok=True)
    open(os.path.join(W, d, "b.tcl"), "w").write(txt)
    queue.submit("true", cwd=os.path.join(W, d), state="held")
parts = RM.license_parts()
devs = sorted(RM._device(p) for p in parts)
assert devs == ["xc7vx330t", "xc7z020"], parts
print("ok")
""")
    assert "ok" in out


def test_device_license_diagnosis():
    out, *_ = py(r"""
from mysys import issues
d = issues.diagnose("INFO x\nERROR: [Common 17-345] A valid license was not found for feature 'Synthesis' and/or device 'xc7vx330t'.\n")
assert d and d["name"] == "Device needs a license" and "ms remote licenses" in d["fix"], d
print("ok")
""")
    assert "ok" in out


# ---------------------------------------------------------------- GUI
GUI = r"""
import os, sys, time, importlib.util, json, socket
os.environ["QT_QPA_PLATFORM"] = "offscreen"
ms = os.environ["MY_SYSTEM"]
sys.path.insert(0, os.path.join(ms, "lib"))
from mysys import config
# 30 run logs, 2 of them with a run that never ended (they used to be read again on every refresh)
paths = []
for f in range(30):
    d = os.path.join(os.environ["WORK"], f"proj{f}"); os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "msrun.jsonl"); paths.append(p)
    with open(p, "w") as fh:
        for r in range(60):
            rid = f"r{f}-{r}"
            fh.write(json.dumps({"ev": "start", "run": rid, "name": f"run{r}", "ts": "2026-11-01T10:00:00",
                                 "pid": os.getpid(), "host": socket.gethostname()}) + "\n")
            for k in range(10):
                fh.write(json.dumps({"ev": "metric", "run": rid, "name": f"m{k}", "value": k}) + "\n")
            if not (f < 2 and r == 0):
                fh.write(json.dumps({"ev": "end", "run": rid, "status": "ok", "ts": "2026-11-01T10:10:00"}) + "\n")
open(config.STATE / "runlog_paths.txt", "w").write("\n".join(paths) + "\n")
spec = importlib.util.spec_from_file_location("gui", os.path.join(ms, "gui", "my_system_gui.py"))
G = importlib.util.module_from_spec(spec); spec.loader.exec_module(G)
from PyQt6.QtWidgets import QApplication
app = QApplication(sys.argv); G.set_theme(False); G.apply_theme(app)
t0 = time.time(); w = G.Main(); w.show(); app.processEvents()
start = time.time() - t0
t0 = time.time()
while not (w.nav_future and w.nav_future.done()) and time.time() - t0 < 60:
    app.processEvents(); time.sleep(0.02)
time.sleep(0.3); app.processEvents()
assert w.nav_future.done() and w.nav_future.applied, "sidebar filled from the background thread"
names = [type(p).__name__ for p in w.pages]
for i, p in enumerate(w.pages):                 # every page refreshes without an exception
    row = [r for r, k in w.row_page.items() if k == i][0]
    w.nav.setCurrentRow(row); app.processEvents()
    assert "refresh error" not in w.foot.text(), (names[i], w.foot.text())
row = [r for r, k in w.row_page.items() if k == names.index("RunLogsPage")][0]
w.nav.setCurrentRow(row); app.processEvents()
ticks = []
for _ in range(3):
    a = time.time(); w.tick(); ticks.append(time.time() - a)
G.PROFILE = True; w.write_profile()
prof = open(config.STATE / "gui_profile.txt").read()
w.close()
print(f"start={start:.2f} ticks={max(ticks):.2f}")
assert start < 3.0, start
assert max(ticks) < 0.5, ticks
assert "RunLogsPage" in prof, prof
print("ok")
"""


def test_gui_opens_fast_and_never_blocks():
    try:
        import PyQt6
        import PyQt6.QtWidgets  # noqa: F401
    except Exception:
        print("skipped: PyQt6 is not installed")
        return
    tmp, ms = fake_ms()
    shutil.copytree(SRC / "gui", ms / "gui")
    # the app runs with HOME changed: a PyQt6 installed in the user's own site-packages (pip --user, as on GitHub's
    # runners) would not be found there, so its folder is passed on explicitly
    site = os.path.dirname(os.path.dirname(PyQt6.__file__))
    env = dict(os.environ, MY_SYSTEM=str(ms), MS_NO_CGROUP="1", HOME=str(tmp), WORK=str(tmp / "work"),
               QT_QPA_PLATFORM="offscreen",
               PYTHONPATH=os.pathsep.join(x for x in (site, os.environ.get("PYTHONPATH", "")) if x))
    r = subprocess.run([sys.executable, "-c", GUI], env=env, capture_output=True, text=True, timeout=240)
    assert r.returncode == 0 and "ok" in r.stdout, r.stdout[-3000:] + r.stderr[-3000:]


if __name__ == "__main__":                # ms selftest runs each test file as a script (no pytest needed)
    t0 = time.time()
    for k, fn in list(globals().items()):
        if k.startswith("test_") and callable(fn):
            fn()
            print("ok", k)
    print(f"all passed in {time.time() - t0:.1f}s")
