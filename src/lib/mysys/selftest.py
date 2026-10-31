"""`ms selftest` — check that my-system works on this machine without disturbing anything.

Safety:
  * Every test job runs in a SEPARATE, temporary my-system (own queue, own state, own config) under a
    scratch folder; the real queue, its jobs and your running simulations are never touched or signalled.
  * Test jobs are tiny (echo/sleep, at most ~400 MB RAM for one memory-limit test), run with nice 19 and
    ionice idle, one or two at a time. The whole test takes 2–5 minutes (more while the machine is busy).
  * Checks of the real installation only read (scheduler alive, hardware data age, MCP registration …).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import config

HARNESS = r'''
import json, os, sys, time, glob
MS = os.environ["MY_SYSTEM"]
for d in ("lib", "plugins", "runlog"):
    sys.path.insert(0, os.path.join(MS, d))
from mysys import probes, queue, flow, runlog, lineage
probes.loadavg = lambda: (0.0, 0.0, 0.0)      # test jobs are tiny: ignore the real machine's load here
probes.PSI_DIR = "/nonexistent"              # real memory/disk pressure must not hold back test jobs
S = queue.Scheduler()
RES = []
def ok(name, cond, detail=""):
    RES.append((name, bool(cond), detail))
def until(cond, limit=90):
    t0 = time.time()
    while time.time() - t0 < limit:
        S.tick()
        if cond():
            return True
        time.sleep(0.5)
    return False
def st(i):
    return queue.load(i)["state"]
W = os.path.join(os.environ["MS_TEST_WORK"], "jobs")
os.makedirs(W, exist_ok=True)
small = dict(mem_gb=0.2, cores=1, nice=19)

# 1. basic jobs, dependencies, failure
a = queue.submit("echo selftest-ok", cwd=W, name="t_echo", **small)
b = queue.submit("exit 3", cwd=W, name="t_fail", **small)
c = queue.submit("echo never", cwd=W, name="t_skip", depends=[b["id"]], **small)
until(lambda: all(st(i) in queue.TERMINAL for i in (a["id"], b["id"], c["id"])))
ok("job runs and finishes", st(a["id"]) == "done", st(a["id"]))
ok("failed job is reported as failed", st(b["id"]) == "failed" and queue.load(b["id"])["rc"] == 3, queue.load(b["id"]).get("reason"))
ok("dependent of a failed job is skipped", st(c["id"]) == "skipped", st(c["id"]))
ok("standard run log is valid", runlog.validate(os.path.join(W, "msrun.jsonl")) == [], str(runlog.validate(os.path.join(W, "msrun.jsonl"))[:3]))

# 2. timeouts
t = queue.submit("sleep 60", cwd=W, name="t_idle", idle_timeout_s=3, kill_seq="TERM,2,KILL", **small)
until(lambda: st(t["id"]) in queue.TERMINAL, 60)
ok("inactivity timeout stops a silent job", st(t["id"]) == "timeout", queue.load(t["id"]).get("reason"))

# 3. progress on fd 3, outputs, warnings, quality gate, failure diagnosis
p = queue.submit("echo 50 >&3; echo wns=0.12 >> $MSQ_OUTPUTS; echo 'CRITICAL WARNING: [Timing 38-282] constraints not met'",
                 cwd=W, name="t_gate", quality_gate={"new_critical": 0}, **small)
f = queue.submit("echo 'ERROR: [Place 30-640] Place Check : This design requires more Slice LUTs'; exit 1", cwd=W, name="t_diag", **small)
until(lambda: st(p["id"]) in queue.TERMINAL and st(f["id"]) in queue.TERMINAL)
jp = queue.load(p["id"])
ok("progress reported on fd 3", any(e.get("ev") == "progress" for e in runlog.events(os.path.join(W, "msrun.jsonl"), jp.get("run_id"))), "")
ok("outputs passed via $MSQ_OUTPUTS", (jp.get("outputs") or {}).get("wns") == "0.12", str(jp.get("outputs")))
ok("quality gate marks new critical warnings", jp["state"] == "warning", jp.get("reason"))
ok("failure knowledge base explains the error", "Placement failed" in (queue.load(f["id"]).get("reason") or ""), queue.load(f["id"]).get("reason"))

# 4. named resource (license seats): two jobs needing the only seat never overlap
open(os.path.join(MS, "config", "resources.conf"), "w").write("test_license 1\n")
r1 = queue.submit("sleep 2", cwd=W, name="t_lic1", resources={"test_license": 1}, **small)
r2 = queue.submit("sleep 2", cwd=W, name="t_lic2", resources={"test_license": 1}, **small)
until(lambda: st(r1["id"]) in queue.TERMINAL and st(r2["id"]) in queue.TERMINAL)
j1, j2 = queue.load(r1["id"]), queue.load(r2["id"])
overlap = j1.get("started") and j2.get("started") and j1["started"] < j2["ended"] and j2["started"] < j1["ended"]
ok("license seats are respected (no overlap)", j1["state"] == j2["state"] == "done" and not overlap, f"{j1['state']} {j2['state']}")

# 5. flow: matrix, per-item deps, outputs between steps, conditions, scratch folder, temp files, lineage
F = os.path.join(os.environ["MS_TEST_WORK"], "flow.yaml")
open(F, "w").write("""
name: selftest
cwd: %s
defaults: {mem_gb: 0.2, cores: 1, nice: 19}
steps:
  - name: gen
    matrix: {n: [1, 2]}
    folder: "run_{{n}}"
    cmd: "echo size=$(( {{n}} * 5 )) >> $MSQ_OUTPUTS; echo result {{n}} > out.txt; touch junk.tmp"
    outputs: ["out.txt"]
    scratch: true
  - name: big
    matrix: gen
    depends: [gen]
    folder: "run_{{n}}"
    when: "{{steps.gen.outputs.size}} > 7"
    cmd: "cat out.txt > big.txt"
    outputs: ["big.txt"]
    temp: ["out.txt"]
    clean_on: success
""" % os.path.join(os.environ["MS_TEST_WORK"], "flowrun"))
rec, specs = flow.submit_flow(F)
until(lambda: flow.load_flow(rec["id"]).get("notified"), 90)
js = {j["name"]: j for j in flow.flow_jobs(rec)}
base = os.path.join(os.environ["MS_TEST_WORK"], "flowrun")
ok("flow finished", flow.flow_state(rec)[2], str(flow.flow_state(rec)[0]))
ok("condition skipped the small item", js.get("big_1", {}).get("state") == "skipped" and js.get("big_2", {}).get("state") == "done",
   f"{js.get('big_1', {}).get('state')} {js.get('big_2', {}).get('state')}")
ok("scratch folder returned only declared outputs", os.path.exists(base + "/run_1/out.txt") and not os.path.exists(base + "/run_1/junk.tmp"), "")
ok("temp file removed after its consumer finished", not os.path.exists(base + "/run_2/out.txt") and os.path.exists(base + "/run_2/big.txt"), "")
sha, prod, cons = lineage.query(base + "/run_2/big.txt")
ok("lineage knows which job produced a file", bool(prod), "")
rec2, _ = flow.submit_flow(F, resume=True)
ok("resume skips work that is already done", all(j["state"] in ("cached", "skipped") or j["name"].startswith("big_1")
                                                for j in flow.flow_jobs(rec2)), str([j["state"] for j in flow.flow_jobs(rec2)]))
for j in flow.flow_jobs(rec2):
    queue.cancel(j["id"])

# 6. hard memory limit (only where systemd user scopes work)
if queue.cgroup_ok():
    m = queue.submit(sys.executable + " -c \"b=bytearray(400*1024*1024); import time; time.sleep(2)\"", cwd=W, name="t_oom",
                     mem_gb=0.1, cores=1, nice=19)
    until(lambda: st(m["id"]) in queue.TERMINAL, 60)
    jm = queue.load(m["id"])
    ok("memory limit stops a job that uses too much RAM", jm["state"] == "failed" and "memory" in (jm.get("reason") or ""), jm.get("reason"))
else:
    RES.append(("memory limit (systemd user scope)", None, "not available on this system — jobs run without a hard RAM limit"))

# 7. (1.6) groups, explain, edit, input, OOM priority, flaky, race
open(os.path.join(MS, "config", "groups.conf"), "w").write("t_grp 1\n")
g1 = queue.submit("sleep 2", cwd=W, name="t_g1", group="t_grp", **small)
g2 = queue.submit("sleep 2", cwd=W, name="t_g2", group="t_grp", **small)
until(lambda: st(g1["id"]) in queue.TERMINAL and st(g2["id"]) in queue.TERMINAL)
k1, k2 = queue.load(g1["id"]), queue.load(g2["id"])
ovl = k1.get("started") and k2.get("started") and k1["started"] < k2["ended"] and k2["started"] < k1["ended"]
ok("group limit is respected (no overlap)", k1["state"] == k2["state"] == "done" and not ovl, f"{k1['state']} {k2['state']}")
h = queue.submit("echo edited-$X", cwd=W, name="t_edit", state="held", **small)
ex = queue.explain(h["id"])
ok("explain lists the conditions of a waiting job", any(c == "RAM" for _, c, _ in ex) and any(c == "CPU" for _, c, _ in ex), str(ex[:2]))
e_ok, e_msg = queue.edit(h["id"], cmd="echo edited-ok", mem_gb=0.3)
queue.release(h["id"])
until(lambda: st(h["id"]) in queue.TERMINAL)
ok("a waiting job can be edited", e_ok and "edited-ok" in open(queue.LOGS / f"{h['id']}.log").read(), e_msg)
from mysys import qcli
i = queue.submit("read l; echo got-$l; cat /proc/self/oom_score_adj > adj.txt", cwd=W, name="t_stdin", stdin=True, **small)
until(lambda: st(i["id"]) == "running", 30)
t0 = time.time()
while qcli.send_input(i["id"], "hello") != 0 and time.time() - t0 < 20:
    time.sleep(0.5)
until(lambda: st(i["id"]) in queue.TERMINAL, 30)
ok("ms q send types into a running job", "got-hello" in open(queue.LOGS / f"{i['id']}.log").read(), st(i["id"]))
try:
    adj = int(open(os.path.join(W, "adj.txt")).read())
except (OSError, ValueError):
    adj = None
ok("queue jobs are killed before your desktop if RAM runs out (oom_score_adj)", adj is not None and adj >= 300, str(adj))
fk = queue.submit("test -f flaky.flag || (touch flaky.flag; exit 1)", cwd=W, name="t_flaky", retries=1, retry_on=["fail"],
                  retry_delay="1s", **small)
until(lambda: st(fk["id"]) in queue.TERMINAL and queue.load(fk["id"])["attempt"] == 2, 60)
ok("flaky job is recognised (failed, then passed unchanged)", queue.load(fk["id"]).get("flaky") is True, queue.load(fk["id"]).get("reason"))
RF = os.path.join(os.environ["MS_TEST_WORK"], "race.yaml")
open(RF, "w").write("""
name: selftest_race
cwd: %s
defaults: {mem_gb: 0.2, cores: 1, nice: 19}
steps:
  - name: try
    matrix: {s: [1, 2]}
    folder: "s{{s}}"
    stop_when: "good == 1"
    cmd: "if [ {{s}} = 1 ]; then echo good=1 >> $MSQ_OUTPUTS; else sleep 60; fi"
""" % os.path.join(os.environ["MS_TEST_WORK"], "race"))
rr, _ = flow.submit_flow(RF)
until(lambda: flow.load_flow(rr["id"]).get("notified"), 60)
rj = {j["name"]: j for j in flow.flow_jobs(rr)}
ok("race: other variants stop once one reaches the goal", rj.get("try_2", {}).get("race_lost") and flow.flow_state(rr)[2],
   str({k: v["state"] for k, v in rj.items()}))
print(json.dumps(RES))
'''


def _scratch():
    for d in (config.get("SCRATCH_DIR"), config.get("BIG_FOLDER_DEST")):
        if d and os.path.isdir(os.path.expanduser(d)):
            return os.path.expanduser(d)
    return None


def _writable(path):
    """The installed code is read-only (locked); the temporary copy must be writable and removable."""
    for dp, dns, fns in os.walk(path):
        for n in [dp] + [os.path.join(dp, f) for f in fns]:
            try:
                os.chmod(n, os.stat(n).st_mode | 0o200)
            except OSError:
                pass


def _isolated():
    root = Path(tempfile.mkdtemp(prefix="ms-selftest-", dir=_scratch()))
    ms = root / "My_System"
    for d in ("bin", "lib", "plugins", "runlog"):
        shutil.copytree(config.MS / d, ms / d, ignore=shutil.ignore_patterns("__pycache__"))
    _writable(root)
    for d in ("config", "state", "reports"):
        (ms / d).mkdir(parents=True)
    (ms / "config" / "my_system.conf").write_text('MACHINE_LABEL="selftest"\nQUEUE_KEEP_DAYS=999\n')
    (ms / "config" / "plugins.conf").write_text("vivado\n")
    note = ms / "bin" / "notify.sh"
    note.write_text("#!/bin/sh\nexit 0\n")      # no desktop/phone messages from the test
    for f in (ms / "bin").iterdir():
        f.chmod(0o755)
    return root, ms


def live_checks():
    """Read-only checks of the real installation."""
    from . import capacity, hw, queue
    out = []
    st, d = queue.daemon_state()
    out.append(("real scheduler is running", st == "running", st + (f" ({d.get('error')})" if d and d.get("error") else "")))
    hst, _, age = hw.collector_state()
    out.append(("hardware collector data is fresh", hst == "ok" if hst != "missing" else None,
                f"{age:.0f} h old" if age is not None else "collector not installed"))
    out.append(("capacity can be computed", bool(capacity.now_text()), ""))
    if shutil.which("claude"):
        # read Claude Code's own config instead of `claude mcp list` (that one contacts every server and can hang)
        reg = None
        try:
            cfg = json.loads((Path.home() / ".claude.json").read_text())
            reg = "my-system" in (cfg.get("mcpServers") or {}) or any(
                "my-system" in (p.get("mcpServers") or {}) for p in (cfg.get("projects") or {}).values())
        except (OSError, ValueError):
            reg = None
        out.append(("MCP server registered for Claude Code", reg, "" if reg is not None else "~/.claude.json not readable"))
    try:
        lg = subprocess.run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger", "--value"],
                            capture_output=True, text=True, timeout=15).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        lg = ""
    out.append(("queue keeps running after log out (linger)", lg == "yes" if lg else None, lg or "unknown"))
    from . import probes
    ps = probes.psi()
    out.append(("kernel pressure information (PSI) available", bool(ps) if ps else None,
                f"memory stalls {ps.get('memory', {}).get('full', 0):.0f}%, disk stalls {ps.get('io', {}).get('some', 0):.0f}%" if ps
                else "not provided by this kernel — the queue uses free RAM only"))
    for mp in dict.fromkeys(config.words("DATA_DISKS") + config.words("NET_SHARES")):
        r = probes.responsive(mp, 5)
        out.append((f"{mp} responds", bool(r and os.path.ismount(mp)), "mounted" if r and os.path.ismount(mp)
                    else ("not answering (hung)" if r is False else "not mounted")))
    hist = sorted((config.STATE / "history").glob("*.jsonl*"))
    out.append(("minute-by-minute history is recorded (ms replay)", bool(hist) if hist else None,
                f"{len(hist)} day(s)" if hist else "starts with the next watcher tick after this update"))
    pings = [k for k in ("PING_WATCH", "PING_DAILY", "PING_BACKUP", "PING_VERIFY") if (config.get(k) or "").startswith("http")]
    out.append(("dead-man's switch (alert when the computer stops pinging)", True if pings else None,
                ", ".join(pings) if pings else "optional: set PING_WATCH in my_system.conf (healthchecks.io)"))
    try:
        import urllib.request
        urllib.request.urlopen("https://api.osv.dev/v1/vulns/PYSEC-2021-1", timeout=10).read(10)
        osv = True
    except Exception:
        osv = None
    out.append(("vulnerability database (osv.dev) reachable", osv, "" if osv else "offline or blocked — audit skipped"))
    pol = Path("/etc/claude-code/managed-settings.json")
    out.append(("AI policy installed", pol.exists(), ""))
    out.append(("code and config locked", (config.MS / "bin").stat().st_uid == 0, ""))
    return out


def run(args):
    quick = "--quick" in args
    keep = "--keep" in args
    t0 = time.time()
    lines = [f"my-system self-test — {time.strftime('%F %T')}", ""]
    results = []
    print("Checking the installation (read-only)…", flush=True)
    try:
        results += live_checks()
    except Exception as e:          # a broken check must never stop the test
        results.append(("installation checks", False, f"{type(e).__name__}: {e}"))
    print("Running test jobs in a separate, temporary queue (your real jobs are not touched)…", flush=True)
    root, ms = _isolated()
    try:
        env = dict(os.environ, MY_SYSTEM=str(ms), MS_TEST_WORK=str(root / "work"), MSQ_TICK="0.5")
        r = subprocess.run(["nice", "-n", "19", "ionice", "-c3", sys.executable, "-c", HARNESS], env=env,
                           capture_output=True, text=True, timeout=900)
        try:
            results += [tuple(x) for x in json.loads(r.stdout.strip().splitlines()[-1])]
        except (ValueError, IndexError):
            results.append(("end-to-end test jobs", False, (r.stderr or r.stdout)[-800:]))
        if not quick:
            tdir = config.MS / "tests"
            for t in sorted(tdir.glob("test_*.py")) if tdir.exists() else []:
                print(f"Running {t.name}…", flush=True)
                rr = subprocess.run(["nice", "-n", "19", "ionice", "-c3", sys.executable, str(t)], capture_output=True, text=True,
                                    timeout=1200, cwd=str(root), env=dict(os.environ, MS_NO_CGROUP="1"))
                results.append((f"unit tests {t.stem}", rr.returncode == 0,
                                "" if rr.returncode == 0 else (rr.stderr or rr.stdout)[-600:]))
    except subprocess.TimeoutExpired:
        results.append(("test run finished in time", False, "timed out"))
    finally:
        if not keep:
            _writable(root)
            shutil.rmtree(root, ignore_errors=True)
    passed = sum(1 for _, ok, _ in results if ok is True)
    failed = [(n, d) for n, ok, d in results if ok is False]
    skipped = [(n, d) for n, ok, d in results if ok is None]
    for n, ok, d in results:
        mark = "PASS" if ok is True else "FAIL" if ok is False else "SKIP"
        lines.append(f"  {mark}  {n}" + (f"   — {d}" if d and ok is not True else ""))
    lines.append("")
    lines.append(f"{passed} passed, {len(failed)} failed, {len(skipped)} skipped · {time.time() - t0:.0f} s"
                 + ("" if keep else " · temporary test folder removed"))
    text = "\n".join(lines)
    print(text)
    try:
        (config.REPORTS / f"selftest-{time.strftime('%Y%m%d-%H%M')}.txt").write_text(text + "\n")
    except OSError:
        pass
    return 1 if failed else 0
