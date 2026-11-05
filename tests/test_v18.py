"""1.18: server jobs survive odd logs, a busy Slurm controller and unsafe result links; the node's locale is fixed;
`ms remote tools` starts the AMD tools on a node and explains what is missing.
Run: python3 -m pytest tests/test_v18.py"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_remote import CONF, cluster, run  # noqa: E402
from test_features import fake_ms  # noqa: E402


def test_non_utf8_log_and_busy_controller():
    out, *_ = run(r'''
import pathlib
os.makedirs(os.path.join(W, "u"), exist_ok=True)
# the controller is busy for the first polls: the job must not be taken for finished
pathlib.Path(os.environ["FAKE_SLURM"], "flaky").write_text("6")
a = queue.submit("printf 'temp 45\\xb0C caf\\xc3\\xa9\\n'; sleep 3; echo done-here > r.txt",
                 cwd=os.path.join(W, "u"), name="u8", remote="titan", **small)
assert until(lambda: st(a["id"]) in queue.TERMINAL, 90), queue.load(a["id"])
j = queue.load(a["id"])
log = open(queue.LOGS / f"{a['id']}.log", errors="replace").read()
assert j["state"] == "done", (j["state"], j.get("reason"), log)
assert open(os.path.join(W, "u", "r.txt")).read() == "done-here\n", "results came back after the hiccup"
assert "café" in log and "45" in log, log
print("ok")
''', shared=False)
    assert "ok" in out


def test_unsafe_result_links_are_skipped_not_fatal():
    out, *_ = run(r'''
os.makedirs(os.path.join(W, "k"), exist_ok=True)
a = queue.submit("echo good > good.txt; ln -s /etc/passwd evil_link; ln -s ../../../x up_link; echo also > z.txt; ln -s good.txt nice_link",
                 cwd=os.path.join(W, "k"), name="lnk", remote="titan", **small)
assert until(lambda: st(a["id"]) in queue.TERMINAL, 60), queue.load(a["id"])
log = open(queue.LOGS / f"{a['id']}.log").read()
assert st(a["id"]) == "done", log
d = os.path.join(W, "k")
assert open(os.path.join(d, "good.txt")).read() == "good\n" and open(os.path.join(d, "z.txt")).read() == "also\n", log
assert not os.path.lexists(os.path.join(d, "evil_link")) and not os.path.lexists(os.path.join(d, "up_link")), os.listdir(d)
assert "not copied back" in log and "evil_link" in log, log
assert os.readlink(os.path.join(d, "nice_link")) == "good.txt", "a link inside the folder comes back"
assert not any(f.startswith(".msq-") for f in os.listdir(d)), os.listdir(d)
print("ok")
''', shared=False)
    assert "ok" in out


def test_bad_locale_falls_back_to_c_utf8():
    out, *_ = run(r'''
os.makedirs(os.path.join(W, "loc"), exist_ok=True)
os.environ["LANG"] = "xx_YY.UTF-8"           # sent along by ssh/sbatch, unknown on the node
os.environ.pop("LC_ALL", None)
a = queue.submit("echo \"lang=$LANG all=$LC_ALL\" > loc.txt", cwd=os.path.join(W, "loc"), name="loc", remote="titan", **small)
assert until(lambda: st(a["id"]) in queue.TERMINAL, 60), queue.load(a["id"])
assert st(a["id"]) == "done"
assert open(os.path.join(W, "loc", "loc.txt")).read().strip() == "lang=C.UTF-8 all=C.UTF-8"
print("ok")
''', shared=True)
    assert "ok" in out


def test_wrong_folder_at_link_place_is_reported():
    out, *_ = run(r'''
tgt = os.path.join(os.environ["FAKE_HEAD"], "shared_tools"); os.makedirs(tgt, exist_ok=True)
os.makedirs(os.path.join(os.environ["FAKE_HEAD"], "bzm", "xilinx"), exist_ok=True)   # a real folder is already there
with open(os.path.join(MS, "config", "remotes.conf"), "a") as f:
    f.write(f"links = ~/bzm/xilinx -> {tgt}\n")
os.makedirs(os.path.join(W, "wf"), exist_ok=True)
a = queue.submit("true", cwd=os.path.join(W, "wf"), name="wf", remote="titan", **small)
assert until(lambda: st(a["id"]) in queue.TERMINAL, 60)
log = open(queue.LOGS / f"{a['id']}.log").read()
assert "a real folder/file is there" in log and "is NOT used" in log, log
print("ok")
''', shared=True)
    assert "ok" in out


def _hermetic(env):
    """No AMD tools of this machine in a test: only the fake cluster's stubs and the system's own programs."""
    stubs = env["PATH"].split(":")[0]
    env["PATH"] = f"{stubs}:/usr/local/bin:/usr/bin:/bin"
    for k in [k for k in env if k.startswith(("XILINX", "LD_LIBRARY_PATH"))]:
        del env[k]
    return env


FAKE_SETTINGS = r'''#!/bin/bash
R=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export XILINX_VIVADO=$R/Vivado XILINX_VITIS=$R/Vitis
export PATH=$R/Vivado/bin:$R/Vitis/bin:$PATH
'''


def _fake_install(head, with_vitis=True, broken_vivado=False):
    root = Path(head) / "xil" / "2025.2.1"
    for prod in ("Vivado", "Vitis"):
        (root / prod / "bin").mkdir(parents=True, exist_ok=True)
        (root / prod / "settings64.sh").write_text(FAKE_SETTINGS)
    (root / "Vivado/data/parts/xilinx/zynq").mkdir(parents=True, exist_ok=True)
    viv = root / "Vivado/bin/vivado"
    viv.write_text("#!/bin/sh\necho 'vivado: error while loading shared libraries: libtinfo.so.5: cannot open shared "
                   "object file' >&2; exit 127\n" if broken_vivado else "#!/bin/sh\necho 'vivado v2025.2.1 (64-bit)'\n")
    viv.chmod(0o755)
    if with_vitis:
        for t in ("vitis-run", "v++"):
            p = root / "Vitis/bin" / t
            p.write_text(f"#!/bin/sh\necho '{t} v2025.2.1'\n")
            p.chmod(0o755)
    return root


def _tools(ms, tmp, extra, shared=True, **inst):
    env = dict(__import__("os").environ, MY_SYSTEM=str(ms), MS_NO_CGROUP="1", HOME=str(tmp))
    env.update(cluster(tmp, shared=shared))
    _hermetic(env)
    root = _fake_install(env["FAKE_HEAD"], **inst)
    (ms / "config/remotes.conf").write_text(CONF.format(offload="never") + extra.format(root=root))
    return subprocess.run([sys.executable, str(ms / "bin/ms"), "remote", "tools", "titan"], env=env,
                          capture_output=True, text=True, timeout=180)


def test_remote_tools_all_good():
    tmp, ms = fake_ms()
    r = _tools(ms, tmp, "setup = source {root}/Vitis/settings64.sh\nmissing_tools = vivado vitis vitis_hls xsim\n")
    assert r.returncode == 0, r.stdout + r.stderr
    for want in ("ok  setup loads the AMD tools", "ok  vivado starts — vivado v2025.2.1", "ok  vitis-run starts",
                 "no 'vitis_hls' command there", "device families installed there: zynq",
                 "take them out of missing_tools", "missing_tools = vitis_hls xsim"):
        assert want in r.stdout, (want, r.stdout)


def test_remote_tools_explains_problems():
    tmp, ms = fake_ms()
    # nothing set up
    r = _tools(ms, tmp, "")
    assert r.returncode == 1 and "XILINX_VIVADO and XILINX_VITIS are empty" in r.stdout, r.stdout
    # only Vivado sourced, and its start fails on a missing library
    tmp, ms = fake_ms()
    r = _tools(ms, tmp, "setup = export XILINX_VIVADO={root}/Vivado; export PATH={root}/Vivado/bin:$PATH\n",
               with_vitis=True, broken_vivado=True)
    assert r.returncode == 1, r.stdout
    assert "NO  vivado starts" in r.stdout and "libtinfo" in r.stdout, r.stdout
    assert "HLS (vitis-run / v++) is on PATH" in r.stdout and "Vitis/settings64.sh" in r.stdout, r.stdout


def test_cancel_reaches_slurm_even_with_slow_polling():
    out, *_ = run(r'''
os.environ["MSQ_REMOTE_POLL"] = "60"          # the runner sleeps long between polls
os.makedirs(os.path.join(W, "c"), exist_ok=True)
c = queue.submit("sleep 120", cwd=os.path.join(W, "c"), name="long", remote="titan", kill_seq="TERM,5,KILL", **small)
assert until(lambda: queue.load(c["id"]).get("remote_state") == "RUNNING", 30), (queue.load(c["id"]), open(queue.LOGS / f"{c['id']}.log").read())
time.sleep(1)
queue.cancel(c["id"])
assert until(lambda: st(c["id"]) in queue.TERMINAL and not queue._alive(queue.load(c["id"]).get("pid")), 30)
sid = queue.load(c["id"])["remote_job"]
assert open(os.path.join(os.environ["FAKE_SLURM"], sid + ".state")).read().strip() == "CANCELLED", "scancel ran"
print("ok")
''', shared=True)
    assert "ok" in out


def test_job_that_dies_before_its_log_does_not_hang():
    out, *_ = run(r'''
os.makedirs(os.path.join(W, "d"), exist_ok=True)
# the node cannot fetch the job files (scp to the head node fails): exit 92 before any log is written
import pathlib
stub = pathlib.Path(os.environ["PATH"].split(":")[0], "scp")
stub.write_text("#!/bin/bash\necho 'Host key verification failed.' >&2; exit 1\n")
pathlib.Path(stub.parent, "sleep").write_text("#!/bin/sh\nexit 0\n")       # the node's 8 retries, without waiting
os.chmod(stub.parent / "sleep", 0o755)
a = queue.submit("echo never", cwd=os.path.join(W, "d"), name="early", remote="titan", **small)
assert until(lambda: st(a["id"]) in queue.TERMINAL, 200), (queue.load(a["id"]), open(queue.LOGS / f"{a['id']}.log").read())
log = open(queue.LOGS / f"{a['id']}.log").read()
assert st(a["id"]) == "failed", log
assert "exit 92" in log and "known_hosts" in log, log
print("ok")
''', shared=False)
    assert "ok" in out


def test_mem_refused_is_retried_without_mem():
    out, *_ = run(r'''
os.environ["FAKE_NOMEM"] = "1"; os.environ["FAKE_SINFO_DOWN"] = "1"   # Titan-like, and sinfo cannot tell us
os.makedirs(os.path.join(W, "m"), exist_ok=True)
a = queue.submit("echo fine > m.txt", cwd=os.path.join(W, "m"), name="mem", remote="titan", **small)
assert until(lambda: st(a["id"]) in queue.TERMINAL, 60)
log = open(queue.LOGS / f"{a['id']}.log").read()
assert st(a["id"]) == "done" and "submitting without it" in log, log
print("ok")
''', shared=True)
    assert "ok" in out



DIAG_STUBS = {
    "ssh": "#!/bin/bash\nwhile [ $# -gt 0 ]; do case \"$1\" in -o) shift 2;; -*) shift;; *) break;; esac; done\nshift\n"
           "cd \"$FAKE_HEAD\" && HOME=\"$FAKE_HEAD\" exec bash -c \"$*\"\n",
    "sbatch": "#!/bin/bash\nout=; for a in \"$@\"; do case \"$a\" in --output=*) out=\"${a#--output=}\";; esac; done\n"
              "script=\"${@: -1}\"; out=\"${out//\\'/}\"; script=\"${script//\\'/}\"\n"
              "(cd /tmp && LANG=xx_YY.UTF-8 bash \"$script\" > \"$out\" 2>&1; echo done > \"$FAKE_HEAD/.diag_done\") &\necho 555\n",
    "squeue": "#!/bin/bash\n[ -f \"$FAKE_HEAD/.diag_done\" ] || echo 'RUNNING node03 None'\n",
    "sacct": "#!/bin/bash\necho 'COMPLETED|0:0|node03'\n",
    "scancel": "#!/bin/bash\nexit 0\n",
    "sleep": "#!/bin/sh\nexit 0\n",
}
FAKE_VIVADO = "#!/bin/sh\ncase \"$*\" in *-source*) echo util > util.rpt; echo 'synth_design completed successfully';; " \
              "*) echo 'vivado v2025.2.1 (64-bit)';; esac\n"
FAKE_VITIS_RUN = "#!/bin/sh\ncase \"$*\" in *--tcl*) mkdir -p p/s/syn/report; echo x > p/s/syn/report/csynth.xml; " \
                 "echo 'Finished C synthesis';; *) echo 'vitis-run v2025.2.1';; esac\n"


def test_titan_diagnose_script_against_fake_cluster():
    """tests/live/titan_diagnose.sh, end to end against a fake head node, share and node."""
    import os
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    stubs, head, share = tmp / "stubs", tmp / "head", tmp / "share"
    for d in (stubs, head, share):
        d.mkdir()
    for n, body in DIAG_STUBS.items():
        (stubs / n).write_text(body)
        (stubs / n).chmod(0o755)
    root = share / "bzm/xilinx/2025.2.1"
    for prod in ("Vivado", "Vitis"):
        (root / prod / "bin").mkdir(parents=True)
    (root / "Vivado/settings64.sh").write_text(f"export XILINX_VIVADO={root}/Vivado PATH={root}/Vivado/bin:$PATH\n")
    (root / "Vitis/settings64.sh").write_text(f"export XILINX_VIVADO={root}/Vivado XILINX_VITIS={root}/Vitis "
                                              f"PATH={root}/Vitis/bin:{root}/Vivado/bin:$PATH\n")
    for p, body in ((root / "Vivado/bin/vivado", FAKE_VIVADO), (root / "Vitis/bin/vitis-run", FAKE_VITIS_RUN)):
        p.write_text(body)
        p.chmod(0o755)
    env = _hermetic(dict(os.environ, PATH=f"{stubs}:{os.environ['PATH']}", FAKE_HEAD=str(head), ACCOUNT="bzm",
                         SHARE=str(share), HOME=str(tmp)))
    script = Path(__file__).resolve().parent / "live" / "titan_diagnose.sh"
    r = subprocess.run(["bash", str(script), "titan"], env=env, capture_output=True, text=True, timeout=120, cwd=tmp)
    o = r.stdout
    for want in ("ok  ssh titan", "copy on the share", "submitted Slurm job 555", "ok  share mounted on the node",
                 "ok  link ~/bzm/xilinx on the node", "ok  the tools (2025.2.1) are visible", "ok  the node can ssh/scp",
                 "the locale sent from here does not exist", "ok  vivado starts", "ok  vitis-run starts",
                 "ok  Vivado synthesizes a design", "ok  HLS C synthesis works (ok(vitis-run))", "full report:"):
        assert want in o, (want, o, r.stderr)
    assert not list(share.glob("bzm/diag-*")), "the result folder on the share is removed"
    assert list(tmp.glob("titan-diagnose-*.txt")), "report saved here"
    # Vivado's settings only, and the copy lost the execute bit
    (root / "Vitis/settings64.sh").unlink()
    (root / "Vivado/bin/vivado").chmod(0o644)
    (head / ".diag_done").unlink()
    r = subprocess.run(["bash", str(script), "titan"], env=env, capture_output=True, text=True, timeout=120, cwd=tmp)
    assert "Vitis/settings64.sh missing on the share" in r.stdout, r.stdout
    assert "vivado is not executable" in r.stdout and "NO  Vivado synthesizes" in r.stdout, r.stdout
    assert "NO  libraries" not in r.stdout, r.stdout



FAKE_VIVADO_TCL = r"""#!/bin/bash
# a fake vivado: with -source, read the Tcl like Vivado would (paths must exist on the node)
case "$*" in *-source*) ;; *) echo 'vivado v2025.2.1 (64-bit)'; exit 0;; esac
tcl=$(echo "$*" | sed -E 's/.*-source ([^ ]+).*/\1/')
src=$(sed -n 's/^read_verilog //p' "$tcl"); rpt=$(sed -n 's/.*report_utilization -file //p' "$tcl")
[ -f "$src" ] || { echo "ERROR: [Common 17-69] File not found: $src"; exit 1; }
echo util > "$rpt" && echo 'synth_design completed successfully'
"""


def test_live_titan_script_against_fake_cluster():
    """tests/live/live_titan.py runs all four steps against the simulated cluster (as on Titan: no shared disk)."""
    import os
    tmp, ms = fake_ms()
    (ms / "tests").mkdir(exist_ok=True)
    env = dict(os.environ, MY_SYSTEM=str(ms), MS_NO_CGROUP="1", HOME=str(tmp))
    env.update(cluster(tmp, shared=False))
    _hermetic(env)
    root = _fake_install(env["FAKE_HEAD"])
    (root / "Vivado/bin/vivado").write_text(FAKE_VIVADO_TCL)
    (root / "Vitis/bin/vitis-run").write_text(FAKE_VITIS_RUN)
    (ms / "config/remotes.conf").write_text(CONF.format(offload="never") + f"setup = source {root}/Vitis/settings64.sh\n")
    script = Path(__file__).resolve().parent / "live" / "live_titan.py"
    r = subprocess.run([sys.executable, str(script), "titan"], env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
    for want in ("PASS  server and plain job", "PASS  Vivado starts on a node", "PASS  Vivado synthesis job",
                 "PASS  HLS synthesis job", "your Tcl file here was not changed"):
        assert want in r.stdout, (want, r.stdout)



def test_explain_says_why_a_job_was_not_sent_after_it_ran_here():
    out, *_ = run(r"""
os.makedirs(os.path.join(W, "o"), exist_ok=True)
open(os.path.join(W, "o", "run.sh"), "w").write("cat /mnt/only_here/data.txt\n")   # a path that exists only here
a = queue.submit("echo here > h.txt", cwd=os.path.join(W, "o"), name="stay", remote="auto", **small)
assert until(lambda: st(a["id"]) in queue.TERMINAL, 60), queue.load(a["id"])
j = queue.load(a["id"])
assert not j.get("remote") and st(a["id"]) == "done", j
lines = {k: v for _, k, v in queue.explain(a["id"])}
assert "/mnt/only_here/data.txt" in lines.get("not sent to a server", ""), lines
assert "server first" in lines.get("may run on a server", ""), lines
print("ok")
""", offload="prefer", extra="tools = *\n")
    assert "ok" in out


if __name__ == "__main__":                # ms selftest runs each test file as a script (no pytest needed)
    import time
    t0 = time.time()
    for k, fn in list(globals().items()):
        if k.startswith("test_") and callable(fn):
            fn()
            print("ok", k)
    print(f"all passed in {time.time() - t0:.1f}s")
