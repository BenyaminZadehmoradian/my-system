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
