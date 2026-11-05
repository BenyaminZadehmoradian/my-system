"""Live test against the REAL server, through my-system (install 1.18 first). Not run by ms selftest: it sends real
Slurm jobs to a shared cluster, so you start it yourself:

    python3 tests/live/live_titan.py            # server "titan" from config/remotes.conf
    python3 tests/live/live_titan.py titan
    PART=xc7z020clg400-1 python3 tests/live/live_titan.py titan

Steps (each one only runs when the one before it worked):
  1. ms remote check + test job       ssh, Slurm, work folder, a plain job runs and its results come back
  2. ms remote tools                  Vivado / Vitis start on a node; what is missing
  3. Vivado synthesis job             a tiny design, synthesized there; the Tcl uses this machine's absolute path
                                      (tests path translation) and the report must come back here
  4. HLS job                          a tiny C++ function through vitis-run --mode hls (or vitis_hls)
The test folders are made under ~/My_System/state/remote and removed afterwards (KEEP=1 keeps them).
Everything is also written to ~/My_System/reports/live-titan-TIME.txt.
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

MS = Path(os.environ.get("MY_SYSTEM", Path.home() / "My_System"))
sys.path.insert(0, str(MS / "lib"))
from mysys import __version__, config, queue  # noqa: E402
from mysys import remote as RM  # noqa: E402

NAME = sys.argv[1] if len(sys.argv) > 1 else "titan"
PART = os.environ.get("PART", "xc7z020clg400-1")
LINES = []


def out(s=""):
    print(s, flush=True)
    LINES.append(s)


def job_in(folder, cmd, name, cores=2, mem_gb=4):
    """Run `cmd` in `folder` on the server exactly like a queue job; returns (rc, log)."""
    j = queue.submit(cmd, cwd=str(folder), name=name, mem_gb=mem_gb, cores=cores, remote=NAME, state="held",
                     limit_memory=False, timeout_s=3 * 3600)
    queue.update(j["id"], state="running", started=time.time())
    old = os.getcwd()
    os.chdir(folder)
    try:
        rc = RM.run_job(j["id"])
    finally:
        os.chdir(old)
    queue.update(j["id"], state="done" if rc == 0 else "failed", ended=time.time(), rc=rc)
    try:
        log = Path(folder, f"msq-{j['id']}.log").read_text(errors="replace")
    except OSError:
        log = ""
    return rc, log


def show_tail(log, n=25):
    for line in log.splitlines()[-n:]:
        out("       | " + line)


def main():
    out(f"my-system {__version__} — live test of server '{NAME}' — {time.strftime('%F %T')} — part {PART}")
    try:
        r = RM.get(NAME)
    except KeyError as e:
        out(f"NO  {e}")
        return 1
    results = []
    out("\n== 1. connection, Slurm and a plain test job")
    ok1 = RM.check(NAME, test_job=True, out=out)
    results.append(("server and plain job", ok1))
    if not ok1:
        return summary(results)
    out("\n== 2. AMD tools on a node")
    ok2 = RM.tools_check(NAME, out=out)
    results.append(("Vivado starts on a node", ok2))
    if not ok2:
        return summary(results)
    RM.STATE.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="live-titan-", dir=str(RM.STATE)))
    try:
        out("\n== 3. Vivado synthesis through the queue (with this machine's absolute paths in the Tcl)")
        syn = base / "syn"
        syn.mkdir()
        (syn / "top.v").write_text("module top(input clk, input rst, output reg [7:0] q);\n"
                                   "  always @(posedge clk) if (rst) q <= 0; else q <= q + 1;\nendmodule\n")
        (syn / "syn.tcl").write_text(f"read_verilog {syn}/top.v\nsynth_design -top top -part {PART}\n"
                                     f"report_utilization -file {syn}/util.rpt\n")
        rc, log = job_in(syn, "vivado -mode batch -nojournal -source syn.tcl", "live_vivado_synth", cores=2, mem_gb=6)
        ok3 = rc == 0 and (syn / "util.rpt").exists()
        out(f"  {'ok ' if ok3 else 'NO '} Vivado synthesis there, report back here" + ("" if ok3 else f" (exit {rc})"))
        if ok3 and str(syn) in (syn / "syn.tcl").read_text():
            out("  ok  your Tcl file here was not changed by the path translation")
        if not ok3:
            show_tail(log)
        results.append(("Vivado synthesis job", ok3))

        out("\n== 4. HLS C synthesis through the queue")
        hls = base / "hls"
        hls.mkdir()
        (hls / "add.cpp").write_text("int add(int a, int b) { return a + b; }\n")
        (hls / "hls.tcl").write_text(f"open_project -reset p\nset_top add\nadd_files add.cpp\n"
                                     f"open_solution -reset s -flow_target vivado\nset_part {PART}\n"
                                     f"create_clock -period 10\ncsynth_design\nexit\n")
        cmd = ("if command -v vitis-run >/dev/null; then vitis-run --mode hls --tcl hls.tcl; "
               "else vitis_hls -f hls.tcl; fi")
        rc, log = job_in(hls, cmd, "live_hls_csynth", cores=2, mem_gb=6)
        ok4 = rc == 0 and any(hls.rglob("csynth.xml"))
        out(f"  {'ok ' if ok4 else 'NO '} HLS C synthesis there, csynth.xml back here" + ("" if ok4 else f" (exit {rc})"))
        if not ok4:
            show_tail(log)
        results.append(("HLS synthesis job", ok4))
    finally:
        if os.environ.get("KEEP") != "1":
            shutil.rmtree(base, ignore_errors=True)
        else:
            out(f"\nkept: {base}")
    return summary(results)


def summary(results):
    out("\n================ SUMMARY ================")
    for n, ok in results:
        out(f"  {'PASS' if ok else 'FAIL'}  {n}")
    rep = config.REPORTS / f"live-{NAME}-{time.strftime('%Y%m%d-%H%M')}.txt"
    try:
        rep.parent.mkdir(parents=True, exist_ok=True)
        rep.write_text("\n".join(LINES) + "\n")
        print(f"\nreport: {rep}")
    except OSError:
        pass
    return 0 if results and all(ok for _, ok in results) and len(results) == 4 else 1


if __name__ == "__main__":
    sys.exit(main())
