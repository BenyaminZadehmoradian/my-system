"""Job queue and flows. Run: python3 tests/test_queue.py"""
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
import json, os, sys, time
MS = os.environ["MY_SYSTEM"]
sys.path.insert(0, MS + "/lib"); sys.path.insert(0, MS + "/plugins"); sys.path.insert(0, MS + "/runlog")
from mysys import probes, queue, flow
G = 2**30
AVAIL = [64 * G]
probes.threads = lambda: 8
probes.loadavg = lambda: (0.0, 0.0, 0.0)
probes.PSI_DIR = "/nonexistent"              # real memory/disk pressure must not hold back test jobs
probes.meminfo = lambda: {"MemTotal": 96 * G, "MemAvailable": AVAIL[0], "SwapTotal": 0, "SwapFree": 0}
S = queue.Scheduler()
def run_until(cond, limit=60):
    t0 = time.time()
    while time.time() - t0 < limit:
        S.tick()
        if cond():
            return True
        time.sleep(0.3)
    return False
def st(i):
    return queue.load(i)["state"]
'''


def fake_ms():
    tmp = Path(tempfile.mkdtemp())
    ms = tmp / "My_System"
    for d in ("bin", "lib", "plugins", "runlog"):
        _ct(SRC / d, ms / d)
    for d in ("config", "state", "reports"):
        (ms / d).mkdir()
    (ms / "config/protected_paths.txt").write_text("/etc\n")
    (ms / "config/plugins.conf").write_text("vivado\n")
    (ms / "bin/notify.sh").write_text(f'#!/bin/sh\necho "$1|$2" >> {tmp}/notes\n')
    (ms / "bin/notify.sh").chmod(0o755)
    return tmp, ms


def py(ms, code, tmp):
    env = dict(os.environ, MY_SYSTEM=str(ms), MS_NO_CGROUP="1", HOME=str(tmp))
    r = subprocess.run([sys.executable, "-c", HARNESS + code], env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def test_jobs_deps_retry_timeout():
    tmp, ms = fake_ms()
    w = tmp / "work"
    w.mkdir()
    py(ms, f'''
W = {str(w)!r}
a = queue.submit("echo hello; sleep 1", cwd=W, name="a", mem_gb=1, cores=1)
b = queue.submit("exit 3", cwd=W, name="b", mem_gb=1, cores=1)
c = queue.submit("echo never", cwd=W, name="c", depends=[b["id"]], mem_gb=1, cores=1)
d = queue.submit("echo after-any", cwd=W, name="d", depends=[b["id"]], dep_mode="any", mem_gb=1, cores=1)
# fails once, then succeeds -> retried
e = queue.submit("test -f flag || (touch flag; exit 1)", cwd=W, name="e", retries=1, retry_on=["fail"], mem_gb=1, cores=1)
f = queue.submit("sleep 30", cwd=W, name="f", timeout_s=2, mem_gb=1, cores=1)
ids = [x["id"] for x in (a, b, c, d, e, f)]
assert run_until(lambda: all(st(i) in queue.TERMINAL and not (st(i) in ("failed", "timeout") and not queue.load(i).get("retried")) for i in ids), 120), [queue.load(i) for i in ids]
assert st(a["id"]) == "done" and "hello" in (queue.LOGS / f"{{a['id']}}.log").read_text()
assert st(b["id"]) == "failed" and queue.load(b["id"])["rc"] == 3
assert st(c["id"]) == "skipped" and "dependency" in queue.load(c["id"])["reason"]
assert st(d["id"]) == "done"
je = queue.load(e["id"]); assert je["state"] == "done" and je["attempt"] == 2, je
assert st(f["id"]) == "timeout", queue.load(f["id"])
# run log written for every job, with runtime metric and provenance
from mysys import runlog
runs = runlog.read(W + "/msrun.jsonl")
assert len(runs) >= 6 and all("runtime_s" in r["metrics"] for r in runs.values() if r["status"] != "running")
first = open(W + "/msrun.jsonl").readline()
assert '"x_provenance"' in first
assert runlog.validate(W + "/msrun.jsonl") == []
''', tmp)
    notes = (tmp / "notes").read_text()
    assert "Job failed: b" in notes and "Job failed: f" in notes


def test_capacity_gate_and_suspend():
    tmp, ms = fake_ms()
    w = tmp / "w2"
    w.mkdir()
    py(ms, f'''
W = {str(w)!r}
big = queue.submit("sleep 20", cwd=W, name="big", mem_gb=80, cores=1)       # does not fit in 64 GB - reserve
S.tick(); time.sleep(0.5); S.tick()
j = queue.load(big["id"]); assert j["state"] == "queued" and "RAM" in j["reason"], j
queue.cancel(big["id"])
x = queue.submit("sleep 30", cwd=W, name="x", mem_gb=1, cores=1)
y = queue.submit("sleep 30", cwd=W, name="y", mem_gb=1, cores=1, priority=5)
assert run_until(lambda: st(x["id"]) == "running" and st(y["id"]) == "running", 20)
AVAIL[0] = 2 * G                                   # RAM almost gone -> newest low-priority job paused
S.last_action = 0
S.tick()
assert st(x["id"]) == "suspended" and st(y["id"]) == "running", (st(x["id"]), st(y["id"]))
AVAIL[0] = 64 * G
S.last_action = 0
S.tick()
assert st(x["id"]) == "running"
for i in (x["id"], y["id"]):
    queue.cancel(i)
assert run_until(lambda: st(x["id"]) == "cancelled" and st(y["id"]) == "cancelled", 30)
''', tmp)


def test_flow():
    tmp, ms = fake_ms()
    base = tmp / "proj"
    (base / "rtl").mkdir(parents=True)
    (base / "rtl/top.v").write_text("module top; endmodule\n")
    fl = base / "sweep.yaml"
    fl.write_text("""
name: demo
cwd: %s
defaults: {mem_gb: 1, cores: 1}
params: {part: xc7}
steps:
  - name: synth
    matrix: {seed: [1, 2], mode: [a, b]}
    folder: "doe_{{seed}}_{{mode}}"
    cmd: "echo synth {{part}} {{seed}} {{mode}} > synth.out; mkdir -p .Xil; touch junk.jou"
    inputs: ["../rtl/*.v"]
    outputs: ["synth.out"]
  - name: impl
    matrix: synth
    depends: [synth]
    folder: "doe_{{seed}}_{{mode}}"
    cmd: "test -f synth.out && echo impl > impl.out"
    temp: [".Xil", "*.jou"]
  - name: collect
    depends: [impl]
    cmd: "ls doe_*/impl.out | wc -l > count.txt"
on_success: "touch hook_ok"
""" % base)
    out = py(ms, f'''
F = {str(fl)!r}
f, specs, errs = flow.check(F)
assert not errs, errs
assert len(specs) == 9 and sum(1 for s in specs if s["step"] == "impl" and s["per_item"]) == 4
plan, total, peak = flow.estimate(specs)
assert peak >= 4 and total > 0
rec, _ = flow.submit_flow(F)
assert run_until(lambda: flow.flow_state(json.load(open(flow.FLOWS / (rec["id"] + ".json"))))[1] and
                 json.load(open(flow.FLOWS / (rec["id"] + ".json"))).get("notified"), 120)
cnt, done, ok = flow.flow_state(rec)
assert ok, cnt
B = {str(base)!r}
assert open(B + "/count.txt").read().strip() == "4"
assert os.path.exists(B + "/hook_ok")
assert not os.path.exists(B + "/doe_1_a/.Xil") and not os.path.exists(B + "/doe_1_a/junk.jou")
assert open(B + "/doe_2_b/synth.out").read().strip() == "synth xc7 2 b"
# impl of item 1 depended only on synth of item 1
js = flow.flow_jobs(rec)
impl0 = [j for j in js if j["flow"]["step"] == "impl"][0]
syn = [j for j in js if j["flow"]["step"] == "synth"]
assert impl0["depends"] == [syn[0]["id"]], (impl0["depends"], [s["id"] for s in syn])
# second run: synth is up to date (same inputs, outputs exist) -> cached; resume skips the rest
rec2, _ = flow.submit_flow(F, resume=True)
states = sorted(set(j["state"] for j in flow.flow_jobs(rec2)))
assert states == ["cached"], states
# changing an input reruns synth
time.sleep(1.1)
open(B + "/rtl/top.v", "a").write("// change\\n")
rec3, _ = flow.submit_flow(F)
S.tick()
assert any(j["state"] != "cached" for j in flow.flow_jobs(rec3) if j["flow"]["step"] == "synth")
for j in flow.flow_jobs(rec3):
    queue.cancel(j["id"])
print("ok")
''', tmp)
    assert "ok" in out
    assert "Flow finished: demo" in (tmp / "notes").read_text()


def test_flow_validation_and_cron():
    sys.path.insert(0, str(SRC / "lib"))
    from mysys import flow
    bad = {"name": "x", "cwd": "/tmp", "bogus": 1, "steps": [
        {"name": "a", "cmd": "echo {{nope}}", "depends": ["b"]},
        {"name": "b", "cmd": "echo", "depends": ["a"], "colour": "red"}]}
    _, errs = flow.expand(bad)
    txt = " | ".join(errs)
    assert "unknown top-level key 'bogus'" in txt and "unknown key 'colour'" in txt and "cycle" in txt, txt
    _, errs = flow.expand({"name": "x", "cwd": "/tmp", "steps": [{"name": "a", "cmd": "echo {{nope}}"}]})
    assert any("unknown placeholder {{nope}}" in e for e in errs), errs
    # Tcl braces survive
    jobs, errs = flow.expand({"name": "x", "cwd": "/tmp", "params": {"n": 3},
                              "steps": [{"name": "a", "cmd": "vivado -tclargs {a b} {{n}}"}]})
    assert not errs and jobs[0]["cmd"] == "vivado -tclargs {a b} 3"
    t = time.mktime((2026, 10, 31, 2, 17, 0, 0, 0, -1))      # Saturday 02:17
    assert flow.cron_match("17 2 * * *", t) and not flow.cron_match("18 2 * * *", t)
    assert flow.cron_match("*/17 2 * * 6", t) and flow.cron_match("0-20 1-3 31 10 6", t)
    assert not flow.cron_match("17 2 * * 1-5", t)


def test_mcp():
    tmp, ms = fake_ms()
    env = dict(os.environ, MY_SYSTEM=str(ms), HOME=str(tmp), MS_NO_CGROUP="1")
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "queue_submit",
                                                                           "arguments": {"cmd": "echo hi", "cwd": "/etc"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "queue_submit",
                                                                           "arguments": {"cmd": "echo hi", "cwd": str(tmp / "p")}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "queue_list", "arguments": {}}}]
    r = subprocess.run([sys.executable, str(ms / "bin/ms"), "mcp"], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                       env=env, capture_output=True, text=True, timeout=60)
    out = [json.loads(x) for x in r.stdout.splitlines()]
    assert [o["id"] for o in out] == [1, 2, 3, 4, 5], r.stdout + r.stderr
    assert out[0]["result"]["serverInfo"]["name"] == "my-system"
    names = {t["name"] for t in out[1]["result"]["tools"]}
    assert {"capacity", "queue_submit", "runs_list", "flow_run"} <= names
    assert out[2]["result"].get("isError") and "cwd must be inside" in out[2]["result"]["content"][0]["text"]
    assert "queued job" in out[3]["result"]["content"][0]["text"]
    assert '"state": "queued"' in out[4]["result"]["content"][0]["text"]


def test_cli():
    tmp, ms = fake_ms()
    env = dict(os.environ, MY_SYSTEM=str(ms), HOME=str(tmp), MS_NO_CGROUP="1")
    (tmp / "work").mkdir(exist_ok=True)

    def ms_(*a):
        # a project folder: ms q add refuses jobs that would run in the home folder (1.19)
        return subprocess.run([sys.executable, str(ms / "bin/ms"), *a], cwd=tmp / "work", env=env, capture_output=True, text=True)
    r = ms_("q", "add", "--name", "t1", "--mem", "1", "--cores", "1", "--priority", "3", "--", "echo", "a b")
    assert r.returncode == 0 and r.stdout.strip() == "1", r.stdout + r.stderr
    j = json.loads((ms / "state/queue/jobs/1.json").read_text())
    assert j["cmd"] == "echo 'a b'" and j["priority"] == 3 and j["mem_gb"] == 1.0
    assert "t1" in ms_("q").stdout
    assert ms_("q", "hold", "1").returncode == 0 and json.loads((ms / "state/queue/jobs/1.json").read_text())["state"] == "held"
    assert ms_("q", "release", "1").returncode == 0
    assert ms_("q", "cancel", "1").returncode == 0
    ex = ms_("flow", "example").stdout
    (tmp / "ex.yaml").write_text(ex)
    chk = ms_("flow", "check", str(tmp / "ex.yaml"))
    assert chk.returncode == 0 and "12 job(s)" in chk.stdout, chk.stdout
    assert "scheduler: stopped" in ms_("q", "status").stdout


if __name__ == "__main__":
    t0 = time.time()
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            fn()
            print("ok", k)
    print(f"all passed in {time.time() - t0:.1f}s")
