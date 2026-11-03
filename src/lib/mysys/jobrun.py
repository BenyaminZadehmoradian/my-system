"""Runner of one queued job (`ms q _exec ID`, started by the scheduler in its own session).

Per job it handles: standard run log, provenance, private TMPDIR, optional shadow/scratch folder
(only declared outputs come back), progress on fd 3, following extra log files, last output line,
memory/CPU/IO sampling and a time series, wall-clock and inactivity timeouts with a configurable kill
sequence and a warning signal before the timeout, time-limit alerts, exit-code mapping, warning
patterns, outputs passed to later steps, assertions, output checks, protection, publishing, JUnit,
issue (warning) tracking with quality gates, the failure knowledge base, lineage, benchmarks
(repeat N times), repeat-until and wait steps, stub runs and secret masking.
"""
import glob
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config, expr, issues, lineage, queue

GiB = 2**30
SERIES = queue.Q / "series"
SECRETS = config.STATE / "secrets.env"


def load_secrets():
    out = {}
    try:
        for line in SECRETS.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def scratch_base(kind):
    if kind == "ram":
        return "/dev/shm"
    if isinstance(kind, str) and kind not in ("true", "True", "yes") and os.path.isabs(os.path.expanduser(kind)):
        return os.path.expanduser(kind)
    d = config.get("SCRATCH_DIR") or config.get("BIG_FOLDER_DEST") or ""
    d = os.path.expanduser(d)
    return os.path.join(d, ".ms_scratch") if d and os.path.isdir(d) else os.environ.get("TMPDIR", "/tmp")


def _io(pid):
    r = w = 0
    try:
        for line in open(f"/proc/{pid}/io"):
            if line.startswith("read_bytes:"):
                r = int(line.split()[1])
            elif line.startswith("write_bytes:"):
                w = int(line.split()[1])
    except (OSError, ValueError):
        pass
    return r, w


def parse_kill_seq(s):
    """'TERM,30,INT,10,KILL' -> [(SIGTERM, 30), (SIGINT, 10), (SIGKILL, 0)]"""
    out = []
    parts = [p.strip() for p in str(s or "TERM,30,KILL").split(",") if p.strip()]
    i = 0
    while i < len(parts):
        sig = getattr(signal, "SIG" + parts[i].upper().replace("SIG", ""), signal.SIGTERM)
        wait = 0
        if i + 1 < len(parts) and parts[i + 1].isdigit():
            wait = int(parts[i + 1])
            i += 1
        out.append((sig, wait))
        i += 1
    if not out or out[-1][0] != signal.SIGKILL:
        out.append((signal.SIGKILL, 0))
    return out


class Runner:
    def __init__(self, jid):
        self.jid = jid
        self.j = queue.load(jid)
        self.hz = os.sysconf("SC_CLK_TCK")
        self.ticks, self.io = {}, {}
        self.peak = 0
        self.in_scope = False
        self.cg = None
        self.scope_oom = False
        self.last_out = time.time()
        self.last_line = ""
        self.errs = 0
        self.warn_hits = 0
        self.progress = None
        self.secrets = {}
        self.lock = threading.Lock()

    # ---------------------------------------------------------- helpers
    def mask(self, s):
        for v in self.secrets.values():
            if v and len(v) >= 4:
                s = s.replace(v, "***")
        return s

    def procs(self):
        return [x for x in queue._session_procs(os.getsid(0)) if x[0] != os.getpid()]

    def kill(self, seq_text):
        for sig, wait in parse_kill_seq(seq_text):
            for pid, _, _ in self.procs():
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
            t0 = time.time()
            while time.time() - t0 < wait and self.p.poll() is None:
                time.sleep(0.5)
            if self.p.poll() is not None and not self.procs():
                return

    def write_out(self, line, prefix=""):
        line = self.mask(line)
        with self.lock:
            self.logf.write(prefix + line)
            self.size += len(line)
            if time.time() - getattr(self, "_flushed", 0) > 1:   # ms q log / the GUI see output live
                self.logf.flush()
                self._flushed = time.time()
            if self.size > self.cap:
                self.logf.close()
                os.replace(self.logp, str(self.logp) + ".1")
                self.logf = open(self.logp, "a")
                self.size = 0
        s = line.strip()
        if s:
            self.last_line = s[:200]
            self.last_out = time.time()
        if self.errs < 100 and re.match(r"^(ERROR|FATAL|Error|Fatal)\b", s):
            self.errs += 1
            self.run.log("ERROR", s[:1000])
        elif s.startswith("CRITICAL WARNING") and self.warn_hits < 200:
            self.run.log("WARN", s[:1000])
        for rx in self.warn_rx:
            if rx.search(s):
                self.warn_hits += 1
                break

    def pump(self, stream):
        for line in stream:
            self.write_out(line)

    def fd3(self, r):
        with os.fdopen(r, errors="replace") as f:
            for line in f:
                m = re.match(r"^\s*(?:progress\s+)?%?\s*(\d+(?:\.\d+)?)\s*%?\s*(.*)$", line.strip(), re.I)
                if m:
                    self.progress = min(100.0, float(m.group(1)))
                    self.run.progress(self.progress, m.group(2) or None)
                    self.last_out = time.time()
                elif line.strip():
                    self.run.log("INFO", line.strip()[:500])

    def follow(self, stop):
        pos = {}
        while not stop.is_set():
            for pat in self.j.get("follow") or []:
                for f in glob.glob(os.path.join(self.workdir, pat)):
                    try:
                        sz = os.path.getsize(f)
                        if sz < pos.get(f, 0):
                            pos[f] = 0
                        if sz > pos.get(f, 0):
                            with open(f, errors="replace") as fh:
                                fh.seek(pos.get(f, 0))
                                for line in fh:
                                    self.write_out(line, f"[{os.path.basename(f)}] ")
                                pos[f] = fh.tell()
                    except OSError:
                        pass
            stop.wait(5)

    def sample(self):
        procs = self.procs()
        rss = sum(r for _, r, _ in procs)
        for pid, _, t in procs:
            self.ticks[pid] = max(t, self.ticks.get(pid, 0))
            r, w = _io(pid)
            o = self.io.get(pid, (0, 0))
            self.io[pid] = (max(r, o[0]), max(w, o[1]))
        self.peak = max(self.peak, rss)
        return rss

    def cpu_s(self):
        return round(sum(self.ticks.values()) / self.hz, 1)

    def io_tot(self):
        return sum(v[0] for v in self.io.values()), sum(v[1] for v in self.io.values())

    def watch_cgroup(self):
        """Remember the job's memory-limited scope and its parent's OOM-kill counter (the scope itself is removed
        right after the job ends, so the parent's counter is how an out-of-memory kill is recognised)."""
        try:
            line = open(f"/proc/{self.p.pid}/cgroup").read().strip().split("\n")[-1]
            path = line.split("::", 1)[-1]
        except (OSError, IndexError):
            return
        if "msq-" not in path:
            return
        scope = "/sys/fs/cgroup" + path
        parent = os.path.dirname(scope)
        self.cg = {"scope": scope, "parent": parent, "base": _oom_count(parent)}

    def stdin_pump(self):
        """Forward lines written with `ms q send ID TEXT` (a named pipe) to the job's standard input."""
        fifo = queue.LOGS / f"{self.jid}.in"
        try:
            if not fifo.exists():
                os.mkfifo(fifo, 0o600)
            fd = os.open(fifo, os.O_RDWR)          # read+write: never blocks, never sees end-of-file
        except OSError:
            return
        with os.fdopen(fd, "r", errors="replace") as f:
            for line in f:
                if self.p.poll() is not None:
                    break
                try:
                    self.p.stdin.write(line)
                    self.p.stdin.flush()
                    self.write_out(f"[input] {line if line.endswith(chr(10)) else line + chr(10)}")
                except (OSError, ValueError):
                    break

    # ---------------------------------------------------------- one execution of the command
    def run_once(self, cmd_argv, env, t_start):
        j = self.j
        self.cg, self.scope_oom = None, False
        r_fd, w_fd = os.pipe()

        adj = int(config.num("QUEUE_OOM_SCORE_ADJ") or 0)

        def pre():
            os.dup2(w_fd, 3)
            os.set_inheritable(3, True)
            if adj:                     # if RAM runs out, the kernel kills queue jobs before your desktop/interactive tools
                try:
                    with open("/proc/self/oom_score_adj", "w") as f:
                        f.write(str(adj))
                except OSError:
                    pass
        want_in = bool(j.get("stdin"))
        self.p = subprocess.Popen(cmd_argv, cwd=self.workdir, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  stdin=subprocess.PIPE if want_in else subprocess.DEVNULL,
                                  text=True, errors="replace", bufsize=1, preexec_fn=pre, pass_fds=(w_fd, 3))
        os.close(w_fd)
        if want_in:
            threading.Thread(target=self.stdin_pump, daemon=True).start()
        th = threading.Thread(target=self.pump, args=(self.p.stdout,), daemon=True)
        th.start()
        tp = threading.Thread(target=self.fd3, args=(r_fd,), daemon=True)
        tp.start()
        stop = threading.Event()
        tf = threading.Thread(target=self.follow, args=(stop,), daemon=True)
        if j.get("follow"):
            tf.start()
        to = j.get("timeout_s") if isinstance(j.get("timeout_s"), (int, float)) else None
        idle_to = j.get("idle_timeout_s")
        sig_before = j.get("signal_before")        # "USR1@300"
        sent_before, told = False, set()
        why = None
        last_save, series_t, ver_t = 0, 0, 0
        SERIES.mkdir(parents=True, exist_ok=True)
        ser = open(SERIES / f"{self.jid}.csv", "a")
        if ser.tell() == 0:
            ser.write("t,rss_gb,cores,read_mb,write_mb\n")
        prev_cpu, prev_t = self.cpu_s(), time.time()
        usual = queue.usual_seconds(j)
        slow_f = float(config.num("QUEUE_SLOW_FACTOR") or 0)
        while self.p.poll() is None:
            now = time.time()
            if self.in_scope and not self.cg:
                self.watch_cgroup()
            elif self.cg:
                self.scope_oom = self.scope_oom or _oom_count(self.cg["scope"]) > 0
            rss = self.sample()
            el = now - t_start
            if now - series_t >= 10:
                c = self.cpu_s()
                cores = (c - prev_cpu) / max(1e-6, now - prev_t)
                prev_cpu, prev_t = c, now
                rd, wr = self.io_tot()
                ser.write(f"{int(now)},{rss / GiB:.3f},{cores:.2f},{rd / 2**20:.0f},{wr / 2**20:.0f}\n")
                ser.flush()
                series_t = now
            with self.lock:
                try:
                    self.logf.flush()          # the last lines of a quiet job, too
                except (ValueError, OSError):
                    pass
            if now - last_save > 15:
                queue.update(self.jid, peak_rss=self.peak, rss=rss, cpu_s=self.cpu_s(), last_line=self.last_line,
                             progress=self.progress)
                last_save = now
            if to:
                for pct in (50, 80, 90):
                    if el >= to * pct / 100 and pct not in told and to >= 3600:
                        told.add(pct)
                        if pct >= 80:
                            notify(f"Job at {pct}% of its time limit: {j['name']}",
                                   f"#{j['id']} has run {el / 3600:.1f} h of {to / 3600:.1f} h", "default", "hourglass")
                if sig_before and not sent_before:
                    sname, _, secs = str(sig_before).partition("@")
                    if el >= to - int(secs or 300):
                        sent_before = True
                        s = getattr(signal, "SIG" + sname.upper().replace("SIG", ""), signal.SIGUSR1)
                        for pid, _, _ in self.procs():
                            try:
                                os.kill(pid, s)
                            except OSError:
                                pass
                        self.run.log("WARN", f"sent {sname} {secs or 300} s before the time limit")
                if el > to and not why:
                    why = "timeout"
                    self.run.log("ERROR", f"time limit of {int(to)} s reached — stopping")
                    self.kill(j.get("kill_seq"))
            if slow_f and usual >= 300 and el > slow_f * usual and "slow" not in told:
                told.add("slow")
                msg = f"running {el / 60:.0f} min, usually {usual / 60:.0f} min"
                self.run.log("WARN", f"slower than usual: {msg}")
                queue.update(self.jid, slow=True)
                notify(f"Slower than usual: {j['name']}", f"#{j['id']} {msg} — check: ms q log {j['id']}", "default", "turtle")
            if idle_to and now - self.last_out > idle_to and not why:
                why = "idle"
                self.run.log("ERROR", f"no output for {int(idle_to)} s — stopping")
                self.kill(j.get("kill_seq"))
            if not why and now - ver_t >= 3:      # XILINX_ALLOWED: stop a job that runs another tool version
                ver_t = now
                from . import versions
                for pid, _, _ in self.procs():
                    bad = versions.check_proc(pid, j["cwd"])
                    if bad:
                        why, self.version_bad = "version", bad
                        self.write_out(versions.message(bad, j["cwd"]) + " — stopping the job\n")
                        self.run.log("ERROR", versions.message(bad, j["cwd"]))
                        self.kill(j.get("kill_seq"))
                        break
            if (queue.load(self.jid) or {}).get("state") == "cancelled" and not why:
                why = "cancelled"
                self.kill(j.get("kill_seq"))
            time.sleep(2)
        rc = self.p.wait()
        th.join(timeout=10)
        stop.set()
        ser.close()
        return rc, why

    # ---------------------------------------------------------- main
    def execute(self):
        sys.path.insert(0, str(config.MS / "runlog"))
        import msrun
        j = self.j
        if not j:
            return 2
        jid = self.jid
        os.makedirs(j["cwd"], exist_ok=True)
        self.secrets = {k: v for k, v in load_secrets().items() if k in (j.get("secrets") or [])}
        self.warn_rx = []
        for p in j.get("warn_patterns") or []:
            try:
                self.warn_rx.append(re.compile(p))
            except re.error:
                pass
        self.logp = queue.LOGS / f"{jid}.log"
        self.cap = int(float(config.get("QUEUE_LOG_MAX_MB") or 200) * 2**20)
        self.logf = open(self.logp, "a")
        self.size = self.logf.tell()
        # ----- environment
        env = dict(os.environ)
        env.update({k: str(v) for k, v in (j.get("env") or {}).items()})
        env.update(self.secrets)
        n = max(1, int(round(float(j.get("cores") or 1))))
        env.setdefault("OMP_NUM_THREADS", str(n))
        env["MSQ_JOB_ID"] = str(jid)
        env["MSQ_SLOT"] = str(j.get("slot") or 1)
        env["MSQ_THREADS"] = str(n)
        outputs_file = queue.Q / "outputs" / f"{jid}.env"
        outputs_file.parent.mkdir(parents=True, exist_ok=True)
        outputs_file.write_text("")
        env["MSQ_OUTPUTS"] = str(outputs_file)
        if j.get("gpu_ids") is not None:
            env["CUDA_VISIBLE_DEVICES"] = ",".join(map(str, j["gpu_ids"]))
        tmp = Path(scratch_base(None)) / f"msq-tmp-{jid}-{j['attempt']}"
        tmp.mkdir(parents=True, exist_ok=True)
        env["TMPDIR"] = str(tmp)
        # ----- shadow / scratch folder
        self.workdir = j["cwd"]
        shadow = None
        if j.get("scratch"):
            shadow = Path(scratch_base(j["scratch"])) / f"msq-shadow-{jid}-{j['attempt']}"
            shadow.mkdir(parents=True, exist_ok=True)
            for e in os.listdir(j["cwd"]):
                if e in ("msrun.jsonl", ".msq_done.json"):
                    continue
                try:
                    os.symlink(os.path.join(j["cwd"], e), shadow / e)
                except OSError:
                    pass
            self.workdir = str(shadow)
        # ----- run log
        from . import queue as Q_
        self.run = msrun.Run(j["name"], params=dict(j.get("params") or {}), folder=j["cwd"], cmd=self.mask(j["cmd"]),
                             project=(j.get("flow") or {}).get("name"), extra={"provenance": Q_.provenance(j)})
        self.run.start()
        t0 = time.time()
        queue.update(jid, run_id=self.run.run, started=t0, sid=os.getsid(0), pid=os.getpid())
        if j.get("created"):
            self.run.metric("queue_wait_s", round(t0 - j["created"]), "s")
        lineage.record(j, j.get("inputs"), "consumed", self.run.run)
        self.logf.write(f"=== attempt {j['attempt']} · {time.strftime('%F %T')} · {self.mask(j['cmd'])}\n")
        self.logf.flush()
        status, state, reason, rc = "ok", "done", "", 0
        from . import versions
        bad_ver = versions.check_text(j["cmd"], j["cwd"])
        try:
            if bad_ver:                          # XILINX_ALLOWED: another tool version may not be used
                rc, why = 126, "version"
                self.version_bad = bad_ver
                self.write_out(versions.message(bad_ver, j["cwd"]) + "\n")
            elif j.get("wait"):
                rc, why = self.do_wait(j["wait"])
            else:
                rc, why = self.do_commands(env, t0)
        except OSError as e:
            rc, why = 127, None
            self.write_out(f"cannot start: {e}\n")
        secs = time.time() - t0
        cur = queue.load(jid) or j
        # SIGKILL (-9/137) that the runner did not send: in a memory-limited scope that is the kernel's OOM killer
        # (the scope is gone by now, so memory.events may be unreadable and the last RSS sample may be old)
        # SIGKILL (-9/137) that the runner did not send is an out-of-memory kill only with evidence: the kernel's
        # OOM counter of the job's scope or its parent went up, or the job was at its memory limit. (A manual kill -9
        # is not retried as "out of memory".)
        parent_oom = bool(self.cg) and _oom_count(self.cg["parent"]) > self.cg["base"]
        oom = rc in (-9, 137) and not why and (self.scope_oom or parent_oom or queue._cgroup_oom(j)
                                               or self.peak >= 0.9 * float(j["mem_gb"]) * 1.25 * GiB)
        codes = {str(k): v for k, v in (j.get("exit_codes") or {}).items()}
        mapped = codes.get(str(rc))
        if why == "cancelled" or cur.get("state") == "cancelled":
            status, state, reason = "cancelled", "cancelled", cur.get("cancel_reason") or "cancelled by user"
        elif why == "timeout":
            status, state, reason = "failed", "timeout", f"time limit {int(j['timeout_s'])} s reached"
        elif why == "idle":
            status, state, reason = "failed", "timeout", f"no output for {int(j['idle_timeout_s'])} s"
        elif why == "version":
            from . import versions
            status, state, reason = "failed", "failed", versions.message(getattr(self, "version_bad", "?"), j["cwd"])
        elif why == "wait-timeout":
            status, state, reason = "failed", "timeout", "waited too long"
        elif (rc == 0 and not self.errs) or mapped in ("ok", "success", "done"):
            status, state = "ok", "done"
        elif mapped == "warning":
            status, state, reason = "ok", "warning", f"exit code {rc} counts as warning"
        else:
            status, state = "failed", "failed"
            reason = "out of memory (killed at the job's memory limit)" if oom else f"exit code {rc}" + (
                f", {self.errs} ERROR line(s)" if self.errs else "")
        if state == "done" and self.warn_hits:
            state, reason = "warning", f"{self.warn_hits} line(s) matched warn_patterns"
        # ----- outputs passed on
        outs = {}
        try:
            for line in outputs_file.read_text().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    outs[k.strip()] = v.strip()
        except OSError:
            pass
        # ----- shadow: bring declared outputs back
        if shadow and state in ("done", "warning"):
            for pat in j.get("outputs") or []:
                for f in glob.glob(os.path.join(shadow, pat), recursive=True):
                    if os.path.islink(f):
                        continue
                    dest = os.path.join(j["cwd"], os.path.relpath(f, shadow))
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    if os.path.isdir(dest) and not os.path.islink(dest):
                        shutil.rmtree(dest)
                    shutil.move(f, dest)
        # ----- checks on success
        if state in ("done", "warning"):
            ens = j.get("ensure") or {}
            for pat in ens.get("non_empty") or []:
                fs = glob.glob(os.path.join(j["cwd"], pat))
                if not fs or any(os.path.isfile(f) and os.path.getsize(f) == 0 for f in fs):
                    status, state, reason = "failed", "failed", f"output {pat} is missing or empty"
            for f, want in (ens.get("sha256") or {}).items():
                try:
                    if lineage.fingerprint(os.path.join(j["cwd"], f)) != want:
                        status, state, reason = "failed", "failed", f"checksum of {f} does not match"
                except OSError:
                    status, state, reason = "failed", "failed", f"{f} missing"
            for pat in j.get("outputs") or []:
                if not glob.glob(os.path.join(j["cwd"], pat)) and j.get("require_outputs", True):
                    status, state, reason = "failed", "failed", f"declared output {pat} was not produced"
        # ----- tests
        ju = issues.junit(j.get("junit"), j["cwd"])
        if ju:
            for k in ("tests_total", "tests_failed", "tests_error", "tests_skipped", "tests_passed"):
                self.run.metric(k, ju[k])
            if (ju["tests_failed"] or ju["tests_error"]) and state == "done":
                state, reason = "warning", f"{ju['tests_failed'] + ju['tests_error']} test(s) failed: " + ", ".join(ju["failing"][:5])
        # ----- issues and quality gate
        with self.lock:
            self.logf.flush()
        try:
            text = Path(self.logp).read_text(errors="replace")[-50_000_000:]
        except OSError:
            text = ""
        found = issues.parse_text(text)
        issues.save(jid, found)
        ref = reference_job(j)
        cmp = issues.compare(found, issues.load(ref["id"]) if ref else None) if ref else None
        summ = issues.summarize(found)
        self.run.metric("critical_warnings", summ["critical"])
        self.run.metric("warnings", summ["warning"])
        if cmp:
            self.run.metric("new_critical_warnings", issues.summarize(cmp["new"])["critical"])
            self.run.metric("fixed_issues", len(cmp["fixed"]))
        if j.get("quality_gate") and state in ("done", "warning"):
            ok, msgs = issues.gate(cmp, found, j["quality_gate"])
            if not ok:
                lvl = (j["quality_gate"] or {}).get("level", "warning")
                state = "failed" if lvl == "failed" else "warning"
                status = "failed" if lvl == "failed" else status
                reason = "quality gate: " + "; ".join(msgs)
        # ----- assertion on outputs/metrics
        if j.get("assert") and state in ("done", "warning"):
            names = dict(_numify(outs))
            names.update(_run_metrics(j["cwd"], self.run.run))
            try:
                if not expr.evaluate(j["assert"], names):
                    status, state, reason = "failed", "failed", f"assertion failed: {j['assert']}"
            except expr.ExprError as e:
                status, state, reason = "failed", "failed", f"assertion error: {e}"
        # ----- known failure cause
        diag = None
        if state in ("failed", "timeout"):
            diag = issues.diagnose(text)
            if diag:
                reason = f"{reason} — likely cause: {diag['name']}. {diag['fix']}"
                self.run.log("INFO", f"known failure: {diag['name']}: {diag['line']}")
            if not j.get("keep_incomplete") and j.get("outputs"):
                for pat in j["outputs"]:
                    for f in glob.glob(os.path.join(j["cwd"], pat)):
                        try:
                            shutil.rmtree(f) if os.path.isdir(f) and not os.path.islink(f) else os.remove(f)
                        except OSError:
                            pass
        # ----- success side effects
        if state in ("done", "warning"):
            if j.get("protect"):
                for pat in j.get("outputs") or []:
                    for f in glob.glob(os.path.join(j["cwd"], pat)):
                        try:
                            os.chmod(f, os.stat(f).st_mode & ~0o222)
                        except OSError:
                            pass
            publish(j)
            lineage.record(j, j.get("outputs"), "produced", self.run.run)
            queue.mark_done(j)
            from . import flow as F
            F.cache_store(j)
        # ----- metrics, end of run log
        rd, wr = self.io_tot()
        self.run.metric("runtime_s", round(secs, 1), "s")
        self.run.metric("peak_rss_gb", round(self.peak / GiB, 2), "GB")
        self.run.metric("cpu_s", self.cpu_s(), "s")
        self.run.metric("io_read_gb", round(rd / GiB, 3), "GB")
        self.run.metric("io_write_gb", round(wr / GiB, 3), "GB")
        for k, v in _numify(outs):
            if re.match(r"^[a-z][a-z0-9_]*$", k):
                self.run.metric(k, v)
        is_flaky = state in ("done", "warning") and j.get("attempt", 1) > 1 and j.get("prev_fail") == "fail"
        if is_flaky:
            key, cnt = queue.mark_flaky(j)
            reason = (reason + "; " if reason else "") + f"flaky: failed, then passed on retry {j['attempt'] - 1}"
            self.run.log("WARN", f"flaky: {key} failed and then passed on retry without changes ({cnt}x so far)")
            if cnt in (1, 3, 10):
                notify(f"Flaky job: {j['name']}", f"#{j['id']} failed, then passed on retry ({cnt}x for {key}). "
                                                  "Look for races, random seeds or resource limits.", "default", "warning")
        end_status = "ok" if state in ("done", "warning") else ("cancelled" if state == "cancelled" else "failed")
        self.run.end(end_status, rc=rc if rc is not None else 1, error=None if end_status == "ok" else (reason or "failed"))
        cores_used = self.cpu_s() / secs if secs > 0 else 0
        try:
            (queue.LOGS / f"{jid}.in").unlink()
        except OSError:
            pass
        queue.update(jid, state=state, rc=rc, ended=time.time(), reason=reason, peak_rss=self.peak, oom=bool(oom), flaky=is_flaky,
                     cpu_s=self.cpu_s(), cores_used=round(cores_used, 2), outputs=outs, io_read=rd, io_write=wr,
                     last_line=self.last_line, progress=100 if state in ("done", "warning") else self.progress,
                     issues=summ, diagnosis=diag, junit=ju)
        # ----- cleanup
        shutil.rmtree(tmp, ignore_errors=True)
        if shadow and (state in ("done", "warning") or not config.get("KEEP_FAILED_SCRATCH")):
            shutil.rmtree(shadow, ignore_errors=True)
        self.logf.close()
        return 0

    def do_commands(self, env, t0):
        j = self.j
        if j.get("stub_mode"):
            cmd = j.get("stub") or " ; ".join(f"mkdir -p $(dirname {o}) && touch {o}" for o in j.get("outputs") or []) or "true"
            jj = dict(j, cmd=cmd, limit_memory=False)
        else:
            jj = j
        argv = queue.build_command(jj)
        self.in_scope = self.in_scope or argv[:1] == ["systemd-run"]
        reps = max(1, int(j.get("benchmark") or 1))
        rep = j.get("repeat") or {}
        rc, why = 0, None
        times = []
        n = 0
        while True:
            n += 1
            t = time.time()
            rc, why = self.run_once(argv, env, t0)
            times.append(time.time() - t)
            if why:
                break
            if reps > 1 and len(times) < reps and rc == 0:
                continue
            if rep:
                done = rc == int(rep.get("until_exit", 0))
                if rep.get("until"):
                    done = subprocess.run(rep["until"], shell=True, cwd=self.workdir, env=env).returncode == 0
                if done or n >= int(rep.get("limit", 100)):
                    if not done:
                        rc = rc or 1
                    break
                self.write_out(f"--- repeat {n}: not yet, next try in {int(rep.get('interval', 60))} s\n")
                time.sleep(float(rep.get("interval", 60)))
                continue
            break
        if reps > 1:
            for i, tt in enumerate(times, 1):
                self.run.metric(f"bench_{i}_s", round(tt, 2), "s")
            self.run.metric("bench_mean_s", round(sum(times) / len(times), 2), "s")
            self.run.metric("bench_min_s", round(min(times), 2), "s")
        return rc, why

    def do_wait(self, w):
        t0 = time.time()
        limit = queue.parse_duration(w.get("timeout")) if w.get("timeout") else None
        until_t = None
        if w.get("until"):
            hh, mm = map(int, str(w["until"]).split(":"))
            lt = time.localtime()
            until_t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
            if until_t < t0:
                until_t += 86400
        dur = queue.parse_duration(w.get("duration")) if w.get("duration") else None
        while True:
            ok = True
            if w.get("file"):
                ok &= bool(glob.glob(os.path.join(self.j["cwd"], os.path.expanduser(w["file"]))))
            if until_t:
                ok &= time.time() >= until_t
            if dur:
                ok &= time.time() - t0 >= dur
            if w.get("cmd"):
                ok &= subprocess.run(w["cmd"], shell=True, cwd=self.j["cwd"]).returncode == 0
            if ok:
                return 0, None
            if limit and time.time() - t0 > limit:
                return 1, "wait-timeout"
            if (queue.load(self.jid) or {}).get("state") == "cancelled":
                return 1, "cancelled"
            self.last_out = time.time()
            queue.update(self.jid, last_line=f"waiting ({int(time.time() - t0)} s)")
            time.sleep(float(w.get("interval", 15)))


def _oom_count(cg_dir):
    try:
        for line in open(os.path.join(cg_dir, "memory.events")):
            if line.startswith("oom_kill "):
                return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return 0


def _numify(d):
    for k, v in (d or {}).items():
        try:
            yield k, (float(v) if "." in str(v) or "e" in str(v).lower() else int(v))
        except (TypeError, ValueError):
            yield k, v


def _run_metrics(cwd, run_id):
    from . import runlog
    r = runlog.read(os.path.join(cwd, runlog.FILENAME)).get(run_id)
    return dict(r["metrics"]) if r else {}


def reference_job(j):
    """Last successful earlier run of the same step (same flow name + step + item) or same name+folder."""
    key = (j.get("step_key") or "").split(":", 1)[-1] if j.get("step_key") else None
    best = None
    for x in queue.all_jobs():
        if x["id"] >= j["id"] or x["state"] not in ("done", "warning"):
            continue
        same = (key and (x.get("step_key") or "").split(":", 1)[-1] == key and
                (x.get("flow") or {}).get("name") == (j.get("flow") or {}).get("name")) or \
               (not key and x["name"] == j["name"] and x["cwd"] == j["cwd"])
        if same and issues.load(x["id"]) is not None:
            best = x
    return best


def publish(j):
    p = j.get("publish")
    if not p or not p.get("dir"):
        return
    sub = "/".join(f"{k}={v}" for k, v in sorted((j.get("params") or {}).items()))
    dest_dir = os.path.join(os.path.expanduser(p["dir"]), (j.get("flow") or {}).get("step", ""), sub)
    os.makedirs(dest_dir, exist_ok=True)
    mode = p.get("mode", "copy")
    for pat in p.get("pattern") and [p["pattern"]] or j.get("outputs") or []:
        for f in glob.glob(os.path.join(j["cwd"], pat)):
            dest = os.path.join(dest_dir, os.path.basename(f))
            try:
                if os.path.lexists(dest):
                    shutil.rmtree(dest) if os.path.isdir(dest) and not os.path.islink(dest) else os.remove(dest)
                if mode == "symlink":
                    os.symlink(os.path.abspath(f), dest)
                elif mode == "move":
                    shutil.move(f, dest)
                elif os.path.isdir(f):
                    shutil.copytree(f, dest)
                else:
                    shutil.copy2(f, dest)
            except OSError:
                pass


def notify(title, msg, prio="default", tag="computer"):
    try:
        subprocess.Popen([str(config.MS / "bin" / "notify.sh"), title, msg[:400], prio, tag],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def execute(jid):
    return Runner(jid).execute()
