"""AI assistants must start Vivado/Vitis/ISE through the queue. Run: python3 tests/test_ai_queue.py"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_features import fake_ms  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / ("src/ai" if (ROOT / "src").is_dir() else "ai") / "claude/hooks/guard.py"


def test_guard_blocks_direct_heavy_runs():
    tmp, ms = fake_ms()
    proj = tmp / "MCGEMMA" / "hardware"
    (proj / "runners").mkdir(parents=True)
    (proj / "runners/ise_env.sh").write_text('#!/bin/bash\nsource /opt/Xilinx/14.7/ISE_DS/settings64.sh\nexec "$@"\n')
    (proj / "full_sweep.tcl").write_text('foreach n {5 6} { exec xtclsh impl_run.tcl --nc $n & }\n')
    (proj / "parse.py").write_text('import re\nfor l in open("vivado.log"): print(l)\n')
    (proj / "run_impl.sh").write_text('#!/bin/sh\nvivado -mode batch -source impl.tcl\n')
    (proj / "Makefile").write_text('impl:\n\tvivado -mode batch -source impl.tcl\n')
    (proj / "submit.sh").write_text('#!/bin/sh\nms q add --name impl -- vivado -mode batch -source impl.tcl\n')
    for f in ("run_impl.sh", "submit.sh"):
        (proj / f).chmod(0o755)
    env = dict(os.environ, MY_SYSTEM=str(ms), HOME=str(tmp))
    code = f'''
import importlib.util
spec = importlib.util.spec_from_file_location("g", {str(SRC)!r}); g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
g.CWD = {str(proj)!r}
PYC = "python3 -c " + repr("import subprocess; subprocess.run(['vivado','-mode','batch'])")
bad = ["vivado -mode batch -source impl.tcl", "nohup vivado -mode batch -source x.tcl &",
       "bash runners/ise_env.sh xtclsh full_sweep.tcl", "./run_impl.sh", "make impl", "cd x && vitis-run --mode hls --csim",
       "timeout 2h xtclsh full_sweep.tcl", "/mnt/data/Apps/2025.2.1/Vivado/bin/vivado -mode tcl -source a.tcl",
       "$(which vivado) -mode batch -source a.tcl", "xargs -n1 vivado -mode batch -source", "eval vivado -mode batch",
       "flock /tmp/l vivado -mode batch", "unbuffer vivado -mode batch", "timeout -s KILL 10h vivado -mode batch",
       "docker run img vivado -mode batch", PYC,
       "bash -c 'vivado -mode batch -source a.tcl'", "cd .. && cd hardware && ./run_impl.sh", "make -C . impl",
       "bash -lc 'vivado -mode batch'", "sh -xc 'vivado -mode batch'", "ms log run --name x -- vivado -mode batch",
       "ms sem lic -- vivado -mode batch", "exec -a foo vivado -mode batch", "find . -name a.tcl -exec vivado -mode batch -source {{}} ;",
       "script -qc 'vivado -mode batch' /dev/null", "parallel -j4 vivado -mode batch -source ::: a.tcl b.tcl",
       "systemd-run --user vivado -mode batch", "V=vivado; $V -mode batch"]
good = ["ms q add --name impl --cores 4 -- vivado -mode batch -source impl.tcl", "vivado -version", "xsim --help",
        "python3 parse.py", "grep -i vivado impl.log", "cat vivado.log | tail", "ls /mnt/data/Apps/2025.2.1/Vivado",
        "~/My_System/bin/ms flow run sweep.yaml", "git status",
        "ms q add --name x -- " + chr(34) + "cd hw && vivado -mode batch -source a.tcl" + chr(34),
        "ms q add -- sh -c 'cd hw; vivado -mode batch'",
        "git commit -m " + chr(34) + "(vivado -mode batch)" + chr(34), "./submit.sh", "echo 'run vivado -mode batch later'"]
for c in bad:
    assert g.check_bash(c, []), c
for c in good:
    assert not g.check_bash(c, []), (c, g.check_bash(c, []))
print("ok")
'''
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=str(proj))
    assert "ok" in r.stdout, r.stdout + r.stderr
    with open(ms / "config/my_system.conf", "a") as f:
        f.write("\nAI_HEAVY_VIA_QUEUE=0\n")
    code2 = code.replace("for c in bad:\n    assert g.check_bash(c, []), c", "for c in bad:\n    assert not g.check_bash(c, []), c")
    r = subprocess.run([sys.executable, "-c", code2], env=env, capture_output=True, text=True, cwd=str(proj))
    assert "ok" in r.stdout, "switch off works: " + r.stdout + r.stderr


def test_watcher_tells_when_ai_ran_outside_queue():
    tmp, ms = fake_ms()
    d = Path(tempfile.mkdtemp())
    fake = d / "claude"                      # a process named "claude" that starts a long tool run
    fake.write_text("#!/bin/sh\nsleep 30\n")
    fake.chmod(0o755)
    with open(ms / "config/my_system.conf", "a") as f:
        f.write('\nAI_PROCESS_NAMES="claude"\n')
    env = dict(os.environ, MY_SYSTEM=str(ms), HOME=str(tmp))
    code = f'''
import os, sys, subprocess, time
sys.path.insert(0, os.path.join(os.environ["MY_SYSTEM"], "lib"))
from mysys import watcher
p = subprocess.Popen(["{fake}"])
time.sleep(0.5)
notes = []
watcher.notify = lambda t, m, *a, **k: notes.append((t, m))
cur = [dict(tool="vivado", cwd="/home/x/MCGEMMA/hardware/.work/impl-1", pids=[p.pid], key="k")]
s = {{}}
watcher.ai_outside_queue(cur, s, time.time())
watcher.ai_outside_queue(cur, s, time.time())      # once, not every minute
p.kill()
assert len(notes) == 1 and "claude started 1 vivado run" in notes[0][1], notes
print("ok")
'''
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert "ok" in r.stdout, r.stdout + r.stderr


if __name__ == "__main__":
    t0 = time.time()
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            fn()
            print("ok", k)
    print(f"all passed in {time.time() - t0:.1f}s")
