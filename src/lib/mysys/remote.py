"""Run queue jobs on a remote Slurm cluster (e.g. Titan at UGent CSL) — by request or automatically when this
machine is busy or too small for the job.

config/remotes.conf (INI, one section per server):

  [titan]
  type = slurm                 # slurm (sbatch on a head node)
  ssh = titan                  # Host from ~/.ssh/config (key login, no password prompts)
  head = bacchus               # how the compute nodes reach the head node (scp head:...), if no shared disk
  account = YOUR_UGENT_LOGIN   # sbatch --account (jobs are accounted to you)
  partition = batch
  qos = batch_qos
  workdir = jobs/msq/{account} # on the head node, relative to its home; one folder per job inside
  container =                  # docker image on the nodes to run jobs in (empty = run directly on the node)
  docker_args = -v /mnt/perflab:/mnt/perflab:ro
  default_time = 24:00:00
  max_jobs = 8                 # at most this many of your jobs there at once
  max_upload_gb = 2            # bigger job folders must declare their inputs (--inputs)
  exclude = .git .Xil *.jou *.backup.log .msq_shadow
  offload = never              # never | busy | always: automatic use for allowed jobs
  offload_after_min = 30       # busy = waited this long here for CPU/RAM
  tools =                      # tools that may be offloaded automatically (e.g. vitis_hls xsim); empty = only jobs added with --on auto

How a job runs there: its folder is packed and copied to <workdir>/<job> on the head node with a job script;
sbatch starts it; on the node the script fetches the files (or uses them directly when the disk is shared), runs
the command (inside the container if one is set), copies the output log back every minute and the changed files at
the end. Here, the job looks like any other queue job: live log, run log, notifications, results copied back into
its folder. Nothing secret is sent: jobs with --secret are refused, and no password is ever stored or used.
"""
import configparser
import json
import os
import shlex
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from . import config

CONF = "remotes.conf"
STATE = config.STATE / "remote"
DEFAULTS = {"type": "slurm", "ssh": "", "head": "", "account": "", "partition": "", "qos": "", "workdir": "jobs/msq/{account}",
            "container": "", "docker_args": "", "default_time": "24:00:00", "max_jobs": "8", "max_upload_gb": "2",
            "exclude": ".git .Xil *.jou *.backup.log", "offload": "never", "offload_after_min": "30", "tools": ""}
GiB = 2**30


# ---------------------------------------------------------------- configuration
def remotes():
    """{name: settings} from config/remotes.conf."""
    cp = configparser.ConfigParser(inline_comment_prefixes=("#",), interpolation=None)
    try:
        cp.read(config.CONF / CONF)
    except configparser.Error:
        return {}
    out = {}
    for name in cp.sections():
        r = dict(DEFAULTS)
        r.update({k: v.strip() for k, v in cp[name].items()})
        r["name"] = name
        r["workdir"] = r["workdir"].replace("{account}", r["account"] or "me")
        out[name] = r
    return out


def get(name):
    r = remotes().get(name)
    if not r:
        raise KeyError(f"no remote '{name}' in config/{CONF} (ms remote setup {name})")
    return r


TEMPLATE = """# Remote servers for the my-system queue (ms remote help). Edit: ms unlock -> edit -> ms lock
# Example for Titan (UGent CSL PerfLab, Slurm). Remove the leading '#' and fill in your login.
#
#[titan]
#type = slurm
#ssh = titan
#head = bacchus
#account = YOUR_UGENT_LOGIN
#partition = batch
#qos = batch_qos
#workdir = jobs/msq/{account}
#container =
#docker_args = -v /mnt/perflab:/mnt/perflab:ro
#default_time = 24:00:00
#max_jobs = 8
#max_upload_gb = 2
#exclude = .git .Xil *.jou *.backup.log
#offload = never
#offload_after_min = 30
#tools =
"""


# ---------------------------------------------------------------- transport
def _ssh_base(r):
    STATE.mkdir(parents=True, exist_ok=True)
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
            "-o", "ControlMaster=auto", "-o", f"ControlPath={STATE}/cm-%r@%h-%p", "-o", "ControlPersist=10m", r["ssh"]]


def ssh(r, cmd, timeout=60, input_=None, text=True):
    """Run a shell command on the head node: (rc, stdout, stderr). Never prompts for a password."""
    try:
        p = subprocess.run(_ssh_base(r) + [cmd], capture_output=True, text=text, timeout=timeout, input=input_)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "" if text else b"", "timed out"
    except OSError as e:
        return 127, "" if text else b"", str(e)


def _excluded(rel, patterns):
    import fnmatch
    parts = rel.split("/")
    return any(fnmatch.fnmatch(p, pat) for pat in patterns for p in parts)


def pack(src, dest_tar, patterns=None, only=None):
    """tar.gz of folder `src` (or only the files matching the globs in `only`). Returns (files, bytes)."""
    import glob
    patterns = patterns or []
    n = size = 0
    with tarfile.open(dest_tar, "w:gz") as tf:
        if only:
            files = sorted({os.path.relpath(f, src) for g in only for f in glob.glob(os.path.join(src, g), recursive=True)})
        else:
            files = []
            for dp, dns, fns in os.walk(src):
                rel_dp = os.path.relpath(dp, src)
                dns[:] = [d for d in dns if not _excluded(os.path.normpath(os.path.join(rel_dp, d)), patterns)]
                for f in fns:
                    rel = os.path.normpath(os.path.join(rel_dp, f))
                    if not _excluded(rel, patterns):
                        files.append(rel)
        for rel in files:
            full = os.path.join(src, rel)
            if os.path.isfile(full) and not os.path.islink(full):
                tf.add(full, arcname=rel)
                n += 1
                size += os.path.getsize(full)
            elif os.path.islink(full):
                tf.add(full, arcname=rel)
    return n, size


def folder_size(src, patterns):
    total = 0
    for dp, dns, fns in os.walk(src):
        rel_dp = os.path.relpath(dp, src)
        dns[:] = [d for d in dns if not _excluded(os.path.normpath(os.path.join(rel_dp, d)), patterns)]
        for f in fns:
            rel = os.path.normpath(os.path.join(rel_dp, f))
            if not _excluded(rel, patterns):
                try:
                    total += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
    return total


# ---------------------------------------------------------------- the job script that runs on the node
JOB_SH = r"""#!/bin/bash
#SBATCH --job-name=msq-{jid}
{sbatch_lines}
# my-system remote job {jid} (attempt {attempt}) — generated, do not edit
REMOTE_DIR={remote_dir}
HEAD={head}
copy() {{  # copy with retries: copy SRC DST (one of them host:path)
  for i in 1 2 3 4 5 6 7 8; do scp -q -o BatchMode=yes "$1" "$2" && return 0; sleep $((i * i)); done; return 1
}}
if [ -d "$REMOTE_DIR" ] && [ -f "$REMOTE_DIR/in.tar.gz" ]; then
  SHARED=1; W="$(cd "$REMOTE_DIR" && pwd)/run"; mkdir -p "$W"; cd "$W" || exit 90
  tar xzf ../in.tar.gz || exit 91
else
  SHARED=0; W=$(mktemp -d "${{TMPDIR:-/tmp}}/msq-{jid}-XXXXXX"); cd "$W" || exit 90
  copy "$HEAD:$REMOTE_DIR/in.tar.gz" in.tar.gz || {{ echo "cannot fetch the job files from $HEAD" >&2; exit 92; }}
  tar xzf in.tar.gz && rm -f in.tar.gz
fi
touch .msq-{jid}-start; sleep 1
echo "[remote] $(hostname) · $(nproc) cpus · started $(date '+%F %T')" > msq-{jid}.log
export MSQ_OUTPUTS="$W/.msq-{jid}-outputs" MSQ_JOB_ID={jid} MSQ_THREADS={cores} OMP_NUM_THREADS={cores}
: > .msq-{jid}-outputs
( while sleep 60; do [ "$SHARED" = 1 ] && cp msq-{jid}.log ../msq-{jid}.log || copy msq-{jid}.log "$HEAD:$REMOTE_DIR/msq-{jid}.log"; done ) &
SYNC=$!
{run_line} >> msq-{jid}.log 2>&1
RC=$?
kill $SYNC 2>/dev/null
echo "[remote] finished $(date '+%F %T') rc=$RC" >> msq-{jid}.log
echo $RC > .msq-{jid}-rc
find . -newer .msq-{jid}-start -type f ! -name in.tar.gz ! -name out.tar.gz ! -name .msq-{jid}-start -printf '%P\n' > .msq-{jid}-changed
tar czf out.tar.gz -T .msq-{jid}-changed .msq-{jid}-rc .msq-{jid}-outputs msq-{jid}.log 2>/dev/null
if [ "$SHARED" = 1 ]; then mv out.tar.gz ../out.tar.gz; cp msq-{jid}.log ../msq-{jid}.log
else copy out.tar.gz "$HEAD:$REMOTE_DIR/out.tar.gz"; copy msq-{jid}.log "$HEAD:$REMOTE_DIR/msq-{jid}.log"; cd /; rm -rf "$W"; fi
exit $RC
"""


def _slurm_time(seconds):
    s = int(seconds)
    return f"{s // 86400}-{s % 86400 // 3600:02d}:{s % 3600 // 60:02d}:00" if s >= 86400 else f"{s // 3600:02d}:{s % 3600 // 60:02d}:00"


def job_script(r, j, remote_dir):
    cores = max(1, int(round(float(j.get("cores") or 1))))
    mem = max(1, int(float(j.get("mem_gb") or 2) * 1.1 + 0.999))
    lines = [f"#SBATCH --cpus-per-task={cores}", f"#SBATCH --mem={mem}G",
             f"#SBATCH --time={_slurm_time(j['timeout_s']) if isinstance(j.get('timeout_s'), (int, float)) else r['default_time']}",
             "#SBATCH --output=slurm-%j.out", "#SBATCH --error=slurm-%j.out"]
    if r.get("account"):
        lines.insert(0, f"#SBATCH --account={r['account']}")
    if r.get("partition"):
        lines.append(f"#SBATCH --partition={r['partition']}")
    if r.get("qos"):
        lines.append(f"#SBATCH --qos={r['qos']}")
    env = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in (j.get("env") or {}).items())
    inner = (env + " " if env else "") + "bash -c " + shlex.quote(j["cmd"])
    if r.get("container"):
        run = (f'docker run --rm --init -v "$W":/mnt/run -w /mnt/run -e MSQ_OUTPUTS=/mnt/run/.msq-{j["id"]}-outputs '
               f'-e OMP_NUM_THREADS={cores} --cpus={cores} --memory={mem}g {r.get("docker_args", "")} '
               f'{shlex.quote(r["container"])} bash -c {shlex.quote(inner)}')
    else:
        run = inner
    head = r.get("head") or r["ssh"]
    return JOB_SH.format(jid=j["id"], attempt=j.get("attempt", 1), sbatch_lines="\n".join(lines),
                         remote_dir=shlex.quote(remote_dir), head=shlex.quote(head), cores=cores, run_line=run)


# ---------------------------------------------------------------- run one job (called by the queue runner)
def run_job(jid):
    """Upload, sbatch, follow, download. Prints progress (it becomes the job's log); returns the job's exit code."""
    from . import queue
    j = queue.load(jid)
    r = get(j["remote"])
    if j.get("secrets"):
        print(f"[{r['name']}] refused: jobs with secrets are never sent to a shared server", flush=True)
        return 2
    src = os.getcwd()
    pats = r["exclude"].split()
    if not j.get("inputs"):
        sz = folder_size(src, pats)
        if sz > float(r["max_upload_gb"]) * GiB:
            print(f"[{r['name']}] the job folder is {sz / GiB:.1f} GB (limit {r['max_upload_gb']} GB). Declare what the job "
                  f"needs with --inputs GLOB (flows: inputs:), or raise max_upload_gb.", flush=True)
            return 2
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = STATE / f"in-{jid}-{j['attempt']}.tar.gz"
    only = (j.get("inputs") or []) + (j.get("remote_extra") or []) if j.get("inputs") else None
    n, size = pack(src, tmp, pats, only)
    rdir = f"{r['workdir'].rstrip('/')}/{jid}-{j['attempt']}"
    print(f"[{r['name']}] sending {n} files ({size / 2**20:.1f} MB) to {r['ssh']}:{rdir}", flush=True)
    rc, _, err = ssh(r, f"mkdir -p {shlex.quote(rdir)} && cat > {shlex.quote(rdir)}/in.tar.gz", 1800,
                     input_=tmp.read_bytes(), text=False)
    tmp.unlink(missing_ok=True)
    if rc != 0:
        print(f"[{r['name']}] upload failed: {(err or b'').decode(errors='replace') if isinstance(err, bytes) else err}", flush=True)
        return 3
    script = job_script(r, j, rdir)
    rc, out, err = ssh(r, f"cat > {shlex.quote(rdir)}/job.sh && cd {shlex.quote(rdir)} && sbatch --parsable job.sh", 120,
                       input_=script)
    sid = (out or "").strip().split(";")[0].strip()
    if rc != 0 or not sid.isdigit():
        print(f"[{r['name']}] sbatch failed: {(err or out).strip()[:500]}", flush=True)
        return 4
    queue.update(jid, remote_job=sid, remote_dir=rdir)
    print(f"[{r['name']}] submitted as Slurm job {sid}", flush=True)

    stop = {"now": False}

    def on_term(signum, frame):        # cancelled / timeout here -> cancel there too
        stop["now"] = True
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    seen, last_state, offset = 0, "", 0
    while True:
        if stop["now"]:
            crc, _, cerr = ssh(r, f"scancel {sid}", 60)
            print(f"[{r['name']}] cancelled Slurm job {sid}" + ("" if crc == 0 else f" — scancel said: {cerr.strip()[:200]}"),
                  flush=True)
            return 130
        rc, out, _ = ssh(r, f"squeue -h -j {sid} -o '%T|%N|%r' 2>/dev/null; echo ===; "
                            f"tail -c +{offset + 1} {shlex.quote(rdir)}/msq-{jid}.log 2>/dev/null", 60)
        if rc == 0:
            state_part, _, log_part = out.partition("===\n")
            st = state_part.strip()
            if log_part:
                sys.stdout.write(log_part)
                sys.stdout.flush()
                offset += len(log_part.encode())
            if st != last_state:
                if st:
                    s_, node, why = (st.split("|") + ["", ""])[:3]
                    print(f"[{r['name']}] {s_.lower()}" + (f" on {node}" if node and s_ == "RUNNING" else "")
                          + (f" ({why})" if s_ == "PENDING" and why and why != "None" else ""), flush=True)
                    queue.update(jid, remote_state=s_, remote_node=node or None)
                last_state = st
            if not st:
                seen += 1
                if seen >= 2:                   # gone from the queue twice in a row: finished
                    break
            else:
                seen = 0
        time.sleep(float(os.environ.get("MSQ_REMOTE_POLL", "30")))
    # results
    rc, out, _ = ssh(r, f"sacct -n -X -P -j {sid} -o State,ExitCode 2>/dev/null | head -1", 60)
    acct = (out or "").strip()
    data = subprocess.run(_ssh_base(r) + [f"cat {shlex.quote(rdir)}/out.tar.gz"], capture_output=True, timeout=3600)
    job_rc = None
    if data.returncode == 0 and data.stdout:
        tmp = STATE / f"out-{jid}.tar.gz"
        tmp.write_bytes(data.stdout)
        with tarfile.open(tmp) as tf:
            members = [m for m in tf.getmembers() if not m.name.startswith(("/", "..")) and ".." not in Path(m.name).parts]
            try:
                tf.extractall(src, members=members, filter="data")
            except TypeError:                     # Python < 3.12
                tf.extractall(src, members=members)
        tmp.unlink(missing_ok=True)
        tag = f".msq-{jid}-"                 # per job: two jobs in one folder never mix up their results
        try:
            job_rc = int(Path(src, tag + "rc").read_text().strip())
        except (OSError, ValueError):
            job_rc = None
        outs = Path(src, tag + "outputs")
        if outs.exists() and os.environ.get("MSQ_OUTPUTS"):
            with open(os.environ["MSQ_OUTPUTS"], "a") as f:
                f.write(outs.read_text())
        for f in ("rc", "outputs", "changed", "start"):
            Path(src, tag + f).unlink(missing_ok=True)
        print(f"[{r['name']}] results copied back" + (f" (Slurm: {acct})" if acct else ""), flush=True)
    else:
        print(f"[{r['name']}] no results came back (Slurm: {acct or 'unknown'}). Files stay in {r['ssh']}:{rdir}", flush=True)
    if job_rc is None:
        code = acct.split("|")[-1].split(":")[0] if "|" in acct else ""
        job_rc = int(code) if code.isdigit() and int(code) else 5
    if job_rc == 0 and os.environ.get("MSQ_REMOTE_KEEP") != "1":
        ssh(r, f"rm -rf {shlex.quote(rdir)}", 120)
    return job_rc


# ---------------------------------------------------------------- capacity and automatic offload
def _cache(name):
    try:
        return json.loads((STATE / f"{name}.json").read_text())
    except (OSError, ValueError):
        return {}


def status(r, fresh=False):
    """{'ok', 'idle_cpus', 'total_cpus', 'mine', 'nodes', 'error', 't'} — cached for 5 minutes."""
    c = _cache(r["name"])
    if not fresh and time.time() - c.get("t", 0) < 300:
        return c
    rc, out, err = ssh(r, "sinfo -h -o '%C' 2>&1; echo ===; squeue -h " + (f"-A {shlex.quote(r['account'])} " if r["account"] else "")
                          + "-o %T 2>/dev/null | sort | uniq -c", 30)
    res = {"t": time.time(), "ok": rc == 0}
    if rc == 0:
        a, _, b = out.partition("===")
        try:
            alloc, idle, other, total = (int(x) for x in a.strip().split("/"))
            res.update(idle_cpus=idle, total_cpus=total)
        except ValueError:
            res.update(ok=False, error=a.strip()[:200])
        mine = {}
        for line in b.strip().splitlines():
            n, _, st = line.strip().partition(" ")
            if n.isdigit():
                mine[st.strip()] = int(n)
        res["mine"] = mine
    else:
        res["error"] = (err or out).strip()[:200]
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / f"{r['name']}.json").write_text(json.dumps(res))
    return res


def running_here(name, jobs):
    return sum(1 for x in jobs if x.get("remote") == name and x["state"] in ("running", "suspended"))


def offload_target(j, jobs, waited_s, local_cap):
    """Name of a remote to run a locally blocked job on, or None. Only for jobs that allow it: added with --on auto,
    or whose tool is listed in the remote's 'tools' while offload = busy/always."""
    if j.get("secrets") or j.get("container") or j.get("host") or j.get("remote_never"):
        return None
    for name, r in remotes().items():
        mode = r["offload"]
        if mode not in ("busy", "always"):
            continue
        allowed = j.get("remote_ok") or (j.get("tool") and j["tool"] in r["tools"].split())
        if not allowed:
            continue
        too_big = float(j.get("mem_gb") or 0) * GiB > local_cap.get("total", 0) * 0.9 or \
            float(j.get("cores") or 1) > local_cap.get("threads", 1)
        if mode == "busy" and not too_big and waited_s < float(r["offload_after_min"]) * 60:
            continue
        if running_here(name, jobs) >= int(r["max_jobs"]):
            continue
        st = status(r)
        if st.get("ok") and st.get("idle_cpus", 0) >= max(1, int(round(float(j.get("cores") or 1)))):
            return name
    return None


# ---------------------------------------------------------------- setup checks
def check(name, test_job=False, out=print):
    """Step-by-step check of a remote. Returns True when everything needed works."""
    try:
        r = get(name)
    except KeyError as e:
        out(f"  NO  configuration: {e}")
        return False
    ok_all = True

    def step(ok, what, detail="", fix=""):
        nonlocal ok_all
        out(f"  {'ok ' if ok else 'NO '} {what}" + (f" — {detail}" if detail else ""))
        if not ok and fix:
            out(f"       fix: {fix}")
        ok_all = ok_all and ok
        return ok
    cfg = Path.home() / ".ssh" / "config"
    has = cfg.exists() and any(l.split()[:2] == ["Host", r["ssh"]] or (l.strip().lower().startswith("host ") and r["ssh"] in l.split()[1:])
                              for l in cfg.read_text(errors="replace").splitlines())
    step(has, f"~/.ssh/config has 'Host {r['ssh']}'", "", "add the Host block (see ms remote help)")
    step(bool(r["account"]) and r["account"] != "YOUR_UGENT_LOGIN", "account set", r["account"] or "empty",
         "set account = <your login> in config/remotes.conf")
    rc, o, e = ssh(r, "echo ok; hostname; command -v sbatch squeue sacct scancel | wc -l", 30)
    err = (e or "").strip()
    if "Host key verification failed" in err:
        fix = (f"the server is not known yet — accept its key once: ssh -o StrictHostKeyChecking=accept-new {r['ssh']} hostname"
               " (compare the fingerprint with the one the admins publish, if any)")
    elif "REMOTE HOST IDENTIFICATION HAS CHANGED" in err:
        fix = ("the server's key CHANGED — do not continue until the admins confirm it (then: "
               f"ssh-keygen -R <hostname of {r['ssh']}>)")
    elif "Permission denied" in err:
        fix = "the server does not accept your key yet — ask the admins to install your .pub key (or check IdentityFile)"
    elif "Could not resolve" in err or "timed out" in err or "Connection refused" in err or "No route" in err:
        fix = "the server cannot be reached — outside the university, turn on the UGent VPN"
    else:
        fix = "ssh-add your key or check the key in ~/.ssh/config; first login once by hand: ssh " + r["ssh"]
    if not step(rc == 0 and o.startswith("ok"), f"ssh {r['ssh']} works without a password",
                o.split()[1] if rc == 0 and len(o.split()) > 1 else (err[:150] or "no answer"), fix):
        return False
    step(o.split()[-1] == "4", "Slurm commands found (sbatch, squeue, sacct, scancel)")
    st = status(r, fresh=True)
    step(st.get("ok"), "cluster answers (sinfo)", f"{st.get('idle_cpus')} of {st.get('total_cpus')} CPUs idle"
         if st.get("ok") else st.get("error", ""))
    rc, o, e = ssh(r, f"mkdir -p {shlex.quote(r['workdir'])} && test -w {shlex.quote(r['workdir'])} && df -h {shlex.quote(r['workdir'])} | tail -1", 30)
    step(rc == 0, f"work folder {r['workdir']} is writable", o.split()[3] + " free" if rc == 0 and len(o.split()) > 3 else e.strip()[:150])
    rc, o, _ = ssh(r, "sinfo -h -N -o '%N %c %m %t' | sort -u | head -40", 30)
    if rc == 0 and o.strip():
        nodes = [l.split() for l in o.strip().splitlines() if len(l.split()) >= 4]
        cpus = sorted({int(n[1]) for n in nodes if n[1].isdigit()})
        mems = sorted({int(n[2]) // 1024 for n in nodes if n[2].isdigit()})
        step(True, f"{len(nodes)} node(s)", f"CPUs per node {cpus}, RAM per node {mems} GB")
    out("  -- this server is shared: never put passwords, keys or private data in jobs sent there")
    if test_job and ok_all:
        out("  .. sending a 1-minute test job (1 CPU, 1 GB) to learn what the nodes offer …")
        ok_all = step(_test_job(r, out), "test job ran and its results came back") and ok_all
    return ok_all


def _test_job(r, out):
    from . import queue
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="ms-remote-test-", dir=str(STATE) if STATE.exists() else None))
    probe = ("echo host=$(hostname); echo cpus=$(nproc); free -g | awk '/Mem:/{print \"mem_gb=\"$2}'; "
             "for t in vivado vitis_hls vitis-run xsim xelab docker apptainer singularity python3; do "
             "p=$(command -v $t 2>/dev/null); echo tool_$t=${p:-none}; done; "
             "ls -d /mnt/perflab/* 2>/dev/null | head -20 | sed 's/^/perflab=/'; "
             "docker image ls --format 'image={{.Repository}}:{{.Tag}}' 2>/dev/null | head -30; echo probe=done > probe.txt")
    j = queue.submit(probe, cwd=str(d), name="remote_probe", mem_gb=1, cores=1, remote=r["name"], state="held", limit_memory=False)
    queue.update(j["id"], state="running", started=time.time())
    old = os.getcwd()
    os.chdir(d)
    try:
        rc = run_job(j["id"])
    finally:
        os.chdir(old)
    queue.update(j["id"], state="done" if rc == 0 else "failed", ended=time.time(), rc=rc)
    log = ""
    for f in (f"msq-{j['id']}.log",):
        try:
            log = Path(d, f).read_text()
        except OSError:
            pass
    facts = [l for l in log.splitlines() if "=" in l and not l.startswith("[")]
    for l in facts:
        out("       " + l)
    (STATE / f"{r['name']}-probe.txt").write_text("\n".join(facts) + "\n")
    return rc == 0 and Path(d, "probe.txt").exists()


HELP = """ms remote — run queue jobs on a Slurm cluster (e.g. Titan) when this machine is busy or too small

  ms remote setup NAME        create config/remotes.conf with an example section NAME (then fill in your login)
  ms remote list              configured servers and their state
  ms remote check NAME        step-by-step check: ssh, Slurm, account, work folder, nodes
  ms remote test NAME         check + a 1-minute test job that reports what the nodes offer (CPUs, RAM, tools)
  ms remote status NAME       free CPUs there and your jobs
  ms q add --on NAME -- CMD   run this job there
  ms q add --on auto -- CMD   run here, or there when this machine is busy (offload = busy in remotes.conf)
  flow step key:  remote: titan | auto

~/.ssh/config needs a block like:
  Host titan
      Hostname bacchus.ugent.be
      User slurmslave
      IdentityFile ~/.ssh/titan_ed25519
      IdentitiesOnly yes
"""


def cli(a):
    if not a or a[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0
    sub = a[0]
    if sub == "setup":
        f = config.CONF / CONF
        if f.exists():
            print(f"{f} exists — edit it (ms unlock first if it is locked)")
            return 0
        try:
            f.write_text(TEMPLATE)
        except OSError as e:
            print(f"cannot write {f}: {e} — run ms unlock first")
            return 1
        print(f"written {f}: remove the '#' in front of the [titan] lines and set your account")
        return 0
    if sub == "list":
        rs = remotes()
        if not rs:
            print(f"no remotes configured (ms remote setup titan)")
        for n, r in rs.items():
            c = _cache(n)
            state = "unknown" if not c else ("reachable" if c.get("ok") else f"not reachable: {c.get('error', '')}")
            print(f"{n:<12} {r['type']} via ssh {r['ssh']} · account {r['account'] or '-'} · offload {r['offload']} · {state}")
        return 0
    if sub == "_run":
        return run_job(int(a[1]))
    if len(a) < 2:
        print(HELP)
        return 2
    if sub in ("check", "test"):
        print(f"{'Testing' if sub == 'test' else 'Checking'} remote {a[1]}:")
        return 0 if check(a[1], test_job=sub == "test") else 1
    if sub == "status":
        r = get(a[1])
        st = status(r, fresh=True)
        if not st.get("ok"):
            print(f"{a[1]}: not reachable — {st.get('error', '')}")
            return 1
        print(f"{a[1]}: {st['idle_cpus']} of {st['total_cpus']} CPUs idle · your jobs there: "
              + (", ".join(f"{v} {k.lower()}" for k, v in st.get("mine", {}).items()) or "none"))
        return 0
    print(HELP)
    return 2
