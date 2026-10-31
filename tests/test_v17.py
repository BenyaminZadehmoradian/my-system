"""1.7.2 fixes found on the real machine: external CPU load must not freeze the queue, CPU patience, waiting advice,
varied tool durations, batch notifications (driver programs, repeated identical failures).
Run: python3 tests/test_v17.py"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_features import fake_ms, py  # noqa: E402


def test_external_load_patience_advice():
    out, notes, tmp = py(r'''
probes.threads = lambda: 16
probes.loadavg = lambda: (10.0, 10.0, 10.0)        # your own Vivado runs outside the queue use 10 threads
open(os.path.join(MS, "config", "queue.conf"), "w").write(
    "QUEUE_STARVE_MIN=0.001\nQUEUE_CPU_PATIENCE_MIN=999\nQUEUE_STUCK_NOTIFY_H=0.0001\n")
big = queue.submit("echo big", cwd=W, name="big", mem_gb=2, cores=13)
time.sleep(0.1)
S.tick()
small = queue.submit("echo small", cwd=W, name="small", mem_gb=1, cores=2)
assert until(lambda: st(small["id"]) == "done", 30), queue.load(small["id"])["reason"]   # not blocked "keeping room"
jb = queue.load(big["id"])
assert jb["state"] == "queued" and "CPU" in jb["reason"] and jb.get("max_free_t") is not None, jb
S._stuck_t = 0
S.tick()
assert queue.load(big["id"]).get("waiting_told")
ex = {c: d for ok, c, d in queue.explain(big["id"])}
assert "CPU patience" in ex and "at most" in ex["CPU"], ex
# patience: after waiting, it starts once half of its cores are free
open(os.path.join(MS, "config", "queue.conf"), "w").write("QUEUE_CPU_PATIENCE_MIN=0.001\n")
assert until(lambda: st(big["id"]) == "done", 30), queue.load(big["id"])["reason"]
assert "started with 6 of 12 cores free" in queue.load(big["id"]).get("start_note", ""), queue.load(big["id"])
print("ok")
''')
    assert "ok" in out
    assert "1 job waiting a long time" in notes and "ms q edit" in notes and "--cores 6" in notes, notes


def test_varied_tool_durations():
    tmp, ms = fake_ms()
    runs = [{"t": 1, "peak_gb": 2, "cores": 1, "core_max": 1, "minutes": m, "partial": False}
            for m in [2, 2, 2, 3, 7, 8, 9, 12, 250, 330, 349]]
    (ms / "state/job_profiles.json").write_text(json.dumps({"vivado": runs, "xelab": runs[:5]}))
    env = dict(os.environ, MY_SYSTEM=str(ms), HOME=str(tmp))
    code = r'''
import os, sys
sys.path.insert(0, os.path.join(os.environ["MY_SYSTEM"], "lib"))
from mysys import capacity, queue
p = capacity.profiles()
assert p["vivado"]["duration_varies"] and capacity.typical_minutes(p["vivado"]) is None, p["vivado"]
assert not p["xelab"]["duration_varies"] and capacity.typical_minutes(p["xelab"]) == 2
assert queue.resolve_timeout("auto", p["vivado"]) is None        # never kill a 6 h run because most runs take 8 min
assert queue.resolve_timeout("auto", p["xelab"]) == 1800
assert queue.usual_seconds({"id": 1, "name": "x", "cwd": "/x", "tool": "vivado"}) == 0
print("ok")
'''
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stdout + r.stderr


def test_batch_notifications():
    tmp, ms = fake_ms()
    home = tmp
    (ms / "config/my_system.conf").write_text(f'DATA_DISKS="{tmp}/data"\nBATCH_QUIET=0\n')
    env = dict(os.environ, MY_SYSTEM=str(ms), HOME=str(home))
    code = r'''
import os, sys, time
sys.path.insert(0, os.path.join(os.environ["MY_SYSTEM"], "lib")); sys.path.insert(0, os.path.join(os.environ["MY_SYSTEM"], "plugins"))
from mysys import watcher, config
H = str(config.HOME)
proj = H + "/Critical_path_Lifetime"
assert watcher.batch_key(proj) == proj                          # not "benyamin/"
assert watcher.batch_key(proj + "/runs/sweep/sim_1") == proj + "/runs/sweep"
drv = [{"cwd": proj}]
assert watcher.batch_busy(proj + "/runs/sweep", drv)            # the driver program still runs above the batch
assert not watcher.batch_busy(proj + "/runs/sweep", [{"cwd": H}])
class Plug:
    NAME = "fake"
    def log_summary(self, cwd, tool):
        bad = "io" in cwd
        return {"errors": 7 if bad else 0, "critical": 0, "first_error":
                "ERROR: [Place 30-58] IO placement is infeasible. Number of unplaced IO Ports (1)" if bad else "", "log": ""}
watcher.plugin_by_name = lambda n: Plug()
watcher.record_result = lambda r, d, summ, st: {}
watcher.record_profile = lambda r, d: None
now = time.time()
S26 = H + "/work/S26"
running = {f"vivado|{S26}/w{i}_io|1": {"tool": "vivado", "cwd": f"{S26}/w{i}_io", "start": now - 600, "plugin": "fake", "cpu_ticks": 0}
           for i in range(4)}
running.update({f"vivado|{S26}/ok{i}|1": {"tool": "vivado", "cwd": f"{S26}/ok{i}", "start": now - 600, "plugin": "fake", "cpu_ticks": 0}
                for i in range(3)})
s = {"v": watcher.VERSION, "runs": running, "pending": {}, "last_tick": now - 60}
conf = {"MIN_RUN": 60, "BATCH_QUIET": 0}
watcher.jobs = lambda: []
watcher.track_jobs(s, now, conf)
print("ok")
'''
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stdout + r.stderr
    notes = (tmp / "notes").read_text()
    assert notes.count("vivado failed") == 1, notes                # 4 identical failures -> 1 alert
    assert "Finished: 7 vivado runs in S26/" in notes and "3 OK, 4 failed" in notes, notes
    assert "failures: [Place 30-58] IO placement is infeasible ×4" in notes, notes


if __name__ == "__main__":
    t0 = time.time()
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            fn()
            print("ok", k)
    print(f"all passed in {time.time() - t0:.1f}s")
