"""1.6 features: pressure-aware scheduling, groups, room for starving jobs, explain/edit/send, flaky and slow jobs,
flow races, CPU pinning, OOM priority, time-travel history and crash report, watcher warnings, housekeeping,
vulnerability audit, run comparison, dead-man's-switch pings. Run: python3 tests/test_v16.py"""
import http.server
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_features import SRC, fake_ms, py  # noqa: E402


def test_pressure_groups_room_explain_edit():
    out, notes, tmp = py(r'''
psi_dir = os.path.join(W, "psi"); os.makedirs(psi_dir)
def set_psi(mem_full=0.0, io=0.0):
    open(os.path.join(psi_dir, "memory"), "w").write(f"some avg10=0 avg60=0 avg300=0 total=0\nfull avg10={mem_full} avg60=0 avg300=0 total=0\n")
    open(os.path.join(psi_dir, "io"), "w").write(f"some avg10={io} avg60=0 avg300=0 total=0\nfull avg10=0 avg60=0 avg300=0 total=0\n")
    open(os.path.join(psi_dir, "cpu"), "w").write("some avg10=0 avg60=0 avg300=0 total=0\n")
probes.PSI_DIR = psi_dir
set_psi(mem_full=50)
a = queue.submit("echo a", cwd=W, name="a", **small)
S.tick()
assert st(a["id"]) == "queued" and "short of memory" in queue.load(a["id"])["reason"], queue.load(a["id"])
ex = queue.explain(a["id"])
assert any(c == "system pressure" and not ok for ok, c, d in ex), ex
set_psi(io=90)
S.tick()
assert "disks are overloaded" in queue.load(a["id"])["reason"]
set_psi()
assert until(lambda: st(a["id"]) == "done")

# groups: at most one job of group g at a time
open(os.path.join(MS, "config", "groups.conf"), "w").write("g 1\n")
gs = [queue.submit("sleep 1", cwd=W, name=f"g{i}", group="g", **small) for i in range(3)]
seen = 0
def chk():
    global seen
    seen = max(seen, sum(1 for x in gs if st(x["id"]) == "running"))
    return all(st(x["id"]) == "done" for x in gs)
assert until(chk, 60) and seen == 1, seen
assert any(c == "group g" for ok, c, d in queue.explain(queue.submit("true", cwd=W, name="gx", group="g", state="held", **small)["id"]))

# room kept for a job that waited too long (no starvation by smaller jobs)
open(os.path.join(MS, "config", "queue.conf"), "w").write("QUEUE_STARVE_MIN=0.001\nQUEUE_BACKFILL_MIN=0\n")
blk = queue.submit("sleep 6", cwd=W, name="blocker", mem_gb=30, cores=1)
assert until(lambda: st(blk["id"]) == "running")
big = queue.submit("echo big", cwd=W, name="big", mem_gb=25, cores=1)
time.sleep(0.2)
S.tick()
smalljob = queue.submit("echo small", cwd=W, name="small", mem_gb=5, cores=1)
S.tick()
r = queue.load(smalljob["id"])
assert r["state"] == "queued" and f"keeping room for #{big['id']}" in r["reason"], r["reason"]
ex = dict((c, (ok, d)) for ok, c, d in queue.explain(big["id"]))
assert ex["RAM"][0] is False, ex
# edit the waiting job: lower its memory request -> it fits
ok, msg = queue.edit(big["id"], mem_gb=1, priority=5, name="big2")
assert ok and queue.load(big["id"])["mem_gb"] == 1.0 and queue.load(big["id"])["name"] == "big2", msg
assert not queue.edit(blk["id"], mem_gb=1)[0]          # running: not editable
assert not queue.edit(big["id"], foo=1)[0]
assert until(lambda: st(big["id"]) == "done" and st(smalljob["id"]) == "done", 60)

# filters and JSON list
from mysys import qcli
lab = queue.submit("true", cwd=W, name="lab", labels=["doe"], state="held", **small)
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    qcli.q_cmd(["list", "--all", "--label", "doe", "--json"])
js = json.loads(buf.getvalue())
assert [j["id"] for j in js] == [lab["id"]], js
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    qcli.q_cmd(["release", "--group", "g"])
assert "1 job(s) of group g" in buf.getvalue(), buf.getvalue()
print("ok")
''')
    assert "ok" in out


def test_stdin_slow_oom_pin_flaky():
    out, notes, tmp = py(r'''
from mysys import qcli
open(os.path.join(MS, "config", "queue.conf"), "w").write("QUEUE_PIN_CPUS=1\nQUEUE_SLOW_FACTOR=0.01\n")
probes.cpu_siblings = lambda: [[0, 4], [1, 5], [2, 6], [3, 7]]
probes.threads = lambda: 8
a = queue.submit("read line; echo got:$line; cat /proc/self/oom_score_adj > adj.txt; grep Cpus_allowed_list /proc/self/status > cpus.txt",
                 cwd=W, name="inp", stdin=True, mem_gb=0.2, cores=2)
assert until(lambda: st(a["id"]) == "running")
t0 = time.time()
while qcli.send_input(a["id"], "hello") != 0 and time.time() - t0 < 20:
    time.sleep(0.3)
assert until(lambda: st(a["id"]) == "done", 30), queue.load(a["id"])
log = open(queue.LOGS / f"{a['id']}.log").read()
assert "got:hello" in log, log
assert int(open(os.path.join(W, "adj.txt")).read()) >= 300
ja = queue.load(a["id"])
assert ja["cpus"] == [0, 4], ja["cpus"]
cpus = open(os.path.join(W, "cpus.txt")).read()
assert "0,4" in cpus or "0" in cpus.split()[-1], cpus
assert not (queue.LOGS / f"{a['id']}.in").exists()
# slower than usual: two earlier runs of 300 s
for i in range(2):
    queue.save(dict(queue.load(a["id"]), id=900 + i, name="slowjob", state="done", started=1000.0, ended=1300.0, cwd=W))
assert queue.usual_seconds({"id": 0, "name": "slowjob", "cwd": W}) == 300
s = queue.submit("sleep 5", cwd=W, name="slowjob", **small)
assert until(lambda: st(s["id"]) == "done", 30)
assert queue.load(s["id"]).get("slow") is True
print("ok")
''')
    assert "ok" in out
    assert "Slower than usual: slowjob" in notes, notes


def test_flow_race():
    out, notes, tmp = py(r'''
base = os.path.join(W, "race"); os.makedirs(base)
F = os.path.join(base, "r.yaml")
open(F, "w").write("""
name: race
cwd: %s
defaults: {mem_gb: 0.2, cores: 1}
steps:
  - name: impl
    matrix: {seed: [1, 2, 3, 4]}
    folder: "s{{seed}}"
    stop_when: "wns >= 0"
    cmd: "if [ {{seed}} = 2 ]; then echo wns=0.05 >> $MSQ_OUTPUTS; else sleep 30; echo wns=-1 >> $MSQ_OUTPUTS; fi"
  - name: report
    depends: [impl]
    cmd: "echo done > report.txt"
""" % base)
rec, _ = flow.submit_flow(F)
assert until(lambda: flow.load_flow(rec["id"]).get("notified"), 90), [(j["name"], j["state"], j["reason"]) for j in flow.flow_jobs(rec)]
js = {j["name"]: j for j in flow.flow_jobs(flow.load_flow(rec["id"]))}
win = [j for j in js.values() if j.get("race_ok")]
assert len(win) == 1 and win[0]["name"].endswith("2"), [(n, j["state"]) for n, j in js.items()]
lost = [j for j in js.values() if j.get("race_lost")]
assert len(lost) == 3 and all("already reached the goal" in j["reason"] for j in lost), [(j["name"], j["reason"]) for j in lost]
assert os.path.exists(os.path.join(base, "report.txt"))
assert flow.flow_state(flow.load_flow(rec["id"]))[2], "flow should count as successful"
print("ok")
''')
    assert "ok" in out
    assert "Goal reached" in notes


def test_history_crash_replay():
    tmp, ms = fake_ms()
    env = dict(os.environ, MY_SYSTEM=str(ms), HOME=str(tmp))
    code = r'''
import json, os, sys, time
sys.path.insert(0, os.path.join(os.environ["MY_SYSTEM"], "lib"))
from mysys import history, probes, queue, config
probes.boot_id = lambda: "boot-new"
s = {}
smp = history.record(s, ["vivado in a/b"])
assert smp and smp["jobs"] == ["vivado in a/b"] and smp["top"]
assert history.record(s) is None                 # at most once per minute
s["hist_last"] = 0
history.record(s)
rows = history.around(time.time(), 120)
assert len(rows) == 2 and "load" in history.fmt(rows[0])
# previous boot ended without a clean stop while the queue service was running
old = dict(smp, t=time.time() - 3600, mem_avail_gb=0.5, mem_total_gb=92)
history.LAST.write_text(json.dumps({"boot_id": "boot-old", "sample": old}))
queue._ensure()
queue.DAEMON.write_text(json.dumps({"pid": 1, "t": old["t"] - 20}))
told = []
r = history.check_unclean(lambda *a: told.append(a))
assert r["clean"] is False and "out of memory" in r["cause"], r
assert told and told[0][0] == "The computer stopped unexpectedly"
assert history.check_unclean(lambda *a: told.append(a)) is None and len(told) == 1
assert "out of memory" in history.CRASHES.read_text()
# clean stop recorded for the old boot -> nothing reported
history.LAST.write_text(json.dumps({"boot_id": "boot-old2", "sample": old}))
history.CLEAN.write_text(json.dumps({"boot_id": "boot-old2", "t": time.time()}))
assert history.check_unclean(lambda *a: told.append(a)) == {"clean": True} and len(told) == 1
# housekeeping gzips old days
d = history.DIR / time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400 * 2))
open(str(d) + ".jsonl", "w").write(json.dumps(dict(old, t=time.time() - 86400 * 2)) + "\n")
history.housekeeping()
assert os.path.exists(str(d) + ".jsonl.gz")
assert history.around(time.time() - 86400 * 2, 60)
print("ok")
'''
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stdout + r.stderr
    r = subprocess.run([sys.executable, str(ms / "bin/ms"), "replay", "5m"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "RAM free" in r.stdout, r.stdout + r.stderr
    r = subprocess.run([sys.executable, str(ms / "bin/ms"), "replay", "crashes"], env=env, capture_output=True, text=True)
    assert "out of memory" in r.stdout


def test_watcher_warnings():
    tmp, ms = fake_ms()
    (ms / "config/my_system.conf").write_text(f'DATA_DISKS="{tmp}/nomount"\nNET_SHARES=""\n')
    psi = tmp / "psi"
    psi.mkdir()
    (psi / "memory").write_text("some avg10=60 avg60=0 avg300=0 total=0\nfull avg10=40 avg60=0 avg300=0 total=0\n")
    env = dict(os.environ, MY_SYSTEM=str(ms), HOME=str(tmp), MS_PSI_DIR=str(psi))
    code = r'''
import os, sys
sys.path.insert(0, os.path.join(os.environ["MY_SYSTEM"], "lib")); sys.path.insert(0, os.path.join(os.environ["MY_SYSTEM"], "plugins"))
from mysys import watcher, probes
G = 2**30
watcher.jobs = lambda: []
probes.meminfo = lambda: {"MemTotal": 64 * G, "MemAvailable": 1 * G, "SwapTotal": 0, "SwapFree": 0}
watcher.tick()
watcher.tick()
probes.responsive = lambda p, t=5: False
watcher.tick()
'''
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    notes = (tmp / "notes").read_text()
    for want in ("RAM almost exhausted", "Machine is thrashing", "Disk not mounted", "Share not responding"):
        assert want in notes, (want, notes)
    assert notes.count("RAM almost exhausted") == 1 and notes.count("Disk not mounted") == 1


def test_housekeeping_and_ping():
    tmp, ms = fake_ms()
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    (ms / "config/my_system.conf").write_text(f'PING_TEST="http://127.0.0.1:{srv.server_port}/abc"\nNOTIFY_LOG_MAX_MB=1\n')
    (ms / "reports/notifications.log").write_text("x" * (2 * 2**20))
    (ms / "reports/ai_audit").mkdir()
    (ms / "reports/ai_audit/2001-01.jsonl").write_text("{}\n")
    (ms / "reports/ai_audit" / time.strftime("%Y-%m.jsonl")).write_text("{}\n")
    (ms / "reports/metrics.csv").write_text("time,a\n2001-01-01 00:00,1\n" + time.strftime("%Y-%m-%d %H:%M") + ",2\n")
    env = dict(os.environ, MY_SYSTEM=str(ms), HOME=str(tmp))
    ms_ = [sys.executable, str(ms / "bin/ms")]
    r = subprocess.run(ms_ + ["housekeeping"], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (ms / "reports/notifications.log").stat().st_size == 0 and (ms / "reports/notifications.log.1.gz").exists()
    assert not (ms / "reports/ai_audit/2001-01.jsonl").exists() and (ms / "reports/ai_audit" / time.strftime("%Y-%m.jsonl")).exists()
    assert "2001" not in (ms / "reports/metrics.csv").read_text()
    assert subprocess.run(ms_ + ["ping", "test", "start"], env=env).returncode == 0
    assert subprocess.run(ms_ + ["ping", "test", "1"], env=env).returncode == 0
    assert subprocess.run(ms_ + ["ping", "test"], env=env).returncode == 0
    assert subprocess.run(ms_ + ["ping", "missing"], env=env).returncode == 1
    srv.shutdown()
    assert hits == ["/abc/start", "/abc/1", "/abc"], hits


def test_vulnerability_audit():
    tmp, ms = fake_ms()
    env = dict(os.environ, MY_SYSTEM=str(ms), HOME=str(tmp))
    code = r'''
import os, sys
sys.path.insert(0, os.path.join(os.environ["MY_SYSTEM"], "lib"))
from mysys import software
data = {"venvs": {"/h/.venv": {"packages": {"requests": {"version": "2.19.0"}, "numpy": {"version": "2.1.0"}}}},
        "projects": {"/h/proj": {"packages": {"requests": {"version": "2.19.0", "venv": "/h/.venv"}}}},
        "updates": [], "libs": {}}
sent = []
def post(url, body):
    sent.append(body)
    return {"results": [{"vulns": [{"id": "GHSA-x"}]} if q["package"]["name"] == "requests" else {} for q in body["queries"]]}
def get(url):
    return {"summary": "Leaks credentials", "affected": [{"package": {"name": "requests"}, "ranges": [{"events": [{"introduced": "0"}, {"fixed": "2.20.0"}]}]}]}
res = software.audit(data, post, get)
assert sent[0]["queries"][0]["package"]["ecosystem"] == "PyPI"
f = res["findings"]
assert len(f) == 1 and f[0]["fixed"] == "2.20.0" and f[0]["projects"] == ["/h/proj"], f
assert "GHSA-x" in software.audit_text(software.load_audit())
data["vulns"] = f
rows = software.advise(dict(data, restart_needed=[]))
assert any(r["kind"] == "pip security" and r["new"] == "2.20.0" for r in rows), rows
def boom(url, body):
    raise OSError("offline")
r2 = software.audit(data, boom, get)
assert r2["error"] and software.load_audit()["findings"], "offline run must keep the previous result"
print("ok")
'''
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stdout + r.stderr


def test_runlog_compare_diff():
    tmp, ms = fake_ms()
    env = dict(os.environ, MY_SYSTEM=str(ms), HOME=str(tmp))
    d = tmp / "doe"
    code = f'''
import os, sys
sys.path.insert(0, os.path.join(os.environ["MY_SYSTEM"], "runlog"))
import msrun
for seed, wns in ((1, -0.2), (2, 0.05), (3, -0.01)):
    with msrun.Run(f"s{{seed}}", params={{"seed": seed, "part": "xc7"}}, folder={str(d)!r}) as r:
        r.metric("wns", wns)
        r.metric("lut", 1000)
'''
    assert subprocess.run([sys.executable, "-c", code], env=env).returncode == 0
    ms_ = [sys.executable, str(ms / "bin/ms"), "log"]
    out = subprocess.run(ms_ + ["compare", str(d), "--sort", "wns", "--desc"], env=env, capture_output=True, text=True).stdout
    lines = out.splitlines()
    assert "p:seed" in lines[0] and "wns" in lines[0] and "lut" not in lines[0] and "p:part" not in lines[0], out
    assert lines[1].startswith("s2") and lines[-1].startswith("s1"), out
    csvf = tmp / "c.csv"
    subprocess.run(ms_ + ["compare", str(d), "--csv", str(csvf)], env=env, check=True, capture_output=True)
    assert csvf.read_text().count("\n") == 4
    out = subprocess.run(ms_ + ["diff", "s1", "s2"], env=env, capture_output=True, text=True).stdout
    assert "seed" in out and "wns" in out and "lut" not in out, out


if __name__ == "__main__":
    t0 = time.time()
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            fn()
            print("ok", k)
    print(f"all passed in {time.time() - t0:.1f}s")
