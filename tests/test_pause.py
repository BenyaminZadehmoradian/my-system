"""ms pause / resume: turning parts of my-system off for a while. Run: python3 tests/test_pause.py"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_features import py, fake_ms  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "src" / "bin" if (ROOT / "src").is_dir() else ROOT / "bin"


def ms_env(tmp, ms, **kw):
    return dict(os.environ, MY_SYSTEM=str(ms), HOME=str(tmp), **kw)


def test_cli_and_expiry():
    tmp, ms = fake_ms()
    env = ms_env(tmp, ms)
    m = [sys.executable, str(ms / "bin/ms")]
    out = subprocess.run(m + ["paused"], env=env, capture_output=True, text=True).stdout
    assert "nothing is paused" in out
    out = subprocess.run(m + ["pause", "queue", "--for", "2h"], env=env, capture_output=True, text=True).stdout
    assert "queue paused until" in out and "ms resume queue" in out, out
    subprocess.run(m + ["pause", "phone"], env=env, check=True, capture_output=True)
    out = subprocess.run(m + ["paused"], env=env, capture_output=True, text=True).stdout
    assert "queue" in out and "phone          paused until you turn it on" in out, out
    r = subprocess.run(m + ["pause", "nonsense"], env=env, capture_output=True, text=True)
    assert r.returncode == 2 and "unknown feature" in r.stdout
    subprocess.run(m + ["resume", "queue"], env=env, check=True, capture_output=True)
    out = subprocess.run(m + ["paused"], env=env, capture_output=True, text=True).stdout
    assert "queue" not in out and "phone" in out
    code = r'''
import os, sys, time
sys.path.insert(0, os.path.join(os.environ["MY_SYSTEM"], "lib"))
from mysys import pause
pause.set_pause("backup", time.time() - 5)          # already over: counts as not paused, file removed
assert not pause.paused("backup") and not (pause.DIR / "backup").exists()
pause.set_pause("all", time.time() + 60)
assert pause.paused("watcher") and pause.paused("daily")
end = pause.parse_end(["--until", "08:00"])
assert 0 < end - time.time() <= 86400
pause.resume("all")
assert not pause.paused("watcher") and not pause.paused("phone")
print("ok")
'''
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert "ok" in r.stdout, r.stdout + r.stderr


def test_notifications_and_phone():
    tmp, ms = fake_ms()
    for f in ("notify.sh", "common.sh"):
        shutil.copy(BIN / f, ms / "bin" / f)
        (ms / "bin" / f).chmod(0o755)
    (ms / "state/ntfy_topic").write_text("topic\n")
    stubs = tmp / "stubs"
    stubs.mkdir()
    (stubs / "curl").write_text(f'#!/bin/sh\necho "$@" >> {tmp}/phone.log\n')
    (stubs / "notify-send").write_text(f'#!/bin/sh\necho "$@" >> {tmp}/desktop.log\n')
    for f in stubs.iterdir():
        f.chmod(0o755)
    env = ms_env(tmp, ms, PATH=f"{stubs}:{os.environ['PATH']}")
    n = str(ms / "bin/notify.sh")
    (ms / "state/paused").mkdir(parents=True)
    (ms / "state/paused/phone").write_text("0\n")
    subprocess.run([n, "A failed", "x", "high"], env=env, check=True)
    assert "A failed" in (tmp / "desktop.log").read_text() and not (tmp / "phone.log").exists()
    (ms / "state/paused/notifications").write_text(str(int(time.time()) + 600) + "\n")
    subprocess.run([n, "B failed", "x", "high"], env=env, check=True)
    assert "B failed" not in (tmp / "desktop.log").read_text()
    assert "B failed" in (ms / "reports/notifications.log").read_text(), "still logged"
    for f in (ms / "state/paused").iterdir():
        f.unlink()
    subprocess.run([n, "C failed", "x", "high"], env=env, check=True)
    assert "C failed" in (tmp / "phone.log").read_text()


def test_queue_remote_watcher_report():
    out, notes, tmp = py(r'''
from mysys import pause, brief, watcher
pause.set_pause("queue", 0)
a = queue.submit("echo hi", cwd=W, name="a", **small)
S.tick(); S.tick()
assert st(a["id"]) == "queued" and "queue is paused" in queue.load(a["id"])["reason"], queue.load(a["id"])
assert any("paused: queue" in x for x in brief.attention())
pause.resume("queue")
assert until(lambda: st(a["id"]) == "done")
open(os.path.join(MS, "config", "remotes.conf"), "w").write("[titan]\nssh = titan\naccount = me\n")
pause.set_pause("remote", 0)
r = queue.submit("echo hi", cwd=W, name="r", remote="titan", **small)
S.tick()
assert st(r["id"]) == "queued" and "servers is paused" in queue.load(r["id"])["reason"]
queue.cancel(r["id"])
pause.set_pause("status-report", 0)
s = {}
t8 = time.mktime(time.strptime(time.strftime("%Y-%m-%d") + " 08:05", "%Y-%m-%d %H:%M"))
assert not brief.due(s, t8)
pause.set_pause("watcher", 0)
probes.meminfo = lambda: {"MemTotal": 64 * G, "MemAvailable": 1 * G, "SwapTotal": 0, "SwapFree": 0}
watcher.jobs = lambda: []
watcher.tick()
print("ok")
''')
    assert "ok" in out
    assert "RAM almost exhausted" not in notes, notes


def test_daily_backup_and_ai_guard():
    tmp, ms = fake_ms()
    for f in ("common.sh", "run_daily.sh", "backup.sh"):
        shutil.copy(BIN / f, ms / "bin" / f)
        (ms / "bin" / f).chmod(0o755)
    (ms / "config/backup.conf").write_text('REPO=""\nKEYRING_ATTR="x y"\n')
    (ms / "state/paused").mkdir(parents=True)
    (ms / "state/paused/all").write_text("0\n")
    env = ms_env(tmp, ms)
    r = subprocess.run([str(ms / "bin/run_daily.sh")], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "daily check paused" in r.stdout, r.stdout + r.stderr
    r = subprocess.run([str(ms / "bin/backup.sh")], env=env, capture_output=True, text=True)
    assert "backup paused" in r.stdout, r.stdout + r.stderr
    r = subprocess.run([str(ms / "bin/backup.sh")], env=dict(env, MS_FORCE="1"), capture_output=True, text=True)
    assert "backup paused" not in r.stdout and "no repository configured" in r.stdout, r.stdout + r.stderr
    # an AI assistant may not pause, resume, unlock or lock my-system
    import importlib.util
    spec = importlib.util.spec_from_file_location("g", ROOT / ("src/ai" if (ROOT / "src").is_dir() else "ai") / "claude/hooks/guard.py")
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    for c in ("ms pause queue", "ms resume all", "cd x && ms pause notifications --for 2h", "~/My_System/bin/ms pause all"):
        assert g.check_bash(c, []), c
    for c in ("ms paused", "ms q list", "ms brief"):
        assert not g.check_bash(c, []), c


if __name__ == "__main__":
    t0 = time.time()
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            fn()
            print("ok", k)
    print(f"all passed in {time.time() - t0:.1f}s")
