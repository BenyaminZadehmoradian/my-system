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
assert "only 2025.2" in t and "NOT allowed" in t and "2023.2" in t, t
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
from mysys import qcli
assert qcli._ids(["named"]) == [named["id"]] and qcli._ids(["3,4"]) == [3, 4]
print("VERSIONS-OK")
''' % (inst,)
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=300)
    assert "VERSIONS-OK" in r.stdout, r.stdout[-3000:] + r.stderr[-3000:]


if __name__ == "__main__":
    t0 = time.time()
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            fn()
            print("ok", k)
    print(f"all passed in {time.time() - t0:.1f}s")
