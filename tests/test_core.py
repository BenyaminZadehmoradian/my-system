"""Core tests: guard hook, watcher batching, detection, policy. Run: python3 tests/test_core.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def make_ms():
    """A throw-away my-system folder with the source code and a minimal config."""
    tmp = Path(tempfile.mkdtemp())
    ms = tmp / "My_System"
    for d in ("bin", "lib", "plugins", "ai"):
        shutil.copytree(SRC / d, ms / d)
    (ms / "config").mkdir()
    (ms / "state").mkdir()
    (ms / "reports").mkdir()
    (ms / "config" / "protected_paths.txt").write_text("/etc\n/opt\n~/.bashrc\n~/.venvs\n~/My_System\n")
    (ms / "config" / "plugins.conf").write_text("vivado\nvitis_hls\n")
    (ms / "config" / "my_system.conf").write_text('MIN_RUN=60\nBATCH_QUIET=300\n')
    note = ms / "bin" / "notify.sh"
    note.write_text(f'#!/bin/sh\necho "$1|$2|$3" >> {tmp}/notes\n')
    note.chmod(0o755)
    return tmp, ms


def guard(ms, home, tool, inp):
    env = dict(os.environ, MY_SYSTEM=str(ms), HOME=str(home))
    r = subprocess.run([sys.executable, str(ms / "ai/claude/hooks/guard.py")],
                       input=json.dumps({"tool_name": tool, "tool_input": inp}), capture_output=True, text=True, env=env)
    return r.returncode == 2


def test_guard():
    tmp, ms = make_ms()
    home = ms.parent
    block = ['echo "x" >> ~/.bashrc', "sed -i s/a/b/ /etc/hosts", "cp x /etc/hosts", "rm -rf $HOME/.venvs/a",
             "sudo apt install x", "pkill vivado", "tee -a ~/.bashrc <<< x", "echo hi > ~/My_System/ai/AI_RULES.md",
             "systemctl stop docker", "pip install numpy --break-system-packages",
             "python3 -c \"open('/etc/hosts','w').write('x')\""]
    allow = ["cat ~/.bashrc 2>/dev/null", "ls /usr/bin > out.txt", "sed s/a/b/ /etc/hosts > /tmp/h", "cp /etc/hosts ./h",
             "make -j8 2>&1 | tee build.log", "git status", "cat ~/My_System/reports/latest/health.txt",
             "grep -r foo /opt/x | head", "python3 -m venv .venv && .venv/bin/pip install numpy",
             "systemctl --user status my-system-daily", "mv ~/proj/a ~/proj/b"]
    bad = [c for c in block if not guard(ms, home, "Bash", {"command": c})]
    bad += [c for c in allow if guard(ms, home, "Bash", {"command": c})]
    assert not bad, f"misclassified: {bad}"
    assert guard(ms, home, "Edit", {"file_path": str(home / ".bashrc")})
    assert not guard(ms, home, "Edit", {"file_path": str(home / "proj/a.py")})
    log = next((ms / "reports/ai_audit").glob("*.jsonl")).read_text().splitlines()
    assert len(log) == len(block) + len(allow) + 2


def test_watcher_batch():
    tmp, ms = make_ms()
    env = dict(os.environ, MY_SYSTEM=str(ms))
    code = f"""
import sys, json, time
sys.path.insert(0, {str(ms / 'lib')!r}); sys.path.insert(0, {str(ms / 'plugins')!r})
from mysys import watcher, plugins
fake = []
watcher.jobs = lambda: [dict(j) for j in fake]
def J(cwd, start): return {{"tool": "vivado", "plugin": "vivado", "cwd": cwd, "start": start, "cpu_ticks": 0, "rss": 1,
                            "pids": [1], "key": f"vivado|{{cwd}}|{{start}}"}}
now = time.time()
base = {str(tmp)!r}
import os
for d in ("S18/doe_1", "S18/doe_2", "S18/doe_3"):
    os.makedirs(base + "/" + d, exist_ok=True)
open(base + "/S18/doe_2/vivado.log", "w").write("ERROR: [Place 30-1] placement failed\\n")
fake[:] = [J(base + "/S18/doe_1", now - 900), J(base + "/S18/doe_2", now - 900), J(base + "/S18/doe_3", now - 900)]
watcher.tick()                    # adopt
fake[:] = []
watcher.tick()                    # all finished -> 1 immediate failure alert
s = watcher.load_state()
for b in s["pending"].values(): b["last_end"] -= 400
watcher.save_state(s)
watcher.tick()                    # quiet period over -> 1 summary
"""
    subprocess.run([sys.executable, "-c", code], env=env, check=True)
    notes = (tmp / "notes").read_text().splitlines()
    assert len(notes) == 2, notes
    assert notes[0].startswith("vivado failed: 1 ERROR(s)") and "placement failed" in notes[0]
    assert notes[1].startswith("Finished: 3 vivado runs in S18/") and "2 OK, 1 failed" in notes[1]
    rows = (ms / "reports/results.csv").read_text().splitlines()
    assert len(rows) == 4


def test_detect_tools():
    sys.path.insert(0, str(SRC / "lib"))
    from mysys import detect
    f = Path(tempfile.mkdtemp())
    for p in ["mnt/data/Apps/2025.2.1/Vivado/bin/vivado", "opt/Xilinx/Vivado/2019.1/bin/vivado",
              "home/u/.local/share/oss-cad-suite/bin/yosys", "home/u/MATLAB/R2026a/bin/matlab"]:
        (f / p).parent.mkdir(parents=True, exist_ok=True)
        (f / p).touch()
    (f / "opt/Xilinx/14.7/ISE_DS").mkdir(parents=True)
    got = {l: str(Path(p).relative_to(f)) for l, p in detect.tool_installs([str(f / "opt"), str(f / "mnt/data"), str(f / "home/u")], docker=False)}
    assert got == {"ise_14.7": "opt/Xilinx/14.7", "matlab_R2026a": "home/u/MATLAB/R2026a",
                   "oss-cad-suite": "home/u/.local/share/oss-cad-suite", "vivado_2019.1": "opt/Xilinx/Vivado/2019.1",
                   "vivado_2025.2.1": "mnt/data/Apps/2025.2.1"}, got


def test_policy():
    tmp, ms = make_ms()
    env = dict(os.environ, MY_SYSTEM=str(ms))
    out = subprocess.run([sys.executable, "-c", f"import sys, json; sys.path.insert(0, {str(ms / 'lib')!r}); "
                          "from mysys import policy; print(json.dumps(policy.build()))"],
                         env=env, capture_output=True, text=True, check=True).stdout
    p = json.loads(out)
    assert "Bash(sudo *)" in p["permissions"]["deny"]
    assert any(d.startswith("Edit(//etc") for d in p["permissions"]["deny"])
    assert "guard.py" in p["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert p["permissions"]["disableBypassPermissionsMode"] == "disable"


if __name__ == "__main__":
    t0 = time.time()
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            fn()
            print("ok", k)
    print(f"all passed in {time.time() - t0:.1f}s")
