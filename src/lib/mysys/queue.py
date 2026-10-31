"""my-system job queue: memory-aware scheduling of long jobs on one workstation.

  state/queue/jobs/<id>.json   one file per job (written atomically)
  state/queue/logs/<id>.log    combined stdout/stderr of the job
  state/queue/daemon.json      heartbeat of the scheduler

The scheduler (`ms q daemon`, user service my-system-queue) starts a job only when the CPU
threads and RAM it needs are free right now (live load + RAM of jobs it already started that
are still growing), suspends the newest low-priority job when RAM runs low and resumes it
later, retries failed jobs (more memory after an out-of-memory kill), honours dependencies,
priorities, preconditions, idle-only jobs, GPUs, containers and remote hosts.
Each job is executed by a small runner (`ms q _exec ID`) that records the standard run log,
samples memory/CPU, enforces the timeout and, when possible, a hard memory limit (cgroup).
"""
import fcntl
import glob
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import capacity, config, probes

GiB = 2**30
Q = config.STATE / "queue"
JOBS = Q / "jobs"
LOGS = Q / "logs"
DAEMON = Q / "daemon.json"
LOCK = Q / "lock"
TERMINAL = {"done", "failed", "timeout", "cancelled", "skipped", "cached"}
ACTIVE = {"running", "suspended"}
STATES = TERMINAL | ACTIVE | {"queued", "held"}

DEFAULTS = {"mem_gb": None, "cores": None, "priority": 0, "retries": 0, "retry_on": ["oom"], "timeout_s": None,
            "nice": 10, "limit_memory": True, "gpus": 0, "preempt": False, "when_idle": False, "dep_mode": "ok"}


# ---------------------------------------------------------------- storage
def _ensure():
    for d in (JOBS, LOGS):
        d.mkdir(parents=True, exist_ok=True)


class _Lock:
    def __enter__(self):
        _ensure()
        self.f = open(LOCK, "a")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def _path(jid):
    return JOBS / f"{int(jid)}.json"


def load(jid):
    try:
        return json.loads(_path(jid).read_text())
    except (OSError, ValueError):
        return None


def save(job):
    _ensure()
    tmp = _path(job["id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=1, default=str))
    tmp.replace(_path(job["id"]))


def update(jid, **kw):
    with _Lock():
        j = load(jid)
        if j is None:
            return None
        j.update(kw)
        save(j)
        return j


def all_jobs():
    _ensure()
    out = []
    for f in JOBS.glob("*.json"):
        try:
            out.append(json.loads(f.read_text()))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda j: j["id"])


def _next_id():
    ids = [int(p.stem) for p in JOBS.glob("*.json") if p.stem.isdigit()]
    seq = Q / "seq"
    try:
        last = int(seq.read_text())
    except (OSError, ValueError):
        last = 0
    n = max([last] + ids) + 1
    seq.write_text(str(n))
    return n


def parse_duration(s):
    """'90', '90s', '45m', '2h', '1d' -> seconds; None/'' -> None; 'auto' -> 'auto'."""
    if s in (None, "", 0):
        return None
    if isinstance(s, (int, float)):
        return int(s)
    s = str(s).strip().lower()
    if s == "auto":
        return "auto"
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd]?)", s)
    if not m:
        raise ValueError(f"bad duration {s!r} (use 90s, 45m, 2h, 1d)")
    return int(float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)])


def guess_tool(cmd):
    from . import plugins
    try:
        argv = shlex.split(cmd)
    except ValueError:
        argv = cmd.split()
    for i in range(len(argv)):
        plug, tool = plugins.match(argv[i:])
        if plug:
            return tool
    return None


def submit(cmd, cwd=None, name=None, **opt):
    """Add a job; returns it. `cmd` is a shell command line."""
    cwd = os.path.abspath(os.path.expanduser(cwd or os.getcwd()))
    tool = opt.pop("tool", None) or guess_tool(cmd)
    prof = capacity.profiles().get(tool or "", {})
    j = dict(DEFAULTS)
    j.update({k: v for k, v in opt.items() if v is not None})
    if j.get("mem_gb") is None:
        j["mem_gb"] = prof.get("peak_gb", 2.0)
    if j.get("cores") is None:
        j["cores"] = prof.get("cores") or 1.0
    if isinstance(j.get("timeout_s"), str):
        j["timeout_s"] = parse_duration(j["timeout_s"])
    if j.get("timeout_s") == "auto":
        m = prof.get("minutes")
        j["timeout_s"] = int(max(1800, 3 * m * 60)) if m else None
    nm = re.sub(r"[^A-Za-z0-9_.+-]", "_", name or (Path(shlex.split(cmd)[0]).name if cmd.strip() else "job"))[:60]
    with _Lock():
        jid = _next_id()
        j.update({"id": jid, "name": nm, "cmd": cmd, "cwd": cwd, "tool": tool, "state": opt.get("state") or "queued",
                  "created": time.time(), "attempt": 1, "started": None, "ended": None, "rc": None, "reason": "",
                  "pid": None, "peak_rss": 0, "cpu_s": 0.0, "env": opt.get("env") or {},
                  "depends": [int(x) for x in (opt.get("depends") or [])], "user": os.environ.get("USER", "")})
        save(j)
    return j


def runtime(j):
    if not j.get("started"):
        return 0
    return (j.get("ended") or time.time()) - j["started"]


# ---------------------------------------------------------------- process helpers
def _session_procs(sid):
    out = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            st = (p / "stat").read_text()
            f = st[st.rfind(")") + 2:].split()
            if int(f[3]) == sid:            # session id
                out.append((int(p.name), int(f[21]) * os.sysconf("SC_PAGE_SIZE"), int(f[11]) + int(f[12])))
        except (OSError, ValueError, IndexError):
            continue
    return out


def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        st = Path(f"/proc/{pid}/stat").read_text()
        return st[st.rfind(")") + 2] != "Z"
    except (OSError, IndexError):
        return False


def signal_job(j, sig):
    """Send `sig` to the job's whole process session (the runner is the session leader)."""
    sid = j.get("sid") or j.get("pid")
    if not sid:
        return False
    sent = False
    for pid, _, _ in _session_procs(sid):
        if pid == j.get("pid") and sig in (signal.SIGSTOP, signal.SIGCONT):
            continue                          # keep the runner itself responsive
        try:
            os.kill(pid, sig)
            sent = True
        except OSError:
            pass
    return sent


def job_rss(j):
    sid = j.get("sid") or j.get("pid")
    return sum(r for _, r, _ in _session_procs(sid)) if sid else 0


# ---------------------------------------------------------------- preconditions
def check_require(req, cwd):
    """(ok, reason). Forms: 'mounted PATH', 'exists PATH', 'free_gb PATH N', 'cmd SHELL'."""
    for r in req or []:
        kind, _, arg = str(r).strip().partition(" ")
        arg = os.path.expanduser(arg.strip())
        if kind == "mounted":
            if not os.path.ismount(arg):
                return False, f"{arg} is not mounted"
        elif kind == "exists":
            p = arg if os.path.isabs(arg) else os.path.join(cwd, arg)
            if not glob.glob(p):
                return False, f"{arg} does not exist"
        elif kind == "free_gb":
            path, _, n = arg.rpartition(" ")
            d = probes.disk(path) or probes.disk(os.path.dirname(path) or "/")
            try:
                if d and d[1] < float(n) * GiB:
                    return False, f"less than {n} GB free on {path}"
            except ValueError:
                return False, f"bad require: {r}"
        elif kind == "cmd":
            try:
                if subprocess.run(arg, shell=True, cwd=cwd if os.path.isdir(cwd) else None, timeout=60,
                                  capture_output=True).returncode != 0:
                    return False, f"check failed: {arg}"
            except (OSError, subprocess.TimeoutExpired):
                return False, f"check failed: {arg}"
        else:
            return False, f"unknown require: {r}"
    return True, ""


# ---------------------------------------------------------------- GPUs (only when nvidia-smi works)
def free_gpus():
    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.used,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    res = []
    for line in out.splitlines():
        try:
            i, used, tot = [x.strip() for x in line.split(",")]
            if float(used) / max(1.0, float(tot)) < 0.1:
                res.append(int(i))
        except ValueError:
            continue
    return res


# ---------------------------------------------------------------- the runner (one per job)
def build_command(j):
    """argv that runs the job's command with nice, memory limit, container or remote host."""
    cmd = j["cmd"]
    if j.get("container"):
        uid, gid = os.getuid(), os.getgid()
        mem = f"--memory={int(float(j['mem_gb']) * 1.25 * 1024)}m" if j.get("limit_memory") else ""
        cmd = (f"docker run --rm --init -u {uid}:{gid} -v {shlex.quote(j['cwd'])}:{shlex.quote(j['cwd'])} "
               f"-w {shlex.quote(j['cwd'])} {mem} {shlex.quote(j['container'])} bash -c {shlex.quote(j['cmd'])}")
    if j.get("host"):
        envs = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in (j.get("env") or {}).items())
        remote = "cd " + shlex.quote(j["cwd"]) + " && " + (envs + " " if envs else "") + cmd
        cmd = "ssh -o BatchMode=yes " + shlex.quote(j["host"]) + " " + shlex.quote(remote)
    argv = ["nice", "-n", str(int(j.get("nice", 10)))] if j.get("nice") else []
    argv += ["/bin/bash", "-c", cmd]
    if j.get("limit_memory") and not j.get("container") and not j.get("host") and cgroup_ok():
        lim = f"{max(1, int(float(j['mem_gb']) * 1.25 * 1024))}M"
        argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit=msq-{j['id']}-{j['attempt']}",
                "-p", f"MemoryMax={lim}", "-p", "MemorySwapMax=0"] + argv
    return argv


def cgroup_ok():
    """Can we put a job in its own memory-limited scope? (checked once, remembered for a day)"""
    if os.environ.get("MS_NO_CGROUP") == "1" or not shutil.which("systemd-run"):
        return False
    f = Q / "cgroup_ok"
    try:
        if time.time() - f.stat().st_mtime < 86400:
            return f.read_text().strip() == "1"
    except OSError:
        pass
    try:
        ok = subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "--collect", "-p", "MemoryMax=64M", "true"],
                            capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    try:
        f.write_text("1" if ok else "0")
    except OSError:
        pass
    return ok


def provenance(j):
    """What exactly ran: tool binary and version string, input file fingerprints, environment."""
    p = {"host": socket.gethostname(), "attempt": j["attempt"], "queue_id": j["id"], "mem_gb": j["mem_gb"],
         "cores": j["cores"], "kernel": os.uname().release}
    try:
        exe = shlex.split(j["cmd"])[0]
    except (ValueError, IndexError):
        exe = ""
    w = shutil.which(exe, path=(j.get("env") or {}).get("PATH") or os.environ.get("PATH")) if exe else None
    if w:
        p["tool_path"] = os.path.realpath(w)
        m = re.search(r"/(\d{4}\.\d(?:\.\d)?)/", p["tool_path"])
        if m:
            p["tool_version"] = m.group(1)
    fp = {}
    for pat in j.get("inputs") or []:
        for f in sorted(glob.glob(os.path.join(j["cwd"], pat), recursive=True))[:200]:
            try:
                st = os.stat(f)
                fp[os.path.relpath(f, j["cwd"])] = f"{st.st_size}:{int(st.st_mtime)}"
            except OSError:
                pass
    if fp:
        p["inputs"] = fp
    keep = ("XILINX_VIVADO", "XILINX_VITIS", "XILINX_HLS", "LM_LICENSE_FILE", "XILINXD_LICENSE_FILE", "PATH",
            "OMP_NUM_THREADS", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX")
    env = dict(os.environ, **(j.get("env") or {}))
    p["env"] = {k: env[k][:300] for k in keep if k in env}
    return p


def execute(jid):
    """Run one job (called as `ms q _exec ID` in its own session). Updates the job file when done."""
    sys.path.insert(0, str(config.MS / "runlog"))
    import msrun
    j = load(jid)
    if not j:
        return 2
    os.makedirs(j["cwd"], exist_ok=True)
    logf = LOGS / f"{jid}.log"
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (j.get("env") or {}).items()})
    n = max(1, int(round(float(j.get("cores") or 1))))
    env.setdefault("OMP_NUM_THREADS", str(n))
    env["MSQ_JOB_ID"] = str(jid)
    if j.get("gpu_ids") is not None:
        env["CUDA_VISIBLE_DEVICES"] = ",".join(map(str, j["gpu_ids"]))
    run = msrun.Run(j["name"], params=dict(j.get("params") or {}), folder=j["cwd"], cmd=j["cmd"],
                    project=(j.get("flow") or {}).get("name"), extra={"provenance": provenance(j)})
    run.start()
    t0 = time.time()
    update(jid, run_id=run.run, started=t0, sid=os.getsid(0), pid=os.getpid())
    with open(logf, "a") as lf:
        lf.write(f"=== attempt {j['attempt']} · {time.strftime('%F %T')} · {j['cmd']}\n")
        lf.flush()
        try:
            p = subprocess.Popen(build_command(j), cwd=j["cwd"], env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
        except OSError as e:
            run.error(f"cannot start: {e}")
            run.end("failed", rc=127, error=str(e))
            update(jid, state="failed", rc=127, ended=time.time(), reason=f"cannot start: {e}")
            return 127
        errs = [0]

        def pump():
            for line in p.stdout:
                lf.write(line)
                s = line.strip()
                if errs[0] < 100 and re.match(r"^(ERROR|FATAL|Error|Fatal)\b", s):
                    errs[0] += 1
                    run.log("ERROR", s[:1000])
                elif errs[0] < 200 and s.startswith("CRITICAL WARNING"):
                    run.log("WARN", s[:1000])
            lf.flush()
        th = threading.Thread(target=pump, daemon=True)
        th.start()
        peak, timed_out = 0, False
        hz = os.sysconf("SC_CLK_TCK")
        last_save = 0
        seen_ticks = {}
        while p.poll() is None:
            procs = [x for x in _session_procs(os.getsid(0)) if x[0] != os.getpid()]
            rss = sum(r for _, r, _ in procs)
            for pid_, _, t_ in procs:
                seen_ticks[pid_] = max(t_, seen_ticks.get(pid_, 0))
            peak = max(peak, rss)
            if time.time() - last_save > 20:
                update(jid, peak_rss=peak, rss=rss, cpu_s=round(sum(seen_ticks.values()) / hz, 1))
                last_save = time.time()
            to = j.get("timeout_s")
            if isinstance(to, (int, float)) and to and time.time() - t0 > to and not timed_out:
                timed_out = True
                run.log("ERROR", f"timeout after {int(to)} s — stopping")
                for pid, _, _ in procs:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
                for _ in range(30):
                    if p.poll() is not None:
                        break
                    time.sleep(1)
                for pid, _, _ in _session_procs(os.getsid(0)):
                    if pid != os.getpid():
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except OSError:
                            pass
            time.sleep(3)
        rc = p.wait()
        th.join(timeout=10)
    secs = time.time() - t0
    update(jid, cpu_s=round(sum(seen_ticks.values()) / hz, 1))
    cur = load(jid) or j
    oom = rc in (-9, 137) and (peak >= 0.9 * float(j["mem_gb"]) * 1.25 * GiB or _cgroup_oom(j))
    if cur.get("state") == "cancelled":
        status, state, reason = "cancelled", "cancelled", "cancelled by user"
    elif timed_out:
        status, state, reason = "failed", "timeout", f"timeout after {int(j['timeout_s'])} s"
    elif rc == 0 and not errs[0]:
        status, state, reason = "ok", "done", ""
    else:
        status, state = "failed", "failed"
        reason = "out of memory (killed at the job's memory limit)" if oom else f"exit code {rc}" + (
            f", {errs[0]} ERROR line(s)" if errs[0] else "")
    cores_used = (cur.get("cpu_s") or 0) / secs if secs > 0 else 0
    run.metric("runtime_s", round(secs, 1), "s")
    run.metric("peak_rss_gb", round(peak / GiB, 2), "GB")
    if status == "ok":
        mark_done(cur)
    run.end(status, rc=rc if rc is not None else 1, error=None if status == "ok" else reason)
    update(jid, state=state, rc=rc, ended=time.time(), reason=reason, peak_rss=peak, oom=bool(oom),
           cores_used=round(cores_used, 2))
    return 0


def _cgroup_oom(j):
    for f in glob.glob(f"/sys/fs/cgroup/user.slice/**/msq-{j['id']}-{j['attempt']}.scope/memory.events", recursive=True):
        try:
            for line in open(f):
                if line.startswith("oom_kill ") and int(line.split()[1]) > 0:
                    return True
        except (OSError, ValueError):
            pass
    return False


# ---------------------------------------------------------------- incremental / resume markers
def job_key(j):
    import hashlib
    h = hashlib.sha256()
    h.update(j["cmd"].encode())
    h.update(json.dumps(j.get("params") or {}, sort_keys=True).encode())
    for pat in sorted(j.get("inputs") or []):
        for f in sorted(glob.glob(os.path.join(j["cwd"], pat), recursive=True)):
            try:
                st = os.stat(f)
                h.update(f"{os.path.relpath(f, j['cwd'])}:{st.st_size}:{st.st_mtime_ns}".encode())
            except OSError:
                pass
    return h.hexdigest()[:20]


def _marker(j):
    return Path(j["cwd"]) / ".msq_done.json"


def mark_done(j):
    try:
        m = json.loads(_marker(j).read_text())
    except (OSError, ValueError):
        m = {}
    m[j.get("step_key") or j["name"]] = {"key": job_key(j), "t": int(time.time())}
    try:
        _marker(j).write_text(json.dumps(m, indent=1))
    except OSError:
        pass


def up_to_date(j):
    """True when this exact job (same command, parameters and inputs) already succeeded and its outputs exist."""
    try:
        m = json.loads(_marker(j).read_text())
    except (OSError, ValueError):
        return False
    e = m.get(j.get("step_key") or j["name"])
    if not e or e.get("key") != job_key(j):
        return False
    for pat in j.get("outputs") or []:
        if not glob.glob(os.path.join(j["cwd"], pat)):
            return False
    return True


# ---------------------------------------------------------------- scheduler
def _launch(j, gpu_ids=None):
    upd = {"state": "running", "started": time.time(), "reason": "", "gpu_ids": gpu_ids}
    update(j["id"], **upd)
    devnull = open(os.devnull, "w")
    p = subprocess.Popen([sys.executable, str(config.MS / "bin" / "ms"), "q", "_exec", str(j["id"])],
                         stdin=subprocess.DEVNULL, stdout=devnull, stderr=devnull, start_new_session=True,
                         env=dict(os.environ, MY_SYSTEM=str(config.MS)))
    update(j["id"], pid=p.pid, sid=p.pid)
    return p.pid


class Scheduler:
    def __init__(self):
        self.last_action = 0
        self.told = set()
        self.procs = {}
        self.flow_told = set()

    def notify(self, title, msg, prio="default", tag="computer"):
        try:
            subprocess.Popen([str(config.MS / "bin" / "notify.sh"), title, msg[:400], prio, tag],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def capacity(self, jobs):
        th = probes.threads()
        m = probes.meminfo()
        total, avail = m.get("MemTotal", 0), m.get("MemAvailable", 0)
        res = capacity.reserve_bytes(total)
        l1 = probes.loadavg()[0]
        now = time.time()
        grow = 0
        recent_cores = 0.0
        for j in jobs:
            if j["state"] == "running":
                rss = j.get("rss") or 0
                grow += max(0, float(j["mem_gb"]) * GiB - rss)
                if now - (j.get("started") or now) < 90:
                    recent_cores += float(j["cores"])
        max_slots = config.num("QUEUE_MAX_JOBS") or th
        free_threads = th - l1 - recent_cores
        free_mem = avail - res - grow
        return {"threads": th, "free_threads": free_threads, "free_mem": free_mem, "avail": avail, "reserve": res,
                "max_slots": max_slots, "total": total}

    def idle(self):
        from . import watcher
        others = [x for x in watcher.jobs() if not os.environ.get("MSQ_JOB_ID")]
        return probes.loadavg()[0] < probes.threads() * 0.2 and not others

    def tick(self):
        while True:                       # reap finished runners (they are our children)
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
        jobs = all_jobs()
        byid = {j["id"]: j for j in jobs}
        now = time.time()
        # 1. runners that died without writing a result
        for j in jobs:
            if j["state"] in ACTIVE and j.get("pid") and not _alive(j["pid"]):
                time.sleep(0.5)
                k = load(j["id"])
                if k and k["state"] in ACTIVE:
                    update(j["id"], state="failed", ended=now, reason="runner stopped unexpectedly")
        jobs = all_jobs()
        byid = {j["id"]: j for j in jobs}
        # 2. retries
        for j in jobs:
            if j["state"] in ("failed", "timeout") and not j.get("retried"):
                why = "oom" if j.get("oom") else "timeout" if j["state"] == "timeout" else "fail"
                if j["attempt"] <= int(j.get("retries") or 0) and (why in (j.get("retry_on") or []) or "any" in (j.get("retry_on") or [])):
                    mem = float(j["mem_gb"]) * (1.5 if why == "oom" else 1.0)
                    update(j["id"], state="queued", attempt=j["attempt"] + 1, mem_gb=round(mem, 1), started=None,
                           ended=None, rc=None, not_before=now + 30, oom=False,
                           reason=f"retry {j['attempt']}/{j['retries']} after {why}"
                                  + (f", memory raised to {mem:.1f} GB" if why == "oom" else ""))
                else:
                    update(j["id"], retried=True)
                    if not j.get("flow") and j["id"] not in self.told:
                        self.told.add(j["id"])
                        self.notify(f"Job failed: {j['name']}", f"#{j['id']} · {j['reason']} · {short(j['cwd'])}", "high", "x")
        jobs = all_jobs()
        byid = {j["id"]: j for j in jobs}
        # 3. dependencies
        for j in jobs:
            if j["state"] != "queued" or not j.get("depends"):
                continue
            deps = [byid.get(d) for d in j["depends"]]
            if any(d is None for d in deps):
                update(j["id"], state="skipped", reason="dependency missing", ended=now)
                continue
            if j.get("dep_mode", "ok") == "ok" and any(d["state"] in TERMINAL - {"done", "cached"} for d in deps):
                bad = next(d for d in deps if d["state"] in TERMINAL - {"done", "cached"})
                update(j["id"], state="skipped", reason=f"dependency #{bad['id']} {bad['state']}", ended=now)
        jobs = all_jobs()
        cap = self.capacity(jobs)
        running = [j for j in jobs if j["state"] == "running"]
        # 4. memory pressure: suspend the newest lowest-priority job; resume when there is room again
        if cap["avail"] < cap["reserve"] * 0.6 and running and now - self.last_action > 30:
            victim = sorted(running, key=lambda j: (j["priority"], -(j.get("started") or 0)))[0]
            if len(running) > 1 or cap["avail"] < cap["reserve"] * 0.3:
                signal_job(victim, signal.SIGSTOP)
                update(victim["id"], state="suspended", reason="paused: RAM almost full", suspended_at=now)
                self.notify("Job paused (RAM low)", f"#{victim['id']} {victim['name']} paused to protect the machine; "
                                                    "it continues when memory frees up.", "default", "pause_button")
                self.last_action = now
                return
        susp = sorted([j for j in jobs if j["state"] == "suspended"], key=lambda j: (-j["priority"], j.get("started") or 0))
        if susp and now - self.last_action > 30:
            s = susp[0]
            need = (s.get("rss") or 0) * 0.2 + cap["reserve"] * 0.5
            if cap["avail"] - cap["reserve"] > need and (s.get("reason", "").startswith("paused: RAM") or cap["free_threads"] >= float(s["cores"])):
                signal_job(s, signal.SIGCONT)
                update(s["id"], state="running", reason="resumed")
                self.last_action = now
                return
        # 5. start what fits, highest priority first (smaller jobs may backfill)
        ready = []
        for j in jobs:
            if j["state"] != "queued" or (j.get("not_before") or 0) > now:
                continue
            if j.get("depends") and not all(byid.get(d, {}).get("state") in ("done", "cached") or
                                            (j.get("dep_mode") == "any" and byid.get(d, {}).get("state") in TERMINAL)
                                            for d in j["depends"]):
                continue
            ready.append(j)
        ready.sort(key=lambda j: (-int(j.get("priority") or 0), j["id"]))
        slots = cap["max_slots"] - len(running)
        free_t, free_m = cap["free_threads"], cap["free_mem"]
        gpus = None
        for j in ready:
            if slots <= 0:
                break
            if j.get("inputs") or j.get("outputs"):
                if j.get("incremental", True) and up_to_date(j):
                    update(j["id"], state="cached", reason="up to date (same command and inputs)", ended=now)
                    continue
            ok, why = check_require(j.get("require"), j["cwd"])
            if not ok:
                if j.get("reason") != f"waiting: {why}":
                    update(j["id"], reason=f"waiting: {why}")
                continue
            if j.get("when_idle") and not self.idle():
                if j.get("reason") != "waiting for an idle machine":
                    update(j["id"], reason="waiting for an idle machine")
                continue
            need_t = min(max(1.0, float(j["cores"])), cap["threads"] * 0.75)
            need_m = float(j["mem_gb"]) * GiB
            gpu_ids = None
            if int(j.get("gpus") or 0):
                gpus = free_gpus() if gpus is None else gpus
                if len(gpus) < int(j["gpus"]):
                    update(j["id"], reason="waiting for a free GPU" if shutil.which("nvidia-smi") else "no usable GPU on this machine")
                    continue
                gpu_ids, gpus = gpus[:int(j["gpus"])], gpus[int(j["gpus"]):]
            if free_t >= need_t and free_m >= need_m:
                _launch(j, gpu_ids)
                running.append(j)
                slots -= 1
                free_t -= need_t
                free_m -= need_m
            else:
                why = "RAM" if free_m < need_m else "CPU"
                msg = f"waiting for {why} ({need_m / GiB:.1f} GB, {need_t:.1f} cores needed)"
                if j.get("reason") != msg:
                    update(j["id"], reason=msg)
                # 6. preemption: a high-priority job may pause lower-priority running jobs (CPU only)
                if j.get("preempt") and why == "CPU" and now - self.last_action > 30:
                    low = sorted([r for r in running if int(r.get("priority") or 0) < int(j.get("priority") or 0)
                                  and load(r["id"]).get("state") == "running"],
                                 key=lambda r: (r["priority"], -(r.get("started") or 0)))
                    if low:
                        v = low[0]
                        signal_job(v, signal.SIGSTOP)
                        update(v["id"], state="suspended", reason=f"paused for higher-priority job #{j['id']}")
                        self.last_action = now
                        return
        # 7. stuck detection for jobs without an explicit timeout
        for j in running:
            prof = capacity.profiles().get(j.get("tool") or "", {})
            m = prof.get("minutes") if prof.get("measured") else None
            if m and not j.get("timeout_s") and runtime(j) > max(1800, 3 * m * 60) and f"stuck{j['id']}" not in self.told:
                self.told.add(f"stuck{j['id']}")
                self.notify(f"Job may be stuck: {j['name']}", f"#{j['id']} has run {runtime(j) / 3600:.1f} h; "
                                                               f"{j['tool']} usually takes {m} min.", "default", "hourglass")
        # 8. flows that just finished: summary, hooks, temp cleanup
        from . import flow
        flow.finish_flows(self)

    def loop(self):
        _ensure()
        last_sched = 0
        while True:
            try:
                self.tick()
            except Exception as e:  # keep scheduling whatever happens
                DAEMON.write_text(json.dumps({"pid": os.getpid(), "t": time.time(), "error": f"{type(e).__name__}: {e}"}))
            else:
                DAEMON.write_text(json.dumps({"pid": os.getpid(), "t": time.time()}))
            if time.time() - last_sched > 55:
                last_sched = time.time()
                try:
                    from . import flow
                    flow.run_schedules(self)
                    export_prometheus()
                except Exception:
                    pass
            time.sleep(float(os.environ.get("MSQ_TICK", "3")))


def short(p, n=3):
    parts = Path(p).parts
    return "/".join(parts[-n:]) if len(parts) > n else str(p)


def daemon_state():
    try:
        d = json.loads(DAEMON.read_text())
    except (OSError, ValueError):
        return "stopped", None
    if time.time() - d.get("t", 0) > 30 or not _alive(d.get("pid")):
        return "stopped", d
    return ("error" if d.get("error") else "running"), d


# ---------------------------------------------------------------- control
def cancel(jid):
    j = load(jid)
    if not j or j["state"] in TERMINAL:
        return False
    if j["state"] in ACTIVE:
        update(jid, state="cancelled", reason="cancelled by user")
        signal_job(j, signal.SIGCONT)
        signal_job(j, signal.SIGTERM)
    else:
        update(jid, state="cancelled", reason="cancelled by user", ended=time.time())
    return True


def hold(jid):
    j = load(jid)
    if j and j["state"] == "queued":
        update(jid, state="held", reason="held by user")
        return True
    return False


def release(jid):
    j = load(jid)
    if j and j["state"] == "held":
        update(jid, state="queued", reason="")
        return True
    return False


def suspend(jid):
    j = load(jid)
    if j and j["state"] == "running":
        signal_job(j, signal.SIGSTOP)
        update(jid, state="suspended", reason="paused by user")
        return True
    return False


def resume(jid):
    j = load(jid)
    if j and j["state"] == "suspended":
        signal_job(j, signal.SIGCONT)
        update(jid, state="running", reason="resumed by user")
        return True
    return False


def retry(jid):
    j = load(jid)
    if j and j["state"] in TERMINAL:
        update(jid, state="queued", attempt=j["attempt"] + 1, started=None, ended=None, rc=None, reason="retried by user",
               retried=False, oom=False)
        return True
    return False


def set_priority(jid, p):
    return update(jid, priority=int(p)) is not None


def clear(older_than_days=0):
    n = 0
    for j in all_jobs():
        if j["state"] in TERMINAL and time.time() - (j.get("ended") or j["created"]) >= older_than_days * 86400:
            _path(j["id"]).unlink(missing_ok=True)
            (LOGS / f"{j['id']}.log").unlink(missing_ok=True)
            n += 1
    return n


# ---------------------------------------------------------------- reports
def efficiency(j):
    """seff-like numbers: memory used vs reserved, CPU used vs reserved."""
    req_m = float(j.get("mem_gb") or 0)
    peak = (j.get("peak_rss") or 0) / GiB
    secs = runtime(j)
    cu = j.get("cores_used")
    if cu is None and j.get("cpu_s") and secs:
        cu = j["cpu_s"] / secs
    return {"mem_req_gb": req_m, "mem_peak_gb": round(peak, 2), "mem_eff": round(peak / req_m * 100) if req_m else None,
            "cores_req": float(j.get("cores") or 0), "cores_used": round(cu, 2) if cu else None,
            "cpu_eff": round(cu / float(j["cores"]) * 100) if cu and j.get("cores") else None, "seconds": round(secs)}


def export_prometheus():
    """reports/metrics.prom for node_exporter's textfile collector (or any scraper)."""
    jobs = all_jobs()
    m = probes.meminfo()
    d = probes.disk("/")
    L = ["# HELP msq_jobs Jobs in the my-system queue by state", "# TYPE msq_jobs gauge"]
    for st in sorted(STATES):
        L.append(f'msq_jobs{{state="{st}"}} {sum(1 for j in jobs if j["state"] == st)}')
    L += ["# TYPE ms_load1 gauge", f"ms_load1 {probes.loadavg()[0]:.2f}",
          "# TYPE ms_mem_available_bytes gauge", f"ms_mem_available_bytes {m.get('MemAvailable', 0)}",
          "# TYPE ms_root_free_bytes gauge", f"ms_root_free_bytes {d[1] if d else 0}"]
    t = probes.cpu_temp()
    if t is not None:
        L += ["# TYPE ms_cpu_temp_celsius gauge", f"ms_cpu_temp_celsius {t}"]
    try:
        tmp = config.REPORTS / "metrics.prom.tmp"
        tmp.write_text("\n".join(L) + "\n")
        tmp.replace(config.REPORTS / "metrics.prom")
    except OSError:
        pass
