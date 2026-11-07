"""Remote Slurm server (Titan-like) against a simulated cluster: fake ssh/scp/sbatch/squeue/sacct/sinfo/scancel.
The fake head node is a folder; compute nodes fetch files with scp like on Titan (or see a shared disk).
Run: python3 tests/test_remote.py"""
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_features import HARNESS, fake_ms  # noqa: E402
ROOT = Path(__file__).resolve().parent.parent

STUBS = {
    # ssh [opts] HOST CMD  -> run CMD in the head node's home
    "ssh": r'''#!/bin/bash
while [ $# -gt 0 ]; do case "$1" in -o|-p|-i|-l) shift 2;; -*) shift;; *) break;; esac; done
host="$1"; shift
[ "$host" = "deadhost" ] && { echo "ssh: connect to host deadhost: Connection refused" >&2; exit 255; }
[ "$host" = "newhost" ] && { echo "Host key verification failed." >&2; exit 255; }
cd "$FAKE_HEAD" && HOME="$FAKE_HEAD" exec bash -c "$*"
''',
    # scp [opts] SRC DST ; host:path -> $FAKE_HEAD/path
    "scp": r'''#!/bin/bash
args=(); while [ $# -gt 0 ]; do case "$1" in -o) shift 2;; -*) shift;; *) args+=("$1"); shift;; esac; done
map() { case "$1" in *:*) p="${1#*:}"; case "$p" in /*) echo "$p";; *) echo "$FAKE_HEAD/$p";; esac;; *) echo "$1";; esac; }
cp -r "$(map "${args[0]}")" "$(map "${args[1]}")"
''',
    "sbatch": r'''#!/bin/bash
script="${@: -1}"
exec 9>"$FAKE_SLURM/lock"; flock 9          # job numbers are unique, like real Slurm
n=$(( $(cat "$FAKE_SLURM/seq" 2>/dev/null || echo 100) + 1 )); echo $n > "$FAKE_SLURM/seq"
flock -u 9
grep -q '^#SBATCH --account=' "$script" || { echo "sbatch: error: account required" >&2; exit 1; }
if [ "$FAKE_NOMEM" = 1 ] && grep -q '^#SBATCH --mem=' "$script"; then
  echo "sbatch: error: Memory specification can not be satisfied" >&2; exit 1; fi
cp "$script" "$FAKE_SLURM/$n.sh"
echo RUNNING > "$FAKE_SLURM/$n.state"
if [ "$FAKE_SHARED" = 1 ]; then run_in="$FAKE_HEAD"; else run_in=$(mktemp -d); fi
cat > "$FAKE_SLURM/$n.run" <<EOF
cd "$run_in"; bash "$FAKE_SLURM/$n.sh" > "$FAKE_SLURM/$n.out" 2>&1; rc=\$?
if [ "\$(cat "$FAKE_SLURM/$n.state")" != CANCELLED ]; then
  [ \$rc = 0 ] && echo COMPLETED > "$FAKE_SLURM/$n.state" || echo FAILED > "$FAKE_SLURM/$n.state"; echo \$rc > "$FAKE_SLURM/$n.rc"; fi
EOF
# like a real cluster: the job runs detached from whoever submitted it
setsid bash "$FAKE_SLURM/$n.run" > /dev/null 2>&1 < /dev/null &
echo $! > "$FAKE_SLURM/$n.pid"
echo "$n"
''',
    "squeue": r'''#!/bin/bash
# a busy controller: squeue answers nothing for a while (FAKE_SLURM/flaky holds how many more times)
if [ -f "$FAKE_SLURM/flaky" ]; then n=$(cat "$FAKE_SLURM/flaky"); if [ "$n" -gt 0 ]; then echo $((n - 1)) > "$FAKE_SLURM/flaky";
  echo "slurm_load_jobs error: Socket timed out on send/recv operation" >&2; exit 1; fi; fi
j=""; while [ $# -gt 0 ]; do case "$1" in -j) j="$2"; shift 2;; -o|-A) shift 2;; *) shift;; esac; done
for f in "$FAKE_SLURM"/*.state; do [ -e "$f" ] || continue; n=$(basename "$f" .state)
  [ -n "$j" ] && [ "$n" != "$j" ] && continue
  st=$(cat "$f"); [ "$st" = RUNNING ] && { [ -n "$j" ] && echo "RUNNING|node07|None" || echo RUNNING; }; done
''',
    "sacct": r'''#!/bin/bash
j=""; while [ $# -gt 0 ]; do case "$1" in -j) j="$2"; shift 2;; *) shift;; esac; done
echo "$(cat "$FAKE_SLURM/$j.state")|$(cat "$FAKE_SLURM/$j.rc" 2>/dev/null || echo 0):0"
''',
    "sinfo": r'''#!/bin/bash
m=64000; [ "$FAKE_NOMEM" = 1 ] && m=1
case "$*" in *-N*) echo "node07 16 $m idle"; echo "node08 16 $m mix";; *%m*) [ "$FAKE_SINFO_DOWN" = 1 ] && exit 1; echo $m; echo $m;; *) echo "8/24/0/32";; esac
''',
    "scancel": r'''#!/bin/bash
acct=""; for a in "$@"; do case "$a" in --account=*) acct="${a#--account=}";; esac; done
id="${@: -1}"
grep -q "^#SBATCH --account=$acct\$" "$FAKE_SLURM/$id.sh" || { echo "scancel: not your job" >&2; exit 1; }
echo CANCELLED > "$FAKE_SLURM/$id.state"; pkill -TERM -s "$(cat "$FAKE_SLURM/$id.pid")" 2>/dev/null; exit 0
''',
}


def cluster(tmp, shared):
    stubs, head, slurm = tmp / "stubs", tmp / "head", tmp / "slurm"
    for d in (stubs, head, slurm):
        d.mkdir(exist_ok=True)
    for n, body in STUBS.items():
        (stubs / n).write_text(body)
        (stubs / n).chmod(0o755)
    home = tmp
    (home / ".ssh").mkdir(exist_ok=True)
    (home / ".ssh/config").write_text("Host titan\n  Hostname bacchus.example\n  User slurmslave\n")
    return dict(PATH=f"{stubs}:{os.environ['PATH']}", FAKE_HEAD=str(head), FAKE_SLURM=str(slurm),
                FAKE_SHARED="1" if shared else "0", MSQ_REMOTE_POLL="0.3")


CONF = """[titan]
type = slurm
ssh = titan
head = titan
account = bzm
partition = batch
qos = batch_qos
max_jobs = 2
max_upload_gb = 0.001
offload = {offload}
offload_after_min = 0
"""


def run(code, shared=True, offload="never", extra=""):
    tmp, ms = fake_ms()
    (ms / "config/remotes.conf").write_text(CONF.format(offload=offload) + extra)
    env = dict(os.environ, MY_SYSTEM=str(ms), MS_NO_CGROUP="1", HOME=str(tmp), WORK=str(tmp / "work"))
    env.update(cluster(tmp, shared))
    r = subprocess.run([sys.executable, "-c", HARNESS + code], env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    notes = (tmp / "notes").read_text() if (tmp / "notes").exists() else ""
    return r.stdout, notes, tmp, ms, env


JOB = r'''
os.makedirs(os.path.join(W, "p"), exist_ok=True)
open(os.path.join(W, "p", "design.v"), "w").write("module top; endmodule\n")
a = queue.submit("cat design.v > copy.v; echo hello-from-node; echo wns=0.25 >> $MSQ_OUTPUTS; exit 0",
                 cwd=os.path.join(W, "p"), name="impl", mem_gb=4, cores=2, remote="titan")
b = queue.submit("echo broken; exit 3", cwd=os.path.join(W, "p"), name="bad", mem_gb=1, cores=1, remote="titan")
assert until(lambda: st(a["id"]) in queue.TERMINAL and st(b["id"]) in queue.TERMINAL, 90), (queue.load(a["id"]), queue.load(b["id"]))
ja, jb = queue.load(a["id"]), queue.load(b["id"])
log = open(queue.LOGS / f"{a['id']}.log").read()
assert ja["state"] == "done", (ja["state"], ja["reason"], log)
assert open(os.path.join(W, "p", "copy.v")).read() == "module top; endmodule\n", "result file came back"
assert ja["outputs"].get("wns") == "0.25", ja["outputs"]
assert "submitted as Slurm job" in log and "hello-from-node" in log and "results copied back" in log, log
assert ja.get("remote_job") and not os.path.exists(os.path.join(os.environ["FAKE_HEAD"], ja["remote_dir"])), "cleaned up there"
assert jb["state"] == "failed" and jb["rc"] == 3, (jb["state"], jb["rc"], jb["reason"])
script = open(os.path.join(os.environ["FAKE_SLURM"], ja["remote_job"] + ".sh")).read()
for want in ("--account=bzm", "--partition=batch", "--qos=batch_qos", "--cpus-per-task=2", "--mem=5G"):
    assert want in script, want
assert not any(f.startswith(".msq-") for f in os.listdir(os.path.join(W, "p"))), os.listdir(os.path.join(W, "p"))
assert os.path.exists(os.path.join(W, "p", f"msq-{a['id']}.log")) and os.path.exists(os.path.join(W, "p", f"msq-{b['id']}.log"))
print("ok")
'''


def test_remote_job_shared_disk():
    out, notes, *_ = run(JOB, shared=True)
    assert "ok" in out


def test_remote_job_node_fetches_with_scp():
    out, notes, *_ = run(JOB, shared=False)
    assert "ok" in out


def test_cancel_limits_and_refusals():
    out, notes, tmp, ms, env = run(r'''
os.makedirs(os.path.join(W, "q"), exist_ok=True)
open(os.path.join(W, "q", "a.txt"), "w").write("x")
c = queue.submit("sleep 60", cwd=os.path.join(W, "q"), name="long", mem_gb=1, cores=1, remote="titan")
assert until(lambda: queue.load(c["id"]).get("remote_job"), 30)
d = queue.submit("sleep 60", cwd=os.path.join(W, "q"), name="long2", mem_gb=1, cores=1, remote="titan")
e = queue.submit("true", cwd=os.path.join(W, "q"), name="third", mem_gb=1, cores=1, remote="titan")
assert until(lambda: st(d["id"]) == "running", 30)
S.tick()
assert st(e["id"]) == "queued" and "2 job(s) already on titan" in queue.load(e["id"])["reason"]
queue.cancel(c["id"]); queue.cancel(d["id"]); queue.cancel(e["id"])
assert until(lambda: st(c["id"]) in queue.TERMINAL and not queue._alive(queue.load(c["id"]).get("pid")), 40)
sid = queue.load(c["id"])["remote_job"]
assert open(os.path.join(os.environ["FAKE_SLURM"], sid + ".state")).read().strip() == "CANCELLED"
# too big without declared inputs, then fine with --inputs
os.makedirs(os.path.join(W, "big"), exist_ok=True)
open(os.path.join(W, "big", "huge.bin"), "wb").write(os.urandom(2 * 2**20))
open(os.path.join(W, "big", "top.v"), "w").write("module t; endmodule\n")
f = queue.submit("ls > files.txt", cwd=os.path.join(W, "big"), name="big", mem_gb=1, cores=1, remote="titan")
assert until(lambda: st(f["id"]) in queue.TERMINAL, 40)
assert st(f["id"]) == "failed" and "limit" in open(queue.LOGS / f"{f['id']}.log").read()
g = queue.submit("ls > files.txt", cwd=os.path.join(W, "big"), name="big2", mem_gb=1, cores=1, remote="titan", inputs=["*.v"])
assert until(lambda: st(g["id"]) in queue.TERMINAL, 40), queue.load(g["id"])
assert st(g["id"]) == "done" and "huge.bin" not in open(os.path.join(W, "big", "files.txt")).read()
h = queue.submit("true", cwd=os.path.join(W, "q"), name="sec", mem_gb=1, cores=1, remote="titan", secrets=["TOKEN"])
assert until(lambda: st(h["id"]) in queue.TERMINAL, 30)
assert st(h["id"]) == "failed" and "never sent" in open(queue.LOGS / f"{h['id']}.log").read()
x = queue.submit("true", cwd=os.path.join(W, "q"), name="nope", mem_gb=1, cores=1, remote="nosuch")
S.tick()
assert st(x["id"]) == "failed" and "no remote 'nosuch'" in queue.load(x["id"])["reason"]
print("ok")
''')
    assert "ok" in out


def test_auto_offload_when_busy():
    out, notes, *_ = run(r'''
probes.meminfo = lambda: {"MemTotal": 16 * G, "MemAvailable": 3 * G, "SwapTotal": 0, "SwapFree": 0}
os.makedirs(os.path.join(W, "o"), exist_ok=True)
a = queue.submit("echo ran-there > where.txt", cwd=os.path.join(W, "o"), name="auto", mem_gb=8, cores=2, remote="auto")
loc = queue.submit("true", cwd=os.path.join(W, "o"), name="local_only", mem_gb=8, cores=2)
assert until(lambda: st(a["id"]) in queue.TERMINAL, 60), queue.load(a["id"])
ja = queue.load(a["id"])
assert ja["state"] == "done" and ja["remote"] == "titan" and "sent to titan" in ja.get("start_note", ""), ja
assert open(os.path.join(W, "o", "where.txt")).read().strip() == "ran-there"
S.tick()
assert st(loc["id"]) == "queued" and not queue.load(loc["id"]).get("remote"), "jobs without --on auto stay here"
ex = dict((c, d) for ok, c, d in queue.explain(loc["id"]))
print("ok")
''', offload="busy")
    assert "ok" in out


def test_prefer_server_first_with_fallback():
    out, notes, tmp, ms, env = run(r'''
from mysys import remote as RM
os.makedirs(os.path.join(W, "f"), exist_ok=True)
# plenty of room here, but the server comes first
a = queue.submit("echo ran > where.txt", cwd=os.path.join(W, "f"), name="sim", mem_gb=1, cores=1)
assert until(lambda: st(a["id"]) in queue.TERMINAL, 60), queue.load(a["id"])
ja = queue.load(a["id"])
assert ja["state"] == "done" and ja["remote"] == "titan" and "first" in ja["start_note"], ja
# a program the server lacks: stays here
v = queue.submit("vivado -mode batch -source x.tcl || true", cwd=os.path.join(W, "f"), name="impl", mem_gb=1, cores=1)
assert until(lambda: st(v["id"]) in queue.TERMINAL, 60)
assert not queue.load(v["id"]).get("remote"), queue.load(v["id"])
# fails there (file only on this machine) -> runs once more here and succeeds
open(os.path.join(os.environ["HOME"], "only_here.txt"), "w").write("x")
b = queue.submit("cat $HOME/only_here.txt", cwd=os.path.join(W, "f"), name="needs_local", mem_gb=1, cores=1)
assert until(lambda: st(b["id"]) in queue.TERMINAL and queue.load(b["id"]).get("fell_back"), 90), queue.load(b["id"])
assert until(lambda: st(b["id"]) in queue.TERMINAL, 60)
jb = queue.load(b["id"])
assert jb["state"] == "done" and not jb.get("remote") and "running it here instead" in jb["start_note"], jb
# a real failure everywhere: only one extra try, then failed
c = queue.submit("exit 4", cwd=os.path.join(W, "f"), name="broken", mem_gb=1, cores=1)
assert until(lambda: st(c["id"]) in queue.TERMINAL and queue.load(c["id"]).get("fell_back"), 90)
assert until(lambda: st(c["id"]) in queue.TERMINAL, 60)
assert st(c["id"]) == "failed" and queue.load(c["id"])["rc"] == 4
# measured slower there -> here
jobs = [dict(id=900 + i, state="done", name="x", cwd="/w", started=1, ended=601 if i < 2 else 61,
             remote="titan" if i < 2 else None) for i in range(4)]
t, note = RM.prefer_target({"id": 1, "name": "x", "cwd": "/w", "cmd": "make", "cores": 1}, jobs)
assert t is None and "slower" in note, note
print("ok")
''', shared=True, offload="prefer", extra="tools = *\nmissing_tools = vivado vitis\n")
    assert "ok" in out


def test_check_and_probe():
    tmp, ms = fake_ms()
    (ms / "config/remotes.conf").write_text(CONF.format(offload="never"))
    env = dict(os.environ, MY_SYSTEM=str(ms), MS_NO_CGROUP="1", HOME=str(tmp))
    env.update(cluster(tmp, shared=False))
    r = subprocess.run([sys.executable, str(ms / "bin/ms"), "remote", "test", "titan"], env=env, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    for want in ("ok  ~/.ssh/config has 'Host titan'", "ok  ssh titan works", "ok  Slurm commands found",
                 "24 of 32 CPUs idle", "2 node(s)", "cpus=", "tool_vivado=", "ok  test job ran"):
        assert want in r.stdout, (want, r.stdout)
    r = subprocess.run([sys.executable, str(ms / "bin/ms"), "remote", "status", "titan"], env=env, capture_output=True, text=True)
    assert "24 of 32 CPUs idle" in r.stdout, r.stdout
    (ms / "config/remotes.conf").write_text(CONF.format(offload="never").replace("ssh = titan", "ssh = deadhost"))
    r = subprocess.run([sys.executable, str(ms / "bin/ms"), "remote", "check", "titan"], env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "NO  ~/.ssh/config has 'Host deadhost'" in r.stdout and "UGent VPN" in r.stdout, r.stdout
    (ms / "config/remotes.conf").write_text(CONF.format(offload="never").replace("ssh = titan", "ssh = newhost"))
    r = subprocess.run([sys.executable, str(ms / "bin/ms"), "remote", "check", "titan"], env=env, capture_output=True, text=True)
    assert "StrictHostKeyChecking=accept-new newhost" in r.stdout, r.stdout


def test_other_peoples_files_are_never_touched():
    """One shared account: my-system may only touch its own folder and its own jobs."""
    tmp, ms = fake_ms()
    (ms / "config/remotes.conf").write_text(CONF.format(offload="never"))
    env = dict(os.environ, MY_SYSTEM=str(ms), MS_NO_CGROUP="1", HOME=str(tmp), WORK=str(tmp / "work"))
    env.update(cluster(tmp, shared=True))
    head = Path(env["FAKE_HEAD"])
    others = [head / "jobs/submitted/job_x.sh", head / "results/results_1_1.tar.gz", head / "jobs/msq/someone/5-1/keep.txt",
              head / "jobs/msq/bzm/notes.txt", head / "jobs/msq/bzm/old-run/data.bin"]
    for f in others:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("not mine")
    mine_old = head / "jobs/msq/bzm/7-1"
    mine_old.mkdir(parents=True)
    (mine_old / "in.tar.gz").write_text("x")
    os.utime(mine_old, (time.time() - 30 * 86400,) * 2)
    code = HARNESS + r'''
from mysys import remote
os.makedirs(os.path.join(W, "p"), exist_ok=True)
a = queue.submit("echo hi > out.txt", cwd=os.path.join(W, "p"), name="ok", mem_gb=1, cores=1, remote="titan")
assert until(lambda: st(a["id"]) in queue.TERMINAL, 60) and st(a["id"]) == "done"
r = remote.get("titan")
assert remote._own_job_dir(r, "jobs/msq/bzm/12-1") and not remote._own_job_dir(r, "jobs/msq/bzm/notes.txt")
assert not remote._own_job_dir(r, "jobs/msq/someone/5-1") and not remote._own_job_dir(r, "results")
for bad in ("", "/home/slurmslave", "~", "jobs", "jobs/../results", "results/bzm", "jobs/msq/*", "jobs/msq/other"):
    r2 = dict(r, workdir=bad)
    try:
        remote.safe_workdir(r2)
        raise SystemExit(f"accepted unsafe workdir {bad!r}")
    except KeyError:
        pass
print("ok")
'''
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "ok" in r.stdout, r.stdout + r.stderr
    r = subprocess.run([sys.executable, str(ms / "bin/ms"), "remote", "clean", "titan"], env=env, capture_output=True, text=True)
    assert "removed 1 old job folder" in r.stdout and "7-1" in r.stdout, r.stdout + r.stderr
    assert not mine_old.exists()
    for f in others:
        assert f.exists() and f.read_text() == "not mine", f"touched {f}"
    # someone else's Slurm job cannot be cancelled through my-system's scancel
    slurm = Path(env["FAKE_SLURM"])
    (slurm / "999.sh").write_text("#SBATCH --account=someone\n")
    r = subprocess.run(["scancel", "--account=bzm", "999"], env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "not your job" in r.stderr


def test_cluster_without_node_memory():
    """Like Titan: nodes report no RAM to Slurm and any --mem is refused — jobs must be sent without it."""
    tmp, ms = fake_ms()
    (ms / "config/remotes.conf").write_text(CONF.format(offload="never"))
    env = dict(os.environ, MY_SYSTEM=str(ms), MS_NO_CGROUP="1", HOME=str(tmp))
    env.update(cluster(tmp, shared=False))
    env["FAKE_NOMEM"] = "1"
    r = subprocess.run([sys.executable, str(ms / "bin/ms"), "remote", "test", "titan"], env=env, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0 and "ok  test job ran" in r.stdout and "does not know the nodes' RAM" in r.stdout, r.stdout + r.stderr


def test_ai_knows_and_obeys_the_server_rules():
    import importlib.util
    tmp, ms = fake_ms()
    (ms / "config/remotes.conf").write_text(CONF.format(offload="prefer") + "tools = *\nmissing_tools = vivado vitis\n")
    (tmp / ".ssh").mkdir()
    (tmp / ".ssh/config").write_text("Host titan\n  HostName bacchus.ugent.be\n  User slurmslave\n")
    env = dict(os.environ, MY_SYSTEM=str(ms), HOME=str(tmp))
    code = r"""
import os, sys
sys.path.insert(0, os.path.join(os.environ["MY_SYSTEM"], "lib"))
from mysys import aictx, queue
t = aictx.build()
assert "## Remote servers" in t and "server first" in t and "vivado vitis" in t and "--on here" in t, t
j = queue.submit("make", cwd=os.environ["HOME"], name="x", remote="here")
assert j.get("remote_never") and not j.get("remote"), j
print("ok")
"""
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert "ok" in r.stdout, r.stdout + r.stderr
    src = ROOT / ("src/ai" if (ROOT / "src").is_dir() else "ai") / "claude/hooks/guard.py"
    rules = (src.parent.parent.parent / "AI_RULES.md").read_text()
    assert "Never use ssh, scp, rsync, sbatch" in rules and "self-contained" in rules
    code = f"""
import importlib.util, sys
spec = importlib.util.spec_from_file_location("g", {str(src)!r}); g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)
bad = ["ssh titan sinfo", "ssh slurmslave@bacchus.ugent.be ls", "scp x.v titan:jobs/", "rsync -a . titan:x",
       "sbatch job.sh", "cd x && scancel 123", "FOO=1 srun hostname", "nice -n 5 ssh titan rm -rf jobs",
       "ssh -oProxyJump=titan other", "ssh -J titan other", "S=titan; ssh $S ls", "'ssh' titan ls", "rsync -e ssh . titan:x",
       "rsync -a . $H:x"]
good = ["ms q add -- make", "ms remote list", "ssh github.com", "grep sbatch notes.txt", "echo titan", "git push",
        "rsync -a $SRC/ backup/"]
for c in bad:
    assert g.check_bash(c, []), c
for c in good:
    assert not g.check_bash(c, []), (c, g.check_bash(c, []))
print("ok")
"""
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert "ok" in r.stdout, r.stdout + r.stderr


def test_links_and_setup_on_the_node():
    out, notes, tmp, ms, env = run(r'''
from mysys import remote as RM
tgt = os.path.join(os.environ["FAKE_HEAD"], "shared_tools")
os.makedirs(os.path.join(tgt, "bin"), exist_ok=True)
open(os.path.join(tgt, "bin", "fakevivado"), "w").write("#!/bin/sh\necho fake-vivado-ok\n")
os.chmod(os.path.join(tgt, "bin", "fakevivado"), 0o755)
with open(os.path.join(MS, "config", "remotes.conf"), "a") as f:
    f.write(f"links = ~/bzm/xilinx -> {tgt}\nsetup = export PATH=$HOME/bzm/xilinx/bin:$PATH\n")
os.makedirs(os.path.join(W, "L"), exist_ok=True)
a = queue.submit("fakevivado > out.txt; readlink $HOME/bzm/xilinx >> out.txt", cwd=os.path.join(W, "L"), name="lk", remote="titan", **small)
assert until(lambda: st(a["id"]) in queue.TERMINAL, 60), queue.load(a["id"])
assert st(a["id"]) == "done", open(queue.LOGS / f"{a['id']}.log").read()
o = open(os.path.join(W, "L", "out.txt")).read()
assert "fake-vivado-ok" in o and tgt in o, o
assert "link ~/bzm/xilinx -> " + tgt + ": ok" in open(queue.LOGS / f"{a['id']}.log").read()
# a link outside your own folder is refused
open(os.path.join(MS, "config", "remotes.conf"), "w").write("[titan]\nssh = titan\naccount = bzm\nlinks = ~/other/x -> /tmp\n")
try:
    RM.get("titan"); raise SystemExit("should refuse")
except KeyError as e:
    assert "unsafe link" in str(e)
print("ok")
''', shared=True)
    assert "ok" in out


def test_path_map_on_the_node():
    out, notes, tmp, ms, env = run(r"""
from mysys import remote as RM
LT = os.path.join(os.path.dirname(W), "localtools", "2025.2.1")        # the tool's path on this machine
SRV = os.path.join(os.environ["FAKE_HEAD"], "srvtools", "2025.2.1")    # where it is on the server
os.makedirs(os.path.join(SRV, "bin")); os.makedirs(os.path.join(LT, "bin"))
open(os.path.join(SRV, "bin", "tool"), "w").write("#!/bin/sh\necho srv-tool\n"); os.chmod(os.path.join(SRV, "bin", "tool"), 0o755)
open(os.path.join(LT, "bin", "tool"), "w").write("#!/bin/sh\necho local-tool\n"); os.chmod(os.path.join(LT, "bin", "tool"), 0o755)
with open(os.path.join(MS, "config", "remotes.conf"), "a") as f:
    f.write(f"path_map = {LT} => ~/srvtools/2025.2.1\n")
P = os.path.join(W, "pm")
os.makedirs(P)
run_sh = f"#!/bin/sh\n{LT}/bin/tool > out.txt\necho dir={P}/res >> out.txt\necho tools={LT} >> out.txt\ncat {P}/cfg.txt >> out.txt\n"
open(os.path.join(P, "run.sh"), "w").write(run_sh)
open(os.path.join(P, "cfg.txt"), "w").write(f"cfg-in {P}\n")
t0 = os.path.getmtime(os.path.join(P, "run.sh"))
a = queue.submit(f"sh {P}/run.sh", cwd=P, name="pm", remote="titan", **small)
assert until(lambda: st(a["id"]) in queue.TERMINAL, 60), queue.load(a["id"])
log = open(queue.LOGS / f"{a['id']}.log").read()
assert st(a["id"]) == "done", log
o = open(os.path.join(P, "out.txt")).read()
assert "srv-tool" in o, o                               # the server's copy of the tool ran
assert f"[remote] path {LT} -> " + os.environ["FAKE_HEAD"] + "/srvtools/2025.2.1" in log, log  # the log says what was translated
assert f"dir={P}/res" in o and f"tools={LT}" in o, o      # paths in files it wrote are mapped back home
assert f"cfg-in {P}" in o, o
assert open(os.path.join(P, "run.sh")).read() == run_sh and os.path.getmtime(os.path.join(P, "run.sh")) == t0, "my files untouched"
# a job that needs files that exist only here is not sent automatically, and explain says why
r = RM.get("titan")
Q = os.path.join(W, "needs_local"); os.makedirs(Q)
open(os.path.join(Q, "go.tcl"), "w").write("source /home/someone/only_here/lib.tcl\n")
j = {"cwd": Q, "cmd": "tclsh go.tcl", "name": "x"}
r["tools"] = "*"
wn = RM.why_not(j, r)
assert wn and "/home/someone/only_here/lib.tcl" in wn, wn
j2 = {"cwd": P, "cmd": f"sh {P}/run.sh", "name": "y"}
assert RM.why_not(j2, r) is None, RM.why_not(j2, r)     # mapped paths and its own folder are fine
try:
    RM.parse_path_map(dict(r, path_map="relative => /x")); raise SystemExit("should refuse")
except KeyError as e:
    assert "bad path_map" in str(e)
print("ok")
""", shared=False)
    assert "ok" in out


def test_path_translation_is_exact():
    """Shell-level check of the node-side translation: path boundaries, quotes, binary files, time stamps."""
    import tempfile, shutil
    tmp, ms = fake_ms()
    env = dict(os.environ, MY_SYSTEM=str(ms), HOME=str(tmp))
    d = Path(tempfile.mkdtemp())
    P = "/home/u/proj"
    (d / "run.tcl").write_text(f"source {P}/a.tcl\nexec ls {P}_tmp/x\nputs '{P}/b'\nopen \"{P}\"\nset t /mnt/data/Apps/2025.2.1/Vivado\n"
                               f"cd {P};ls\nPATH=/mnt/data/Apps/2025.2.1:x\nset l {{{P}}}\n")
    os.utime(d / "run.tcl", (1_000_000_000, 1_000_000_000))
    (d / "bin.dat").write_bytes(b"head " + P.encode() + b"/x " + b"\0" * 10 + b" tail")
    code = r"""
import os, sys
sys.path.insert(0, os.path.join(os.environ["MY_SYSTEM"], "lib"))
from mysys import remote as RM
r = dict(RM.DEFAULTS, name="t", account="u", path_map="/mnt/data/Apps/2025.2.1 => ~/u/xilinx/2025.2.1")
j = {"id": 7, "cmd": "rm -rf %s_tmp/* && vivado -source '%s/a.tcl'", "cwd": "%s", "attempt": 1}
txt = RM.job_script(r, j, "jobs/msq/u/7-1")
lines = txt.splitlines()
a = next(i for i, l in enumerate(lines) if l.startswith("JD="))
b = next(i for i, l in enumerate(lines) if l.startswith("touch .msq-7-start"))
print("\n".join(['W="$PWD"'] + lines[a:b]))
""" % (P, P, P)
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    sh = d / "part.sh"
    sh.write_text(r.stdout)
    rr = subprocess.run(["bash", str(sh)], cwd=d, capture_output=True, text=True, env=dict(env, HOME="/nodehome"))
    assert rr.returncode == 0, rr.stdout + rr.stderr
    got = (d / "run.tcl").read_text()
    W = str(d)
    assert f"source {W}/a.tcl" in got and f"'{W}/b'" in got and f'"{W}"' in got, got
    assert f"{P}_tmp/x" in got, "a longer folder name that starts the same is not touched: " + got
    assert f"cd {W};ls" in got and "PATH=/nodehome/u/xilinx/2025.2.1:x" in got and f"set l {{{W}}}" in got, got
    assert "/nodehome/u/xilinx/2025.2.1/Vivado" in got, got
    assert os.path.getmtime(d / "run.tcl") == 1_000_000_000, "time stamp kept"
    assert (d / "bin.dat").read_bytes().count(P.encode()) == 1, "binary file untouched"
    c = (d / ".msq-7-cmd.sh").read_text()
    assert f"rm -rf {P}_tmp/*" in c and f"vivado -source '{W}/a.tcl'" in c, c
    shutil.rmtree(d)


def test_results_filter_and_size_limit():
    out, notes, tmp, ms, env = run(r"""
with open(os.path.join(MS, "config", "remotes.conf"), "a") as f:
    f.write("max_download_gb = 0.05\nresults_exclude = *.dcp scratch\n")   # 50 MB
P = os.path.join(W, "big"); os.makedirs(P)
cmd = ("head -c 110000000 /dev/zero > huge.bin; echo small > small.txt; echo d > a.dcp; mkdir -p scratch; "
       "echo s > scratch/x; mkdir -p out; echo r > out/res.txt")
a = queue.submit(cmd, cwd=P, name="bigres", remote="titan", **small)
assert until(lambda: st(a["id"]) in queue.TERMINAL, 60), queue.load(a["id"])
log = open(queue.LOGS / f"{a['id']}.log").read()
assert st(a["id"]) == "done", log
got = sorted(os.listdir(P))
assert "small.txt" in got and "out" in got, got
assert "huge.bin" not in got and "a.dcp" not in got and "scratch" not in got, got
assert "stay on the server" in log and "stayed on the server" in log, log
# --outputs limits what comes back
Q = os.path.join(W, "outs"); os.makedirs(Q)
b = queue.submit("echo 1 > keep.rpt; echo 2 > drop.log", cwd=Q, name="outs", remote="titan", outputs=["*.rpt"], **small)
assert until(lambda: st(b["id"]) in queue.TERMINAL, 60), queue.load(b["id"])
got = sorted(x for x in os.listdir(Q) if not x.startswith(("msq-", ".msq", "msrun")))
assert got == ["keep.rpt"], got
print("ok")
""", shared=False)
    assert "ok" in out


if __name__ == "__main__":
    t0 = time.time()
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            fn()
            print("ok", k)
    print(f"all passed in {time.time() - t0:.1f}s")
