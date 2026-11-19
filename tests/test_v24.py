"""1.24: a long sweep does not block every other job for days ("keeping room" only for jobs that are really starved).
Run: python3 tests/test_v24.py"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_features import py  # noqa: E402


def test_a_sweep_does_not_starve_small_jobs():
    out, notes, tmp = py(r'''
open(os.path.join(MS, "config", "queue.conf"), "w").write("QUEUE_STARVE_MIN=0.1\nQUEUE_BACKFILL_MIN=0\n")
d = os.path.join(W, "sw"); os.makedirs(d)
sweep = [queue.submit("sleep 4", cwd=d, name=f"sweep_{i}", mem_gb=18, cores=1, flow={"name": "f", "step": "s", "item": {}}) for i in range(8)]
time.sleep(7)                       # the later sweep jobs have now "waited too long"
small = queue.submit("echo small", cwd=d, name="small", mem_gb=2, cores=1)
assert until(lambda: st(small["id"]) == "done", 20), queue.load(small["id"])["reason"]
left = [x for x in sweep if st(x["id"]) == "queued"]
assert left, "the small job must start while the sweep still waits (it fits beside the running sweep jobs)"
assert until(lambda: all(st(x["id"]) == "done" for x in sweep), 60)
print("SWEEP-OK")
''')
    assert "SWEEP-OK" in out, out


def test_a_big_job_behind_a_sweep_does_not_block_small_jobs():
    """k79 (32 GB) behind 60 ISE jobs: room kept for it never reaches it (the sweep ahead takes it), so small jobs
    behind it must still run."""
    out, notes, tmp = py(r'''
open(os.path.join(MS, "config", "queue.conf"), "w").write("QUEUE_STARVE_MIN=0.05\nQUEUE_BACKFILL_MIN=0\n")
d = os.path.join(W, "bg"); os.makedirs(d)
sweep = [queue.submit("sleep 4", cwd=d, name=f"sw_{i}", mem_gb=18, cores=1) for i in range(8)]
big = queue.submit("sleep 1", cwd=d, name="big", mem_gb=40, cores=1)
time.sleep(4)
small = queue.submit("echo small", cwd=d, name="small", mem_gb=2, cores=1)
assert until(lambda: st(small["id"]) == "done", 20), queue.load(small["id"])["reason"]
assert st(big["id"]) == "queued"
assert until(lambda: st(big["id"]) == "done", 90), queue.load(big["id"])["reason"]
print("BIG-OK")
''')
    assert "BIG-OK" in out, out


def test_jobs_that_can_only_fail_there_are_not_sent():
    """ISE-only projects (Titan has no ISE) and jobs that call ../ outside their folder stay here; others still go."""
    import test_remote as TR
    out, notes, tmp, ms, env = TR.run(r'''
import json
from mysys import remote, versions
ise = os.path.join(W, "mcg", "run1"); other = os.path.join(W, "lab", "r1"); os.makedirs(ise); os.makedirs(other)
open(os.path.join(W, "mcg", "optbuild.sh"), "w").write("echo hi\n")
open(os.path.join(other, "go.sh"), "w").write("echo go\n")
versions.FILE.write_text(json.dumps({"default": ["2025.2"], "projects": {os.path.join(W, "mcg"): ["14.7"]}}))
r = remote.get("titan")
remote.STATE.mkdir(parents=True, exist_ok=True)
probe = remote.STATE / "titan-tools.txt"
mk = lambda cmd, cwd: {"id": 0, "cmd": cmd, "cwd": cwd, "name": "x", "tool": None, "remote_ok": True}
assert remote.server_has_ise("titan") is None and remote.why_not(mk("bash go.sh", ise), r, scan=False) is None   # never probed: allowed
probe.write_text("T tool.vivado=/x/2025.2.1/Vivado/bin/vivado\n")
assert "ISE 14.7" in remote.why_not(mk("bash go.sh", ise), r, scan=False)
assert remote.why_not(mk("bash go.sh", other), r, scan=False) is None
probe.write_text("T settings=/opt/Xilinx/14.7/ISE_DS/settings64.sh\n")
assert remote.server_has_ise("titan") is True and remote.why_not(mk("bash go.sh", ise), r, scan=False) is None
w = remote.why_not(mk("bash ../optbuild.sh m_r8 7", ise), r, scan=False)
assert w and "outside its folder: ../optbuild.sh" in w, w
for ok in ("bash ./go.sh", "cd sub/.. && bash go.sh", "bash $X/../a.sh", "python3 -c 'print(1)'"):
    assert remote.why_not(mk(ok, other), r, scan=False) is None, (ok, remote.why_not(mk(ok, other), r, scan=False))
print("NOSEND-OK")
''', offload="prefer")
    assert "NOSEND-OK" in out, out


def test_backfill_before_the_first_running_job_ends():
    out, notes, tmp = py(r'''
S2 = queue.Scheduler
queue.usual_seconds = lambda j: {"long": 3600, "tiny": 600, "unknown": 0}.get(j.get("name"), 0)
now = time.time()
running = [{"id": 1, "name": "long", "state": "running", "started": now - 1800}]
assert S2.backfill_ok({"id": 2, "name": "tiny"}, 0, running, now)          # 10 min < 30 min left
assert not S2.backfill_ok({"id": 3, "name": "long"}, 0, running, now)      # 60 min > 30 min left
assert not S2.backfill_ok({"id": 4, "name": "unknown"}, 0, running, now)   # unknown length: no
assert S2.backfill_ok({"id": 2, "name": "tiny"}, 900, [], now)             # QUEUE_BACKFILL_MIN still works
r = [{"id": 5, "state": "running", "started": now - 60, "mem_gb": 6, "cores": 1}]
assert S2.served_recently({"id": 6}, 1.0, 6 * G, r, now, 1800)
assert not S2.served_recently({"id": 6}, 1.0, 25 * G, r, now, 1800)        # only smaller jobs start: starved
assert not S2.served_recently({"id": 6}, 1.0, 6 * G, r, now + 3600, 1800)  # nothing started lately
print("BF-OK")
''')
    assert "BF-OK" in out, out


if __name__ == "__main__":
    t0 = time.time()
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            fn()
            print("ok", k)
    print(f"all passed in {time.time() - t0:.1f}s")
