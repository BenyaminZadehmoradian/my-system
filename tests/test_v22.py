"""1.22: findings of the full review on real use (Nov 2026). Run: python3 tests/test_v22.py"""
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_features import fake_ms, py  # noqa: E402
import test_remote as TR  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def test_job_done_is_told_once_across_restarts():
    """Seen: 'Job done: efpga_lit #12' arrived 36 times in 10 days — once after every restart of the scheduler
    (each install, each reboot), because what had been told was kept only in memory."""
    out, notes, tmp = py(r'''
a = queue.submit("true", cwd=W, name="told", notify="always", **small)
assert until(lambda: st(a["id"]) == "done", 30)
S.tick()
notes = os.path.join(os.path.dirname(MS), "notes")
count = lambda: open(notes).read().count("Job done: told") if os.path.exists(notes) else 0
t0 = time.time()
while not count() and time.time() - t0 < 10:      # (the message is sent by a helper program)
    time.sleep(0.1)
n1 = count()
S2 = queue.Scheduler()                     # the service restarts
for _ in range(3):
    S2.tick()
time.sleep(1.5)
n2 = count()
assert (n1, n2) == (1, 1), (n1, n2)
print("TOLD-OK")
''')
    assert "TOLD-OK" in out, out


def test_two_quick_reboots_are_not_a_crash():
    """Seen 12 Nov 2026: reboot into the BIOS and back within a minute. The clean stop of the second boot replaced
    the record of the first one, and the first was reported as 'power loss, a hard freeze or a forced power-off'."""
    out, notes, tmp = py(r'''
from mysys import history, probes
smp = {"t": time.time() - 400, "load": 0.5, "mem_avail_gb": 80, "mem_total_gb": 92, "temp": 50}
history.LAST.write_text(json.dumps({"boot_id": "boot-A", "sample": smp}))
queue._ensure()
queue.DAEMON.write_text(json.dumps({"pid": 1, "t": smp["t"] + 5}))
real = probes.boot_id
probes.boot_id = lambda: "boot-A"; history.mark_clean_stop()          # reboot 1 (clean)
probes.boot_id = lambda: "boot-B"; history.mark_clean_stop()          # reboot 2 a minute later; watcher never ran
probes.boot_id = lambda: "boot-C"
told = []
r = history.check_unclean(lambda *a: told.append(a))
assert r == {"clean": True} and not told, (r, told)
# and a real crash of a later boot is still reported
history.LAST.write_text(json.dumps({"boot_id": "boot-C", "sample": dict(smp, t=time.time() - 100)}))
queue.DAEMON.write_text(json.dumps({"pid": 1, "t": time.time() - 100}))
probes.boot_id = lambda: "boot-D"
r = history.check_unclean(lambda *a: told.append(a))
assert r["clean"] is False and len(told) == 1, r
print("REBOOT-OK")
''')
    assert "REBOOT-OK" in out, out


def test_server_job_survives_a_restart_of_this_computer():
    """Before: a reboot (or a killed runner) marked every server job failed and ran each one a second time on this
    machine, while the Slurm jobs kept running unseen. Now the runner is started again and follows the same Slurm
    job; a job that was still uploading is queued again; nothing is submitted twice."""
    out, notes, tmp, ms, env = TR.run(r'''
import signal
d = os.path.join(W, "ra"); os.makedirs(d, exist_ok=True)
a = queue.submit("echo part-$((0+1)); sleep 25; echo part-$((1+1)); echo ok > result.txt", cwd=d, name="ra", mem_gb=1, cores=1,
                 remote="titan")
assert until(lambda: queue.load(a["id"]).get("remote_state") == "RUNNING", 40)
time.sleep(1.5)
j = queue.load(a["id"])
os.killpg(j["pid"], signal.SIGKILL)                    # the computer "restarts": the runner is gone
os.waitpid(j["pid"], 0)
S2 = queue.Scheduler()
S2.tick()
k = queue.load(a["id"])
assert k["state"] == "running" and k.get("reattached") == 1 and k["pid"] != j["pid"], k
assert k["started"] == j["started"] and k["remote_job"] == j["remote_job"]
assert until(lambda: st(a["id"]) in queue.TERMINAL, 60)
k = queue.load(a["id"])
log = open(queue.LOGS / f"{a['id']}.log").read()
assert k["state"] == "done" and open(os.path.join(d, "result.txt")).read().strip() == "ok", (k["state"], k["reason"], log)
assert "following Slurm job" in log and 1 <= log.count("part-1") <= 2 and log.count("part-2") == 1, log
assert len(glob.glob(os.path.join(os.environ["FAKE_SLURM"], "*.sh"))) == 1, "submitted once"
print("REATTACH-OK")
''')
    assert "REATTACH-OK" in out, out


def test_shutdown_does_not_cancel_server_jobs():
    """At shutdown every process gets SIGTERM. The link to a server job took that as "cancelled" and ran scancel:
    a reboot cancelled all jobs on the server. Only a real cancel (ms q cancel) or a time limit does that now."""
    out, notes, tmp, ms, env = TR.run(r'''
import signal
d = os.path.join(W, "sd"); os.makedirs(d, exist_ok=True)
os.environ["MSQ_FAKE_STOPPING"] = "1"
a = queue.submit("sleep 12; echo ok > r.txt", cwd=d, name="sd", mem_gb=1, cores=1, remote="titan")
assert until(lambda: queue.load(a["id"]).get("remote_state") == "RUNNING", 40)
j = queue.load(a["id"])
os.killpg(j["pid"], signal.SIGTERM)                     # what a shutdown does
os.waitpid(j["pid"], 0)
time.sleep(1.5)
sid = j["remote_job"]
assert open(os.path.join(os.environ["FAKE_SLURM"], sid + ".state")).read().strip() == "RUNNING", "not cancelled there"
k = queue.load(a["id"])
assert k["state"] == "running", k["state"]
S2 = queue.Scheduler(); S2.tick()
assert until(lambda: st(a["id"]) in queue.TERMINAL, 60)
assert st(a["id"]) == "done" and os.path.exists(os.path.join(d, "r.txt")), queue.load(a["id"])
# a real cancel still cancels there
os.environ["MSQ_FAKE_STOPPING"] = "0"
b = queue.submit("sleep 30", cwd=d, name="sd2", mem_gb=1, cores=1, remote="titan")
assert until(lambda: queue.load(b["id"]).get("remote_state") == "RUNNING", 40)
queue.cancel(b["id"])
sid = queue.load(b["id"])["remote_job"]
assert until(lambda: open(os.path.join(os.environ["FAKE_SLURM"], sid + ".state")).read().strip() == "CANCELLED", 30)
print("SHUTDOWN-OK")
''')
    assert "SHUTDOWN-OK" in out, out


def test_own_error_on_the_server_is_not_run_again_here():
    """offload = prefer: a job that could not run on the server (missing file there) runs here once; a job whose
    own command failed there (exit 7) does not — it would fail here as well and only load this machine."""
    out, notes, tmp, ms, env = TR.run(r'''
d = os.path.join(W, "own"); os.makedirs(d, exist_ok=True)
a = queue.submit("echo design-error; exit 7", cwd=d, name="own_error", mem_gb=1, cores=1)
assert until(lambda: st(a["id"]) in queue.TERMINAL, 60)
S.tick(); S.tick()
j = queue.load(a["id"])
assert j["state"] == "failed" and j["rc"] == 7 and j.get("remote") == "titan" and not j.get("fell_back"), j
assert queue.server_side_failure({"rc": 3}) and queue.server_side_failure({"rc": 1, "reason": "x: command not found"})
assert not queue.server_side_failure({"rc": 1, "reason": "exit code 1, 4 ERROR line(s)"})
print("OWN-OK")
''', offload="prefer", extra="tools = *\n")
    assert "OWN-OK" in out, out


def test_quiet_slurm_controller_does_not_fail_running_jobs():
    """squeue answered nothing for 14 polls (controller busy) while sacct said RUNNING: after 10 quiet polls the job
    was called failed although it still ran. It now waits as long as accounting says it runs."""
    out, notes, tmp, ms, env = TR.run(r'''
d = os.path.join(W, "quiet"); os.makedirs(d, exist_ok=True)
a = queue.submit("sleep 7; echo ok > r.txt", cwd=d, name="quiet", mem_gb=1, cores=1, remote="titan")
assert until(lambda: queue.load(a["id"]).get("remote_job"), 40)
open(os.path.join(os.environ["FAKE_SLURM"], "flaky"), "w").write("14")
assert until(lambda: st(a["id"]) in queue.TERMINAL, 60)
j = queue.load(a["id"])
assert j["state"] == "done" and os.path.exists(os.path.join(d, "r.txt")), (j["state"], j["reason"], open(queue.LOGS / f"{a['id']}.log").read())
print("QUIET-OK")
''')
    assert "QUIET-OK" in out, out


def test_upload_slots_and_fast_job_list():
    sys.path.insert(0, str(ROOT / "src/lib"))
    out, notes, tmp = py(r'''
from mysys import remote as R
os.environ["MSQ_UPLOAD_SLOTS"] = "2"
a, b = R.upload_slot(), R.upload_slot()
a.__enter__(); b.__enter__()
said = []
c = R.upload_slot(said.append, stop=lambda: True)       # third one: no slot, and asked to stop -> returns without one
c.__enter__()
assert c.fh is None and a.fh and b.fh
a.__exit__(); d = R.upload_slot(); d.__enter__(); assert d.fh
# job list: finished jobs come from memory, changes are still seen
ids = [queue.submit("true", cwd=W, name=f"n{i}", state="held", **small)["id"] for i in range(40)]
for i in ids[:30]:
    queue.update(i, state="done", ended=time.time())
n1 = queue.all_jobs(); n2 = queue.all_jobs()
assert len(queue._JOB_CACHE) == 30 and [j["id"] for j in n1] == [j["id"] for j in n2] == ids
n1[0]["name"] = "changed-in-caller"
assert queue.all_jobs()[0]["name"] == "n0", "callers get their own copy"
queue.update(ids[0], state="queued", name="again")
assert queue.all_jobs()[0]["name"] == "again" and len(queue._JOB_CACHE) == 29
queue.clear(0)
assert len(queue.all_jobs()) == 11 and len(queue._JOB_CACHE) == 0
print("SLOTS-LIST-OK")
''')
    assert "SLOTS-LIST-OK" in out, out


GUI = r"""
import os, sys, time, importlib.util, json
os.environ["QT_QPA_PLATFORM"] = "offscreen"
ms = os.environ["MY_SYSTEM"]
sys.path.insert(0, os.path.join(ms, "lib"))
from mysys import config, queue
queue._ensure()
now = time.time()
for i in range(1, 1301):                               # as on the real machine: >1200 jobs, many of them server jobs
    j = {"id": i, "name": f"k70v_aes_c{i}", "cmd": "true", "cwd": os.environ["WORK"], "state": "done", "mem_gb": 24.0,
         "cores": 4.0, "priority": 0, "created": now - 9000, "started": now - 8000, "ended": now - 4000 + i, "attempt": 1,
         "rc": 0, "reason": "", "depends": []}
    if i % 3 == 0:
        j.update(remote="titan", remote_job=str(1000 + i), remote_cores=4, remote_cores_used=1.9, remote_peak_gb=9.5,
                 remote_elapsed_s=3600, remote_node="titan02", remote_state="COMPLETED")
    if i % 50 == 0:
        j.update(state="running", ended=None, remote="titan", remote_job=str(i), remote_state="RUNNING", remote_node="titan07",
                 remote_cores_used=None, remote_peak_gb=None, pid=os.getpid(), rss=5e7)
    if i % 71 == 0:
        j.update(state="queued", started=None, ended=None, remote="titan",
                 reason="waiting: titan: your jobs there reserve 72 cores, +4 would pass max_cores = 72")
    if i % 97 == 0:
        j.update(state="failed", rc=1, reason="exit code 1", progress=None, last_line=None,
                 prefer_note="not sent: titan: it uses paths that exist only here: /opt/Xilinx/14.7")
    open(queue.JOBS / f"{i}.json", "w").write(json.dumps(j))
open(queue.JOBS / "stray.json", "w").write("{}")       # a file that is not a job must not break anything
open(config.CONF / "remotes.conf", "w").write("[titan]\ntype = slurm\nssh = titan\naccount = me\nmax_jobs = 72\nmax_cores = 72\n")
os.makedirs(config.REPORTS, exist_ok=True)
with open(config.REPORTS / "results.csv", "w") as f:   # 3000 result rows, some shorter than the header
    f.write("time,tool,batch,run,status,wns_ns,tns_ns,fmax_mhz,clock_ns,latency_cycles,lut,ff,bram,dsp,power_w,seconds,folder\n")
    for i in range(3000):
        f.write(f"2026-11-0{i % 9 + 1} 10:00,vivado,b{i % 7},r{i},ok,{(i % 5) - 2}.5,0,100,10,5,10,10,1,1,0.5,{i}\n" if i % 400 else "2026-11-01,vivado,b,r\n")
spec = importlib.util.spec_from_file_location("gui", os.path.join(ms, "gui", "my_system_gui.py"))
G = importlib.util.module_from_spec(spec); spec.loader.exec_module(G)
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer
G.install_crash_guards()
app = QApplication(sys.argv); G.set_theme(False); G.apply_theme(app)
w = G.Main(); app._main = w; w.show(); app.processEvents()
names = [type(p).__name__ for p in w.pages]
assert "ServersPage" in names and "BrokenPage" not in names, names
worst = {}
for row, idx in w.row_page.items():                    # open every page twice, as a user clicking through
    for _ in range(2):
        t0 = time.time()
        w.nav.setCurrentRow(row); app.processEvents()
        worst[names[idx]] = max(worst.get(names[idx], 0), time.time() - t0)
        assert "Could not update" not in w.foot.text(), (names[idx], w.foot.text())
slow = {k: round(v, 2) for k, v in worst.items() if v > 3.0}
assert not slow, f"pages that block the window: {slow}"
q = w.pages[names.index("QueuePage")]
w.nav.setCurrentRow([r for r, i in w.row_page.items() if names[i] == "QueuePage"][0]); app.processEvents()
q.show_all.setChecked(True); app.processEvents()
assert q.tab.rowCount() == 1300 and q.tab.columnCount() == 13
t0 = time.time(); q.refresh(); app.processEvents(); t_q = time.time() - t0
assert t_q < 3.0, f"queue refresh with 1300 jobs took {t_q:.1f} s"
q.tab.selectRow(5); q.tab.selectRow(5); ids = q.selected(); q.refresh(); app.processEvents()
assert q.selected() == ids and ids, "the selection survives a refresh"
assert "titan" in q.server.text() and "cores" in q.server.text(), q.server.text()
sv = w.pages[names.index("ServersPage")]; sv.refresh(); app.processEvents()
assert sv.tab.rowCount() >= 1 and "--cores 2" in sv.tab.item(0, 8).text(), sv.tab.item(0, 8).text()
assert sv.why.rowCount() >= 1
res = w.pages[names.index("ResultsPage")]; res.refresh(); res.q.setText("b3"); res.refresh(); app.processEvents()
assert 0 < res.tab.rowCount() < 2000
# an error inside a button / timer handler: logged and shown, the app keeps running (PyQt would abort the program)
QTimer.singleShot(0, lambda: {}["missing-key"])
t0 = time.time()
while time.time() - t0 < 1.0:
    app.processEvents(); time.sleep(0.02)
assert "KeyError" in w.foot.text() and "KeyError" in open(G.GUI_ERRORS).read(), w.foot.text()
# switching the theme closes the old window; its timers must be dead (this ended the app 15 s later)
w.toggle_theme(); app.processEvents()
old, w2 = w, app._main
assert w2 is not old and old.closed and not old.tick_timer.isActive()
old.tick(); old.refresh_nav()                           # late calls into the closed window do nothing
w2.tick(); app.processEvents()
w2.close(); app.processEvents()
print("GUI22-OK", {k: round(v, 2) for k, v in sorted(worst.items(), key=lambda kv: -kv[1])[:4]}, "queue", round(t_q, 2))
"""


def test_gui_stays_alive_and_fast_with_1300_jobs():
    """The app froze for half a minute per refresh with ~300 queue rows (and looked crashed), ended when an error
    escaped from a click or timer handler, and ended 15 s after switching the theme."""
    try:
        import PyQt6
    except ImportError:
        print("skipped (PyQt6 not installed)")
        return
    import shutil
    tmp, ms = fake_ms()
    shutil.copytree(ROOT / "src" / "gui", ms / "gui")
    site = os.path.dirname(os.path.dirname(PyQt6.__file__))
    env = dict(os.environ, MY_SYSTEM=str(ms), MS_NO_CGROUP="1", HOME=str(tmp), WORK=str(tmp / "work"),
               QT_QPA_PLATFORM="offscreen",
               PYTHONPATH=os.pathsep.join(x for x in (site, os.environ.get("PYTHONPATH", "")) if x))
    r = subprocess.run([sys.executable, "-c", GUI], env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0 and "GUI22-OK" in r.stdout, r.stdout[-3000:] + r.stderr[-4000:]
    print("   ", r.stdout.strip().splitlines()[-1])


if __name__ == "__main__":
    t0 = time.time()
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            fn()
            print("ok", k)
    print(f"all passed in {time.time() - t0:.1f}s")
