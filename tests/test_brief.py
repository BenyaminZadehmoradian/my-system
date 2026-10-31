"""Phone status report (ms brief): content, schedule, 'status' requests from the ntfy app.
Run: python3 tests/test_brief.py"""
import http.server
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_features import fake_ms  # noqa: E402

CODE = r'''
import json, os, sys, time
MS = os.environ["MY_SYSTEM"]
sys.path.insert(0, os.path.join(MS, "lib"))
from mysys import brief, probes, queue, config
G = 2**30
probes.threads = lambda: 16
probes.loadavg = lambda: (13.6, 12.0, 11.0)
probes.meminfo = lambda: {"MemTotal": 92 * G, "MemAvailable": 61 * G, "SwapTotal": 0, "SwapFree": 0}
probes.cpu_temp = lambda: 72.0
probes.reboot_required = lambda: False
now = time.time()
W = os.environ["WORK"]
# two Vivado runs started outside the queue (tracked by the watcher), one inside the queue
st = {"runs": {
    "a": {"tool": "vivado", "cwd": "/mnt/data/benyamin/efpga/S22/doe_3", "start": now - 4300, "rss": 9.8 * G},
    "b": {"tool": "vivado", "cwd": "/mnt/data/benyamin/efpga/S22/doe_4", "start": now - 600, "rss": 7.1 * G},
    "q": {"tool": "vivado", "cwd": W + "/impl", "start": now - 900, "rss": 3 * G}},
    "mounts": {"/mnt/ea06csl": "hung"}}
(config.STATE / "watch.json").write_text(json.dumps(st))
r = queue.submit("vivado -mode batch", cwd=W + "/impl", name="impl_s7", mem_gb=12, cores=4)
queue.update(r["id"], state="running", started=now - 900, progress=40, rss=3 * G)
w1 = queue.submit("true", cwd=W, name="impl_s8", mem_gb=30, cores=4)
queue.update(w1["id"], reason="waiting for RAM (30.0 GB, 4.0 cores needed)")
w2 = queue.submit("true", cwd=W, name="report", state="held", mem_gb=1, cores=1)
f1 = queue.submit("true", cwd=W + "/s1", name="synth_s1", mem_gb=1, cores=1)
queue.update(f1["id"], state="done", started=now - 3000, ended=now - 2000, outputs={"wns": "0.120"})
f2 = queue.submit("true", cwd=W + "/s2", name="synth_s2", mem_gb=1, cores=1)
queue.update(f2["id"], state="failed", started=now - 1900, ended=now - 1500, reason="exit code 1 — likely cause: X")
(config.REPORTS / "runs.jsonl").write_text("\n".join(json.dumps(x) for x in [
    {"t": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 400)), "tool": "vivado", "cwd": "/mnt/data/S17/doe_2",
     "seconds": 1260, "errors": 0, "results": {"wns_ns": 1.354}},
    {"t": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 2000)), "tool": "vivado", "cwd": W + "/s1",
     "seconds": 1000, "errors": 0, "results": {}},
    {"t": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 90000)), "tool": "vivado", "cwd": "/old", "seconds": 5, "errors": 0}]) + "\n")
title, text, info = brief.build()
print(title); print(text)
assert title == "Status: 3 running, 2 waiting, 1 problem", title
for want in ("CPU 85% busy (load 13.6 of 16) · 72°C", "RAM 61 GB free of 92", "/mnt/ea06csl not answering",
             "S22/doe_3 — 1h11m", "9.8 GB", f"#{r['id']} impl_s7 — 15m, 40%", f"#{w1['id']} impl_s8 — needs RAM",
             "held by you", "OK  vivado S17/doe_2 — 21m, WNS +1.354 ns", f"FAIL  #{f2['id']} synth_s2 — 6m, exit code 1",
             f"OK  #{f1['id']} synth_s1", "WNS 0.120"):
    assert want in text, (want, text)
assert "/old" not in text and text.count("synth_s1") == 1, "only the last 3, without duplicates"
assert len(text.encode()) < 4000
# schedule
config.load = (lambda f: (lambda: dict(f(), PHONE_REPORT_TIMES="08:00 13:00")))(config.load)
s = {}
t8 = time.mktime(time.strptime(time.strftime("%Y-%m-%d") + " 08:05", "%Y-%m-%d %H:%M"))
assert brief.due(s, t8) and not brief.due(s, t8 + 60), "once per slot and day"
assert not brief.due(s, t8 + 3 * 3600)
assert not brief.due(s, t8 + 5 * 3600 - 600)        # 12:55: the 13:00 report is not due yet
assert brief.due(s, t8 + 5 * 3600)                    # 13:05
print("ok")
'''


def test_brief_content_and_schedule():
    tmp, ms = fake_ms()
    env = dict(os.environ, MY_SYSTEM=str(ms), HOME=str(tmp), WORK=str(tmp / "work"))
    r = subprocess.run([sys.executable, "-c", CODE], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stdout[-3000:] + r.stderr[-3000:]


def test_phone_request_and_send():
    tmp, ms = fake_ms()
    msgs = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            msgs.append(self.path)
            body = "\n".join(json.dumps(x) for x in [
                {"id": "a1", "event": "open"},
                {"id": "a2", "event": "message", "title": "[box] Status: 1 running", "message": "status"},
                {"id": "a3", "event": "message", "message": " Status "}])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    (ms / "config/my_system.conf").write_text(f'NTFY_SERVER="http://127.0.0.1:{srv.server_port}"\nPHONE_REPORT_TIMES=""\n')
    (ms / "state/ntfy_topic").write_text("topic123\n")
    env = dict(os.environ, MY_SYSTEM=str(ms), HOME=str(tmp))
    code = r'''
import os, sys
sys.path.insert(0, os.path.join(os.environ["MY_SYSTEM"], "lib"))
from mysys import brief
s = {}
assert brief.phone_requests(s) is True and s["ntfy_since"] == "a3"
assert not brief.due(s)
brief.send()
print("ok")
'''
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    srv.shutdown()
    assert r.returncode == 0 and "ok" in r.stdout, r.stdout + r.stderr
    assert msgs and "/topic123/json?poll=1&since=" in msgs[0], msgs
    notes = (tmp / "notes").read_text()
    assert notes.startswith("Status: 0 running, 0 waiting") and "LAST 3 FINISHED" in notes, notes
    out = subprocess.run([sys.executable, str(ms / "bin/ms"), "brief"], env=env, capture_output=True, text=True).stdout
    assert "■ SYSTEM" in out and "▶ RUNNING" in out


if __name__ == "__main__":
    t0 = time.time()
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            fn()
            print("ok", k)
    print(f"all passed in {time.time() - t0:.1f}s")
