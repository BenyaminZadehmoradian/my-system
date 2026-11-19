"""1.26: reports about my-system from AI sessions (ms feedback). Run: python3 tests/test_v25.py"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_features import py  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def test_feedback_from_ai_sessions():
    out, notes, tmp = py(r'''
import subprocess, shutil, importlib.util, json
ROOT = %r
shutil.copytree(os.path.join(ROOT, "src", "ai"), os.path.join(MS, "ai"))
msbin = [sys.executable, os.path.join(MS, "bin", "ms")]
ai = dict(os.environ, CLAUDECODE="1")
d = os.path.join(W, "proj", "run1"); os.makedirs(d)
j = queue.submit("echo 'FAIL: x'; exit 1", cwd=d, name="pj", **small)
assert until(lambda: st(j["id"]) in queue.TERMINAL, 30)
r = subprocess.run(msbin + ["feedback", "add", "--kind", "bug", "--job", str(j["id"]), "server path check flags JS strings as paths",
                            "--", "plab_ip waits here because of /data/vivado_kit.js which is text in a JS file"],
                   cwd=d, env=ai, capture_output=True, text=True)
assert r.returncode == 0 and "report #1 filed" in r.stdout, r.stdout + r.stderr
f = json.load(open(os.path.join(MS, "state", "feedback", "1.json")))
assert f["by"] == "ai" and f["kind"] == "bug" and f["jobs"][0]["id"] == j["id"] and "FAIL: x" in f["jobs"][0]["last_line"], f
assert "Report #1" in open(os.path.join(os.environ["HOME"], "notes")).read() if os.path.exists(os.path.join(os.environ["HOME"], "notes")) else True
# the same problem again: added to #1, not a second report
r = subprocess.run(msbin + ["feedback", "add", "Server path check flags JS strings as paths!", "--", "again"], cwd=W, env=ai, capture_output=True, text=True)
assert "added to report #1" in r.stdout, r.stdout
r = subprocess.run(msbin + ["feedback", "add", "--kind", "wish", "show expected end time of sweeps"], cwd=W, env=ai, capture_output=True, text=True)
assert "report #2 filed" in r.stdout, r.stdout
lst = subprocess.run(msbin + ["feedback", "list"], env=ai, capture_output=True, text=True).stdout
assert "#1" in lst and "(+1)" in lst and "#2" in lst, lst
show = subprocess.run(msbin + ["feedback", "show", "1"], env=ai, capture_output=True, text=True).stdout
assert "vivado_kit.js" in show and f"job #{j['id']} pj failed" in show and "+ ai" in show, show
# only the user decides: the guard blocks AI sessions, the command itself works for the user
spec = importlib.util.spec_from_file_location("g", os.path.join(MS, "ai", "claude", "hooks", "guard.py"))
g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
for c in ("ms feedback accept 1 ok", "ms feedback reject 2 no", "cd x && ms feedback done 1"):
    assert g.check_bash(c, []), c
for c in ("ms feedback add 'x' -- y", "ms feedback list", "ms feedback show 1"):
    assert not g.check_bash(c, []), c
assert subprocess.run(msbin + ["feedback", "reject", "2"], env=os.environ, capture_output=True, text=True).returncode == 2   # needs a reason
subprocess.run(msbin + ["feedback", "reject", "2", "the", "app", "already", "shows", "it"], env=os.environ, check=True, capture_output=True)
subprocess.run(msbin + ["feedback", "accept", "1", "will", "only", "scan", "shell/tcl", "files"], env=os.environ, check=True, capture_output=True)
from mysys import feedback, aihelp, policy, mcp
s = aihelp.status_text(d)
assert "#2 rejected: show expected end time" in s and "the app already shows it" in s and "#1 accepted" in s, s
assert "ms feedback add" in s
# a version whose changelog names it marks it done
cl = os.path.join(W, "CHANGELOG.md"); open(cl, "w").write("# Changelog\n\n## 9.9.9 — x\n- paths: only scripts are scanned (feedback #1)\n")
assert feedback.resolve_from_changelog(cl) == [1] and feedback.load(1)["status"] == "done" and feedback.load(1)["fixed_in"] == "9.9.9"
assert feedback.resolve_from_changelog(cl) == []
al = policy.build()["permissions"]["allow"]
assert "Bash(ms feedback add *)" in al and not any("accept" in x or "reject" in x for x in al)
assert "report #3 filed" in mcp.TOOLS["feedback_add"][0]("rules: say how to queue ISE jobs", kind="rule", cwd=W)
assert "rules: say how" in mcp.TOOLS["feedback_list"][0]()
rules = open(os.path.join(MS, "ai", "AI_RULES.md")).read()
assert "ms feedback add" in rules and len(rules) < 9000
print("FB-OK")
''' % str(ROOT))
    assert "FB-OK" in out, out


if __name__ == "__main__":
    t0 = time.time()
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            fn()
            print("ok", k)
    print(f"all passed in {time.time() - t0:.1f}s")
