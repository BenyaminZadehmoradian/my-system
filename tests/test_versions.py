"""XILINX_ALLOWED: only one AMD/Xilinx tool version for queue jobs and AI assistants. Run: python3 tests/test_versions.py"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_features import fake_ms, HARNESS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def setup():
    tmp, ms = fake_ms()
    # fake installs: the allowed one and two others; each has a "vivado" program that just prints its version
    inst = {}
    for label, rel in (("vivado_2025.2.1", "Apps/2025.2.1"), ("vitis_hls_2023.2", "Xilinx/Vitis_HLS/2023.2"),
                       ("ise_14.7", "Xilinx/14.7")):
        d = tmp / "tools" / rel
        (d / "bin").mkdir(parents=True)
        exe = d / "bin" / "vivado"
        exe.write_text(f"#!/bin/sh\necho version-{label}\nsleep 20\n")
        exe.chmod(0o755)
        inst[label] = str(d)
    (ms / "state").mkdir(exist_ok=True)
    (ms / "state/detected.json").write_text(json.dumps({"tools": [[k, v] for k, v in inst.items()]}))
    with open(ms / "config/my_system.conf", "a") as f:
        f.write('\nXILINX_ALLOWED="2025.2"\n')
    return tmp, ms, inst


def test_checks_and_map():
    tmp, ms, inst = setup()
    env = dict(os.environ, MY_SYSTEM=str(ms), HOME=str(tmp))
    code = r'''
import os, sys
sys.path.insert(0, os.path.join(os.environ["MY_SYSTEM"], "lib"))
from mysys import versions, aictx
I = %r
assert versions.check_text(I["vivado_2025.2.1"] + "/bin/vivado -mode batch") is None
assert versions.check_text("source /mnt/data/Apps/2025.2.1/Vivado/settings64.sh && vitis-run --mode hls") is None
assert versions.check_text(I["vitis_hls_2023.2"] + "/bin/vitis_hls -f run.tcl") == "2023.2"
assert versions.check_text("source /opt/Xilinx/Vivado/2023.1/settings64.sh") == "2023.1"
assert versions.check_text("/tools/Xilinx/SDK/2019.1/bin/xsdk") == "2019.1"
assert versions.check_text("source /opt/Xilinx/14.7/ISE_DS/settings64.sh") == "14.7"
assert versions.check_text("make sim; python3 run.py --seed 2023") is None
t = aictx.build()
assert "tool versions per project" in t and "**2025.2**" in t and "Do not run" in t and "2023.2" in t, t
print("ok")
''' % (inst,)
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert "ok" in r.stdout, r.stdout + r.stderr
    src = ROOT / ("src/ai" if (ROOT / "src").is_dir() else "ai") / "claude/hooks/guard.py"
    code = f'''
import importlib.util
spec = importlib.util.spec_from_file_location("g", {str(src)!r}); g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
assert g.check_bash("source /opt/Xilinx/Vivado/2023.2/settings64.sh && vivado", [])
assert g.check_bash({inst["vitis_hls_2023.2"] + "/bin/vitis_hls -version"!r}, [])
assert not g.check_bash({inst["vivado_2025.2.1"] + "/bin/vivado -version"!r}, [])
assert not g.check_bash("ms q add -- make", [])
print("ok")
'''
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert "ok" in r.stdout, r.stdout + r.stderr


def test_queue_jobs():
    tmp, ms, inst = setup()
    (tmp / "work").mkdir(exist_ok=True)
    (tmp / "work" / "old.sh").write_text(f"#!/bin/sh\n{inst['ise_14.7']}/bin/vivado\n")
    env = dict(os.environ, MY_SYSTEM=str(ms), MS_NO_CGROUP="1", HOME=str(tmp), WORK=str(tmp / "work"))
    code = HARNESS + r'''
I = %r
good = queue.submit(I["vivado_2025.2.1"] + "/bin/vivado & sleep 1; kill %%1; echo fine", cwd=W, name="good", **small)
named = queue.submit(I["vitis_hls_2023.2"] + "/bin/vivado", cwd=W, name="named", **small)
hidden = queue.submit("sh old.sh", cwd=W, name="hidden", **small)
assert until(lambda: all(st(x["id"]) in queue.TERMINAL for x in (good, named, hidden)), 60)
jg, jn, jh = (queue.load(x["id"]) for x in (good, named, hidden))
assert jg["state"] == "done", jg
assert jn["state"] == "failed" and "2023.2 is not allowed" in jn["reason"], jn
assert jh["state"] == "failed" and "14.7 is not allowed" in jh["reason"], jh
assert jh["ended"] - jh["started"] < 15, "stopped within seconds, not after the 20 s run"
r0 = queue.submit("/opt/Xilinx/Vivado/2023.2/bin/vivado -version", cwd=W, name="at_once")
assert r0["state"] == "failed" and "2023.2 is not allowed" in r0["reason"], r0
from mysys import qcli
assert qcli._ids(["named"]) == [named["id"]] and qcli._ids(["3,4"]) == [3, 4]
print("VERSIONS-OK")
''' % (inst,)
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=300)
    assert "VERSIONS-OK" in r.stdout, r.stdout[-3000:] + r.stderr[-3000:]


def test_per_project_rules():
    tmp, ms, inst = setup()
    proj = tmp / "Desktop" / "MCGEMMA"
    (proj / "sub").mkdir(parents=True)
    other = tmp / "Desktop" / "HLS_DSE"
    other.mkdir(parents=True)
    d = json.loads((ms / "state/detected.json").read_text())
    d["projects"] = [str(proj), str(other)]
    (ms / "state/detected.json").write_text(json.dumps(d))
    env = dict(os.environ, MY_SYSTEM=str(ms), MS_NO_CGROUP="1", HOME=str(tmp), WORK=str(tmp / "work"))
    m = [sys.executable, str(ms / "bin/ms")]
    r = subprocess.run(m + ["tools", "allow", "mcgemma", "14.7"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "MCGEMMA: 2025.2 / 14.7" in r.stdout, r.stdout + r.stderr
    r = subprocess.run(m + ["tools", "allow", "nosuch", "14.7"], env=env, capture_output=True, text=True)
    assert r.returncode == 2 and "no project named" in r.stdout
    code = HARNESS + r'''
I = %r
P, O = %r, %r
from mysys import versions, aictx
ise = I["ise_14.7"] + "/bin/vivado"
assert versions.check_text(ise, os.path.join(P, "sub")) is None, "MCGEMMA (and its subfolders) may use ISE"
assert versions.check_text(ise, O) == "14.7", "other projects may not"
assert versions.check_text(I["vivado_2025.2.1"] + "/bin/vivado", O) is None
a = queue.submit(ise + " & sleep 1; kill %%1; echo ok", cwd=P, name="ise_ok", **small)
b = queue.submit(ise, cwd=O, name="ise_no", **small)
assert b["state"] == "failed" and "not allowed for this project" in b["reason"] and "2025.2" in b["reason"], b
assert until(lambda: st(a["id"]) in queue.TERMINAL, 40)
assert st(a["id"]) == "done", queue.load(a["id"])
t = aictx.build()
assert "MCGEMMA" in t and "2025.2 / 14.7" in t, t
print("PER-PROJECT-OK")
''' % (inst, str(proj), str(other))
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=300)
    assert "PER-PROJECT-OK" in r.stdout, r.stdout[-3000:] + r.stderr[-3000:]
    src = ROOT / ("src/ai" if (ROOT / "src").is_dir() else "ai") / "claude/hooks/guard.py"
    code = f'''
import importlib.util, json, io, sys
spec = importlib.util.spec_from_file_location("g", {str(src)!r}); g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
g.CWD = {str(proj)!r}
assert not g.check_bash("source " + {inst["ise_14.7"] + "/settings64.sh"!r}, [])
g.CWD = {str(other)!r}
assert g.check_bash("source " + {inst["ise_14.7"] + "/settings64.sh"!r}, [])
assert not g.check_bash("ls -l " + {inst["ise_14.7"] + "/bin/vivado"!r}, []), "only listing is fine"
assert g.check_bash(\'"ms" tools allow HLS_DSE 14.7\', []) and g.check_bash("python3 -m mysys.versions allow X 14.7", [])
for c in ("ms tools allow HLS_DSE 14.7", "ms tools default '*'", "ms tools reset MCGEMMA"):
    assert g.check_bash(c, []), c
assert not g.check_bash("ms tools", [])
print("ok")
'''
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert "ok" in r.stdout, r.stdout + r.stderr


def test_only_the_user_changes_rules():
    import tempfile
    tmp, ms, inst = setup()
    with open(ms / "config/my_system.conf", "a") as f:
        f.write('\nAI_PROCESS_NAMES="fakeai"\n')
    d = Path(tempfile.mkdtemp())
    ai = d / "fakeai"                                  # stands in for an AI assistant process
    ai.write_text('#!/bin/sh\n"$@"\n')
    ai.chmod(0o755)
    env = dict(os.environ, MY_SYSTEM=str(ms), HOME=str(tmp))
    m = [sys.executable, str(ms / "bin/ms")]
    r = subprocess.run([str(ai)] + m + ["tools", "default", "*"], env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "may not change tool access" in r.stdout, r.stdout + r.stderr
    r = subprocess.run([str(ai)] + m + ["pause", "queue"], env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "may not pause" in r.stdout + r.stderr, r.stdout + r.stderr
    r = subprocess.run(m + ["tools", "default", "2025.2"], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    code = r"""
import os, sys, json
sys.path.insert(0, os.path.join(os.environ["MY_SYSTEM"], "lib"))
from mysys import versions as V
json.dump({"default": [], "projects": {}}, open(V.FILE, "w"))
assert V.rules()["default"] == [] and not V.allowed("2025.2.1"), "an empty default allows nothing"
print("ok")
"""
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert "ok" in r.stdout, r.stdout + r.stderr


if __name__ == "__main__":
    t0 = time.time()
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            fn()
            print("ok", k)
    print(f"all passed in {time.time() - t0:.1f}s")
