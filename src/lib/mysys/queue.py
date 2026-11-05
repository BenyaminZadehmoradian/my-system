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
import time
from pathlib import Path

from . import capacity, config, probes

GiB = 2**30
Q = config.STATE / "queue"
JOBS = Q / "jobs"
LOGS = Q / "logs"
DAEMON = Q / "daemon.json"
LOCK = Q / "lock"
TERMINAL = {"done", "warning", "failed", "timeout", "cancelled", "skipped", "cached"}
SUCCESS = {"done", "warning", "cached"}
ACTIVE = {"running", "suspended"}
STATES = TERMINAL | ACTIVE | {"queued", "held", "approval"}

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


def parse_when(s, now=None):
    """'16:00', '+2h', '2026-11-01 08:00', epoch -> epoch seconds."""
    if s in (None, ""):
        return None
    if isinstance(s, (int, float)):
        return float(s)
    now = now or time.time()
    s = str(s).strip()
    if s.startswith("+"):
        return now + parse_duration(s[1:])
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if m:
        lt = time.localtime(now)
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, int(m.group(1)), int(m.group(2)), 0, 0, 0, -1))
        return t if t > now else t + 86400
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(s, fmt))
        except ValueError:
            pass
    raise ValueError(f"bad time {s!r} (use 16:00, +2h or 2026-11-01 08:00)")


def resolve_timeout(val, prof):
    """seconds | 'auto' (3x usual) | 'N%' of the tool's usual duration -> seconds or None."""
    if val in (None, "", 0):
        return None
    if isinstance(val, str) and val.strip().endswith("%"):
        m = capacity.typical_minutes(prof)
        return int(max(600, float(val.strip()[:-1]) / 100 * m * 60)) if m else None
    v = parse_duration(val) if isinstance(val, str) else val
    if v == "auto":
        m = capacity.typical_minutes(prof)
        return int(max(1800, 3 * m * 60)) if m else None
    return v


def submit(cmd, cwd=None, name=None, **opt):
    """Add a job; returns it. `cmd` is a shell command line."""
    cwd = os.path.abspath(os.path.expanduser(cwd or os.getcwd()))
    tool = opt.pop("tool", None) or guess_tool(cmd)
    prof = capacity.profiles().get(tool or "", {})
    j = dict(DEFAULTS)
    j.update({k: v for k, v in opt.items() if v is not None})
    if j.get("remote") == "auto":          # may run on a remote server when this machine is busy
        j["remote"], j["remote_ok"] = None, True
    elif j.get("remote") in ("here", "local"):   # never on a server, even with offload = prefer
        j["remote"], j["remote_never"] = None, True
    if j.get("mem_gb") is None:
        j["mem_gb"] = prof.get("peak_gb", 2.0)
    if j.get("cores") is None:
        j["cores"] = prof.get("cores") or 1.0
    # never ask for more than the machine has (Nextflow resourceLimits)
    total = probes.meminfo().get("MemTotal", 0)
    if total:
        cap_gb = (total - capacity.reserve_bytes(total)) / GiB
        if float(j["mem_gb"]) > cap_gb:
            j["mem_gb"], j["clamped"] = round(cap_gb, 1), True
    j["cores"] = min(float(j["cores"]), float(probes.threads()))
    j["timeout_s"] = resolve_timeout(j.get("timeout_s"), prof)
    if isinstance(j.get("idle_timeout_s"), str):
        j["idle_timeout_s"] = parse_duration(j["idle_timeout_s"])
    for k in ("begin", "deadline"):
        if j.get(k) is not None:
            j[k] = parse_when(j[k])
    nm = re.sub(r"[^A-Za-z0-9_.+-]", "_", name or (Path(shlex.split(cmd)[0]).name if cmd.strip() else "job"))[:60]
    with _Lock():
        # collapse: a newer request with the same key replaces older ones that have not started
        if j.get("collapse_key"):
            for old in all_jobs():
                if old.get("collapse_key") == j["collapse_key"] and old["state"] in ("queued", "held"):
                    old.update(state="cancelled", reason="replaced by a newer request", ended=time.time())
                    save(old)
        jid = _next_id()
        state = opt.get("state") or ("approval" if j.get("approval") else "queued")
        j.update({"id": jid, "name": nm, "cmd": cmd, "cwd": cwd, "tool": tool, "state": state,
                  "created": time.time(), "attempt": 1, "started": None, "ended": None, "rc": None, "reason": "",
                  "pid": None, "peak_rss": 0, "cpu_s": 0.0, "env": opt.get("env") or {},
                  "depends": [int(x) for x in (opt.get("depends") or [])], "user": os.environ.get("USER", "")})
        if state == "approval":
            j["reason"] = f"waiting for your approval: {j['approval']}"
        try:                                    # XILINX_ALLOWED: refused at once, not after waiting in the queue
            from . import versions
            bad = versions.check_text(cmd, cwd)
        except Exception:
            bad = None
        if bad:
            j.update(state="failed", reason=versions.message(bad, cwd), ended=time.time(), retried=True)
        save(j)
    if j["state"] == "approval":
        try:
            subprocess.Popen([str(config.MS / "bin" / "notify.sh"), f"Approval needed: {nm}",
                              f"#{jid} {j['approval']} — approve with: ms q approve {jid}", "high", "raising_hand"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
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
        if pid == j.get("pid") and sig != signal.SIGKILL:
            continue                          # the runner stays: it finishes the job's log and cleans up (e.g. scancel)
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
                             capture_output=True, text=True, errors="replace", timeout=10).stdout
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
    if j.get("remote"):                    # Slurm cluster: a small local process uploads, follows and downloads
        return [sys.executable, str(config.MS / "bin" / "ms"), "remote", "_run", str(j["id"])]
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
    if j.get("cpus") and shutil.which("taskset") and not j.get("host") and not j.get("container"):
        argv += ["taskset", "-c", ",".join(map(str, j["cpus"]))]
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
    from . import jobrun
    return jobrun.execute(jid)


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
def key_parts(j):
    """Hashes of what makes a job "the same": command, parameters, inputs (by mode), software."""
    import hashlib

    def h(x):
        return hashlib.sha256(x.encode()).hexdigest()[:16]
    mode = j.get("cache_mode", "standard")          # standard: size+mtime, lenient: size, deep: content
    inp = []
    for pat in sorted(j.get("inputs") or []):
        for f in sorted(glob.glob(os.path.join(j["cwd"], pat), recursive=True)):
            try:
                st = os.stat(f)
                rel = os.path.relpath(f, j["cwd"])
                if mode == "deep" and os.path.isfile(f):
                    from . import lineage
                    inp.append(f"{rel}:{lineage.fingerprint(f)}")
                elif mode == "lenient":
                    inp.append(f"{rel}:{st.st_size}")
                else:
                    inp.append(f"{rel}:{st.st_size}:{st.st_mtime_ns}")
            except OSError:
                pass
    tool = ""
    try:
        exe = shutil.which(shlex.split(j["cmd"])[0])
        tool = os.path.realpath(exe) if exe else ""
    except (ValueError, IndexError):
        pass
    return {"cmd": h(j["cmd"]), "params": h(json.dumps(j.get("params") or {}, sort_keys=True, default=str)),
            "inputs": h("\n".join(inp)), "software": h(tool), "inputs_list": inp[:200]}


def job_key(j, parts=None):
    import hashlib
    parts = parts or key_parts(j)
    use = j.get("rerun_on") or ["cmd", "params", "inputs"]
    return hashlib.sha256("|".join(parts[k] for k in sorted(use) if k in parts).encode()).hexdigest()[:20]


def why_rerun(j):
    """Explain whether a job is up to date, and if not, what changed (Snakemake --list-changes / Nextflow -dump-hashes)."""
    try:
        e = json.loads(_marker(j).read_text()).get(j.get("step_key") or j["name"])
    except (OSError, ValueError):
        e = None
    if not e:
        return ["never finished successfully here"]
    cur = key_parts(j)
    old = e.get("parts") or {}
    out = [f"{k} changed" for k in ("cmd", "params", "inputs", "software")
           if k in (j.get("rerun_on") or ["cmd", "params", "inputs"]) and old.get(k) and old.get(k) != cur[k]]
    if "inputs" in [x.split()[0] for x in out]:
        before = set(old.get("inputs_list") or [])
        after = set(cur["inputs_list"])
        out += [f"  input changed: {x.split(':')[0]}" for x in sorted(after - before)][:20]
    missing = [p for p in j.get("outputs") or [] if not glob.glob(os.path.join(j["cwd"], p))]
    out += [f"output missing: {p}" for p in missing]
    return out


def _marker(j):
    return Path(j["cwd"]) / ".msq_done.json"


def mark_done(j):
    try:
        m = json.loads(_marker(j).read_text())
    except (OSError, ValueError):
        m = {}
    parts = key_parts(j)
    m[j.get("step_key") or j["name"]] = {"key": job_key(j, parts), "t": int(time.time()), "parts": parts,
                                         "job": j.get("id")}
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


# ---------------------------------------------------------------- 1.6 helpers: pressure, groups, fairness, pinning
def pressure_block(cap, io_since=None, now=None):
    """Reason not to start anything now (kernel pressure stall information), or ''. Only sustained pressure counts
    (10-second and 1-minute averages). Disk pressure caused by your own runs can last for hours: after
    QUEUE_IO_PATIENCE_MIN of it, jobs start again (slowly, see QUEUE_START_DELAY)."""
    mf = float(config.num("QUEUE_PSI_MEM_FULL"))
    io = float(config.num("QUEUE_PSI_IO_SOME"))
    if mf > 0 and cap.get("psi_mem_full", 0) >= mf and cap.get("psi_mem_full60", mf) >= mf / 2:
        return f"waiting: the machine is short of memory (stalled {cap['psi_mem_full']:.0f}% of the time)"
    if io > 0 and cap.get("psi_io_some60", cap.get("psi_io_some", 0)) >= io and cap.get("psi_io_some", 0) >= io / 2:
        pat = float(config.num("QUEUE_IO_PATIENCE_MIN")) * 60
        if not (pat and io_since and (now or time.time()) - io_since >= pat):
            return f"waiting: the disks are overloaded (I/O stalls {cap['psi_io_some60']:.0f}% of the time)"
    return ""


def groups():
    """config/groups.conf: 'NAME LIMIT' per line — at most LIMIT jobs of that group run at once (pueue groups)."""
    out = {}
    for line in config.lst("groups.conf"):
        parts = line.split()
        if len(parts) >= 2:
            try:
                out[parts[0]] = max(0, int(parts[1]))
            except ValueError:
                pass
    return out


def _wait_start(j):
    return max(j.get("created") or 0, j.get("not_before") or 0, j.get("begin") or 0, j.get("released_at") or 0)


_DUR = {}


def _durations():
    """Index of successful run durations by (name, folder) and (flow, step); rebuilt at most once a minute."""
    if time.time() - _DUR.get("t", 0) < 60:
        return _DUR
    by_name, by_step = {}, {}
    for x in all_jobs():
        if x["state"] not in ("done", "warning") or not x.get("started") or not x.get("ended"):
            continue
        d = x["ended"] - x["started"]
        by_name.setdefault((x.get("name"), x.get("cwd")), []).append((x["id"], d))
        fx = x.get("flow") or {}
        if fx:
            by_step.setdefault((fx.get("name"), fx.get("step")), []).append((x["id"], d))
    _DUR.update(t=time.time(), name=by_name, step=by_step)
    return _DUR


def usual_seconds(j):
    """Typical duration of this kind of job: median of earlier successful runs with the same name and folder,
    else of the same flow step, else the tool's measured profile (when consistent); 0 when unknown."""
    idx = _durations()
    fj = j.get("flow") or {}
    for lst_ in (idx["name"].get((j.get("name"), j.get("cwd")), []),
                 idx["step"].get((fj.get("name"), fj.get("step")), []) if fj else []):
        ds = sorted(d for i, d in lst_ if i != j.get("id"))
        if len(ds) >= 2:
            return ds[len(ds) // 2]
    m = capacity.typical_minutes(capacity.profiles().get(j.get("tool") or "", {}))
    return float(m) * 60 if m else 0


def pick_cpus(need_t, running):
    """Whole physical cores not used by other pinned jobs (hyper-thread siblings together)."""
    used = {c for r in running for c in (r.get("cpus") or [])}
    want = max(1, int(round(need_t)))
    got = []
    for core in probes.cpu_siblings():
        if any(c in used for c in core):
            continue
        got += core
        if len(got) >= want:
            break
    if len(got) < want:                                # not enough whole cores: take single free threads
        got += [c for core in probes.cpu_siblings() for c in core if c not in used and c not in got][:want - len(got)]
    if len(got) < want:
        return None                                    # never lock a job onto fewer threads than it needs
    return sorted(got[:want])


def explain(jid):
    """HTCondor-style analysis: every condition for starting job `jid`, as [(ok, check, detail)]."""
    from . import resources
    j = load(jid)
    if not j:
        return [(False, "job", f"no job {jid}")]
    now = time.time()
    jobs = all_jobs()
    byid = {x["id"]: x for x in jobs}
    sch = Scheduler()
    cap = sch.capacity(jobs)
    out = [(j["state"] in ("queued",), "state", j["state"] + (f" — {j['reason']}" if j.get("reason") else ""))]
    if j.get("remote"):
        out.append((True, "runs on", f"{j['remote']}" + (f" (Slurm job {j['remote_job']})" if j.get("remote_job") else "")
                    + " — local CPU and RAM do not matter"))
    elif j.get("remote_ok"):
        out.append((True, "may run on a server", "when this machine stays busy (offload in config/remotes.conf)"))
    if not j.get("remote") and j["state"] == "queued":
        try:
            from . import remote as RM
            for n, r in RM.remotes().items():
                if r["offload"] == "prefer":
                    wn = RM.why_not(j, r)
                    out.append((True, f"server first ({n})", "allowed: it goes there when the server has room"
                                if not wn else f"not for this job: {wn}"))
        except Exception:
            pass
    if j["state"] in TERMINAL | ACTIVE:
        return out
    out.append(((j.get("begin") or 0) <= now and (j.get("not_before") or 0) <= now, "start time",
                "now" if max(j.get("begin") or 0, j.get("not_before") or 0) <= now else
                time.strftime("not before %F %T", time.localtime(max(j.get("begin") or 0, j.get("not_before") or 0)))))
    if j.get("depends"):
        st = {d: (byid.get(d) or {}).get("state", "missing") for d in j["depends"]}
        out.append((sch.deps_ready(j, byid), "dependencies", ", ".join(f"#{d} {v}" for d, v in st.items())))
    if j.get("require"):
        ok, why = check_require(j["require"], j["cwd"])
        out.append((ok, "preconditions", why or "all met"))
    if j.get("when_idle"):
        out.append((sch.idle(), "idle machine", "idle" if sch.idle() else "not idle (load or tracked jobs)"))
    g = j.get("group")
    if g:
        lim = groups().get(g)
        n = len([x for x in jobs if x.get("group") == g and x["state"] in ACTIVE])
        out.append((lim is None or n < lim, "group " + g, f"{n} running, limit {lim if lim is not None else 'none'}"))
    ok, why = resources.fits(j, jobs, resources.load())
    out.append((ok, "resources / licenses / tool caps", why or "available"))
    pb = pressure_block(cap)
    if pb and "disks" in pb and float(config.num("QUEUE_IO_PATIENCE_MIN")):
        pb += f" (after {config.num('QUEUE_IO_PATIENCE_MIN'):.0f} min of this, jobs start one every 5 minutes)"
    out.append((not pb, "system pressure", pb or f"memory stalls {cap['psi_mem_full']:.0f}%, I/O stalls {cap['psi_io_some']:.0f}%"))
    out.append((not resources.swapping(), "swapping", "swapping heavily" if resources.swapping() else "no"))
    out.append((not cap["no_new"], "reserved time window", "a reservation blocks new jobs" if cap["no_new"] else "none active"))
    running = len([x for x in jobs if x["state"] == "running"])
    out.append((running < cap["max_slots"], "job slots", f"{running} of {int(cap['max_slots'])} used"))
    need_t = min(max(1.0, float(j["cores"])), cap["threads"] * 0.75)
    need_m = float(j["mem_gb"]) * GiB
    out.append((cap["free_threads"] >= need_t, "CPU", f"needs {need_t:.1f} threads, {max(0, cap['free_threads']):.1f} free"
                + (f" (at most {j['max_free_t']:.0f} free while it waited)" if j.get("max_free_t") is not None else "")))
    pat = float(config.num("QUEUE_CPU_PATIENCE_MIN") or 0)
    if pat and cap["free_threads"] < need_t:
        w = (now - _wait_start(j)) / 60
        out.append((True, "CPU patience", f"after {pat:.0f} min of waiting it starts once {max(1.0, need_t * 0.5):.0f} threads "
                                          f"are free and its RAM fits (waited {w:.0f} min)"))
    out.append((cap["free_mem"] >= need_m, "RAM", f"needs {need_m / GiB:.1f} GB, {max(0, cap['free_mem']) / GiB:.1f} GB free "
                                                 f"(after the {cap['reserve'] / GiB:.0f} GB safety reserve and what running jobs may still grow)"))
    if need_m > cap["total"] - cap["reserve"]:
        out.append((False, "suggestion", "the memory request is larger than this machine can ever give — lower --mem"))
    elif cap["free_mem"] < need_m:
        out.append((True, "suggestion", "it starts when running jobs finish; ms q edit ID --mem N lowers the request "
                                         "if the job really needs less"))
    return out


def edit(jid, **kw):
    """Change a job that has not started yet (pueue edit). Returns (ok, message)."""
    allowed = {"cmd", "name", "mem_gb", "cores", "priority", "timeout_s", "cwd", "env", "group", "labels", "retries", "nice"}
    bad = set(kw) - allowed
    if bad:
        return False, "cannot change: " + ", ".join(sorted(bad))
    with _Lock():
        j = load(jid)
        if not j:
            return False, f"no job {jid}"
        if j["state"] not in ("queued", "held", "approval"):
            return False, f"job {jid} is {j['state']} — only waiting jobs can be edited (use ms q clone)"
        if "env" in kw:
            kw["env"] = dict(j.get("env") or {}, **kw["env"])
        if "timeout_s" in kw:
            kw["timeout_s"] = resolve_timeout(kw["timeout_s"], capacity.profiles().get(j.get("tool") or "", {}))
        for k in ("mem_gb", "cores"):
            if k in kw:
                kw[k] = float(kw[k])
        if "priority" in kw:
            kw["priority"] = int(kw["priority"])
        j.update(kw)
        j["edited"] = time.time()
        save(j)
    return True, f"job {jid} updated: " + ", ".join(sorted(kw))


def in_group(g):
    return [x["id"] for x in all_jobs() if x.get("group") == g]


def mark_flaky(j):
    """A job that failed and then passed on retry without any change is flaky (Jenkins/Buildbot)."""
    f = config.STATE / "flaky.json"
    try:
        d = json.loads(f.read_text())
    except (OSError, ValueError):
        d = {}
    fl = j.get("flow") or {}
    key = f"{fl.get('name')}:{fl.get('step')}" if fl else f"{j['name']}@{short(j['cwd'])}"
    e = d.setdefault(key, {"count": 0, "jobs": []})
    e["count"] += 1
    e["last"] = time.time()
    e["jobs"] = (e["jobs"] + [j["id"]])[-20:]
    try:
        f.write_text(json.dumps(d, indent=1))
    except OSError:
        pass
    return key, e["count"]


def flaky():
    try:
        return json.loads((config.STATE / "flaky.json").read_text())
    except (OSError, ValueError):
        return {}


class Scheduler:
    def __init__(self):
        self.last_action = 0
        self.last_start = 0
        self.delay = None
        self.told = set()
        self.last_retention = 0

    def notify(self, title, msg, prio="default", tag="computer"):
        try:
            subprocess.Popen([str(config.MS / "bin" / "notify.sh"), title, msg[:400], prio, tag],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass

    def capacity(self, jobs):
        from . import resources
        th = probes.threads()
        m = probes.meminfo()
        total, avail = m.get("MemTotal", 0), m.get("MemAvailable", 0)
        res = capacity.reserve_bytes(total)
        l1 = probes.loadavg()[0]
        now = time.time()
        grow = 0
        recent_cores = 0.0
        settle = float(config.num("QUEUE_MEM_SETTLE_MIN") or 20) * 60
        for j in jobs:
            if j["state"] == "running":
                rss = j.get("rss") or 0
                want = float(j["mem_gb"]) * GiB
                # after the job has settled, expect it to grow only a little above its peak so far (Slurm --mem-update)
                if now - (j.get("started") or now) > settle and j.get("peak_rss"):
                    want = min(want, j["peak_rss"] * 1.15)
                grow += max(0, want - rss)
                if now - (j.get("started") or now) < 90:
                    recent_cores += float(j["cores"])
        keep = float(config.num("QUEUE_KEEP_THREADS") or 0)
        r = resources.active_reservation()
        if r and r.get("keep_threads") is not None:
            keep = max(keep, r["keep_threads"])
        max_slots = config.num("QUEUE_MAX_JOBS") or th
        ps = probes.psi()
        return {"psi_mem_full": ps.get("memory", {}).get("full", 0), "psi_io_some": ps.get("io", {}).get("some", 0),
                "psi_mem_full60": ps.get("memory", {}).get("full60", 0), "psi_io_some60": ps.get("io", {}).get("some60", 0),
                "threads": th, "free_threads": th - keep - l1 - recent_cores, "free_mem": avail - res - grow,
                "avail": avail, "reserve": res, "max_slots": max_slots, "total": total,
                "no_new": bool(r and r.get("no_new_jobs")), "reservation": r}

    def waiting_advice(self, jobs):
        """Tell once when ready jobs have waited long (QUEUE_STUCK_NOTIFY_H) for CPU/RAM, and what would let them
        start. Jobs that wait on purpose (dependencies, start time, groups, licenses, idle machine) are left out.
        All of them in one message."""
        hours = float(config.num("QUEUE_STUCK_NOTIFY_H") or 0)
        if not hours:
            return
        now = time.time()
        byid = {j["id"]: j for j in jobs}
        tips = []
        for j in jobs:
            r = j.get("reason") or ""
            if (j["state"] != "queued" or j.get("waiting_told") or now - _wait_start(j) < hours * 3600
                    or (j.get("not_before") or 0) > now or (j.get("begin") or 0) > now or not self.deps_ready(j, byid)
                    or not (r.startswith("waiting for RAM") or r.startswith("waiting for CPU")
                            or "short of memory" in r or "disks are overloaded" in r or "keeping room" in r)):
                continue
            h = int((now - _wait_start(j)) / 3600)
            if "CPU" in r and j.get("max_free_t") is not None:
                tip = (f"#{j['id']} {j['name']} ({h} h): needs {r.split('GB, ')[-1].split(' cores')[0]} free cores, at most "
                       f"{j['max_free_t']:.0f} were free. Sooner: ms q edit {j['id']} --cores {max(1, int(j['max_free_t']))}")
            elif "RAM" in r and j.get("max_free_gb") is not None:
                tip = (f"#{j['id']} {j['name']} ({h} h): needs {j['mem_gb']} GB, at most {j['max_free_gb']:.0f} GB were free. "
                       f"If it needs less: ms q edit {j['id']} --mem N")
            else:
                tip = f"#{j['id']} {j['name']} ({h} h): {r} — ms q explain {j['id']}"
            update(j["id"], waiting_told=True)
            tips.append(tip)
        if tips:
            more = f"\n+ {len(tips) - 3} more (ms q)" if len(tips) > 3 else ""
            self.notify(f"{len(tips)} job{'s' if len(tips) > 1 else ''} waiting a long time",
                        "\n".join(tips[:3]) + more + "\nThey start by themselves when CPU/RAM frees up.", "default", "hourglass")

    def idle(self):
        from . import watcher
        return probes.loadavg()[0] < probes.threads() * 0.2 and not watcher.jobs()

    # ---------------------------------------------------------------- retries
    def handle_failures(self, jobs, now):
        for j in jobs:
            if j["state"] not in ("failed", "timeout") or j.get("retried"):
                continue
            if j["state"] == "failed" and j.get("remote_auto") and not j.get("fell_back") and j.get("remote"):
                # sent to a server automatically and failed there (missing program, file outside the folder,
                # upload or sbatch problem...): run it here once before calling it a failure
                srv = j["remote"]
                update(j["id"], state="queued", attempt=j["attempt"] + 1, started=None, ended=None, rc=None,
                       remote=None, remote_auto=False, remote_never=True, fell_back=True, remote_job=None,
                       remote_dir=None, remote_state=None, remote_node=None, cpus=None, waiting_told=False,
                       start_note=f"failed on {srv} (exit {j.get('rc')}: {j.get('reason') or 'failed'}); running it here instead",
                       reason=f"failed on {srv}; running it here instead")
                continue
            why = "oom" if j.get("oom") else "timeout" if j["state"] == "timeout" else "fail"
            on = j.get("retry_on") or ["oom"]
            codes = j.get("retry_exit_codes")
            code_ok = codes is None or j.get("rc") in [int(c) for c in codes]
            if j["attempt"] <= int(j.get("retries") or 0) and (why in on or "any" in on) and code_ok:
                mem, to = float(j["mem_gb"]), j.get("timeout_s")
                scale = j.get("retry_scale") or {}
                if why == "oom":
                    peak = (j.get("peak_rss") or 0) / GiB
                    mem = max(mem * float(scale.get("mem", 1.5)), peak * 1.3)
                if why == "timeout" and to:
                    to = int(to * float(scale.get("time", 1.5)))
                sched = j.get("retry_delays")
                if sched:
                    delay = float(parse_duration(sched[min(j["attempt"] - 1, len(sched) - 1)]))
                else:
                    base = float(parse_duration(j.get("retry_delay") or 30))
                    delay = min(base * float(j.get("retry_backoff") or 1) ** (j["attempt"] - 1),
                                float(parse_duration(j.get("retry_max_delay") or 3600)))
                update(j["id"], state="queued", attempt=j["attempt"] + 1, mem_gb=round(mem, 1), timeout_s=to,
                       started=None, ended=None, rc=None, not_before=now + delay, oom=False, prev_fail=why,
                       cpus=None, waiting_told=False, max_free_t=None, max_free_gb=None, start_note=None,
                       reason=f"retry {j['attempt']}/{j['retries']} in {int(delay)} s after {why}"
                              + (f", memory {mem:.1f} GB" if why == "oom" else ""))
                if self.delay is not None and now - (j.get("started") or now) < 120:
                    self.delay = min(600.0, max(self.delay, 1.0) * 1.3)
                continue
            update(j["id"], retried=True)
            if not j.get("flow") and j["id"] not in self.told and j.get("notify", "failure") != "never":
                self.told.add(j["id"])
                self.notify(f"Job failed: {j['name']}", f"#{j['id']} · {j['reason']} · {short(j['cwd'])}", "high", "x")
        for j in jobs:          # successful single jobs: notify only when asked
            if j["state"] in ("done", "warning") and not j.get("flow") and j.get("notify") == "always" \
                    and f"ok{j['id']}" not in self.told:
                self.told.add(f"ok{j['id']}")
                self.notify(f"Job {j['state']}: {j['name']}", f"#{j['id']} · {short(j['cwd'])} {j.get('reason', '')}",
                            "default", "white_check_mark")

    # ---------------------------------------------------------------- dependencies and deadlines
    def resolve_deps(self, jobs, byid, now):
        for j in jobs:
            if j["state"] != "queued":
                continue
            if j.get("deadline") and now > j["deadline"]:
                update(j["id"], state="skipped", reason="deadline passed before it could start", ended=now)
                continue
            if j.get("deadline"):
                m = capacity.typical_minutes(capacity.profiles().get(j.get("tool") or "", {}))
                if m and now + m * 60 > j["deadline"]:
                    update(j["id"], state="skipped", reason=f"cannot finish before the deadline (usually {m} min)", ended=now)
                    continue
            if not j.get("depends"):
                continue
            deps = [byid.get(d) for d in j["depends"]]
            if any(d is None for d in deps):
                update(j["id"], state="skipped", reason="dependency missing", ended=now)
                continue
            mode = j.get("dep_mode", "ok")
            if mode == "ok" and any(d["state"] in TERMINAL - SUCCESS for d in deps):
                bad = next(d for d in deps if d["state"] in TERMINAL - SUCCESS)
                if (bad.get("flow") or {}).get("ignore_failure"):
                    continue
                update(j["id"], state="skipped", reason=f"dependency #{bad['id']} {bad['state']}", ended=now,
                       cond_skip=bool(bad.get("cond_skip")))
            elif mode == "notok" and all(d["state"] in TERMINAL for d in deps) and not any(d["state"] in TERMINAL - SUCCESS for d in deps):
                update(j["id"], state="skipped", reason="dependencies succeeded (this job runs only after a failure)", ended=now)

    def deps_ready(self, j, byid):
        if not j.get("depends"):
            return True
        mode = j.get("dep_mode", "ok")
        st = [byid.get(d, {}).get("state") for d in j["depends"]]
        if mode == "any":
            return all(x in TERMINAL for x in st)
        if mode == "notok":
            return all(x in TERMINAL for x in st) and any(x in TERMINAL - SUCCESS for x in st)
        ign = [(byid.get(d, {}).get("flow") or {}).get("ignore_failure") for d in j["depends"]]
        return all(x in SUCCESS or (g and x in TERMINAL) for x, g in zip(st, ign))

    # ---------------------------------------------------------------- memory pressure and preemption
    def pressure(self, jobs, cap, now):
        running = [j for j in jobs if j["state"] == "running"]
        exempt = float(config.num("QUEUE_PREEMPT_EXEMPT_MIN") or 0) * 60
        lim = float(config.num("QUEUE_PSI_MEM_FULL"))
        thrash = lim > 0 and cap.get("psi_mem_full", 0) >= 2 * lim and cap.get("psi_mem_full60", 0) >= lim
        if (cap["avail"] < cap["reserve"] * 0.6 or thrash) and running and now - self.last_action > 30:
            cands = [j for j in running if now - (j.get("started") or now) >= exempt] or running
            victim = sorted(cands, key=lambda j: (int(j.get("priority") or 0), -(j.get("started") or 0)))[0]
            if len(running) > 1 or cap["avail"] < cap["reserve"] * 0.3:
                signal_job(victim, signal.SIGSTOP)
                update(victim["id"], state="suspended", suspended_at=now,
                       reason="paused: RAM almost full" + (" (the machine was thrashing)" if thrash else ""))
                told = self.__dict__.setdefault("_paused_told", {})
                if now - told.get(victim["id"], 0) > 1800:
                    told[victim["id"]] = now
                    self.notify("Job paused (RAM low)", f"#{victim['id']} {victim['name']} paused to protect the machine; "
                                                        "it continues when memory frees up.", "default", "pause_button")
                self.last_action = now
                return True
        susp = sorted([j for j in jobs if j["state"] == "suspended" and j.get("reason", "").startswith("paused: ")
                       or (j["state"] == "suspended" and "higher-priority" in j.get("reason", ""))],
                      key=lambda j: (-int(j.get("priority") or 0), j.get("started") or 0))
        if susp and now - self.last_action > 30:
            s = susp[0]
            if s.get("reason", "").startswith("paused: RAM"):
                ok = cap["avail"] - cap["reserve"] > (s.get("rss") or 0) * 0.2 + cap["reserve"] * 0.5
                lim = float(config.num("QUEUE_PSI_MEM_FULL"))
                if "thrashing" in s.get("reason", "") and lim > 0:
                    ok = ok and cap.get("psi_mem_full60", 0) < lim / 2 and cap.get("psi_mem_full", 0) < lim / 2
            else:
                ok = cap["free_threads"] >= float(s["cores"]) and not any(
                    x["state"] == "queued" and x.get("preempt") and int(x.get("priority") or 0) > int(s.get("priority") or 0)
                    for x in jobs)
            if ok:
                signal_job(s, signal.SIGCONT)
                update(s["id"], state="running", reason="resumed")
                self.last_action = now
                return True
        return False

    # ---------------------------------------------------------------- start what fits
    def start_jobs(self, jobs, byid, cap, now):
        from . import flow, pause, resources
        running = [j for j in jobs if j["state"] in ACTIVE]
        if pause.paused("queue"):            # ms pause queue: running jobs continue, nothing new starts
            msg = f"waiting: the queue is paused {pause.describe(pause.until('queue'))} (ms resume queue)"
            for j in jobs:
                if j["state"] == "queued" and j.get("reason") != msg:
                    update(j["id"], reason=msg)
            return
        if cap["no_new"]:
            for j in jobs:
                if j["state"] == "queued" and j.get("reason") != "waiting: reserved time window":
                    update(j["id"], reason="waiting: reserved time window")
            return
        if resources.swapping():
            return
        if config.num("QUEUE_DISK_BUSY_PCT"):
            b = resources.disk_busy(config.get("BIG_FOLDER_DEST") or "/")
            if b is not None and b > config.num("QUEUE_DISK_BUSY_PCT"):
                return
        io_hi = cap.get("psi_io_some60", 0) >= float(config.num("QUEUE_PSI_IO_SOME") or 101)
        self.io_since = (getattr(self, "io_since", None) or now) if io_hi else None
        why_p = pressure_block(cap, self.io_since, now)
        base_delay = float(config.num("QUEUE_START_DELAY") or 0)
        if self.delay is None:
            self.delay = base_delay
        if self.delay and now - self.last_start < self.delay:
            return
        ready = [j for j in jobs if j["state"] == "queued" and (j.get("not_before") or 0) <= now
                 and (j.get("begin") or 0) <= now and self.deps_ready(j, byid)]
        ready.sort(key=lambda j: (-int(j.get("order") or 0), -int(j.get("priority") or 0), j["id"]))
        slots = cap["max_slots"] - len([j for j in running if j["state"] == "running"])
        free_t, free_m = cap["free_threads"], cap["free_mem"]
        res_def = resources.load()
        next_res = resources.minutes_to_next_reservation(now)
        disk_reserved = {}
        for r in running:
            if r.get("disk_gb"):
                disk_reserved[r.get("disk_path") or r["cwd"]] = disk_reserved.get(r.get("disk_path") or r["cwd"], 0) + float(r["disk_gb"])
        gpus = None
        glim = groups()
        gcount = {}
        for r in running:
            if r.get("group"):
                gcount[r["group"]] = gcount.get(r["group"], 0) + 1
        try:
            from . import remote as RM
            rms = RM.remotes()
        except Exception:
            rms = {}
        protect = None                    # (job, need_t, need_m): room kept free for a job that waited too long
        starve = float(config.num("QUEUE_STARVE_MIN") or 0) * 60
        short = float(config.num("QUEUE_BACKFILL_MIN") or 0) * 60
        for j in ready:
            if slots <= 0:
                break
            g = j.get("group")
            if g and g in glim and gcount.get(g, 0) >= glim[g]:
                msg = f"waiting: group {g} already runs {glim[g]} job(s)"
                if j.get("reason") != msg:
                    update(j["id"], reason=msg)
                continue
            # late binding: placeholders from earlier steps, conditions, fan-out, sub-flows, shared cache
            act = flow.before_start(self, j, byid)
            if act != "go":
                continue
            j = load(j["id"])
            if (j.get("inputs") or j.get("outputs")) and j.get("incremental", True) and not j.get("force") and up_to_date(j):
                update(j["id"], state="cached", reason="up to date (same command, parameters and inputs)", ended=now)
                continue
            if (not j.get("remote") and rms and not pause.paused("remote")
                    and any(r["offload"] == "prefer" for r in rms.values())):
                from . import remote as RM     # offload = prefer: the server first, this machine as fallback
                target, note = RM.prefer_target(j, all_jobs())
                if target:
                    update(j["id"], remote=target, remote_auto=True, start_note=note)
                    j = load(j["id"])
                elif note and j.get("prefer_note") != f"not sent: {note}":
                    update(j["id"], prefer_note=f"not sent: {note}")
            if j.get("remote") and pause.paused("remote"):
                msg = "waiting: sending jobs to servers is paused (ms resume remote)"
                if j.get("reason") != msg:
                    update(j["id"], reason=msg)
                continue
            if j.get("remote"):                # runs on a server: local CPU/RAM/pressure do not matter
                from . import remote as RM
                try:
                    rmt = RM.get(j["remote"])
                except KeyError as e:
                    update(j["id"], state="failed", ended=now, reason=str(e).strip("'\""))
                    continue
                ok, why = check_require(j.get("require"), j["cwd"])
                if not ok:
                    if j.get("reason") != f"waiting: {why}":
                        update(j["id"], reason=f"waiting: {why}")
                    continue
                if RM.running_here(j["remote"], all_jobs()) >= int(rmt["max_jobs"]):
                    msg = f"waiting: {rmt['max_jobs']} job(s) already on {j['remote']}"
                    if j.get("reason") != msg:
                        update(j["id"], reason=msg)
                    continue
                _launch(load(j["id"]))
                self.last_start = now
                continue
            if not why_p and self.io_since and now - self.last_start < 300:
                why_p = "waiting: the disks are still overloaded — starting one job every 5 minutes"
            if why_p:                      # under pressure: only work that needs no CPU/RAM gets through (above)
                if j.get("reason") != why_p:
                    update(j["id"], reason=why_p)
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
            ok, why = resources.fits(j, jobs + [x for x in running if x not in jobs], res_def)
            if not ok:
                if j.get("reason") != why:
                    update(j["id"], reason=why)
                continue
            if next_res is not None and j.get("respect_reservations", True):
                m = capacity.typical_minutes(capacity.profiles().get(j.get("tool") or "", {}))
                if m and m > next_res:
                    msg = f"waiting: would run into a reserved window in {int(next_res)} min"
                    if j.get("reason") != msg:
                        update(j["id"], reason=msg)
                    continue
            if j.get("disk_gb"):
                path = j.get("disk_path") or j["cwd"]
                d = probes.disk(path) or probes.disk(os.path.dirname(path)) or probes.disk("/")
                if d and d[1] - disk_reserved.get(path, 0) * GiB < float(j["disk_gb"]) * GiB:
                    update(j["id"], reason=f"waiting for {j['disk_gb']} GB free disk on {path}")
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
            # CPU is shared gracefully (jobs run niced, just slower); RAM is not. A job that has waited long enough
            # for cores while its memory is free starts once half of its cores are free.
            patience = float(config.num("QUEUE_CPU_PATIENCE_MIN") or 0) * 60
            waited = now - _wait_start(j)
            patient = (patience and free_m >= need_m and free_t < need_t and waited >= patience
                       and free_t >= max(1.0, need_t * 0.5))
            if protect and (free_t >= need_t or patient) and free_m >= need_m:
                pj, pt, pm = protect
                fits_beside = free_t - need_t >= pt and free_m - need_m >= pm
                if not fits_beside and not (short and 0 < usual_seconds(j) <= short):
                    msg = f"waiting: keeping room for #{pj['id']} (waiting {int((now - _wait_start(pj)) / 60)} min)"
                    if j.get("reason") != msg:
                        update(j["id"], reason=msg)
                    continue
            if (free_t >= need_t or patient) and free_m >= need_m:
                upd = {"slot": resources.free_slot(all_jobs())}
                if patient:
                    upd["start_note"] = (f"started with {free_t:.0f} of {need_t:.0f} cores free after waiting "
                                         f"{waited / 60:.0f} min (its RAM was free; it runs niced, only slower)")
                if config.num("QUEUE_PIN_CPUS"):
                    upd["cpus"] = pick_cpus(need_t, running)
                update(j["id"], **upd)
                _launch(load(j["id"]), gpu_ids)
                running.append(load(j["id"]))
                slots -= 1
                free_t -= need_t
                free_m -= need_m
                if g:
                    gcount[g] = gcount.get(g, 0) + 1
                if j.get("disk_gb"):
                    disk_reserved[j.get("disk_path") or j["cwd"]] = disk_reserved.get(j.get("disk_path") or j["cwd"], 0) + float(j["disk_gb"])
                self.last_start = now
                if self.delay:
                    return            # one start per delay period
                continue
            why = "RAM" if free_m < need_m else "CPU"
            msg = f"waiting for {why} ({need_m / GiB:.1f} GB, {need_t:.1f} cores needed)"
            if j.get("reason") != msg:
                update(j["id"], reason=msg)
            if rms and not pause.paused("remote"):   # busy here: may it run on a remote server instead?
                from . import remote as RM
                target = RM.offload_target(j, all_jobs(), waited, cap)
                if target:
                    update(j["id"], remote=target, remote_auto=True,
                           start_note=f"sent to {target}: this machine had no room ({why}) after {waited / 60:.0f} min")
                    _launch(load(j["id"]))
                    self.last_start = now
                    continue
            key, val = ("max_free_t", free_t) if why == "CPU" else ("max_free_gb", free_m / GiB)
            if val > float(j.get(key) if j.get(key) is not None else -1) + 0.5:
                update(j["id"], **{key: round(max(0.0, val), 1)})
            # keep room only where it helps: when the missing CPU/RAM is held by other queue jobs that will finish.
            # Missing cores used by programs outside the queue would block the whole queue for nothing.
            q_run = [r for r in running if r["state"] == "running"]
            q_t = sum(float(r.get("cores") or 0) for r in q_run)
            q_m = sum(float(r.get("mem_gb") or 0) * GiB for r in q_run)
            helps = (q_m >= need_m - free_m) if why == "RAM" else (q_t >= need_t - free_t)
            if protect is None and starve and waited >= starve and helps:
                protect = (j, need_t, need_m)
            if j.get("preempt") and why == "CPU" and now - self.last_action > 30:
                exempt = float(config.num("QUEUE_PREEMPT_EXEMPT_MIN") or 0) * 60
                low = sorted([r for r in running if r["state"] == "running" and int(r.get("priority") or 0) < int(j.get("priority") or 0)
                              and now - (r.get("started") or now) >= exempt],
                             key=lambda r: (int(r.get("priority") or 0), -(r.get("started") or 0)))
                if low:
                    v = low[0]
                    signal_job(v, signal.SIGSTOP)
                    update(v["id"], state="suspended", reason=f"paused for higher-priority job #{j['id']}")
                    self.last_action = now
                    return

    def tick(self):
        while True:                       # reap finished runners (they are our children)
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
        now = time.time()
        for j in all_jobs():              # runners that died without writing a result
            if j["state"] in ACTIVE and j.get("pid") and not _alive(j["pid"]):
                time.sleep(0.3)
                k = load(j["id"])
                if k and k["state"] in ACTIVE:
                    update(j["id"], state="failed", ended=now, reason="runner stopped unexpectedly")
            elif j["state"] == "cancelled" and not j.get("ended") and j.get("pid") and not _alive(j["pid"]):
                update(j["id"], ended=now)
        jobs = all_jobs()
        if self.delay and jobs:           # successes slowly shrink an automatically raised start delay
            base = float(config.num("QUEUE_START_DELAY") or 0)
            if any(j["state"] == "done" and now - (j.get("ended") or 0) < 10 for j in jobs):
                self.delay = max(base, self.delay * 0.9)
        self.handle_failures(jobs, now)
        jobs = all_jobs()
        from . import flow
        flow.react(self, jobs)            # on_error strategies, halt rules, temp cleanup after consumers
        jobs = all_jobs()
        byid = {j["id"]: j for j in jobs}
        self.resolve_deps(jobs, byid, now)
        jobs = all_jobs()
        byid = {j["id"]: j for j in jobs}
        cap = self.capacity(jobs)
        if not self.pressure(jobs, cap, now):
            self.start_jobs(jobs, byid, cap, now)
        check_stuck = time.time() - getattr(self, "_stuck_t", 0) >= 60
        if check_stuck:
            self._stuck_t = time.time()
            self.waiting_advice(jobs)
        for j in jobs:                    # stuck detection when no explicit timeout
            if j["state"] != "running" or not check_stuck:
                continue
            if f"stuck{j['id']}" in self.told or j.get("timeout_s"):
                continue
            u = usual_seconds(j)
            if u and runtime(j) > max(1800, 3 * u):
                self.told.add(f"stuck{j['id']}")
                self.notify(f"Job may be stuck: {j['name']}", f"#{j['id']} has run {runtime(j) / 3600:.1f} h; "
                                                               f"jobs like it usually take {u / 60:.0f} min.", "default", "hourglass")
        flow.finish_flows(self)
        if now - self.last_retention > 3600:
            self.last_retention = now
            days = config.num("QUEUE_KEEP_DAYS") or 30
            clear(days)

    def loop(self):
        _ensure()
        last_sched = 0

        def stopping(signum, frame):     # a clean shutdown/logout stops this service: remember it (crash detection)
            try:
                (config.STATE / "clean_stop.json").write_text(json.dumps({"t": time.time(), "boot_id": probes.boot_id()}))
            except OSError:
                pass
            sys.exit(0)
        signal.signal(signal.SIGTERM, stopping)
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
def cancel(jid, reason="cancelled by user"):
    j = load(jid)
    if not j or j["state"] in TERMINAL:
        return False
    if j["state"] in ACTIVE:
        update(jid, state="cancelled", reason=reason, cancel_reason=reason)
        signal_job(j, signal.SIGCONT)
        signal_job(j, signal.SIGTERM)
    else:
        update(jid, state="cancelled", reason=reason, ended=time.time())
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
        update(jid, state="queued", reason="", released_at=time.time())
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


def clone(jid, **changes):
    """Run a job again as a new job with the same settings (evQueue relaunch / Jenkins replay)."""
    j = load(jid)
    if not j:
        return None
    keep = {k: v for k, v in j.items() if k not in ("id", "state", "created", "attempt", "started", "ended", "rc", "reason",
                                                    "pid", "sid", "peak_rss", "rss", "cpu_s", "run_id", "outputs", "retried",
                                                    "oom", "cores_used", "issues", "diagnosis", "slot", "gpu_ids", "cmd", "cwd",
                                                    "name", "depends", "last_line", "progress", "junit", "io_read", "io_write")}
    keep.update(changes)
    return submit(changes.get("cmd", j["cmd"]), cwd=j["cwd"], name=j["name"], **keep)


def approve(jid, ok=True):
    j = load(jid)
    if j and j["state"] == "approval":
        if ok:
            update(jid, state="queued", reason="approved", approved_at=time.time())
        else:
            update(jid, state="cancelled", reason="rejected", ended=time.time())
        return True
    return False


def top(jid):
    """Move a waiting job to the front of the queue (task-spooler -u, scontrol top)."""
    mx = max([int(j.get("order") or 0) for j in all_jobs()] + [0])
    return update(jid, order=mx + 1) is not None


def requeue(jid):
    j = load(jid)
    if not j:
        return False
    if j["state"] in ACTIVE:
        cancel(jid)
        for _ in range(60):
            if load(jid)["state"] in TERMINAL:
                break
            time.sleep(1)
    update(jid, state="queued", attempt=load(jid)["attempt"] + 1, started=None, ended=None, rc=None,
           reason="requeued by user", retried=False, oom=False)
    return True


def wait(jid, timeout=None):
    t0 = time.time()
    while True:
        j = load(jid)
        if not j:
            return None
        if j["state"] in TERMINAL:
            return j
        if timeout and time.time() - t0 > timeout:
            return j
        time.sleep(2)


def history(days=7, state=None, name=None):
    out = []
    for j in all_jobs():
        t = j.get("ended") or j.get("started") or j["created"]
        if time.time() - t > days * 86400:
            continue
        if state and j["state"] != state:
            continue
        if name and not re.search(name, j["name"]):
            continue
        out.append(j)
    return out


def set_priority(jid, p):
    return update(jid, priority=int(p)) is not None


def clear(older_than_days=0):
    n = 0
    for j in all_jobs():
        if j["state"] in TERMINAL and time.time() - (j.get("ended") or j["created"]) >= older_than_days * 86400:
            if (j.get("flow") or {}).get("id") and older_than_days == 0 and time.time() - (j.get("ended") or 0) < 60:
                continue
            _path(j["id"]).unlink(missing_ok=True)
            for p in (LOGS / f"{j['id']}.log", LOGS / f"{j['id']}.log.1", Q / "series" / f"{j['id']}.csv",
                      Q / "outputs" / f"{j['id']}.env"):
                p.unlink(missing_ok=True)
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
