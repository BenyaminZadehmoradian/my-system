"""1.20: leading cd, ms q edit --after, ms q --help, policy for rm. Run: python3 tests/test_v20.py"""
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_features import fake_ms, py  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def test_leading_cd_and_edit_after():
    out, notes, tmp = py(r'''
P = os.path.join(W, "proj"); os.makedirs(P)
open(os.path.join(P, "run.sh"), "w").write("pwd > where.txt\n")
c, d = queue.leading_cd(f"bash -c 'cd {P} && bash run.sh > log.txt'", W)
assert (c, d) == ("bash run.sh > log.txt", P), (c, d)
assert queue.leading_cd(f"cd {P}/nope && x", W) == (f"cd {P}/nope && x", W)
assert queue.preflight(c, d) == [], queue.preflight(c, d)
j = queue.submit(f"cd {P} && bash run.sh", cwd=W, name="cdjob", **small)
jj = queue.load(j["id"])
assert jj["cwd"] == P and jj["cmd"] == "bash run.sh" and jj["cmd_typed"].startswith("cd "), jj
assert until(lambda: st(j["id"]) == "done", 30)
assert open(os.path.join(P, "where.txt")).read().strip() == P
h = queue.submit("true", cwd=W, name="h", state="held", **small)
k = queue.submit("true", cwd=W, name="k", state="held", **small)
ok, msg = queue.edit(k["id"], depends=[h["id"]])
assert ok and queue.load(k["id"])["depends"] == [h["id"]], msg
ok, msg = queue.edit(h["id"], depends=[k["id"]])
assert not ok and "waits for" in msg, msg
ok, msg = queue.edit(k["id"], depends=[999])
assert not ok and "no such job" in msg
print("V20-OK")
''')
    assert "V20-OK" in out, out


def test_q_help_and_policy():
    tmp, ms = fake_ms()
    env = dict(os.environ, MY_SYSTEM=str(ms), HOME=str(tmp))
    r = subprocess.run([sys.executable, str(ms / "bin/ms"), "q", "--help"], env=env, capture_output=True, text=True)
    assert "ms q add" in r.stdout and "scheduler:" not in r.stdout, r.stdout[:300]
    r = subprocess.run([sys.executable, str(ms / "bin/ms"), "q", "edit", "--help"], env=env, capture_output=True, text=True)
    assert "usage: ms q edit" in r.stdout and "Traceback" not in r.stderr, r.stdout + r.stderr
    src = ROOT / ("src/ai" if (ROOT / "src").is_dir() else "ai") / "claude/hooks/guard.py"
    spec = importlib.util.spec_from_file_location("g", src)
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    for c in ("rm -rf ~", "rm -rf ~/", "rm -rf ~/*", "rm -rf $HOME", "cd x && rm -rf /"):
        assert g.check_bash(c, []), c
    for c in ("rm -rf ~/hls_dse_runs/tmp_build", "rm -rf ./work", "rm -f ~/proj/a.txt", "rm -rf *", "rm -rf build/*"):
        assert not g.check_bash(c, []), c
    code = r"""
import os, sys
sys.path.insert(0, os.path.join(os.environ["MY_SYSTEM"], "lib"))
from mysys import policy
p = policy.build()["permissions"]
assert "Bash(rm -rf ~*)" not in p["deny"] and "Bash(rm -rf ~/*)" in p["deny"]
assert "Bash(ms q add *)" in p["allow"] and not any("cancel" in x for x in p["allow"]) and "Bash(cd *)" in p["allow"]
print("ok")
"""
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert "ok" in r.stdout, r.stdout + r.stderr


def test_slurm_usage():
    sys.path.insert(0, str(ROOT / "src/lib"))
    from mysys import remote as R
    acct, u = R.slurm_usage("COMPLETED|0:0|7200|05:00:00|4|\nCOMPLETED|0:0|7200|05:00:00|4|9.5G\n")
    assert acct == "COMPLETED|0:0" and u == {"remote_cores": 4, "remote_elapsed_s": 7200, "remote_cores_used": 2.5,
                                              "remote_peak_gb": 9.5}, u
    assert R.slurm_usage("FAILED|1:0") == ("FAILED|1:0", {})
    from mysys import queue as Q
    j = {"mem_gb": 64, "cores": 16, "remote_job": 1, "peak_rss": 3e8, "cpu_s": 100, "started": 1, "ended": 3601}
    e = Q.efficiency(j)
    assert e["remote"] and e["cores_used"] is None and e["mem_peak_gb"] == 0, e
    j.update(remote_cores=16, remote_cores_used=12.0, remote_peak_gb=40.0, remote_elapsed_s=3600)
    e = Q.efficiency(j)
    assert (e["cpu_eff"], e["mem_eff"], e["seconds"]) == (75, 62, 3600), e
    assert R._cpu_seconds("1-00:00:01") == 86401 and abs(R._cpu_seconds("02:03.5") - 123.5) < 1e-9


def test_max_cores():
    sys.path.insert(0, str(ROOT / "src/lib"))
    from mysys import remote as R
    r = {"max_jobs": "12", "max_cores": "8"}
    run = [{"remote": "titan", "state": "running", "cores": 1}] * 6
    assert R.full("titan", r, {"cores": 1}, run) is None, "7 one-core jobs fit in 8 cores"
    assert "max_cores" in R.full("titan", r, {"cores": 4}, run), "6 + 4 > 8"
    assert R.full("titan", r, {"cores": 16}, []) is None, "one job bigger than the cap may start alone"
    assert "max_jobs" in R.full("titan", {"max_jobs": "6", "max_cores": ""}, {"cores": 1}, run)
    assert R.full("titan", {"max_jobs": "12", "max_cores": ""}, {"cores": 64}, run) is None, "empty = no core cap"


if __name__ == "__main__":
    t0 = time.time()
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            fn()
            print("ok", k)
    print(f"all passed in {time.time() - t0:.1f}s")
