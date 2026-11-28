"""1.27: server jobs reserve what jobs like them used; fresh server readings; no success line as failure reason.
Run: python3 tests/test_v27.py"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_features import py  # noqa: E402
import test_remote as TR  # noqa: E402


def test_right_size_from_measured_jobs():
    out, notes, tmp = py(r'''
from mysys import remote
now = time.time()
def past(i, fam, res, used, el=3600):
    return {"id": i, "name": f"{fam}_x{i}", "state": "done", "remote": "titan", "ended": now - 3600,
            "remote_cores": res, "remote_cores_used": used, "remote_elapsed_s": el, "remote_peak_gb": 3.0, "mem_gb": 8}
jobs = [past(i, "k89", 4, 0.9 + (i % 3) * 0.05) for i in range(10)]
r = dict(remote.DEFAULTS, name="titan")
c, note = remote.right_size(r, {"name": "k89_new", "cores": 4}, jobs)
assert c == 1 and "reserving 1 of the 4" in note, (c, note)
assert remote.right_size(r, {"name": "k89_new", "cores": 4, "exact_cores": True}, jobs) == (None, None)
assert remote.right_size(dict(r, right_size="no"), {"name": "k89_new", "cores": 4}, jobs) == (None, None)
assert remote.right_size(r, {"name": "other_new", "cores": 4}, jobs) == (None, None)          # no evidence
assert remote.right_size(r, {"name": "k89_a", "cores": 4}, jobs[:4]) == (None, None)          # under 5 jobs
# jobs capped by their reservation are no evidence: k82 at 1 core using 0.98 says nothing about what it needs
capped = [past(100 + i, "k82", 1, 0.98) for i in range(8)]
assert remote.right_size(r, {"name": "k82_y", "cores": 4}, capped) == (None, None)
mixed = [past(200 + i, "k85", 4, 1.0 + (i % 5) * 0.2) for i in range(10)]
c, _ = remote.right_size(r, {"name": "k85_z", "cores": 4}, mixed)
assert c == 2, c
j = {"name": "k89_new", "cores": 4, "slurm_cores": 1}
assert remote.slurm_cores(j) == 1 and remote.slurm_cores({"cores": 4}) == 4
print("RS-OK")
''')
    assert "RS-OK" in out, out


def test_right_size_on_the_fake_cluster():
    out, notes, tmp, ms, env = TR.run(r'''
from mysys import remote
now = time.time()
d = os.path.join(W, "rs"); os.makedirs(d)
for i in range(6):                                  # earlier jobs: asked for 4, used one
    queue.save({"id": 500 + i, "name": f"k89_old{i}", "state": "done", "remote": "titan", "cmd": "true", "cwd": d,
                "created": now - 7200, "ended": now - 3600, "started": now - 7200, "remote_cores": 4,
                "remote_cores_used": 0.95, "remote_elapsed_s": 3600, "remote_peak_gb": 2.0, "mem_gb": 4, "cores": 4})
queue._JOB_CACHE.clear() if hasattr(queue, "_JOB_CACHE") else None
a = queue.submit("echo hi", cwd=d, name="k89_new", mem_gb=1, cores=4, remote="titan")
assert until(lambda: st(a["id"]) in queue.TERMINAL, 90), queue.load(a["id"])
j = queue.load(a["id"]); log = open(queue.LOGS / f"{a['id']}.log").read()
assert j["state"] == "done" and j.get("slurm_cores") == 1, (j["state"], j.get("slurm_cores"), log[-800:])
script = open(os.path.join(os.environ["FAKE_SLURM"], j["remote_job"] + ".sh")).read()
assert "--cpus-per-task=1" in script and "reserving 1 of the 4" in log, script
print("RS2-OK")
''')
    assert "RS2-OK" in out, out


def test_failure_reason_is_not_a_success_line():
    out, notes, tmp = py(r'''
from mysys import jobrun
assert jobrun.said_why("x\nPASS: all inputs unpacked to data/unpacked/ and verified\n") == ""
assert jobrun.said_why("ERROR: HDLCompiler:1654 bad\nPASS: all inputs verified\n").startswith("ERROR: HDLCompiler")
assert jobrun.said_why("make: *** [all] Error 2\n") == "make: *** [all] Error 2"
assert jobrun.said_why("starting\nsegfault at 0x0\n") == "segfault at 0x0"
d = os.path.join(W, "pz"); os.makedirs(d)
k = queue.submit("echo 'PASS: all inputs verified'; exit 1", cwd=d, name="pz", **small)
assert until(lambda: st(k["id"]) in queue.TERMINAL, 30)
assert queue.load(k["id"])["reason"] == "exit code 1", queue.load(k["id"])["reason"]
print("WHY-OK")
''')
    assert "WHY-OK" in out, out


if __name__ == "__main__":
    t0 = time.time()
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            fn()
            print("ok", k)
    print(f"all passed in {time.time() - t0:.1f}s")
