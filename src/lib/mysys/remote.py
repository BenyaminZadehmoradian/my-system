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
  max_cores =                  # at most this many reserved cores (--cores) of your jobs there at once; empty = no
                               # limit. With it, many small jobs (1 core) can run where few big ones would
  max_upload_gb = 2            # bigger job folders must declare their inputs (--inputs)
  exclude = .git .Xil *.jou *.backup.log .msq_shadow
  offload = never              # never | busy | always | prefer: automatic use for allowed jobs
                               #   busy   = only when this machine had no room for offload_after_min minutes
                               #   always = as soon as this machine has no room
                               #   prefer = the server first; this machine when the server is full, unreachable,
                               #            or has been slower for this kind of job
  offload_after_min = 30       # busy = waited this long here for CPU/RAM
  tools =                      # tools that may be offloaded automatically (e.g. vitis_hls xsim; * = every job);
                               # empty = only jobs added with --on auto
  missing_tools =              # programs the server does not have: jobs that use them always run here
  links =                      # ~/PATH -> TARGET[, ...]: links made on each node before a job, inside your home
                               # (e.g. ~/benyamin/xilinx -> /mnt/perflab/benyamin/xilinx); never replaces anything
  path_map =                   # LOCAL => SERVER[, ...]: paths that are elsewhere on the server (LOCAL may be
                                #   written ~/x for scripts that use that literal text), e.g.
                               # /mnt/data/Apps/2025.2.1 => ~/benyamin/xilinx/2025.2.1
                               # (rewritten in the command and in the job's text files there; see "Paths" below)
  map_job_dir = yes            # the job folder's own absolute path is rewritten to its folder on the node
  max_download_gb = 20         # results bigger than this: files over 100 MB stay on the server (head node, 7 days)
  results_exclude =            # files a job writes that never come back, e.g. *.dcp .Xil xsim.dir *.wdb
                               # (a job's own --outputs GLOB list, if given, limits the results to those files)
  setup =                      # shell lines run before every job command there, e.g.
                               # source ~/benyamin/xilinx/2025.2.1/Vitis/settings64.sh; export XILINX_ROOT=...

How a job runs there: its folder is packed and copied to <workdir>/<job> on the head node with a job script;
sbatch starts it; on the node the script fetches the files (or uses them directly when the disk is shared), runs
the command (inside the container if one is set), copies the output log back every minute and the changed files at
the end. Paths: only the job folder travels. Absolute paths in the command and in the folder's text files (scripts, Tcl,
Makefiles, configs) are rewritten on the node: the job folder's own path -> its folder there (map_job_dir), and each
path_map entry LOCAL -> SERVER. Your files here are never changed: rewritten files keep their time stamps so they do
not come back, and files the job writes are mapped back (SERVER -> LOCAL) before they are copied home. A job that
uses other absolute paths under /home, /mnt, /data, /tools, /opt ... (files that exist only here) is not sent
automatically; ms q explain names the path.
Here, the job looks like any other queue job: live log, run log, notifications, results copied back into
its folder. Nothing secret is sent: jobs with --secret are refused, and no password is ever stored or used.
"""
import configparser
import json
import re
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
            "container": "", "docker_args": "", "default_time": "24:00:00", "max_jobs": "8", "max_cores": "", "max_upload_gb": "2",
            "exclude": ".git .Xil *.jou *.backup.log", "offload": "never", "offload_after_min": "30", "tools": "",
            "missing_tools": "", "links": "", "setup": "", "path_map": "", "map_job_dir": "yes",
            "max_download_gb": "20", "results_exclude": "",
            "slurm_mem": "auto"}
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
    safe_workdir(r)
    parse_links(r)
    parse_path_map(r)
    return r


def safe_workdir(r):
    """The server is shared by many people under one account: my-system only ever writes, and deletes, inside its
    own folder. Refuse a work folder that could point at anyone else's files."""
    import re as _re
    w = r["workdir"].strip().rstrip("/")
    parts = w.split("/")
    if (not w or w.startswith(("/", "~")) or ".." in parts or "msq" not in " ".join(parts)
            or not r.get("account") or r["account"] not in parts or _re.search(r"[\s$`;&|*?]", w)):
        raise KeyError(f"unsafe workdir '{w}' for {r['name']}: it must be a relative path inside your home on the server "
                       f"that contains 'msq' and your account, e.g. jobs/msq/{{account}}")
    return w


def _own_job_dir(r, d):
    """True only for a job folder that my-system created: <workdir>/<id>-<attempt>."""
    import re as _re
    w = safe_workdir(r)
    return d.startswith(w + "/") and _re.fullmatch(r"\d+-\d+", d[len(w) + 1:]) is not None


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
#max_cores = 48              # cap on the reserved cores of your jobs there (with it, max_jobs can be higher)
#max_upload_gb = 2
#exclude = .git .Xil *.jou *.backup.log
#offload = never             # never | busy | always | prefer (prefer = server first, this machine as fallback)
#offload_after_min = 30
#tools =                      # * = every job may go there automatically
#missing_tools =              # programs the server lacks (Titan: vivado vitis vitis_hls xsim xelab xvlog xvhdl)
#slurm_mem = auto             # auto: ask Slurm for memory only if it knows the nodes' RAM (Titan does not)
"""


# ---------------------------------------------------------------- transport
def _ssh_base(r):
    STATE.mkdir(parents=True, exist_ok=True)
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
            "-o", "ControlMaster=auto", "-o", f"ControlPath={STATE}/cm-%r@%h-%p", "-o", "ControlPersist=10m", r["ssh"]]


def ssh(r, cmd, timeout=60, input_=None, text=True, stdin_path=None):
    """Run a shell command on the head node: (rc, stdout, stderr). Never prompts for a password.
    stdin_path: send this file as the command's input (streamed, not read into memory)."""
    fh = None
    try:
        kw = {}
        if stdin_path is not None:
            fh = open(stdin_path, "rb")
            kw["stdin"] = fh
        else:
            kw["input"] = input_
        if text:      # tool logs are not always UTF-8 (and a copy can end mid-character): never crash on them
            p = subprocess.run(_ssh_base(r) + [cmd], capture_output=True, timeout=timeout, encoding="utf-8",
                               errors="replace", **kw)
        else:
            p = subprocess.run(_ssh_base(r) + [cmd], capture_output=True, timeout=timeout, **kw)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "" if text else b"", "timed out"
    except OSError as e:
        return 127, "" if text else b"", str(e)
    finally:
        if fh is not None:
            fh.close()


def ssh_retry(r, cmd, timeout=60, tries=3, wait=None, say=None, **kw):
    """ssh() again when the link itself failed (ssh exit 255: refused, reset, too many sessions; 124: timed out).
    A command that ran and failed is not repeated. Many jobs share one connection, and a busy head node drops
    some attempts: one dropped attempt must not fail a job."""
    wait = float(os.environ.get("MSQ_SSH_RETRY_WAIT", "5")) if wait is None else wait
    rc, out, err = 255, "", ""
    for k in range(max(1, tries)):
        rc, out, err = ssh(r, cmd, timeout, **kw)
        if rc not in (255, 124):
            return rc, out, err
        if k + 1 < tries:
            if say:
                e = err.decode(errors="replace") if isinstance(err, bytes) else (err or "")
                say(f"connection problem ({e.strip()[:120] or 'no answer'}): trying again in {wait * (k + 1):.0f} s")
            time.sleep(wait * (k + 1))
    return rc, out, err


def session_stopping():
    """True while the system is shutting down / rebooting, or the user's session is ending (logout)."""
    if os.environ.get("MSQ_FAKE_STOPPING"):
        return os.environ["MSQ_FAKE_STOPPING"] == "1"
    for cmd in (["systemctl", "is-system-running"], ["systemctl", "--user", "is-system-running"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if out == "stopping":
            return True
    return False


UPLOAD_SLOTS = 6


class upload_slot:
    """At most UPLOAD_SLOTS jobs pack and upload at the same time (the others wait their turn): 60 jobs released
    together would otherwise run 60 compressors and 60 uploads through one ssh connection."""

    def __init__(self, say=None, stop=None):
        self.say, self.stop, self.fh = say, stop, None

    def __enter__(self):
        import fcntl
        STATE.mkdir(parents=True, exist_ok=True)
        n = int(os.environ.get("MSQ_UPLOAD_SLOTS", UPLOAD_SLOTS))
        told = False
        while True:
            for k in range(n):
                fh = open(STATE / f"upload-{k}.lock", "w")
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.fh = fh
                    return self
                except OSError:
                    fh.close()
            if self.stop and self.stop():
                return self
            if not told and self.say:
                self.say(f"waiting for an upload slot ({n} uploads at a time)")
                told = True
            time.sleep(1.0)

    def __exit__(self, *a):
        if self.fh is not None:
            self.fh.close()          # closing releases the lock
        return False


def _excluded(rel, patterns):
    import fnmatch
    parts = rel.split("/")
    return any(fnmatch.fnmatch(p, pat) for pat in patterns for p in parts)


def pack(src, dest_tar, patterns=None, only=None):
    """tar.gz of folder `src` (or only the files matching the globs in `only`). Returns (files, bytes)."""
    import glob
    patterns = patterns or []
    n = size = 0
    # level 1: three times faster than the default level 9, and about the same size for sources and logs
    with tarfile.open(dest_tar, "w:gz", compresslevel=1) as tf:
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
#SBATCH --job-name=msq-{jid}-{attempt}
{sbatch_lines}
# my-system remote job {jid} (attempt {attempt}) — generated, do not edit
REMOTE_DIR={remote_dir}
HEAD={head}
copy() {{  # copy with retries: copy SRC DST (one of them host:path); the last error is shown
  local i e
  for i in 1 2 3 4 5 6 7 8; do e=$(scp -q -o BatchMode=yes -o ConnectTimeout=20 "$1" "$2" 2>&1) && return 0; sleep $((i * i)); done
  echo "[remote] scp $1 -> $2 failed 8 times: $e" >&2; return 1
}}
# AMD tools abort ("locale::facet::_S_create_c_locale name not valid") when the locale sent along by ssh/sbatch
# does not exist on the node: fall back to C.UTF-8, which every node has
if [ -n "$(locale 2>&1 >/dev/null)" ]; then unset LANGUAGE; export LANG=C.UTF-8 LC_ALL=C.UTF-8; fi
if [ -d "$REMOTE_DIR" ] && [ -f "$REMOTE_DIR/in.tar.gz" ]; then
  SHARED=1; W="$(cd "$REMOTE_DIR" && pwd)/run"; mkdir -p "$W"; cd "$W" || exit 90
  tar xzf ../in.tar.gz || exit 91
else
  SHARED=0; W=$(mktemp -d "${{TMPDIR:-/tmp}}/msq-{jid}-XXXXXX") && [ -n "$W" ] && cd "$W" || exit 90
  copy "$HEAD:$REMOTE_DIR/in.tar.gz" in.tar.gz || {{ echo "cannot fetch the job files from $HEAD (node $(hostname)): \
can this node ssh to $HEAD without a password? (key and known_hosts in this node's own home)" >&2; exit 92; }}
  tar xzf in.tar.gz || {{ echo "the job files could not be unpacked on $(hostname)" >&2; exit 91; }}
  rm -f in.tar.gz
fi
{link_lines}
JD={jobdir}
export MSQ_WORKDIR="$JD"
msq_esc() {{ printf '%s' "$1" | sed -e 's/[][\/.^$*+?(){{}}|]/\\&/g'; }}
msq_rep() {{ printf '%s' "$1" | sed -e 's/[\/&]/\\&/g'; }}
msq_text() {{  # a regular text file (no NUL byte anywhere, at most 8 MB), not a link
  [ -f "$1" ] && [ ! -L "$1" ] && [ "$(stat -c %s "$1")" -le 8388608 ] && [ "$(tr -dc '\000' < "$1" | head -c 1 | wc -c)" -eq 0 ]
}}
msq_sub() {{  # FILE FROM TO: FROM when the path ends there (next char not part of a name: / ; : " ' ) }} space ...) -> TO
  local a b; a=$(msq_esc "$2"); b=$(msq_rep "$3")
  sed -E -i "s/${{a}}([^A-Za-z0-9_.+~-]|\$)/${{b}}\1/g" "$1"
}}
msq_map() {{  # job files that mention $1: replace it with $2, keep their time stamps (so they do not come back)
  grep -rlF --exclude=in.tar.gz --exclude='.msq-*' --exclude='msq-*.log' -- "$1" . 2>/dev/null | while IFS= read -r f; do
    msq_text "$f" || continue
    touch -r "$f" .msq-{jid}-ts && msq_sub "$f" "$1" "$2" && touch -r .msq-{jid}-ts "$f"
  done; rm -f .msq-{jid}-ts
}}
msq_unmap() {{  # files the job wrote that mention $1 (a path here): back to $2 (the path at home)
  while IFS= read -r f; do case "$f" in msq-*.log|.msq-*) continue;; esac  # the job log keeps the server's real paths
    msq_text "$f" && grep -qF -- "$1" "$f" 2>/dev/null && msq_sub "$f" "$1" "$2"; done < .msq-{jid}-changed
}}
cat > .msq-{jid}-cmd.sh <<'MSQ_CMD_END_{jid}'
{body}
MSQ_CMD_END_{jid}
{map_lines}
touch .msq-{jid}-start; sleep 1
echo "[remote] $(hostname) · $(nproc) cpus · started $(date '+%F %T')" > msq-{jid}.log
{info_lines}
export MSQ_OUTPUTS="$W/.msq-{jid}-outputs" MSQ_JOB_ID={jid} MSQ_THREADS={cores} OMP_NUM_THREADS={cores}
: > .msq-{jid}-outputs
( while sleep 60; do [ "$SHARED" = 1 ] && cp msq-{jid}.log ../msq-{jid}.log || copy msq-{jid}.log "$HEAD:$REMOTE_DIR/msq-{jid}.log"; done ) &
SYNC=$!
# "echo 40 >&3" (progress) works there as it does here: it becomes a "::ms progress" line in the job's output
{{ {run_line} ; }} 3> >(while IFS= read -r p; do echo "::ms progress $p"; done >> msq-{jid}.log) >> msq-{jid}.log 2>&1
RC=$?
kill $SYNC 2>/dev/null
echo "[remote] finished $(date '+%F %T') rc=$RC" >> msq-{jid}.log
echo $RC > .msq-{jid}-rc
find . -newer .msq-{jid}-start \( -type f -o -type l \) ! -name in.tar.gz ! -name out.tar.gz ! -name .msq-{jid}-start -printf '%P\n' > .msq-{jid}-changed
{unmap_lines}
msq_keep() {{  # which written files come back: the job's --outputs (if any), minus results_exclude
  case "$1" in .msq-*|msq-*.log) return 0;; esac
{keep_case}
  return 0
}}
while IFS= read -r f; do msq_keep "$f" || continue; echo "$(stat -c %s -- "$f" 2>/dev/null || echo 0) $f"; done < .msq-{jid}-changed > .msq-{jid}-sizes
TOT=$(awk '{{s+=$1}} END{{printf "%d", s}}' .msq-{jid}-sizes)
if [ "$TOT" -gt {cap_bytes} ] && [ {cap_bytes} -gt 0 ]; then   # too much to copy home: big files stay on the server
  awk -v b={big_bytes} '{{n=$1; l=$0; sub(/^[0-9]+ /,"",l); if (n>b) print l > ".msq-{jid}-big"; else print l}}' .msq-{jid}-sizes > .msq-{jid}-sel
  NB=$(wc -l < .msq-{jid}-big); BB=$(awk -v b={big_bytes} '$1>b{{s+=$1}} END{{printf "%.1f", s/1073741824}}' .msq-{jid}-sizes)
  echo "[remote] results: $((TOT/1048576)) MB is over max_download_gb; $NB file(s) over 100 MB ($BB GB) stay on the server: {head_dir}/kept (7 days)" >> msq-{jid}.log
  sort -rn .msq-{jid}-sizes | head -5 | awk '{{printf "[remote]   %.1f GB  ", $1/1073741824; $1=""; print substr($0,2)}}' >> msq-{jid}.log
  if [ "$SHARED" = 1 ]; then mkdir -p ../kept && tar cf - -T .msq-{jid}-big | (cd ../kept && tar xf -)
  else tar cf kept.tar -T .msq-{jid}-big && copy kept.tar "$HEAD:$REMOTE_DIR/kept.tar"; rm -f kept.tar; fi
  echo kept > .msq-{jid}-kept; echo .msq-{jid}-kept >> .msq-{jid}-sel
else
  sed 's/^[0-9]* //' .msq-{jid}-sizes > .msq-{jid}-sel
  echo "[remote] results: $((TOT/1048576)) MB" >> msq-{jid}.log
fi
tar czf out.tar.gz -T .msq-{jid}-sel .msq-{jid}-rc .msq-{jid}-outputs msq-{jid}.log 2>/dev/null
if [ "$SHARED" = 1 ]; then mv out.tar.gz ../out.tar.gz; cp msq-{jid}.log ../msq-{jid}.log
else copy out.tar.gz "$HEAD:$REMOTE_DIR/out.tar.gz"; copy msq-{jid}.log "$HEAD:$REMOTE_DIR/msq-{jid}.log"; cd /; case "$W" in */msq-{jid}-??????) rm -rf "$W";; esac; fi
exit $RC
"""


def _slurm_time(seconds):
    s = int(seconds)
    return f"{s // 86400}-{s % 86400 // 3600:02d}:{s % 3600 // 60:02d}:00" if s >= 86400 else f"{s // 3600:02d}:{s % 3600 // 60:02d}:00"


def parse_links(r):
    """[(link, target)] from 'links = ~/a/b -> /target, ~/c -> /t2'. A link must be inside your home on the node
    and contain your account name, so it can never touch anyone else's files."""
    out = []
    for part in (r.get("links") or "").split(","):
        if "->" not in part:
            continue
        a, _, b = (x.strip() for x in part.partition("->"))
        if not a.startswith("~/") or ".." in a.split("/") or not r.get("account") or a.split("/")[1] != r["account"] \
                or not b.startswith("/") or any(ch in a + b for ch in " $`;&|*?\"'"):
            raise KeyError(f"unsafe link '{part.strip()}' for {r['name']}: use ~/{r.get('account') or 'ACCOUNT'}/NAME -> /absolute/target")
        out.append((a, b))
    return out


def parse_path_map(r):
    """[(local, server)] from 'path_map = /mnt/data/Apps/2025.2.1 => ~/benyamin/xilinx/2025.2.1, ...'."""
    out = []
    for part in (r.get("path_map") or "").split(","):
        if "=>" not in part:
            continue
        a, _, b = (x.strip().rstrip("/") for x in part.partition("=>"))
        bad = any(ch in a + b for ch in " $`;&|*?\"'\\")
        # LOCAL may also be written ~/x: then that literal text is replaced (scripts that call ~/.local/bin/tool)
        if bad or not (a.startswith("/") or a.startswith("~/")) or not (b.startswith("/") or b.startswith("~/")) \
                or len(a) < 4:
            raise KeyError(f"bad path_map entry '{part.strip()}' for {r['name']}: use /local/path => /server/path "
                           f"(or ~/path on the server)")
        if r.get("container") and b.startswith("~/"):
            raise KeyError(f"path_map entry '{part.strip()}' for {r['name']}: with a container, give the absolute "
                           f"path inside the container (and mount it with docker_args)")
        out.append((a, b))
    return sorted(out, key=lambda ab: -len(ab[0]))


def _srv(path):
    """Server path for shell use: ~/x -> $HOME/x (expanded on the node)."""
    return "$HOME/" + path[2:] if path.startswith("~/") else path


def _sq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _safe_glob(g):
    import re as _re
    return bool(g) and _re.fullmatch(r"[A-Za-z0-9_.*?/\[\]+-]+", g) is not None and ".." not in g.split("/")


def map_pairs(r, cwd):
    """[(local, server-shell-expr)] longest first: the job folder (map_job_dir) and path_map entries."""
    pairs = [(a, _srv(b)) for a, b in parse_path_map(r)]
    if (r.get("map_job_dir") or "yes").lower() in ("yes", "1", "true", "on") and cwd and len(cwd.rstrip("/")) > 1:
        pairs.append((cwd.rstrip("/"), "$JD"))
    return sorted(pairs, key=lambda ab: -len(ab[0]))


def map_lines_for(r, cwd, cmdfile):
    """Shell lines: translate paths in the command file and the job's text files on the node, and back for
    files the job wrote."""
    m, u = [], []
    for a, b in map_pairs(r, cwd):
        m.append(f'msq_sub {cmdfile} {_sq(a)} "{b}"; msq_map {_sq(a)} "{b}"')
        u.append(f'msq_unmap "{b}" {_sq(a)}')
    return "\n".join(m), "\n".join(u)


SOURCE_EXT = {".sh", ".bash", ".zsh", ".tcl", ".py", ".pl", ".mk", ".make", ".cfg", ".ini", ".json", ".yaml", ".yml",
              ".toml", ".conf", ".env", ".f", ".do", ".xdc", ".sdc", ".prj", ".flist", ".cmake"}


def _is_source(fn, full):
    """Scripts and configs a job reads (not logs, reports or results, which mention many paths of earlier runs)."""
    ext = os.path.splitext(fn)[1].lower()
    if ext in SOURCE_EXT or fn in ("Makefile", "makefile", "GNUmakefile", "CMakeLists.txt"):
        return True
    return not ext and os.access(full, os.X_OK)        # executable without extension: a script


SKIP_DIRS = {".git", ".Xil", "__pycache__", "node_modules", ".venv", "venv", "ip_user_files", ".runs", ".cache", ".sim",
             "xsim.dir", ".hbs", "logs", "reports"}
OUTSIDE_RX = None
_OUT_CACHE = {}


def outside_paths(j, r, limit=3):
    """Absolute paths that the job (its command or the text files in its folder) uses and that exist only on this
    machine: under /home /mnt /data /tools /opt /scratch /media /srv, outside the job folder and not in path_map.
    Such a job would fail on the server, so it is not sent automatically. Cached for 10 minutes per folder."""
    import re as _re
    global OUTSIDE_RX
    if OUTSIDE_RX is None:
        OUTSIDE_RX = _re.compile(r"(?<![\w.~$/])(/(?:home|mnt|data|tools|opt|scratch|media|srv)/[^\s\"'<>|;:,)\]}*?=]+)")
    cwd = (j.get("cwd") or "").rstrip("/")
    skip = [a for a, _ in parse_path_map(r)] + [cwd]
    key = (cwd, r["name"], j.get("cmd"), tuple(j.get("inputs") or []), r.get("path_map"), r.get("exclude"))
    c = _OUT_CACHE.get(key)
    if c and time.time() - c[0] < 600:
        return c[1]
    found = []

    def scan(text):
        for m in OUTSIDE_RX.finditer(text):
            pth = m.group(1).rstrip(".")
            if any(pth == s or pth.startswith(s + "/") for s in skip if s):
                continue
            if pth not in found:
                found.append(pth)
            if len(found) >= limit:
                return True
        return False
    done = scan(j.get("cmd") or "")
    pats = r["exclude"].split()
    n = walked = 0
    if not done and cwd and os.path.isdir(cwd):
        for dp, dns, fns in os.walk(cwd):
            rel_dp = os.path.relpath(dp, cwd)
            dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.endswith((".runs", ".cache", ".sim", ".hw", ".ip_user_files"))
                      and not _excluded(os.path.normpath(os.path.join(rel_dp, d)), pats)]
            walked += len(fns) + len(dns)
            if walked > 30000:                     # huge result trees: what was seen is enough to decide
                break
            for fn in fns:
                if n >= 4000 or done:
                    break
                rel = os.path.normpath(os.path.join(rel_dp, fn))
                if _excluded(rel, pats) or fn.startswith(("msq-", ".msq-", "slurm-")) or not _is_source(fn, os.path.join(dp, fn)):
                    continue
                full = os.path.join(dp, fn)
                try:
                    if os.path.islink(full) or os.path.getsize(full) > 512 * 1024:
                        continue
                    with open(full, "rb") as fh:
                        data = fh.read()
                except OSError:
                    continue
                if b"\0" in data[:4096]:
                    continue                    # binary
                n += 1
                done = scan(data.decode(errors="replace"))
            if n >= 4000 or done:
                break
    _OUT_CACHE[key] = (time.time(), found)
    return found


def link_lines(r):
    lines = []
    for a, b in parse_links(r):
        p = "$HOME/" + a[2:]
        lines.append(f'if [ -L "{p}" ] || [ -e "{p}" ]; then :; else mkdir -p "$(dirname "{p}")" && ln -s "{b}" "{p}"; fi')
    return "\n".join(lines)


def job_script(r, j, remote_dir):
    cores = slurm_cores(j)
    mem = max(1, int(float(j.get("mem_gb") or 2) * 1.1 + 0.999))
    lines = [f"#SBATCH --cpus-per-task={cores}"] + ([f"#SBATCH --mem={mem}G"] if r.get("_slurm_mem", True) else []) + [
             f"#SBATCH --time={_slurm_time(j['timeout_s']) if isinstance(j.get('timeout_s'), (int, float)) else r['default_time']}",
             "#SBATCH --output=slurm-%j.out", "#SBATCH --error=slurm-%j.out"]
    if r.get("account"):
        lines.insert(0, f"#SBATCH --account={r['account']}")
    if r.get("partition"):
        lines.append(f"#SBATCH --partition={r['partition']}")
    if r.get("qos"):
        lines.append(f"#SBATCH --qos={r['qos']}")
    env = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in (j.get("env") or {}).items())
    # the command goes into a file in the job folder, so paths in it are translated like in any other job file
    body = (r.get("setup", "").strip() + "\n" if r.get("setup", "").strip() else "") + j["cmd"]
    cmdfile = f".msq-{j['id']}-cmd.sh"
    if r.get("container"):
        jobdir = "/mnt/run"
        inner = (env + " " if env else "") + f"bash /mnt/run/{cmdfile}"
        run = (f'docker run --rm --init -v "$W":/mnt/run -w /mnt/run -e MSQ_OUTPUTS=/mnt/run/.msq-{j["id"]}-outputs '
               f'-e MSQ_WORKDIR=/mnt/run -e OMP_NUM_THREADS={cores} --cpus={cores} --memory={mem}g {r.get("docker_args", "")} '
               f'{shlex.quote(r["container"])} bash -c {shlex.quote(inner)}')
    else:
        jobdir = '"$W"'
        run = (env + " " if env else "") + f"bash {cmdfile}"
    head = r.get("head") or r["ssh"]
    mlines, ulines = map_lines_for(r, j.get("cwd"), cmdfile)
    keep = []
    outs = [o for o in (j.get("outputs") or []) if _safe_glob(o)]
    if outs:
        keep.append('  case "$1" in ' + "|".join(f"{o}|*/{o}" for o in outs) + ") ;; *) return 1;; esac")
    excl = [e for e in (r.get("results_exclude") or "").split() if _safe_glob(e)]
    if excl:
        keep.append('  case "$1" in ' + "|".join(f"{e}|*/{e}|{e}/*|*/{e}/*" for e in excl) + ") return 1;; esac")
    try:
        cap = int(float(r.get("max_download_gb") or 0) * 2**30)
    except ValueError:
        cap = 20 * 2**30
    info = []
    for a_, b_ in parse_links(r):
        p_ = "$HOME/" + a_[2:]
        info.append(f'if [ -L "{p_}" ] && [ "$(readlink "{p_}")" != "{b_}" ]; then echo "[remote] link {a_}: points to '
                    f'$(readlink "{p_}"), not {b_} (left alone; remove it on $(hostname) if it is yours and old)"; '
                    f'elif [ -e "{p_}" ] && [ ! -L "{p_}" ]; then echo "[remote] link {a_}: a real folder/file is there on '
                    f'$(hostname), so no link was made and {b_} is NOT used"; '
                    f'elif [ -e "{p_}/" ]; then echo "[remote] link {a_} -> {b_}: ok"; else echo "[remote] link {a_} -> {b_}: '
                    f'TARGET NOT FOUND on $(hostname) (is the share mounted there / the tools copied?)"; fi >> msq-{j["id"]}.log')
    for a_, b_ in map_pairs(r, j.get("cwd")):
        info.append(f'echo "[remote] path {a_} -> {b_}" >> msq-{j["id"]}.log')
    return JOB_SH.format(map_lines=mlines, unmap_lines=ulines, jid=j["id"], attempt=j.get("attempt", 1),
                         sbatch_lines="\n".join(lines), link_lines=link_lines(r), jobdir=jobdir, body=body,
                         info_lines="\n".join(info), keep_case="\n".join(keep), cap_bytes=cap,
                         big_bytes=100 * 2**20, head_dir=remote_dir,
                         remote_dir=shlex.quote(remote_dir), head=shlex.quote(head), cores=cores, run_line=run)


# ---------------------------------------------------------------- run one job (called by the queue runner)
def mem_supported(r):
    """Ask Slurm for memory (--mem) only if its nodes have a memory size configured. Some clusters (e.g. Titan)
    leave it at 0/1 MB: then every --mem request is refused and memory is limited by the container instead.
    slurm_mem = yes | no | auto (default; result cached for a day)."""
    v = (r.get("slurm_mem") or "auto").lower()
    if v in ("yes", "no"):
        return v == "yes"
    c = _cache(r["name"] + "-mem")
    if time.time() - c.get("t", 0) < 86400 and "max_mb" in c:
        return c["max_mb"] >= 1024
    rc, out, _ = ssh(r, "sinfo -h -o %m", 30)
    vals = [int(x.rstrip("+")) for x in out.split() if x.rstrip("+").isdigit()] if rc == 0 else []
    if not vals:
        return True
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / f"{r['name']}-mem.json").write_text(json.dumps({"t": time.time(), "max_mb": max(vals)}))
    return max(vals) >= 1024


ENDED = ("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL",
         "DEADLINE", "REVOKED")


def _slurm_ended(r, sid, rdir, jid):
    """True when Slurm's accounting says the job ended, or its results are already there. False while sacct still
    reports it pending/running (or the link failed). None when accounting gives no answer at all."""
    rc, out, _ = ssh(r, f"sacct -n -X -P -j {int(sid)} -o State 2>/dev/null | head -1; "
                        f"test -e {shlex.quote(rdir)}/out.tar.gz && echo HAVE_OUT; true", 60)
    if rc != 0:
        return False                     # the link failed: not a sign that the job ended
    if "HAVE_OUT" in out:
        return True
    state = (out.replace("HAVE_OUT", "").split() or [""])[0].upper()
    if not state:
        return None                      # no accounting answer: the caller gives up only after several quiet polls
    return state.startswith(ENDED)


EARLY_EXIT = {"90": "could not make or enter its folder on the node (is /tmp full there?)",
              "91": "the job files could not be unpacked on the node",
              "92": "the node could not fetch the job files from the head node with scp: each node has its own home, so "
                    "the ssh key and known_hosts for the head node must be in that node's ~/.ssh too"}


def _explain_early_death(r, sid, rdir, acct):
    """A job that ended without writing its log: say why, from the exit code and Slurm's own output file."""
    code = acct.split("|")[-1].split(":")[0] if "|" in acct else ""
    if code in EARLY_EXIT:
        print(f"[{r['name']}] exit {code}: {EARLY_EXIT[code]}", flush=True)
    rc, out, _ = ssh(r, f"sacct -n -X -P -j {int(sid)} -o NodeList,Reason 2>/dev/null | head -1; "
                        f"tail -n 15 {shlex.quote(rdir)}/slurm-{int(sid)}.out 2>/dev/null; true", 60)
    if rc == 0 and out.strip():
        for line in out.strip().splitlines()[:16]:
            print(f"[{r['name']}]   {line}", flush=True)
    print(f"[{r['name']}] Slurm writes its own output (slurm-{sid}.out) on the node, in /tmp when the job folder does "
          f"not exist there; ms remote test {r['name']} checks the whole path", flush=True)


def _extract_results(tar_path, dest):
    """Unpack the results one by one into dest. Unsafe or unusable entries (absolute names, '..', links pointing out of
    the folder, devices) are skipped instead of stopping the whole copy. Returns the skipped entries with reasons."""
    skipped = []
    try:
        tf = tarfile.open(tar_path)
    except (tarfile.TarError, OSError, EOFError) as e:
        return [f"the results archive is damaged ({e})"]
    with tf:
        try:
            members = tf.getmembers()
        except (tarfile.TarError, OSError, EOFError) as e:      # truncated: keep what could be read
            members = list(tf.members)
            skipped.append(f"archive truncated ({e})")
        for m in members:
            if m.name.startswith(("/", "..")) or ".." in Path(m.name).parts:
                skipped.append(f"{m.name}: path outside the job folder")
                continue
            try:
                try:
                    tf.extract(m, dest, filter="data")
                except TypeError:                 # Python < 3.12 without extraction filters
                    if m.issym() or m.islnk():
                        tgt = os.path.normpath(os.path.join(os.path.dirname(m.name), m.linkname))
                        if m.linkname.startswith("/") or tgt.startswith(".."):
                            raise tarfile.TarError("link points outside the job folder")
                    if not (m.isfile() or m.isdir() or m.issym() or m.islnk()):
                        raise tarfile.TarError("special file")
                    tf.extract(m, dest)
            except (tarfile.TarError, OSError) as e:
                skipped.append(f"{m.name}: {e.__class__.__name__}: {e}"[:200])
    return skipped


def run_job(jid):
    """Upload, sbatch, follow, download. Prints progress (it becomes the job's log); returns the job's exit code."""
    from . import queue
    j = queue.load(jid)
    r = get(j["remote"])
    if j.get("secrets"):
        print(f"[{r['name']}] refused: jobs with secrets are never sent to a shared server", flush=True)
        return 2
    stop = {"now": False}

    def on_term(signum, frame):        # cancelled / timeout here -> cancel there too (set before anything is sent,
        stop["now"] = True             # so a job cancelled during the upload never runs there unseen)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    src = os.getcwd()
    pats = r["exclude"].split()
    rdir = f"{r['workdir'].rstrip('/')}/{jid}-{j['attempt']}"
    tagname = f"msq-{jid}-{j['attempt']}"

    def say(msg):
        print(f"[{r['name']}] {msg}", flush=True)

    def cancel_unknown():
        """Cancelled while sbatch may have gone through without us seeing its id: cancel by the job's own name."""
        ssh(r, f"scancel --account={shlex.quote(r['account'])} --name={tagname} 2>/dev/null; true", 60)

    sid, offset = "", 0
    rc, out, err = 0, "", ""
    r["_slurm_mem"] = False
    if str(j.get("remote_job") or "").isdigit() and j.get("remote_dir") == rdir:
        # this job was already submitted (the computer or the scheduler restarted meanwhile): follow it again
        sid, offset = str(j["remote_job"]), int(j.get("remote_log_off") or 0)
        say(f"following Slurm job {sid} again (it kept running on the server while this computer restarted)")
    if not sid:
        if not j.get("inputs"):
            sz = folder_size(src, pats)
            if sz > float(r["max_upload_gb"]) * GiB:
                say(f"the job folder is {sz / GiB:.1f} GB (limit {r['max_upload_gb']} GB). Declare what the job "
                    f"needs with --inputs GLOB (flows: inputs:), or raise max_upload_gb.")
                return 2
        STATE.mkdir(parents=True, exist_ok=True)
        tmp = STATE / f"in-{jid}-{j['attempt']}.tar.gz"
        only = (j.get("inputs") or []) + (j.get("remote_extra") or []) if j.get("inputs") else None
        with upload_slot(say, lambda: stop["now"]):
            if stop["now"]:
                say("cancelled before anything was sent")
                return 130
            try:
                n, size = pack(src, tmp, pats, only)
                say(f"sending {n} files ({size / 2**20:.1f} MB) to {r['ssh']}:{rdir}")
                rc, _, err = ssh_retry(r, f"mkdir -p {shlex.quote(rdir)} && cat > {shlex.quote(rdir)}/in.tar.gz", 1800,
                                       say=say, text=False, stdin_path=tmp)
            finally:
                tmp.unlink(missing_ok=True)
        if rc != 0:
            say(f"upload failed: {(err or b'').decode(errors='replace') if isinstance(err, bytes) else err}")
            return 3
        if stop["now"]:
            say("cancelled before it was submitted")
            if _own_job_dir(r, rdir):
                ssh(r, f"rm -rf {shlex.quote(rdir)}", 120)
            return 130
        r["_slurm_mem"] = mem_supported(r)
        script = job_script(r, j, rdir)
        # one attempt only: a second sbatch after a lost answer could start the job twice
        rc, out, err = ssh(r, f"cat > {shlex.quote(rdir)}/job.sh && cd {shlex.quote(rdir)} && sbatch --parsable job.sh", 120,
                           input_=script)
        sid = (out or "").strip().split(";")[0].strip()
        if rc in (255, 124) and not sid.isdigit():
            # the link broke around sbatch: it may or may not have been submitted — ask Slurm by the job's name
            time.sleep(float(os.environ.get("MSQ_SSH_RETRY_WAIT", "5")))
            rc2, out2, _ = ssh_retry(r, f"squeue -h --name={tagname} -o %i 2>/dev/null | head -1", 60, say=say)
            cand = (out2 or "").strip()
            if rc2 == 0 and cand.isdigit():
                rc, sid = 0, cand
            elif rc2 == 0:
                rc, out, err = ssh_retry(r, f"cd {shlex.quote(rdir)} && sbatch --parsable job.sh", 120, say=say)
                sid = (out or "").strip().split(";")[0].strip()
    if (rc != 0 or not sid.isdigit()) and r["_slurm_mem"] and "memory" in (err or "").lower() \
            and (r.get("slurm_mem") or "auto").lower() == "auto":
        # the nodes report no RAM (like Titan) but sinfo could not tell us: again without --mem, and remember it
        r["_slurm_mem"] = False
        (STATE / f"{r['name']}-mem.json").write_text(json.dumps({"t": time.time(), "max_mb": 0}))
        print(f"[{r['name']}] Slurm refused --mem: submitting without it", flush=True)
        rc, out, err = ssh(r, f"cat > {shlex.quote(rdir)}/job.sh && cd {shlex.quote(rdir)} && sbatch --parsable job.sh",
                           120, input_=job_script(r, j, rdir))
        sid = (out or "").strip().split(";")[0].strip()
    if rc != 0 or not sid.isdigit():
        if stop["now"]:
            cancel_unknown()
            say("cancelled while it was being submitted")
            return 130
        print(f"[{r['name']}] sbatch failed: {(err or out).strip()[:500]}", flush=True)
        return 4
    if not (str(j.get("remote_job") or "") == sid):
        queue.update(jid, remote_job=sid, remote_dir=rdir, remote_log_off=0)
        print(f"[{r['name']}] submitted as Slurm job {sid}", flush=True)

    import codecs
    seen, last_state, saved_off, prev_off = 0, "", offset, offset
    lic_buf, no_lic = "", set()
    dec = codecs.getincrementaldecoder("utf-8")(errors="replace")   # a character cut between two polls stays whole
    while True:
        if stop["now"]:
            cur = queue.load(jid) or {}
            if cur.get("state") != "cancelled" and session_stopping():
                # not cancelled by you: this computer is shutting down or you are logging out. The job keeps
                # running on the server and is followed again when the scheduler is back.
                print(f"[{r['name']}] this computer is stopping: Slurm job {sid} keeps running on the server "
                      f"and will be followed again after the restart", flush=True)
                return 143
            # only this job, and only if it runs under your account
            crc, _, cerr = ssh(r, f"scancel --account={shlex.quote(r['account'])} {int(sid)}", 60)
            print(f"[{r['name']}] cancelled Slurm job {sid}" + ("" if crc == 0 else f" — scancel said: {cerr.strip()[:200]}"),
                  flush=True)
            return 130
        rc, raw, _ = ssh(r, f"squeue -h -j {sid} -o '%T|%N|%r' 2>/dev/null; echo ===; "
                            f"tail -c +{offset + 1} {shlex.quote(rdir)}/msq-{jid}.log 2>/dev/null; true", 60, text=False)
        # (; true: no log yet — pending, or the job died before writing one — must not look like a lost connection)
        if rc == 0:
            state_part, _, log_part = raw.partition(b"===\n")
            st = state_part.decode(errors="replace").strip()
            if log_part:                       # bytes: the offset stays exact and a cut character is completed later
                offset += len(log_part)
                text = dec.decode(log_part)
                sys.stdout.write(text)
                lic_buf = (lic_buf + text)[-8000:]          # a message cut between two polls is still found
                no_lic |= {d.lower() for d in LICENSE_RX.findall(lic_buf)}
                sys.stdout.flush()
            if st != last_state:
                if st:
                    s_, node, why = (st.split("|") + ["", ""])[:3]
                    print(f"[{r['name']}] {s_.lower()}" + (f" on {node}" if node and s_ == "RUNNING" else "")
                          + (f" ({why})" if s_ == "PENDING" and why and why != "None" else ""), flush=True)
                    queue.update(jid, remote_state=s_, remote_node=node or None)
                last_state = st
            if not st:
                seen += 1
                # squeue says nothing both for a finished job and when the controller is busy: ask sacct, and only
                # give up waiting when Slurm says the job ended (or, without accounting, after a few quiet polls)
                # (None = sacct could not tell; False = it says the job is still pending/running: keep waiting)
                if seen >= 2:
                    ended = _slurm_ended(r, sid, rdir, jid)
                    if ended or (ended is None and seen >= 10):
                        break
            else:
                seen = 0
            # where the log stands, for a restart. One poll behind on purpose: the lines of the newest poll may still
            # sit in a buffer on their way to the log file, and repeating a few lines is better than losing them.
            if prev_off != saved_off:
                queue.update(jid, remote_log_off=prev_off)
                saved_off = prev_off
            prev_off = offset
        # wait in short steps: a cancel (SIGTERM) must reach scancel long before the SIGKILL that follows it
        t_end = time.time() + float(os.environ.get("MSQ_REMOTE_POLL", "30"))
        while time.time() < t_end and not stop["now"]:
            time.sleep(min(0.5, max(0.0, t_end - time.time())))
    rest = dec.decode(b"", final=True)
    if rest:
        sys.stdout.write(rest)
        sys.stdout.flush()
        no_lic |= {d.lower() for d in LICENSE_RX.findall(lic_buf + rest)}
    if no_lic:
        save_unlicensed(r, no_lic)
        print(f"[{r['name']}] no license there for {', '.join(sorted(no_lic))}: later jobs for "
              f"{'it' if len(no_lic) == 1 else 'them'} run here (a license for the server, e.g. "
              f"XILINXD_LICENSE_FILE in setup, lifts this)", flush=True)
    # results
    rc, out, _ = ssh_retry(r, f"sacct -n -P -j {sid} -o State,ExitCode,ElapsedRaw,TotalCPU,AllocCPUS,MaxRSS 2>/dev/null", 60)
    acct, use = slurm_usage(out)
    if use:
        queue.update(jid, remote_state=acct.split("|")[0] or None, **use)
        print(f"[{r['name']}] used {use['remote_cores_used']:.1f} of {use['remote_cores']} cores on average"
              + (f", peak RAM {use['remote_peak_gb']:.1f} GB" if use.get("remote_peak_gb") else "")
              + f" ({use['remote_elapsed_s'] / 3600:.1f} h)", flush=True)
    tmp = STATE / f"out-{jid}.tar.gz"
    got = False
    for k in range(3):                             # streamed to disk: results can be many GB
        try:
            with open(tmp, "wb") as fh:
                data = subprocess.run(_ssh_base(r) + [f"cat {shlex.quote(rdir)}/out.tar.gz"], stdout=fh,
                                      stderr=subprocess.DEVNULL, timeout=6 * 3600)
            drc = data.returncode
        except subprocess.TimeoutExpired:
            drc = 124
        except OSError:
            drc = 127
        got = drc == 0 and tmp.stat().st_size > 0
        if got or drc not in (255, 124) or k == 2:
            break
        say(f"connection problem while fetching the results: trying again ({k + 2} of 3)")
        time.sleep(float(os.environ.get("MSQ_SSH_RETRY_WAIT", "5")) * (k + 1))
    job_rc = None
    kept = False
    if got:
        skipped = _extract_results(tmp, src)
        if skipped:
            print(f"[{r['name']}] not copied back ({len(skipped)}): " + "; ".join(skipped[:5]), flush=True)
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
        kept = Path(src, tag + "kept").exists()
        for f in ("rc", "outputs", "changed", "start", "kept"):
            Path(src, tag + f).unlink(missing_ok=True)
        if kept:
            print(f"[{r['name']}] big result files stayed on the server in {r['ssh']}:{rdir} (kept/ or kept.tar); "
                  f"ms remote clean {r['name']} removes them after 7 days", flush=True)
        print(f"[{r['name']}] results copied back" + (f" (Slurm: {acct})" if acct else ""), flush=True)
    else:
        print(f"[{r['name']}] no results came back (Slurm: {acct or 'unknown'}). Files stay in {r['ssh']}:{rdir}", flush=True)
        if not offset:                         # it never even wrote its log: it stopped while getting ready on the node
            _explain_early_death(r, sid, rdir, acct)
    if job_rc is None:
        code = acct.split("|")[-1].split(":")[0] if "|" in acct else ""
        job_rc = int(code) if code.isdigit() and int(code) else 5
    tmp.unlink(missing_ok=True)
    if job_rc == 0 and os.environ.get("MSQ_REMOTE_KEEP") != "1" and not kept:
        if _own_job_dir(r, rdir):                  # never anything but this job's own folder
            ssh(r, f"rm -rf {shlex.quote(rdir)}", 120)
    return job_rc


# ---------------------------------------------------------------- capacity and automatic offload
def _cache(name):
    try:
        return json.loads((STATE / f"{name}.json").read_text())
    except (OSError, ValueError):
        return {}


STATUS_TTL = 120


def status(r, fresh=False):
    """{'ok', 'idle_cpus', 'total_cpus', 'mine', 'nodes', 'error', 't'} — cached for 2 minutes."""
    c = _cache(r["name"])
    if not fresh and time.time() - c.get("t", 0) < STATUS_TTL:
        return c
    part = f"-p {shlex.quote(r['partition'])} " if r.get("partition") else ""   # only CPUs you may use
    rc, out, err = ssh(r, f"sinfo -h {part}-o '%C' 2>&1 | tail -1; echo ===; squeue -h " + (f"-A {shlex.quote(r['account'])} " if r["account"] else "")
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


def idle_now(name, st, jobs):
    """Idle CPUs of the server, less what was sent there since that number was read (it is refreshed every few
    minutes): 60 jobs released together must not all be sent because 10 CPUs were idle a minute ago."""
    t = st.get("t", 0)
    sent = sum(slurm_cores(x) for x in jobs if x.get("remote") == name and x["state"] in ("running", "suspended")
               and (x.get("started") or 0) > t and x.get("remote_state") != "RUNNING")
    return st.get("idle_cpus", 0) - sent


def running_here(name, jobs):
    return sum(1 for x in jobs if x.get("remote") == name and x["state"] in ("running", "suspended"))


def cores_there(name, jobs):
    """Cores reserved (--cores) by your jobs running on server `name`."""
    return sum(slurm_cores(x) for x in jobs if x.get("remote") == name and x["state"] in ("running", "suspended"))


def slurm_cores(j):
    """Whole cores Slurm reserves for a job (--cpus-per-task): 0.5 -> 1, 2.4 -> 2."""
    return max(1, int(round(float(j.get("cores") or 1))))


def full(name, r, j, jobs):
    """Why server `name` takes no more of your jobs right now (max_jobs, max_cores), or None.
    A job bigger than max_cores on its own may still start when none of yours runs there."""
    n = running_here(name, jobs)
    if n >= int(r["max_jobs"]):
        return f"{name} already runs {r['max_jobs']} of your jobs (max_jobs)"
    cap = str(r.get("max_cores") or "").strip()
    if cap:
        try:
            cap_f = float(cap)
        except ValueError:
            return None
        used = cores_there(name, jobs)
        need = slurm_cores(j)
        if n and used + need > cap_f:
            return f"{name}: your jobs there reserve {used:g} cores, +{need:g} would pass max_cores = {cap}"
    return None


def _uses(j, progs):
    """True when the job's tool or any word of its command is one of `progs` (by program name)."""
    import re as _re
    if not progs:
        return False
    if j.get("tool") in progs:
        return True
    words = _re.split(r"[\s;&|()<>'\"=]+", j.get("cmd") or "")
    return any(os.path.basename(w) in progs for w in words if w)


PART_RX = re.compile(r"\b(x[ca][a-z]{0,3}\d[0-9a-z]*(?:-[0-9a-z]+){0,3})\b", re.I)   # xc7z020clg400-1, xczu9eg-…
_PARTS_CACHE = {}
PART_FILES = (".tcl", ".xpr", ".cfg", ".ini", ".sh", ".mk", ".json", ".yaml", ".yml")


def save_devices(name, devices):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / f"{name}-devices.json").write_text(json.dumps({"t": time.time(), "devices": sorted(set(devices))}))
    except OSError:
        pass


def devices(name):
    """Devices installed on server `name` (lower case, e.g. 'xc7z020'), or None when not known yet."""
    try:
        d = json.loads((STATE / f"{name}-devices.json").read_text()).get("devices")
        return [x.lower() for x in d] if d else None
    except (OSError, ValueError):
        return None


LICENSE_RX = re.compile(r"valid license was not found for feature '[^']*' and/or device '([A-Za-z0-9_]+)'")


def save_unlicensed(r, devices_):
    """Remember devices the server has no license for (with the setup line: a new setup, e.g. a license server,
    clears the list)."""
    f = STATE / f"{r['name']}-unlicensed.json"
    try:
        d = json.loads(f.read_text())
    except (OSError, ValueError):
        d = {}
    if d.get("setup") != r.get("setup"):
        d = {"setup": r.get("setup"), "devices": {}}
    for x in devices_:
        d.setdefault("devices", {})[x] = time.time()
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(d))
    except OSError:
        pass


def unlicensed(r, max_age_days=30):
    """Devices known to have no license on server r (seen in the last 30 days, with the same setup)."""
    try:
        d = json.loads((STATE / f"{r['name']}-unlicensed.json").read_text())
    except (OSError, ValueError):
        return []
    if d.get("setup") != r.get("setup"):
        return []
    return [k for k, t in (d.get("devices") or {}).items() if time.time() - t < max_age_days * 86400]


def unlicensed_parts(j, r):
    nl = [x.replace("-", "") for x in unlicensed(r)]
    if not nl:
        return []
    return sorted(p for p in job_parts(j) if any(p.replace("-", "").startswith(d) for d in nl))


def job_parts(j, limit_files=300, max_bytes=262144):
    """FPGA part names a job uses: in its command and in the Tcl/project/config files of its folder (cached 10 min)."""
    key = (j.get("cwd"), j.get("cmd"))
    hit = _PARTS_CACHE.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    found = _scan_parts(j, limit_files, max_bytes)
    _PARTS_CACHE[key] = (time.time(), found)
    return found


def _scan_parts(j, limit_files, max_bytes):
    found = set(m.lower() for m in PART_RX.findall(j.get("cmd") or ""))
    cwd = Path(j.get("cwd") or ".")
    n = 0
    for root, dirs, files in os.walk(cwd):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in (".Xil", "__pycache__")]
        if Path(root) != cwd and len(Path(root).relative_to(cwd).parts) > 2:
            dirs[:] = []
            continue
        for f in files:
            if not f.endswith(PART_FILES):
                continue
            n += 1
            if n > limit_files:
                return found
            try:
                with open(os.path.join(root, f), "rb") as fh:
                    found |= set(m.lower() for m in PART_RX.findall(fh.read(max_bytes).decode(errors="replace")))
            except OSError:
                pass
    return found


def missing_parts(j, name):
    """Parts the job uses that server `name` does not have (empty when unknown or all there)."""
    devs = devices(name)
    if not devs:
        return []
    out = []
    for p in sorted(job_parts(j)):
        q = p.replace("-", "")
        if not any(q.startswith(d.replace("-", "")) for d in devs):
            out.append(p)
    return out


def why_not(j, r, scan=True):
    """Why this job may not go to server `r` automatically, or None when it may. scan=False skips the (slower)
    look into the job folder for paths that exist only here."""
    if j.get("secrets"):
        return "it uses --secret (never sent to a shared server)"
    if j.get("container") or j.get("host"):
        return "it has its own --container/--host"
    if j.get("remote_never"):
        return "it stays here (--on here, or it already failed on a server)"
    if int(j.get("gpus") or 0):
        return "it needs a GPU"
    miss = set(r["missing_tools"].split())
    if _uses(j, miss):
        return f"it uses a program the server lacks (missing_tools: {' '.join(sorted(miss))})"
    tools = r["tools"].split()
    if not (j.get("remote_ok") or "*" in tools or (j.get("tool") and j["tool"] in tools)):
        return "its tool is not in 'tools' (add it, or tools = *, or use --on auto)"
    if not scan:
        return None
    try:
        out = outside_paths(j, r)
    except Exception:
        out = []
    if out:
        return "it uses paths that exist only here: " + ", ".join(out[:3]) + " (add them to path_map, or keep the files inside the job folder)"
    try:
        mp = missing_parts(j, r["name"])
    except Exception:
        mp = []
    if mp:
        return f"it uses a part not installed there: {', '.join(mp[:3])} (add the devices on the server, then ms remote tools {r['name']})"
    try:
        up = unlicensed_parts(j, r)
    except Exception:
        up = []
    if up:
        return f"no license there for: {', '.join(up[:3])} (a job failed for it; a license for the server lifts this)"
    return None


def allowed(j, r):
    """May this job go to server `r` automatically? (--on auto, or its tool listed in 'tools', or tools = *)."""
    return why_not(j, r) is None


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def speed(j, name, jobs):
    """(median seconds on server `name`, median here) of earlier successful runs of the same job (name and folder,
    else the same tool); None where unknown. Server time includes upload, waiting in Slurm and download."""
    ok = [x for x in jobs if x["state"] in ("done", "warning") and x.get("started") and x.get("ended")
          and x["id"] != j.get("id")]

    def pick(match):
        there = [x["ended"] - x["started"] for x in ok if match(x) and x.get("remote") == name]
        here = [x["ended"] - x["started"] for x in ok if match(x) and not x.get("remote")]
        return there, here
    there, here = pick(lambda x: x.get("name") == j.get("name") and x.get("cwd") == j.get("cwd"))
    if (len(there) < 2 or len(here) < 2) and j.get("tool"):
        there, here = pick(lambda x: x.get("tool") == j.get("tool"))
    return (_median(there) if len(there) >= 2 else None), (_median(here) if len(here) >= 2 else None)


def prefer_target(j, jobs):
    """(name, note) of a server with offload = prefer that should run this job before this machine is tried,
    or (None, why-not). The server is skipped when it is full, unreachable, at max_jobs/max_cores, or measurably slower."""
    why = None
    for name, r in remotes().items():
        if r["offload"] != "prefer":
            continue
        wn = why_not(j, r, scan=False)           # cheap checks first; the folder scan only when the server has room
        if wn:
            why = f"{name}: {wn}"
            continue
        fw = full(name, r, j, jobs)
        if fw:
            why = fw
            continue
        st = status(r)
        if not st.get("ok"):
            why = f"{name} not reachable"
            continue
        if idle_now(name, st, jobs) < slurm_cores(j):
            why = f"{name} has no free CPUs right now"
            continue
        wn = why_not(j, r)
        if wn:
            why = f"{name}: {wn}"
            continue
        there, here = speed(j, name, jobs)
        if there and here and there > here * 1.1:
            why = f"{name} was slower for this job ({there / 60:.0f} vs {here / 60:.0f} min here)"
            continue
        note = f"sent to {name} first (offload = prefer)" + (
            f": usually {there / 60:.0f} min there vs {here / 60:.0f} min here" if there and here else "")
        return name, note
    return None, why


def offload_target(j, jobs, waited_s, local_cap):
    """Name of a remote to run a locally blocked job on, or None. Only for jobs that allow it: added with --on auto,
    or whose tool is listed in the remote's 'tools' while offload = busy/always/prefer."""
    for name, r in remotes().items():
        mode = r["offload"]
        if mode not in ("busy", "always", "prefer") or not allowed(j, r):
            continue
        too_big = float(j.get("mem_gb") or 0) * GiB > local_cap.get("total", 0) * 0.9 or \
            float(j.get("cores") or 1) > local_cap.get("threads", 1)
        if mode == "busy" and not too_big and waited_s < float(r["offload_after_min"]) * 60:
            continue
        if full(name, r, j, jobs):
            continue
        st = status(r)
        if st.get("ok") and idle_now(name, st, jobs) >= slurm_cores(j):
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
        if mems and max(mems) > 0:
            step(True, f"{len(nodes)} node(s)", f"CPUs per node {cpus}, RAM per node {mems} GB")
        else:
            step(True, f"{len(nodes)} node(s)", f"CPUs per node {cpus}; Slurm does not know the nodes' RAM, so jobs do "
                                                f"not ask for memory (set container = … to limit it per job)")
    # tools on the server: links, path_map, setup
    for a, b in parse_links(r):
        rc, o, _ = ssh(r, f"test -e {shlex.quote(b)} && echo yes || echo no", 30)
        step(rc == 0 and o.strip() == "yes", f"link {a} -> {b}", "target exists" if o.strip() == "yes" else
             "target not found on the server (yet)", "copy the tools there first, or fix the path")
    for a, b in parse_path_map(r):
        tgt = ("$HOME/" + b[2:]) if b.startswith("~/") else shlex.quote(b)
        rc, o, _ = ssh(r, f"test -e {tgt} && echo yes || echo no", 30)
        step(rc == 0 and o.strip() == "yes", f"path_map {a} => {b}",
             "exists on the server" + (" (on the nodes through the link)" if r.get("links") else "")
             if o.strip() == "yes" else "not found on the server", "check the path, or install/copy it there")
    if r.get("setup", "").strip():
        out(f"  ..  setup before each job: {r['setup'].strip()}")
    if r.get("missing_tools", "").strip():
        out(f"  ..  kept here (missing_tools): {r['missing_tools'].strip()}")
    out("  -- this server is shared: never put passwords, keys or private data in jobs sent there")
    if test_job and ok_all:
        out("  .. sending a 1-minute test job (1 CPU, 1 GB) to learn what the nodes offer …")
        ok_all = step(_test_job(r, out), "test job ran and its results came back") and ok_all
    return ok_all


def _cpu_seconds(t):
    """sacct TotalCPU ([D-]HH:MM:SS, MM:SS.mmm) -> seconds."""
    t = (t or "").strip()
    if not t:
        return 0.0
    days = 0
    if "-" in t:
        d, t = t.split("-", 1)
        days = int(d) if d.isdigit() else 0
    parts = [float(x) for x in t.split(":") if x.replace(".", "", 1).isdigit()]
    sec = 0.0
    for x in parts:
        sec = sec * 60 + x
    return days * 86400 + sec


def _size_gb(v):
    m = re.match(r"([0-9.]+)([KMGT]?)", (v or "").strip())
    if not m:
        return 0.0
    return float(m.group(1)) * {"": 1 / 2**30, "K": 1 / 2**20, "M": 1 / 2**10, "G": 1, "T": 1024}[m.group(2)]


def slurm_usage(out):
    """('STATE|EXIT', {remote_cores, remote_cores_used, remote_elapsed_s, remote_peak_gb}) from
    sacct -P -o State,ExitCode,ElapsedRaw,TotalCPU,AllocCPUS,MaxRSS (job line + its steps)."""
    lines = [ln.split("|") for ln in (out or "").strip().splitlines() if ln.count("|") >= 5]
    if not lines:
        return (out or "").strip().splitlines()[0] if (out or "").strip() else "", {}
    st, ex, el, cpu, n, _ = lines[0][:6]
    acct = f"{st}|{ex}"
    try:
        el, n = int(el), int(n)
    except ValueError:
        return acct, {}
    peak = max((_size_gb(x[5]) for x in lines), default=0.0)
    use = {"remote_cores": n, "remote_elapsed_s": el,
           "remote_cores_used": round(_cpu_seconds(cpu) / el, 2) if el else 0.0}
    if peak:
        use["remote_peak_gb"] = round(peak, 2)
    return acct, use


def measure(j):
    """Ask Slurm afterwards what a finished server job used (jobs that ended before 1.20.1, or when the link was
    down at the end). Saves remote_cores_used / remote_peak_gb in the job. Returns True when it found numbers."""
    from . import queue
    if not j.get("remote_job") or j.get("remote_cores_used") is not None or j.get("state") not in queue.TERMINAL:
        return False
    try:
        r = get(j.get("remote") or "")
        rc, out, _ = ssh(r, f"sacct -n -P -j {int(j['remote_job'])} -o State,ExitCode,ElapsedRaw,TotalCPU,AllocCPUS,MaxRSS"
                            " 2>/dev/null", 30)
    except Exception:
        return False
    acct, use = slurm_usage(out)
    if not use:
        return False
    queue.update(j["id"], remote_state=acct.split("|")[0] or None, **use)
    j.update(use)
    return True


# ---------------------------------------------------------------- how well the server is used
def family(name):
    """Jobs that belong together: the name up to its first '_' or digit run ('k70v_aes_c0' -> 'k70v',
    'rom_stencil3d_s2' -> 'rom', 'titan_lit_ncubed_xc7a200t2' -> 'titan')."""
    m = re.match(r"[A-Za-z]+\d*[A-Za-z]*", name or "")
    return (m.group(0) if m else (name or "?"))[:24]


def usage_summary(days=3, jobs=None, now=None):
    """What finished server jobs of the last `days` days reserved and really used (Slurm accounting).
    {'groups': [{family, n, cores, used, peak_gb, mem_gb, hours, cpu_eff, advice}], 'reserved_h', 'used_h',
     'cpu_eff', 'n'} — groups sorted by wasted core-hours."""
    from . import queue
    now = now or time.time()
    jobs = queue.all_jobs() if jobs is None else jobs
    g = {}
    for j in jobs:
        if j.get("remote_cores_used") is None or not j.get("remote_elapsed_s") or (j.get("ended") or 0) < now - days * 86400:
            continue
        if j["remote_elapsed_s"] < 300:                 # too short to say anything about its needs
            continue
        res = float(j.get("remote_cores") or j.get("cores") or 1)
        d = g.setdefault((family(j.get("name")), res), {"n": 0, "res_h": 0.0, "used_h": 0.0, "used": [], "peak": [],
                                                        "mem": float(j.get("mem_gb") or 0)})
        h = j["remote_elapsed_s"] / 3600
        d["n"] += 1
        d["res_h"] += res * h
        d["used_h"] += float(j["remote_cores_used"]) * h
        d["used"].append(float(j["remote_cores_used"]))
        if j.get("remote_peak_gb"):
            d["peak"].append(float(j["remote_peak_gb"]))
    groups = []
    for (fam, res), d in g.items():
        used = sorted(d["used"])
        hi = used[min(len(used) - 1, int(len(used) * 0.9))]          # what 9 of 10 jobs stay under
        want = max(1, int(hi + 0.7))                                   # 1.2 -> 1, 1.9 -> 2, 2.4 -> 3
        peak = max(d["peak"]) if d["peak"] else None
        advice = ""
        if d["n"] >= 2 and want < res:
            advice = f"--cores {want}" + (f" --mem {max(2, int(peak * 1.25 + 1))}" if peak else "")
        groups.append({"family": fam, "n": d["n"], "cores": res, "used": round(sum(used) / len(used), 2),
                       "used_hi": round(hi, 2), "peak_gb": round(peak, 1) if peak else None, "mem_gb": d["mem"],
                       "hours": round(d["res_h"] / res, 1), "wasted_h": round(d["res_h"] - d["used_h"], 1),
                       "cpu_eff": round(d["used_h"] / d["res_h"] * 100) if d["res_h"] else None, "advice": advice})
    groups.sort(key=lambda x: -x["wasted_h"])
    res_h = sum(d["res_h"] for d in g.values())
    used_h = sum(d["used_h"] for d in g.values())
    return {"groups": groups, "reserved_h": round(res_h, 1), "used_h": round(used_h, 1),
            "cpu_eff": round(used_h / res_h * 100) if res_h else None, "n": sum(d["n"] for d in g.values())}


def usage_text(days=3, limit=12):
    u = usage_summary(days)
    if not u["n"]:
        return f"no finished server jobs with measurements in the last {days} day(s)"
    L = [f"server jobs of the last {days} day(s): {u['n']} measured · reserved {u['reserved_h']:.0f} core-hours, "
         f"used {u['used_h']:.0f} ({u['cpu_eff']}%)",
         f"{'jobs like':<24} {'n':>4} {'reserved':>9} {'used avg':>9} {'9 of 10 <':>10} {'peak GB':>8} {'cpu%':>5}  better"]
    for x in u["groups"][:limit]:
        L.append(f"{x['family']:<24} {x['n']:>4} {x['cores']:>7.0f} c {x['used']:>9.2f} {x['used_hi']:>10.2f} "
                 f"{(str(x['peak_gb']) if x['peak_gb'] else '–'):>8} {str(x['cpu_eff'] or '–'):>5}  {x['advice'] or 'ok'}")
    if any(x["advice"] for x in u["groups"]):
        L.append("'better' = what such jobs really needed. Fewer reserved cores let more of your jobs run at once "
                 "(max_cores) and take less from other users of the server.")
    return "\n".join(L)


def advice_for(name, cores, jobs=None):
    """A one-line hint for a new job named like earlier server jobs that used far less than they reserved."""
    try:
        u = usage_summary(7, jobs)
    except Exception:
        return None
    fam = family(name)
    for x in u["groups"]:
        if x["family"] == fam and x["advice"] and float(cores or 1) >= x["cores"] and x["n"] >= 3:
            return (f"jobs like '{fam}…' used {x['used']:.1f} of {x['cores']:.0f} reserved cores on the server "
                    f"({x['n']} measured): {x['advice']} would let more of them run at once")
    return None


def _probe_job(r, probe, name, cores=1, mem_gb=1, timeout_s=None):
    """Run `probe` on a node of `r` through the normal remote path (upload, setup, links, path_map, results back).
    Returns (rc, log text, names of the files that came back). The temporary job folder is removed."""
    from . import queue
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="ms-remote-test-", dir=str(STATE) if STATE.exists() else None))
    j = queue.submit(probe, cwd=str(d), name=name, mem_gb=mem_gb, cores=cores, remote=r["name"], state="held",
                     limit_memory=False, **({"timeout_s": timeout_s} if timeout_s else {}))
    queue.update(j["id"], state="running", started=time.time())
    old = os.getcwd()
    os.chdir(d)
    try:
        rc = run_job(j["id"])
    finally:
        os.chdir(old)
    queue.update(j["id"], state="done" if rc == 0 else "failed", ended=time.time(), rc=rc)
    try:
        log = Path(d, f"msq-{j['id']}.log").read_text(errors="replace")
    except OSError:
        log = ""
    files = {p.name for p in d.iterdir()} if d.exists() else set()
    import shutil
    shutil.rmtree(d, ignore_errors=True)            # temporary: nothing of it is needed afterwards
    return rc, log, files


def _test_job(r, out):
    probe = ("echo host=$(hostname); echo cpus=$(nproc); free -g | awk '/Mem:/{print \"mem_gb=\"$2}'; "
             "for t in vivado vitis_hls vitis-run v++ xsim xelab docker apptainer singularity python3; do "
             "p=$(command -v $t 2>/dev/null); echo tool_$t=${p:-none}; done; "
             "ls -d /mnt/perflab/* 2>/dev/null | head -20 | sed 's/^/perflab=/'; "
             "docker image ls --format 'image={{.Repository}}:{{.Tag}}' 2>/dev/null | head -30; echo probe=done > probe.txt")
    rc, log, d = _probe_job(r, probe, "remote_probe")
    facts = [l for l in log.splitlines() if "=" in l and not l.startswith("[")]
    for l in facts:
        out("       " + l)
    (STATE / f"{r['name']}-probe.txt").write_text("\n".join(facts) + "\n")
    return rc == 0 and "probe.txt" in d


# ---------------------------------------------------------------- AMD tools on the nodes
TOOLS_PROBE = r"""
echo "T node=$(hostname)"
echo "T os=$( . /etc/os-release 2>/dev/null; echo "$PRETTY_NAME")"
echo "T glibc=$(ldd --version 2>/dev/null | head -1 | awk '{print $NF}')"
echo "T locale=${LC_ALL:-${LANG:-unset}}"
echo "T home=$HOME"; [ -w "$HOME" ] && echo "T home_writable=yes" || echo "T home_writable=no"
echo "T tmp_free_gb=$(df -Pk "${TMPDIR:-/tmp}" 2>/dev/null | awk 'NR==2{printf "%d", $4/1048576}')"
echo "T XILINX_VIVADO=${XILINX_VIVADO:-}"
echo "T XILINX_VITIS=${XILINX_VITIS:-}"
echo "T XILINX_HLS=${XILINX_HLS:-}"
for v in "$XILINX_VIVADO" "$XILINX_VITIS"; do
  [ -n "$v" ] || continue; root=$(dirname "$v")
  for p in Vivado Vitis Vitis_HLS; do [ -f "$root/$p/settings64.sh" ] && echo "T settings=$root/$p/settings64.sh"; done
done
for t in vivado vitis vitis-run v++ vitis_hls xsim xelab xvlog; do
  p=$(command -v "$t" 2>/dev/null)
  echo "T tool.$t=${p:-none}"
  [ -n "$p" ] || continue
  rp=$(readlink -f "$p"); [ -x "$rp" ] && echo "T exec.$t=yes" || echo "T exec.$t=no"
done
[ -e /usr/lib/x86_64-linux-gnu/crt1.o ] && echo "T crt1=yes" || echo "T crt1=no"
[ -e /usr/include/stdio.h ] && echo "T libc_headers=yes" || echo "T libc_headers=no"
if command -v vivado >/dev/null 2>&1; then   # the devices Vivado can really use (folder names list every family)
  mkdir -p tooltest/parts && ( cd tooltest/parts && printf '%s\n' \
    'puts "T devices=[join [lsort -unique [get_property DEVICE [get_parts]]] { }]"' \
    'puts "T families=[join [lsort -unique [get_property ARCHITECTURE [get_parts]]] { }]"' 'exit' > p.tcl && \
    timeout 900 vivado -mode batch -nojournal -nolog -notrace -source p.tcl 2>/dev/null | grep '^T ' )
fi
run() {  # NAME CMD...: run with a time limit, show the first lines and the exit code
  local n=$1; shift
  command -v "$1" >/dev/null 2>&1 || return 0
  mkdir -p "tooltest/$n"
  local o; o=$(cd "tooltest/$n" && timeout 600 "$@" 2>&1 < /dev/null); local rc=$?
  printf '%s\n' "$o" | grep -v '^\s*$' | head -6 | sed "s/^/V $n| /"
  echo "T rc.$n=$rc"
}
run vivado vivado -version
run vitis-run vitis-run --version
run v++ v++ --version
run vitis_hls vitis_hls -version
run xelab xelab -version
echo probe=done > probe.txt
"""


def _parse_probe(log):
    facts, vers = {}, {}
    for line in log.splitlines():
        if line.startswith("T ") and "=" in line:
            k, _, v = line[2:].partition("=")
            if k == "settings":
                facts.setdefault("settings", []).append(v.strip())
            else:
                facts[k.strip()] = v.strip()
        elif line.startswith("V ") and "| " in line:
            n, _, v = line[2:].partition("| ")
            vers.setdefault(n, []).append(v)
    return facts, vers


def tools_check(name, out=print):
    """Run Vivado/Vitis on a compute node exactly as a job would (setup, links, path_map) and explain what is missing.
    Returns True when Vivado works there."""
    try:
        r = get(name)
    except KeyError as e:
        out(f"  NO  configuration: {e}")
        return False
    rc, o, e = ssh(r, "echo ok", 30)
    if rc != 0 or not o.startswith("ok"):
        out(f"  NO  ssh {r['ssh']} does not work — run: ms remote check {name}")
        return False
    out(f"  ..  setup: {r.get('setup') or '(empty)'}")
    for a, b in parse_links(r):
        out(f"  ..  link on each node: {a} -> {b}")
    out("  ..  sending a test job that starts the AMD tools on a node (a few minutes; Vivado is slow to start "
        "from a network share) …")
    rc, log, d = _probe_job(r, TOOLS_PROBE, "remote_tools_check", cores=2, mem_gb=4, timeout_s=3600)
    facts, vers = _parse_probe(log)
    (STATE / f"{r['name']}-tools.txt").write_text(log)
    for line in log.splitlines():                 # what the job script itself reported (links, translations)
        if line.startswith("[remote] link"):
            out("  ..  " + line[9:])
    if not facts:
        out(f"  NO  the test job did not run (exit {rc}). Its log:")
        for line in log.splitlines()[-25:]:
            out("       " + line)
        out(f"       fix: ms remote test {name} first (it checks that plain jobs run there)")
        return False
    out(f"  ..  node {facts.get('node')} · {facts.get('os')} · glibc {facts.get('glibc')} · "
        f"{facts.get('tmp_free_gb', '?')} GB free in /tmp · locale {facts.get('locale')}")
    ok = {"v": True}

    def step(good, what, detail="", fix=""):
        out(f"  {'ok ' if good else 'NO '} {what}" + (f" — {detail}" if detail else ""))
        if not good and fix:
            for i, f in enumerate(fix.split("\n")):
                out(("       fix: " if i == 0 else "            ") + f)
        return good

    have = {t: facts.get(f"tool.{t}", "none") != "none" for t in
            ("vivado", "vitis", "vitis-run", "v++", "vitis_hls", "xsim", "xelab", "xvlog")}
    settings = facts.get("settings", [])
    vitis_settings = next((x for x in settings if "/Vitis/" in x), "")
    if not facts.get("XILINX_VIVADO") and not facts.get("XILINX_VITIS"):
        ok["v"] = False
        step(False, "setup loads the AMD tools", "XILINX_VIVADO and XILINX_VITIS are empty on the node",
             "set in remotes.conf (ms unlock; edit; ms lock):\n"
             "setup = source ~/ACCOUNT/xilinx/VERSION/Vitis/settings64.sh   (Vitis also sets up Vivado)\n"
             "and check that the link target exists on the nodes (the 'link' line above)")
    else:
        step(True, "setup loads the AMD tools", facts.get("XILINX_VITIS") or facts.get("XILINX_VIVADO"))
    for t in ("vivado", "vitis-run", "v++"):
        if have[t] and facts.get(f"exec.{t}") == "no":
            ok["v"] = False
            step(False, f"{t} is executable", facts.get(f"tool.{t}"),
                 "the files lost their execute permission when they were copied to the share (rsync -rlt does\n"
                 "not keep permissions). Copy again with: rsync -rltp …  — or ask for the share to be mounted\n"
                 "with file_mode=0755 / 'noperm'")
    works = {}
    for t in ("vivado", "vitis-run", "v++", "vitis_hls", "xelab"):
        if not have.get(t):
            continue
        trc = facts.get(f"rc.{t}")
        first = next((v for v in vers.get(t, []) if v.strip()), "")
        works[t] = trc == "0"
        if trc == "124":
            step(False, f"{t} starts", "no answer within 10 minutes",
                 "the network share may be very slow on this node; try again, or copy the tools to the\n"
                 "node's local disk")
            ok["v"] = ok["v"] and t != "vivado"
        elif trc == "0":
            step(True, f"{t} starts", first)
        else:              # (ldd on AMD's internal binaries is meaningless: their wrapper sets LD_LIBRARY_PATH)
            step(False, f"{t} starts", f"exit {trc}: " + " / ".join(vers.get(t, [])[-3:]),
                 _tool_fix(vers.get(t, [])))
            ok["v"] = ok["v"] and t != "vivado"
    if not have["vivado"] and (facts.get("XILINX_VIVADO") or facts.get("XILINX_VITIS")):
        ok["v"] = False
        step(False, "vivado is on PATH", "not found after setup", "source the Vivado or Vitis settings64.sh in setup")
    if have["vivado"] and not (have["vitis-run"] or have["v++"] or have["vitis_hls"]):
        step(False, "HLS (vitis-run / v++) is on PATH", "only Vivado is set up",
             (f"to use HLS there, source Vitis instead of Vivado in setup:\nsetup = source {vitis_settings}"
              if vitis_settings else "HLS needs the Vitis product (installer product 1), not Vivado (2)"))
    elif (have["vitis-run"] or have["v++"]) and not have["vitis_hls"]:
        out("  ..  no 'vitis_hls' command there (newer Vitis versions replaced it with 'vitis-run --mode hls' and\n"
            "       'v++ -c --mode hls'); jobs that call vitis_hls must be changed, or keep vitis_hls in missing_tools")
    if (have["vitis-run"] or have["v++"] or have["vitis_hls"]) and (facts.get("crt1") == "no" or facts.get("libc_headers") == "no"):
        step(False, "C compiler files for HLS C simulation (csim)", "the node has no libc6-dev (crt1.o / stdio.h)",
             "csynth and cosim-free flows work; for csim ask the admins for: apt install libc6-dev")
    if facts.get("devices"):
        devs = facts["devices"].split()
        out(f"  ..  devices installed there ({len(devs)}), families: {facts.get('families', '?')}")
        out("       jobs whose Tcl files use another part stay here (ms remote tools again after adding devices)")
        save_devices(r["name"], devs)
    elif have["vivado"]:
        out("  ..  could not ask Vivado for its installed devices")
    nl = unlicensed(r)
    if nl:
        out(f"  ..  no license there (seen in failed jobs) for: {' '.join(sorted(nl))} — jobs for them run here")
    # what can be taken out of missing_tools
    miss = r["missing_tools"].split()
    alias = {"vitis": ("vitis-run", "v++"), "xsim": ("xelab",), "xvlog": ("xelab",), "xvhdl": ("xelab",),
             "xelab": ("xelab",), "vivado": ("vivado",), "vitis_hls": ("vitis_hls",), "vitis-run": ("vitis-run",),
             "v++": ("v++",)}
    can = [m for m in miss if m in alias and any(works.get(a) for a in alias[m])]
    if can:
        rest = [m for m in miss if m not in can]
        out(f"  ->  these work there now; take them out of missing_tools so jobs may be sent: {' '.join(can)}")
        out(f"       missing_tools = {' '.join(rest)}")
    out(f"  ..  full output: {STATE / (r['name'] + '-tools.txt')}")
    return ok["v"] and works.get("vivado", False)


LIC_TCL = r"""
read_verilog top.v
foreach p $parts {
  puts "L begin $p"
  if {[llength [get_parts -quiet $p]] == 0} { puts "L missing $p"; continue }
  catch {close_design -quiet}
  set rc [catch {synth_design -top top -part $p -mode out_of_context} e]
  puts "L msg $p [string map {"\n" " "} $e]"
  puts "L end $p $rc"
  catch {close_design -quiet}
}
exit
"""
# inside catch, Vivado prints only the warning (Common 17-348), not the final 17-345 error
LICENSE_FAIL_RX = re.compile(r"valid license was not found|Failed to get the license for feature|license feature \S+ could not be found",
                             re.I)


def license_parts(limit=20):
    """One full part name (with its speed grade) per device used by recent queue jobs."""
    from . import queue
    seen, out = set(), []
    for j in sorted(queue.all_jobs(), key=lambda j: -(j.get("created") or 0))[:300]:
        if not os.path.isdir(j.get("cwd") or ""):
            continue
        for p in sorted(job_parts(j)):
            if "-" not in p:                    # a bare device (xc7z020) is not a part synth_design accepts
                continue
            dev = _device(p)
            if dev not in seen:
                seen.add(dev)
                out.append(p)
    return out[:limit]


def licenses(name, parts=None, out=print):
    """Synthesize a two-flip-flop design for each part on a node: which devices have a license there."""
    try:
        r = get(name)
    except KeyError as e:
        out(f"  NO  configuration: {e}")
        return None
    parts = [p.lower() for p in (parts or license_parts())]
    if not parts:
        out("  ..  no parts found in your recent queue jobs: give them, e.g. ms remote licenses "
            f"{name} xc7vx330tffg1157-1 xc7z020clg400-1")
        return None
    out(f"  ..  checking {len(parts)} part(s) on {name} with a tiny synthesis each (about a minute per part) …")
    probe = ("cat > top.v <<'MSQ_V'\nmodule top(input clk, input d, output reg q); always @(posedge clk) q <= d; "
             "endmodule\nMSQ_V\n"
             f"cat > lic.tcl <<'MSQ_T'\nset parts {{{' '.join(parts)}}}\n{LIC_TCL}\nMSQ_T\n"
             "vivado -mode batch -nojournal -nolog -notrace -source lic.tcl > lic.log 2>&1; "
             "grep -E '^L |license' lic.log; echo probe=done > probe.txt")   # only what matters, not every synth line
    rc, log, d = _probe_job(r, probe, "remote_licenses", cores=2, mem_gb=4, timeout_s=len(parts) * 600 + 600)
    res = parse_licenses(log, parts)
    no = [p for p, v in res.items() if v == "no license"]
    yes = [p for p, v in res.items() if v == "licensed"]
    if no:
        save_unlicensed(r, [_device(p) for p in no])
    if yes:
        clear_unlicensed(r, [_device(p) for p in yes])
    for p in parts:
        v = res.get(p, "not checked (the job ended early)")
        mark = "ok " if v == "licensed" else "NO " if v == "no license" else ".. "
        out(f"  {mark} {p}: {v}")
    if no:
        out("       jobs for the parts without a license run here (a license server for the server lifts this: "
            "XILINXD_LICENSE_FILE=port@host in setup)")
    if not res:
        for line in log.splitlines()[-20:]:
            out("       " + line)
    return res


def _device(part):
    m = re.match(r"(x[ca][a-z]{0,3}\d+[a-z]*\d*[a-z]*?)(?=[a-z]{1,3}\d{3,4}|-|$)", part.lower())
    return m.group(1) if m else part.lower()


def parse_licenses(log, parts):
    """{part: licensed | no license | not installed there | failed (not a license problem)} from the probe log."""
    res, cur, buf = {}, None, []
    for line in log.splitlines():
        if line.startswith("L begin "):
            cur, buf = line.split()[-1], []
        elif line.startswith("L missing "):
            res[line.split()[-1]] = "not installed there"
            cur = None
        elif line.startswith("L end ") and cur:
            rc = line.split()[-1]
            text = "\n".join(buf)
            res[cur] = ("licensed" if rc == "0" else "no license" if LICENSE_FAIL_RX.search(text)
                        else "failed (not a license problem)")
            cur = None
        elif cur:
            buf.append(line)
    return {p: v for p, v in res.items() if p in parts}


def clear_unlicensed(r, devices_):
    f = STATE / f"{r['name']}-unlicensed.json"
    try:
        d = json.loads(f.read_text())
    except (OSError, ValueError):
        return
    for x in devices_:
        (d.get("devices") or {}).pop(x, None)
    try:
        f.write_text(json.dumps(d))
    except OSError:
        pass


def _tool_fix(lines):
    t = " ".join(lines).lower()
    if "locale" in t:
        return "a locale problem: update my-system (jobs now fall back to C.UTF-8) and try again"
    if "libtinfo" in t or "libncurses" in t:
        return "the node lacks libtinfo/libncurses: ask the admins (apt install libtinfo6 libncurses6),\nor put the .so in ~/ACCOUNT/lib and add it to LD_LIBRARY_PATH in setup"
    if "cannot open shared object" in t or "error while loading shared libraries" in t:
        return "a library is missing on the node (see the line above): ask the admins to install it"
    if "permission denied" in t:
        return "not executable on the share: copy again with rsync -rltp, or a mount with file_mode=0755"
    if "no such file" in t:
        return "a file of the installation is missing (the copy to the share may be incomplete: check rsync.log)"
    if "license" in t:
        return "a license problem: devices outside the free (Standard) edition need a license on the server"
    if "java" in t or "jre" in t:
        return "Java could not start: check free memory and /tmp space on the node"
    return "see the full output (path below)"


HELP = """ms remote — run queue jobs on a Slurm cluster (e.g. Titan) when this machine is busy or too small

  ms remote setup NAME        create config/remotes.conf with an example section NAME (then fill in your login)
  ms remote list              configured servers and their state
  ms remote check NAME        step-by-step check: ssh, Slurm, account, work folder, nodes
  ms remote test NAME         check + a 1-minute test job that reports what the nodes offer (CPUs, RAM, tools)
  ms remote licenses NAME [PART ...]   which devices have a license there (a tiny synthesis per part; default:
                              the parts your recent queue jobs use); jobs for unlicensed parts then stay here
  ms remote tools NAME        start Vivado/Vitis on a node exactly like a job (setup, links, path_map) and say
                              what is missing: settings, permissions, libraries, locale, HLS, devices
  ms remote status [NAME]     free CPUs there and your jobs
  ms remote usage [--days 3]  what your finished server jobs reserved and really used, and what to ask for
  ms remote clean NAME [--days 7]   remove your own old job folders there (kept after failed jobs)

On a shared server my-system only touches: its own folder (workdir, e.g. ~/jobs/msq/benyamin) on the head node,
its own temporary folder on the node while a job runs, and its own Slurm jobs (scancel --account=you JOBID).
It never deletes outside these, never builds or removes docker images, and sends at most max_jobs jobs (max_cores: reserved cores).
  ms q add --on NAME -- CMD   run this job there
  ms q add --on auto -- CMD   run here, or there when this machine is busy (offload = busy in remotes.conf)
  offload = prefer            every allowed job tries the server first; this machine when the server is full,
                              unreachable or slower; a job that fails there is run once more here
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
            print("no remotes configured (ms remote setup titan)")
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
    if sub == "tools":
        print(f"AMD tools on {a[1]}:")
        return 0 if tools_check(a[1]) else 1
    if sub == "licenses":
        print(f"Device licenses on {a[1]}:")
        res = licenses(a[1], a[2:] or None)
        return 0 if res is not None else 1
    if sub in ("check", "test"):
        print(f"{'Testing' if sub == 'test' else 'Checking'} remote {a[1]}:")
        return 0 if check(a[1], test_job=sub == "test") else 1
    if sub == "clean":
        r = get(a[1])
        days = float(a[a.index("--days") + 1]) if "--days" in a else 7
        w = safe_workdir(r)
        rc, out, err = ssh(r, f"cd {shlex.quote(w)} 2>/dev/null && find . -mindepth 1 -maxdepth 1 -type d "
                              f"-regextype posix-extended -regex './[0-9]+-[0-9]+' -mtime +{int(days)} -printf '%f\\n'", 60)
        old_dirs = [d for d in out.split() if _own_job_dir(r, f"{w}/{d}")]
        if not old_dirs:
            print(f"nothing to clean in {r['ssh']}:{w} (job folders older than {days:g} days)")
            return 0
        for d in old_dirs:
            ssh(r, f"rm -rf {shlex.quote(w + '/' + d)}", 120)
        print(f"removed {len(old_dirs)} old job folder(s) from {r['ssh']}:{w} (kept after failures): {', '.join(old_dirs[:20])}")
        return 0
    if sub == "usage":
        days = 3
        if "--days" in a and a.index("--days") + 1 < len(a) and a[a.index("--days") + 1].isdigit():
            days = int(a[a.index("--days") + 1])
        print(usage_text(days))
        return 0
    if sub == "status":
        if len(a) < 2:
            rs = list(remotes())
            if len(rs) != 1:
                print("usage: ms remote status NAME" + (f"   (servers: {', '.join(rs)})" if rs else ""))
                return 2
            a = [a[0], rs[0]]
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
