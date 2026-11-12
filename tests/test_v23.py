"""1.23: what a job tells my-system by printing (::ms markers), required results, rules for AI assistants.
Run: python3 tests/test_v23.py"""
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_features import fake_ms, py  # noqa: E402
import test_remote as TR  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

JOB = r'''echo "::ms stage synth"; echo "::ms progress 30 synthesising"; echo "::ms metric lut 41233"
echo "::ms stage route"; echo "::ms metric wns_ns -0.084 ns"; echo "::ms metric met=true"
echo "::ms warn clock uncertainty high"; echo "report" > timing.rpt; echo "::ms artifact timing.rpt"
echo "::ms metric"; echo "ordinary line"'''


def test_markers_in_a_local_job():
    out, notes, tmp = py(r'''
from mysys import runlog
d = os.path.join(W, "mk"); os.makedirs(d)
open(os.path.join(d, "run.sh"), "w").write(%r)
a = queue.submit("bash run.sh", cwd=d, name="mk", expect_metrics=["wns_ns", "lut"], **small)
b = queue.submit("echo '::ms metric lut 5'", cwd=d, name="mk2", expect_metrics=["wns_ns", "lut"], **small)
assert until(lambda: st(a["id"]) in queue.TERMINAL and st(b["id"]) in queue.TERMINAL, 40)
ja, jb = queue.load(a["id"]), queue.load(b["id"])
assert ja["state"] == "done", (ja["state"], ja["reason"])
assert jb["state"] == "warning" and "did not report: wns_ns" in jb["reason"], (jb["state"], jb["reason"])
r = runlog.read(os.path.join(d, "msrun.jsonl"))[ja["run_id"]]
assert r["metrics"]["lut"] == 41233 and r["metrics"]["wns_ns"] == -0.084 and r["metrics"]["met"] is True, r["metrics"]
assert r["units"].get("wns_ns") == "ns" and ["timing.rpt", "report"] in [list(x) for x in r["artifacts"]], (r["units"], r["artifacts"])
ev = [e for e in runlog.events(os.path.join(d, "msrun.jsonl"), ja["run_id"])]
stages = [(e["stage"], e["state"]) for e in ev if e["ev"] == "stage"]
assert stages == [("synth", "begin"), ("synth", "end"), ("route", "begin"), ("route", "end")], stages
assert any(e["ev"] == "log" and e["level"] == "WARN" and "clock uncertainty" in e["msg"] for e in ev)
assert any(e["ev"] == "log" and "marker not understood" in e["msg"] for e in ev), "a bad marker is a warning, not a crash"
assert any(e["ev"] == "progress" and e["pct"] == 30 for e in ev)
assert not runlog.validate(os.path.join(d, "msrun.jsonl")), runlog.validate(os.path.join(d, "msrun.jsonl"))
assert "ordinary line" in open(queue.LOGS / f"{a['id']}.log").read() and ja["last_line"] == "ordinary line"
print("MARK-OK")
''' % JOB)
    assert "MARK-OK" in out, out


def test_markers_and_progress_from_the_server():
    """On the server a job has no my-system helpers: printing is the only way, and `echo N >&3` must not fail."""
    out, notes, tmp, ms, env = TR.run(r'''
from mysys import runlog
d = os.path.join(W, "mr"); os.makedirs(d)
a = queue.submit("echo '::ms stage hls'; echo 55 >&3; echo '::ms metric latency_cycles 1200'; echo fine",
                 cwd=d, name="mr", mem_gb=1, cores=1, remote="titan", expect_metrics=["latency_cycles"])
assert until(lambda: st(a["id"]) in queue.TERMINAL, 60)
j = queue.load(a["id"]); log = open(queue.LOGS / f"{a['id']}.log").read()
assert j["state"] == "done", (j["state"], j["reason"], log)
r = runlog.read(os.path.join(d, "msrun.jsonl"))[j["run_id"]]
assert r["metrics"]["latency_cycles"] == 1200, r["metrics"]
ev = runlog.events(os.path.join(d, "msrun.jsonl"), j["run_id"])
assert any(e["ev"] == "stage" and e["stage"] == "hls" for e in ev) and any(e["ev"] == "progress" and e["pct"] == 55 for e in ev), ev
print("REMOTE-MARK-OK")
''')
    assert "REMOTE-MARK-OK" in out, out


def test_what_an_ai_session_sees():
    """ms ai status / rules, the SessionStart hook, the allow list, Claude's own memory files, --metrics, q show."""
    out, notes, tmp = py(r'''
import subprocess, importlib.util, shutil
from mysys import policy, aihelp
ROOT = %r
shutil.copytree(os.path.join(ROOT, "src", "ai"), os.path.join(MS, "ai"))
msbin = [sys.executable, os.path.join(MS, "bin", "ms")]
env = dict(os.environ, CLAUDECODE="1")
d = os.path.join(W, "ai1"); os.makedirs(d)
r = subprocess.run(msbin + ["q", "add", "--name", "k70v_aes_c0", "--cores", "1", "--mem", "1", "--metrics", "wns_ns,lut",
                            "--", "echo '::ms stage synth'; echo '::ms metric lut 7'"], cwd=d, env=env, capture_output=True, text=True)
assert r.returncode == 0, r.stdout + r.stderr
j = [x for x in queue.all_jobs() if x["name"] == "k70v_aes_c0"][0]
assert j["expect_metrics"] == ["wns_ns", "lut"] and j.get("submitted_by") == "ai", j
assert until(lambda: st(j["id"]) in queue.TERMINAL, 40)
j = queue.load(j["id"]); assert j["state"] == "warning" and "wns_ns" in j["reason"], (j["state"], j["reason"])
show = subprocess.run(msbin + ["q", "show", str(j["id"])], env=env, capture_output=True, text=True).stdout
assert "results: lut=7" in show and "missing: wns_ns" in show and "stages: synth" in show, show
s = subprocess.run(msbin + ["ai", "status", "--hook"], cwd=d, env=env, capture_output=True, text=True)
assert s.returncode == 0 and "queue: 0 running" in s.stdout and f"#{j['id']} k70v_aes_c0 — warning" in s.stdout, s.stdout + s.stderr
assert "ms q add --name" in s.stdout and "::ms metric" in s.stdout and len(s.stdout) < 4000, s.stdout
for page, word in (("rules", "## 3. Log like this"), ("log", "::ms artifact PATH"), ("server", "max_cores"), ("spec", "::ms stage NAME")):
    o = subprocess.run(msbin + ["ai", page], env=env, capture_output=True, text=True).stdout
    assert word in o, (page, o[:300])
assert subprocess.run(msbin + ["ai", "nope"], env=env, capture_output=True).returncode == 2
rules = open(os.path.join(MS, "ai", "AI_RULES.md")).read()
assert len(rules) < 9000 and "tmux" in rules and "nice -n" not in rules and "msrun.py" not in rules
from mysys import capacity
g = capacity.guide_md()
assert "$MSQ_THREADS" in g and "nice -n" not in g and "or tmux" not in g and "### Rules for running" not in g, g[-1500:]
b = policy.build()
h = b["hooks"]["SessionStart"][0]["hooks"][0]
assert h["command"].endswith("bin/ms ai status --hook") and h["timeout"] <= 20, h
al = b["permissions"]["allow"]
assert "Bash(ms ai *)" in al and "Bash(ms q wait *)" in al and "Bash(ms remote usage*)" in al and not any("cancel" in x for x in al)
spec = importlib.util.spec_from_file_location("g", os.path.join(MS, "ai", "claude", "hooks", "guard.py"))
g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
H = g.HOME; PROT = [H + "/.claude", H + "/.bashrc"]
for ok in (H + "/.claude/projects/-home-x-proj/memory/feedback_notes.md", H + "/.claude/plans/a-plan.md", H + "/.claude/todos/1.json"):
    assert g.check_path(ok, PROT) is None, (ok, g.check_path(ok, PROT))
for bad in (H + "/.claude/settings.json", H + "/.claude/CLAUDE.md", H + "/.claude/projects/x/memory/../../../settings.json",
            H + "/.claude/projects/x/memory/a.sh", H + "/.bashrc"):
    assert g.check_path(bad, PROT), bad
# a valid log that says too little gets notes; a good one none
from mysys import runlog
assert runlog.quality({"status": "ok", "seconds": 3600, "metrics": {"runtime_s": 3600, "peak_rss_gb": 1}, "stages": [], "errors": 0})
assert len(runlog.quality({"status": "ok", "seconds": 3600, "metrics": {"WNS": 1}, "stages": [{"stage": "s"}], "errors": 0})) == 1
assert not runlog.quality({"status": "ok", "seconds": 3600, "metrics": {"wns_ns": 1}, "stages": [{"stage": "s"}], "errors": 0})
assert not runlog.quality({"status": "ok", "seconds": 20, "metrics": {}, "stages": [], "errors": 0})
c = subprocess.run(msbin + ["log", "check", "--strict", d], env=env, capture_output=True, text=True)
assert c.returncode == 0 and "ok " in c.stdout, c.stdout + c.stderr
print("AI-OK")
''' % str(ROOT))
    assert "AI-OK" in out, out


if __name__ == "__main__":
    t0 = time.time()
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            fn()
            print("ok", k)
    print(f"all passed in {time.time() - t0:.1f}s")
