"""Queue/flow features taken from GNU Parallel, task-spooler, Slurm, Snakemake, Nextflow, Dagu, evQueue, Jenkins, Buildbot.
Run: python3 tests/test_features.py"""
import json
import os
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


HARNESS = r'''
import json, os, sys, time, glob
MS = os.environ["MY_SYSTEM"]
for d in ("lib", "plugins", "runlog"):
    sys.path.insert(0, os.path.join(MS, d))
from mysys import probes, queue, flow, expr, issues, resources, runlog
G = 2**30
probes.threads = lambda: 8
probes.loadavg = lambda: (0.0, 0.0, 0.0)
probes.PSI_DIR = "/nonexistent"              # real memory/disk pressure must not hold back test jobs
probes.meminfo = lambda: {"MemTotal": 64 * G, "MemAvailable": 48 * G, "SwapTotal": 0, "SwapFree": 0}
S = queue.Scheduler()
def until(cond, limit=60):
    t0 = time.time()
    while time.time() - t0 < limit:
        S.tick()
        if cond():
            return True
        time.sleep(0.3)
    return False
def st(i):
    return queue.load(i)["state"]
W = os.environ["WORK"]
small = dict(mem_gb=0.2, cores=1)
'''


def fake_ms():
    tmp = Path(tempfile.mkdtemp())
    ms = tmp / "My_System"
    for d in ("bin", "lib", "plugins", "runlog"):
        _ct(SRC / d, ms / d)
    for d in ("config", "state", "reports"):
        (ms / d).mkdir()
    (ms / "config/plugins.conf").write_text("vivado\n")
    (ms / "config/my_system.conf").write_text('PHONE_REPORT_TIMES=""\nPHONE_REQUESTS=0\n')   # no clock-dependent reports
    (ms / "bin/notify.sh").write_text(f'#!/bin/sh\necho "$1|$2" >> {tmp}/notes\n')
    (ms / "bin/notify.sh").chmod(0o755)
    (tmp / "work").mkdir()
    return tmp, ms


def py(code, extra_conf=""):
    tmp, ms = fake_ms()
    if extra_conf:
        (ms / "config/my_system.conf").write_text(extra_conf)
    env = dict(os.environ, MY_SYSTEM=str(ms), MS_NO_CGROUP="1", HOME=str(tmp), WORK=str(tmp / "work"))
    r = subprocess.run([sys.executable, "-c", HARNESS + code], env=env, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    notes = (tmp / "notes").read_text() if (tmp / "notes").exists() else ""
    return r.stdout, notes, tmp


def test_expr():
    sys.path.insert(0, str(SRC / "lib"))
    from mysys import expr
    assert expr.evaluate("1 < 2 and not (3 % 2 == 0)") is True
    assert expr.evaluate("'a' in ('a', 'b')") is True
    assert expr.evaluate("max(1, 5) * 2") == 10
    for bad in ("__import__('os')", "open('x')", "(1).__class__", "[x for x in (1,)]"):
        try:
            expr.evaluate(bad)
            raise AssertionError(bad)
        except expr.ExprError:
            pass


def test_issues_and_kb():
    sys.path.insert(0, str(SRC / "lib"))
    from mysys import issues
    log = ("CRITICAL WARNING: [Timing 38-282] The design failed to meet the timing requirements.\n"
           "WARNING: [Synth 8-327] inferring latch for variable 'x_reg' [/a/b/top.v:12]\n"
           "WARNING: [Synth 8-327] inferring latch for variable 'x_reg' [/a/b/top.v:40]\n"
           "src/x.c:10:5: warning: unused variable 'q' [-Wunused-variable]\n")
    cur = issues.parse_text(log, sup=[])
    assert issues.summarize(cur) == {"critical": 1, "warning": 2, "error": 0}, cur   # latch (x2) + gcc
    latch = [i for i in cur if i["id"] == "Synth 8-327"][0]
    assert latch["count"] == 2                      # same warning at another line -> one fingerprint
    ref = issues.parse_text("CRITICAL WARNING: [Timing 38-282] The design failed to meet the timing requirements.\n", sup=[])
    cmp = issues.compare(cur, ref)
    assert len(cmp["new"]) == 2 and len(cmp["outstanding"]) == 1 and not cmp["fixed"]
    ok, msgs = issues.gate(cmp, cur, {"new_critical": 0, "new_warning": 0})
    assert not ok and "new warning" in msgs[0]
    d = issues.diagnose("blah\nERROR: [Common 17-345] A valid license was not found for feature 'Synthesis'\n")
    assert d and d["name"] == "License not available"
    assert issues.diagnose("all fine") is None
    tmp = Path(tempfile.mkdtemp())
    (tmp / "r.xml").write_text("<testsuite><testcase classname='a' name='t1'/><testcase classname='a' name='t2'>"
                               "<failure>x</failure></testcase><testcase name='t3'><skipped/></testcase></testsuite>")
    ju = issues.junit(["*.xml"], str(tmp))
    assert ju["tests_total"] == 3 and ju["tests_failed"] == 1 and ju["tests_skipped"] == 1 and ju["failing"] == ["a.t2"]


def test_resources_caps_slots_reservations():
    out, notes, tmp = py(r'''
open(os.path.join(MS, "config", "resources.conf"), "w").write("lic 2\nboard 1\ntool:vivado 1\n")
a = queue.submit("sleep 2", cwd=W, name="a", resources={"lic": 1, "board": 1}, **small)
b = queue.submit("sleep 2", cwd=W, name="b", resources={"board": 1}, **small)
c = queue.submit("echo vivado-ish", cwd=W, name="c", tool="vivado", **small)
d = queue.submit("echo also vivado", cwd=W, name="d", tool="vivado", **small)
e = queue.submit("echo x", cwd=W, name="e", resources={"nosuch": 1}, **small)
S.tick(); time.sleep(0.5); S.tick()
assert st(a["id"]) == "running" and st(b["id"]) == "queued" and "board" in queue.load(b["id"])["reason"], (st(b["id"]), queue.load(b["id"])["reason"])
assert "unknown resource" in queue.load(e["id"])["reason"]
assert until(lambda: all(st(x["id"]) in queue.TERMINAL for x in (a, b, c, d)), 60)
jc, jd = queue.load(c["id"]), queue.load(d["id"])
assert jc["ended"] <= jd["started"] or jd["ended"] <= jc["started"], "tool cap 1 overlapped"
slots = {queue.load(x["id"]).get("slot") for x in (a, c)}
assert slots <= {1, 2}
queue.cancel(e["id"])
# reservations: a window now with no_new_jobs blocks starts
days = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][time.localtime().tm_wday]
open(os.path.join(MS, "config", "reservations.conf"), "w").write(f"{days} 00:00-23:59 no_new_jobs\n")
f = queue.submit("echo blocked", cwd=W, name="f", **small)
S.tick()
assert st(f["id"]) == "queued" and "reserved" in queue.load(f["id"])["reason"]
os.remove(os.path.join(MS, "config", "reservations.conf"))
assert until(lambda: st(f["id"]) == "done")
print("ok")
''')
    assert "ok" in out


def test_retry_backoff_codes_scale_and_controls():
    out, notes, tmp = py(r'''
# retry only for exit code 7, delays from a schedule, timeout scaled on retry
a = queue.submit("test -f f1 || (touch f1; exit 7)", cwd=W, name="a", retries=2, retry_on=["fail"], retry_exit_codes=[7],
                 retry_delays=["1s", "5s"], **small)
b = queue.submit("exit 9", cwd=W, name="b", retries=2, retry_on=["fail"], retry_exit_codes=[7], **small)
assert until(lambda: st(a["id"]) == "done" and st(b["id"]) == "failed", 60)
assert queue.load(a["id"])["attempt"] == 2 and queue.load(b["id"])["attempt"] == 1
# exit-code mapping and warn patterns -> warning state (counts as success for dependents)
c = queue.submit("echo 'Note: slow path'; exit 1", cwd=W, name="c", exit_codes={"1": "warning"}, **small)
d = queue.submit("echo after", cwd=W, name="d", depends=[c["id"]], **small)
e = queue.submit("echo 'Timing violation found'", cwd=W, name="e", warn_patterns=["Timing violation"], **small)
assert until(lambda: st(d["id"]) == "done" and st(e["id"]) in queue.TERMINAL, 60)
assert st(c["id"]) == "warning" and st(e["id"]) == "warning"
# after-fail dependency (Slurm afternotok)
f = queue.submit("exit 1", cwd=W, name="f", **small)
g = queue.submit("echo cleanup", cwd=W, name="g", depends=[f["id"]], dep_mode="notok", **small)
h = queue.submit("echo ok", cwd=W, name="h", **small)
i = queue.submit("echo not needed", cwd=W, name="i", depends=[h["id"]], dep_mode="notok", **small)
assert until(lambda: all(st(x["id"]) in queue.TERMINAL for x in (g, i)), 60)
assert st(g["id"]) == "done" and st(i["id"]) == "skipped"
# approval, begin, deadline, collapse, top
k = queue.submit("echo approved", cwd=W, name="k", approval="flash the board?", **small)
assert st(k["id"]) == "approval"
S.tick(); assert st(k["id"]) == "approval"
queue.approve(k["id"])
l = queue.submit("echo later", cwd=W, name="l", begin="+1h", **small)
m = queue.submit("echo too late", cwd=W, name="m", deadline=time.time() - 5, **small)
n1 = queue.submit("echo old", cwd=W, name="n1", collapse_key="synth-main", state="held", **small)
n2 = queue.submit("echo new", cwd=W, name="n2", collapse_key="synth-main", **small)
assert st(n1["id"]) == "cancelled" and "replaced" in queue.load(n1["id"])["reason"]
assert until(lambda: st(k["id"]) == "done" and st(m["id"]) == "skipped", 30)
assert st(l["id"]) == "queued"
queue.top(l["id"]); assert queue.load(l["id"])["order"] >= 1
queue.cancel(l["id"])
# clone and wait
cl = queue.clone(a["id"])
assert cl["cmd"] == queue.load(a["id"])["cmd"] and cl["id"] != a["id"]
queue.cancel(cl["id"])
# clamp: asking for more RAM than the machine has
big = queue.submit("echo", cwd=W, name="big", mem_gb=500, cores=64)
assert big.get("clamped") and big["mem_gb"] < 64 and big["cores"] <= 8
queue.cancel(big["id"])
print("ok")
''')
    assert "ok" in out
    assert "Approval needed" in notes


def test_runner_features():
    out, notes, tmp = py(r'''
# repeat until, benchmark, wait step, follow, signal before timeout, assert, ensure, protect, publish, junit
open(os.path.join(W, "cnt"), "w").write("0")
r = queue.submit("n=$(cat cnt); echo $((n+1)) > cnt; test $((n+1)) -ge 3", cwd=W, name="rep",
                 repeat={"until_exit": 0, "interval": 0.2, "limit": 5}, **small)
bm = queue.submit("sleep 0.2", cwd=W, name="bench", benchmark=3, **small)
wt = queue.submit("true", cwd=W, name="waitf", wait={"file": "flag.txt", "interval": 0.5, "timeout": "20s"}, **small)
fo = queue.submit("echo 'CRITICAL WARNING: [Route 35-1] in tool log' >> tool.log; sleep 7", cwd=W, name="follow",
                  follow=["tool.log"], **small)
sg = queue.submit("trap 'echo got-usr1 >> sig.txt' USR1; for i in 1 2 3 4 5 6 7 8 9 10; do sleep 1; done", cwd=W, name="sig",
                  timeout_s=8, signal_before="USR1@5", kill_seq="TERM,1,KILL", **small)
asr = queue.submit("echo lut=40000 >> $MSQ_OUTPUTS", cwd=W, name="assert", **{"assert": "lut < 30000"}, **small)
en = queue.submit("touch empty.out", cwd=W, name="ensure", outputs=["empty.out"], ensure={"non_empty": ["empty.out"]}, **small)
pr = queue.submit("echo data > keep.bin; mkdir -p rep; echo '<testsuite><testcase name=\"a\"/><testcase name=\"b\"><failure/></testcase></testsuite>' > rep/t.xml",
                  cwd=W, name="pub", outputs=["keep.bin"], protect=True, publish={"dir": W + "/results", "mode": "copy"},
                  params={"seed": 4}, junit=["rep/*.xml"], **small)
time.sleep(1.5)
open(os.path.join(W, "flag.txt"), "w").write("x")
ids = [x["id"] for x in (r, bm, wt, fo, sg, asr, en, pr)]
assert until(lambda: all(st(i) in queue.TERMINAL for i in ids), 90), [(st(i), queue.load(i)["reason"]) for i in ids]
assert st(r["id"]) == "done" and open(W + "/cnt").read().strip() == "3"
mets = {x["name"]: x["metrics"] for x in runlog.read(W + "/msrun.jsonl").values()}
assert "bench_mean_s" in mets["bench"] and "bench_3_s" in mets["bench"]
assert st(wt["id"]) == "done"
assert "[tool.log] CRITICAL WARNING" in open(queue.LOGS / f"{fo['id']}.log").read()
assert queue.load(fo["id"])["issues"]["critical"] == 1
assert st(sg["id"]) == "timeout" and "got-usr1" in open(W + "/sig.txt").read()
assert st(asr["id"]) == "failed" and "assertion failed" in queue.load(asr["id"])["reason"]
assert st(en["id"]) == "failed" and "empty" in queue.load(en["id"])["reason"]
jp = queue.load(pr["id"])
assert jp["state"] == "warning" and "test(s) failed" in jp["reason"], jp["reason"]
assert not os.access(W + "/keep.bin", os.W_OK) or os.geteuid() == 0
assert os.path.exists(W + "/results/seed=4/keep.bin")
assert mets["pub"]["tests_failed"] == 1
assert os.path.exists(queue.Q / "series" / f"{sg['id']}.csv")
# why: unchanged job is up to date, changed input explains itself
j = queue.submit("cp in.txt out.txt", cwd=W, name="why", inputs=["in.txt"], outputs=["out.txt"], **small)
open(W + "/in.txt", "w").write("1")
assert until(lambda: st(j["id"]) == "done")
assert queue.why_rerun(queue.load(j["id"])) == []
time.sleep(1.1); open(W + "/in.txt", "w").write("22")
assert any("inputs changed" in x for x in queue.why_rerun(queue.load(j["id"])))
print("ok")
''')
    assert "ok" in out


def test_flow_features():
    out, notes, tmp = py(r'''
base = os.path.join(W, "f")
os.makedirs(base, exist_ok=True)
open(os.path.join(base, "pts.csv"), "w").write("a,b\n1,x\n2,y\n3,z\n")
F = os.path.join(base, "f.yaml")
open(F, "w").write("""
name: feat
cwd: %s
params: {k: 2}
params_schema: {k: {type: int, min: 1, max: 5}}
profiles: {fast: {params: {k: 3}}}
defaults: {mem_gb: 0.2, cores: 1}
halt: {fail: 2, mode: soon}
notify_mode: always
on_success: "touch hook_success"
on_exit: "touch hook_exit"
steps:
  - name: gen
    cmd: "echo files=alpha,beta,gamma >> $MSQ_OUTPUTS"
  - name: each
    depends: [gen]
    foreach: "{{steps.gen.outputs.files}}"
    folder: "each_{{item}}"
    cmd: "echo {{item}} > item.txt"
  - name: grid
    matrix: {x: [1, 2, 3], y: [a, b]}
    matrix_exclude: [{x: 2, y: b}]
    matrix_filter: "{{x}} <= {{k}}"
    cmd: "echo {{x}}{{y}}"
  - name: pairs
    matrix: {p: [1, 2], q: [u, v]}
    matrix_mode: zip
    cmd: "echo {{p}}{{q}}"
  - name: table
    matrix_csv: pts.csv
    cmd: "echo {{a}}-{{b}}"
  - name: child
    depends: [table]
    flow: sub.yaml
    with: {msg: hello}
""" % base)
open(os.path.join(base, "sub.yaml"), "w").write("""
name: sub
cwd: %s/sub
params: {msg: none}
steps:
  - {name: s1, cmd: "echo {{msg}} > msg.txt"}
""" % base)
f, specs, errs = flow.check(F)
assert not errs, errs
names = sorted(s["name"] for s in specs if s["step"] == "grid")
assert names == ["grid_1_a", "grid_1_b", "grid_2_a"], names
assert sorted(s["name"] for s in specs if s["step"] == "pairs") == ["pairs_1_u", "pairs_2_v"]
assert len([s for s in specs if s["step"] == "table"]) == 3
f2, specs2, errs2 = flow.check(F, profile="fast")
assert len([s for s in specs2 if s["step"] == "grid"]) == 5
_, _, bad = flow.check(F, overrides={"k": 9})
assert any("max" in e for e in bad), bad
assert [s["step"] for s in flow.select(specs, until=["each"])] == ["gen", "each"]
assert {s["step"] for s in flow.select(specs, from_=["table"])} == {"table", "child"}
rec, _ = flow.submit_flow(F)
assert until(lambda: flow.load_flow(rec["id"]).get("notified"), 120), [(j["name"], j["state"], j["reason"]) for j in flow.flow_jobs(rec)]
js = flow.flow_jobs(flow.load_flow(rec["id"]))
each = [j for j in js if (j.get("flow") or {}).get("step") == "each" and not j.get("fanout")]
assert sorted(open(os.path.join(j["cwd"], "item.txt")).read().strip() for j in each) == ["alpha", "beta", "gamma"]
assert open(os.path.join(base, "sub", "msg.txt")).read().strip() == "hello"
assert os.path.exists(os.path.join(base, "hook_success")) and os.path.exists(os.path.join(base, "hook_exit"))
assert flow.flow_state(flow.load_flow(rec["id"]))[2]
html = flow.report_html(rec["id"])
assert "<svg" in html and "Timeline" in html
# lint, summary, touch, untracked
assert any("timeout" in a for a in flow.lint(F))
open(os.path.join(base, "stray.bin"), "w").write("x")
assert any(p.endswith("stray.bin") for p in flow.untracked(F))
print("ok")
''')
    assert "ok" in out
    assert "Flow finished: feat" in notes


def test_flow_errors_halt_notify_cache():
    out, notes, tmp = py(r'''
base = os.path.join(W, "g")
os.makedirs(base, exist_ok=True)
F = os.path.join(base, "g.yaml")
def write(fail):
    open(F, "w").write("""
name: gate
cwd: %s
defaults: {mem_gb: 0.2, cores: 1}
notify_mode: problem
on_fixed: "touch fixed_hook"
on_regression: "touch regression_hook"
steps:
  - name: a
    cmd: "%s"
  - name: b
    depends: [a]
    cmd: "echo b"
""" % (base, "exit 1" if fail else "echo ok"))
write(False)
r1, _ = flow.submit_flow(F); assert until(lambda: flow.load_flow(r1["id"]).get("notified"))
write(True)
r2, _ = flow.submit_flow(F); assert until(lambda: flow.load_flow(r2["id"]).get("notified"))
assert os.path.exists(base + "/regression_hook")
write(False)
r3, _ = flow.submit_flow(F); assert until(lambda: flow.load_flow(r3["id"]).get("notified"))
assert os.path.exists(base + "/fixed_hook")
# on_error: terminate stops the rest; ignore lets dependents run
H = os.path.join(base, "h.yaml")
open(H, "w").write("""
name: err
cwd: %s
defaults: {mem_gb: 0.2, cores: 1}
steps:
  - {name: bad, cmd: "exit 1", on_error: ignore}
  - {name: after, depends: [bad], cmd: "echo ran > after.txt"}
""" % base)
r4, _ = flow.submit_flow(H); assert until(lambda: flow.load_flow(r4["id"]).get("notified"))
assert os.path.exists(base + "/after.txt")
# halt after 2 failures: the rest is skipped
K = os.path.join(base, "k.yaml")
open(K, "w").write("""
name: halting
cwd: %s
defaults: {mem_gb: 0.2, cores: 1}
halt: {fail: 2, mode: soon}
steps:
  - {name: f, matrix: {i: [1, 2, 3, 4, 5, 6, 7, 8]}, cmd: "sleep 0.5; exit 1", max_parallel: 2}
""" % base)
r5, _ = flow.submit_flow(K); assert until(lambda: flow.load_flow(r5["id"]).get("notified"), 90)
states = [j["state"] for j in flow.flow_jobs(r5)]
assert states.count("skipped") >= 4 and flow.load_flow(r5["id"]).get("halted"), states
# shared cache between flows
C = os.path.join(base, "c.yaml")
for nm, folder in (("c1", "one"), ("c2", "two")):
    open(C, "w").write("""
name: %s
cwd: %s
defaults: {mem_gb: 0.2, cores: 1}
steps:
  - {name: ip, folder: %s, cmd: "echo built > ip.dcp; echo run >> runs.txt", outputs: [ip.dcp], cache: shared}
""" % (nm, base, folder))
    r, _ = flow.submit_flow(C); assert until(lambda: flow.load_flow(r["id"]).get("notified"))
j2 = flow.flow_jobs(r)[0]
assert j2["state"] == "cached" and "shared cache" in j2["reason"] and open(base + "/two/ip.dcp").read().strip() == "built"
assert not os.path.exists(base + "/two/runs.txt")
print("ok")
''', extra_conf='SHARED_CACHE_DIR="%s"\n' % tempfile.mkdtemp(prefix="ms-test-cache-"))
    assert "ok" in out
    assert "Flow FAILED: gate" in notes and "back to OK" in notes, notes


def test_schedules():
    sys.path.insert(0, str(SRC / "lib"))
    out, notes, tmp = py(r'''
import subprocess
base = os.path.join(W, "s")
os.makedirs(base, exist_ok=True)
F = os.path.join(base, "s.yaml")
open(F, "w").write("name: sch\ncwd: %s\ndefaults: {mem_gb: 0.2, cores: 1}\nsteps:\n  - {name: a, cmd: 'echo hi'}\n" % base)
# catch-up: a cron time that passed while the scheduler was not running fires once
e = flow.add_schedule(F, cron="*/1 * * * *", catchup_h=1)
sc = flow.schedules(); sc[0]["last_check"] = time.time() - 600; flow.SCHED.write_text(json.dumps(sc))
flow.run_schedules(S)
assert len([r for r in flow.flows() if r.get("schedule") == e["id"]]) == 1
# after another flow
A = os.path.join(base, "a.yaml")
open(A, "w").write("name: upstream\ncwd: %s\ndefaults: {mem_gb: 0.2, cores: 1}\nsteps:\n  - {name: a, cmd: 'echo up'}\n" % base)
e2 = flow.add_schedule(F, after_flow="upstream")
r, _ = flow.submit_flow(A); assert until(lambda: flow.load_flow(r["id"]).get("notified"))
flow.run_schedules(S)
assert any(x.get("schedule") == e2["id"] for x in flow.flows())
# git commit with debounce and important paths only
repo = os.path.join(W, "repo"); os.makedirs(repo)
g = lambda *a: subprocess.run(["git", "-C", repo, *a], capture_output=True, check=True)
g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
open(repo + "/README", "w").write("x"); g("add", "."); g("commit", "-qm", "1")
e3 = flow.add_schedule(F, git=repo, paths=["rtl/**"])
open(repo + "/README", "w").write("y"); g("commit", "-qam", "docs only")
sc = flow.schedules(); [s["git"].update(last_poll=0) for s in sc if s["id"] == e3["id"]]; flow.SCHED.write_text(json.dumps(sc))
flow.run_schedules(S)
n_before = len([x for x in flow.flows() if x.get("schedule") == e3["id"]])
os.makedirs(repo + "/rtl"); open(repo + "/rtl/top.v", "w").write("m"); g("add", "."); g("commit", "-qm", "rtl")
sc = flow.schedules(); [s["git"].update(last_poll=0) for s in sc if s["id"] == e3["id"]]; flow.SCHED.write_text(json.dumps(sc))
flow.run_schedules(S)
n_after = len([x for x in flow.flows() if x.get("schedule") == e3["id"]])
assert n_before == 0 and n_after == 1, (n_before, n_after)
flow.pause_schedule(e["id"])
assert flow.schedules()[0]["paused"]
print("ok")
''')
    assert "ok" in out


def test_software_uses():
    tmp = Path(tempfile.mkdtemp())
    proj = tmp / "home" / "proj"
    proj.mkdir(parents=True)
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(proj / ".venv")], check=True)
    (proj / "a.py").write_text("import json\nimport mylib\n")
    (proj / "run.tcl").write_text("# runs with vivado 2025.2.1\n")
    ms = tmp / "ms"
    for d in ("state", "config", "reports"):
        (ms / d).mkdir(parents=True)
    (ms / "state/detected.json").write_text(json.dumps({"projects": [str(proj)], "venvs": [],
                                                        "tools": [["vivado_2025.2.1", "/mnt/data/Apps/2025.2.1"]]}))
    code = f"""
import sys, json; sys.path.insert(0, {str(SRC / 'lib')!r})
from mysys import software as S
d = S.scan(outdated=False, ldd=False)
p = d['projects'][{str(proj)!r}]
assert p['venvs'] == [{str(proj / '.venv')!r}], p
assert p['tools']['vivado'] == 'vivado_2025.2.1', p['tools']
assert 'mylib' in p['missing_imports'] and 'json' not in p['imports']
assert any('vivado' in x for x in S.uses(d, 'vivado'))
print(S.summary_text(d).splitlines()[0])
"""
    r = subprocess.run([sys.executable, "-c", code], env=dict(os.environ, MY_SYSTEM=str(ms)), capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr


if __name__ == "__main__":
    t0 = time.time()
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            fn()
            print("ok", k, flush=True)
    print(f"all passed in {time.time() - t0:.1f}s")
