"""Auto-detection of what is on this machine.

Used twice:
  * at install time to write sensible starting config files (only files that do not exist yet)
  * every day (`ms detect --state`) to refresh state/detected.json, which feeds the AI
    system map, the health check and the GUI — so new tools, venvs and projects show up
    without editing anything.
"""
import glob
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import config, probes

HOME = config.HOME
LOCAL_FS = {"ext4", "ext3", "xfs", "btrfs", "zfs", "f2fs", "ntfs", "ntfs3", "exfat", "fuseblk"}
NET_FS = {"cifs", "smb3", "nfs", "nfs4", "fuse.sshfs", "fuse.rclone", "9p"}
SKIP_DIRS = {"snap", "node_modules", ".cache", ".git", "__pycache__", ".local", ".npm", ".nuget", ".dotnet",
             ".vscode", ".mozilla", ".java", ".pyenv", ".conda", "site-packages", ".Xil", ".venv", "venv"}


# ---------------- disks ----------------
def data_disks(min_gb=100):
    out = []
    for dev, mp, fs in probes.mounts():
        if fs not in LOCAL_FS or not dev.startswith("/dev/"):
            continue
        if mp == "/" or mp.startswith(("/boot", "/snap", "/var/snap", "/run/", "/proc", "/sys")):
            continue
        d = probes.disk(mp)
        if d and d[2] >= min_gb * 2**30 and mp not in out:
            out.append(mp)
    return out


def net_shares():
    return [mp for _, mp, fs in probes.mounts() if fs in NET_FS and not mp.startswith("/run/user")]


# ---------------- tools ----------------
def _ver(path):
    m = re.search(r"(20\d\d\.\d(?:\.\d)?|R20\d\d[ab]|\d+\.\d+(?:\.\d+)?)", path)
    return m.group(1) if m else ""


def tool_installs(roots=None, docker=True):
    """[(label, root)] of large toolchains found in the usual places."""
    roots = roots or ["/opt", "/tools", str(HOME), "/usr/local"] + data_disks()
    found = {}

    def add(label, root):
        root = os.path.realpath(root)
        if os.path.isdir(root) and root not in found.values():
            found.setdefault(label, root)

    pats = {
        "vivado": ["{r}/Xilinx/Vivado/*/bin/vivado", "{r}/Xilinx/*/Vivado/bin/vivado", "{r}/Xilinx/Vivado/bin/vivado",
                   "{r}/*/*/Vivado/bin/vivado", "{r}/*/Vivado/bin/vivado", "{r}/AMD/*/Vivado/bin/vivado"],
        "vitis_hls": ["{r}/Xilinx/Vitis_HLS/*/bin/vitis_hls", "{r}/Xilinx/*/Vitis_HLS/bin/vitis_hls",
                      "{r}/Xilinx/Vitis_HLS/bin/vitis_hls", "{r}/*/*/Vitis/bin/vitis-run", "{r}/Xilinx/*/Vitis/bin/vitis-run"],
        "ise": ["{r}/Xilinx/*/ISE_DS"],
        "xilinx_sdk": ["{r}/Xilinx/SDK/*"],
        "quartus": ["{r}/intelFPGA*/*/quartus/bin/quartus", "{r}/altera*/*/quartus/bin/quartus"],
        "matlab": ["{r}/MATLAB/R20*/bin/matlab", "{r}/MATLAB/bin/matlab"],
        "oss-cad-suite": ["{r}/.local/share/oss-cad-suite/bin/yosys", "{r}/oss-cad-suite/bin/yosys"],
        "cadence": ["{r}/cadence"], "synopsys": ["{r}/synopsys"], "mentor": ["{r}/mentor"],
        "conda": ["{r}/miniconda3/bin/conda", "{r}/anaconda3/bin/conda", "{r}/miniforge3/bin/conda", "{r}/conda/bin/conda"],
        "cuda": ["{r}/cuda*/bin/nvcc"],
    }
    for label, plist in pats.items():
        for pat in plist:
            for r in roots:
                for hit in sorted(glob.glob(pat.format(r=r))):
                    root = Path(hit)
                    # walk up to the install root: strip "bin/<exe>", then a tool-named folder
                    if root.parent.name == "bin":
                        root = root.parent.parent
                    if root.name in ("Vivado", "Vitis", "Vitis_HLS", "ISE_DS", "quartus"):
                        root = root.parent
                    root = str(root)
                    v = _ver(hit)
                    add(f"{label}_{v}" if v else label, root)
    # docker data root
    try:
        if not docker:
            raise OSError
        r = subprocess.run(["docker", "info", "--format", "{{.DockerRootDir}}"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            add("docker_data", r.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return sorted(found.items())


# ---------------- python environments ----------------
def venvs():
    cands = set()
    for pat in ["~/.venvs/*", "~/.virtualenvs/*", "~/*", "~/*/*", "~/*/*/*"]:
        for d in glob.glob(os.path.expanduser(pat)):
            if os.path.isfile(os.path.join(d, "pyvenv.cfg")):
                cands.add(d)
    out = []
    for d in sorted(cands):
        ver = ""
        try:
            for line in Path(d, "pyvenv.cfg").read_text().splitlines():
                if line.startswith(("version", "version_info")):
                    ver = line.split("=", 1)[1].strip()
        except OSError:
            pass
        out.append({"path": d, "python": ver})
    for envs in ["~/miniconda3/envs/*", "~/anaconda3/envs/*", "~/miniforge3/envs/*"]:
        for d in glob.glob(os.path.expanduser(envs)):
            out.append({"path": d, "python": "conda"})
    return out


# ---------------- projects ----------------
def git_repos(max_depth=3):
    roots = [str(HOME)] + [os.path.join(d, os.environ.get("USER", "")) for d in data_disks()]
    repos = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        base_depth = root.rstrip("/").count("/")
        for dirpath, dirnames, _ in os.walk(root):
            depth = dirpath.count("/") - base_depth
            if ".git" in dirnames:
                repos.append(dirpath)
                dirnames[:] = []          # do not descend into a repo
                continue
            if depth >= max_depth:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
                           and not os.path.islink(os.path.join(dirpath, d))]
    return sorted(set(repos))


# ---------------- assemble ----------------
def detect_all():
    return {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "hostname": probes.hostname(),
        "product": probes.product(),
        "os": probes.os_name(),
        "kernel": os.uname().release,
        "threads": probes.threads(),
        "mem_total": probes.meminfo().get("MemTotal", 0),
        "data_disks": data_disks(),
        "net_shares": net_shares(),
        "tools": tool_installs(),
        "venvs": venvs(),
        "projects": git_repos(),
    }


def write_state():
    d = detect_all()
    config.STATE.mkdir(parents=True, exist_ok=True)
    tmp = config.STATE / "detected.json.tmp"
    tmp.write_text(json.dumps(d, indent=1))
    tmp.replace(config.STATE / "detected.json")
    return d


def load_state():
    try:
        return json.loads((config.STATE / "detected.json").read_text())
    except (OSError, ValueError):
        return {}


# ---------------- initial config files ----------------
GENERIC_PROTECTED = ["/etc", "/opt", "/tools", "/usr", "/boot", "/var/lib", "~/.bashrc", "~/.profile",
                     "~/.bash_profile", "~/.zshrc", "~/.ssh", "~/.claude", "~/.config/systemd", "~/.gnupg"]
WATCH_CANDIDATES = ["~/.bashrc", "~/.profile", "~/.bash_profile", "~/.zshrc", "~/.ssh/authorized_keys",
                    "/etc/fstab", "/etc/hosts", "/etc/crontab", "/etc/docker/daemon.json",
                    "/etc/systemd/zram-generator.conf", "/etc/claude-code/managed-settings.json",
                    "~/.claude/CLAUDE.md", "~/.claude/settings.json"]


def _home_rel(p):
    p = str(p)
    return "~" + p[len(str(HOME)):] if p.startswith(str(HOME) + "/") or p == str(HOME) else p


def initial_files(d):
    user = os.environ.get("USER", "me")
    disks, shares = d["data_disks"], d["net_shares"]
    dest = f"{disks[0]}/{user}" if disks else ""
    files = {}
    files["my_system.conf"] = f"""# my-system main settings (shell syntax; sourced by the scripts, read by the app).
# Edit: ms unlock  ->  edit  ->  ms lock
MACHINE_LABEL="{d['hostname']}"
# Big local data disks and network shares to monitor (space separated mount points)
DATA_DISKS="{' '.join(disks)}"
NET_SHARES="{' '.join(shares)}"
# Where "Move to HDD" puts folders from the SSD (empty = feature off)
BIG_FOLDER_DEST="{dest}"
# Alerts
SSD_WARN_PCT=93          # root disk usage alert (%)
FAST_FILL_GB=5           # alert if root disk loses this much in one minute
HOT_C=90                 # CPU temperature alert
OVERLOAD_RATIO=1.5       # load / CPU threads considered overloaded ...
OVERLOAD_MINUTES=15      # ... for this many minutes
REBOOT_HINT=1            # tell me when a reboot is pending and nothing is running
# Run tracking
MIN_RUN=60               # ignore jobs shorter than this (seconds)
BATCH_QUIET=300          # summarise a batch folder after it has been idle this long
REPORT_KEEP_DAYS=60
NTFY_SERVER="https://ntfy.sh"
PHONE_REPORT_TIMES="08:00 13:00 19:00"   # status report to the phone; empty = only when you type "status" in ntfy
LOW_MEM_PCT=5            # urgent warning when available RAM drops below this (%)
HISTORY_KEEP_DAYS=14     # per-minute record for "ms replay" (what was running at a given time)
# Dead-man's switch (optional): a healthchecks.io-style URL per task; you are alerted when pings STOP,
# e.g. when the computer froze or lost power. Leave empty to disable.
#PING_WATCH="https://hc-ping.com/your-uuid"     # every 5 minutes while the computer runs
#PING_DAILY=""                                   # daily check (start / ok / fail)
#PING_BACKUP=""                                  # each backup (start / ok / exit code)
#PING_VERIFY=""                                  # monthly restore test
"""
    prot = list(GENERIC_PROTECTED) + [_home_rel(r) for _, r in d["tools"]] + \
        [_home_rel(v["path"]) for v in d["venvs"]] + ["~/My_System"]
    files["protected_paths.txt"] = "# AI assistants may not write, move or delete anything under these paths.\n" + \
        "\n".join(dict.fromkeys(prot)) + "\n"
    files["watched_files.conf"] = "# Files fingerprinted daily; any change is reported (accept with: ms accept)\n" + \
        "\n".join(w for w in WATCH_CANDIDATES if os.path.exists(os.path.expanduser(w))) + "\n"
    files["size_folders.txt"] = ("# Folders whose size is tracked daily. AUTO = every top-level folder in home\n"
                                 "# plus every top-level folder on the data disks.\nAUTO\n")
    src = [_home_rel(p) for p in d["projects"]]
    if os.path.isdir(HOME / "Documents"):
        src.append("~/Documents")
    files["backup_sources.txt"] = "# Folders to back up (one per line). Detected git projects + Documents.\n" + \
        "\n".join(dict.fromkeys(src)) + "\n"
    files["backup.conf"] = """# Backup settings. REPO is chosen during `ms backup-setup` (stored in state/backup_repo).
REPO=""
KEYRING_ATTR="service my-system-borg"
COMPRESSION="zstd,6"
KEEP_DAILY=7
KEEP_WEEKLY=4
KEEP_MONTHLY=6
"""
    files["paths.conf"] = "# Extra known paths shown to AI assistants: label|path (tools are detected automatically)\n"
    files["plugins.conf"] = ("# Run-tracking plugins (one per line). Available: see ~/My_System/plugins/\n"
                             "vivado\nvitis_hls\nyosys\nsimulators\n# make\n")
    files["queue.conf"] = """# Job queue settings (also read from my_system.conf). Remove the '#' to change a value.
# QUEUE_MAX_JOBS=16            # never more queued jobs running at once
# QUEUE_KEEP_THREADS=1         # threads kept free for the desktop
# QUEUE_START_DELAY=0          # seconds between two job starts (raised automatically when starts fail quickly)
# QUEUE_DISK_BUSY_PCT=95       # do not start new jobs while the data disk is busier than this
# QUEUE_MEM_SETTLE_MIN=20      # after this many minutes, a job's reservation shrinks towards its real peak
# QUEUE_PREEMPT_EXEMPT_MIN=10  # a job is never paused for another during its first minutes
# QUEUE_KEEP_DAYS=30           # forget finished jobs after this many days
# QUEUE_LOG_MAX_MB=200         # job output above this is rotated
# SCRATCH_DIR=                 # where scratch folders and job TMPDIRs go (default: the data folder)
# SHARED_CACHE_DIR=            # cache shared between flows (default: <data folder>/.ms_cache)
# QUEUE_PSI_MEM_FULL=10        # no new jobs while all programs wait for memory this % of the time
# QUEUE_PSI_IO_SOME=80         # no new jobs while disk I/O stalls this % of the time
# QUEUE_STARVE_MIN=30          # a big job waiting this long gets room kept free (smaller jobs cannot overtake it forever)
# QUEUE_BACKFILL_MIN=20        # ... except jobs that usually finish within this many minutes
# QUEUE_CPU_PATIENCE_MIN=60    # waited this long for cores (RAM free): start once half of its cores are free
# QUEUE_STUCK_NOTIFY_H=2       # tell me when a job has waited this many hours, and what would let it start
# QUEUE_PIN_CPUS=0             # 1 = each job gets its own physical cores (taskset)
# XILINX_ALLOWED="2025.2"      # only this Vivado/Vitis version (and 2025.2.1 etc.) for queue jobs and AI
# QUEUE_OOM_SCORE_ADJ=300      # if RAM runs out, the kernel kills queue jobs before your desktop
# QUEUE_SLOW_FACTOR=2          # notify when a job runs this many times longer than usual
"""
    from . import remote as _rm
    files["remotes.conf"] = _rm.TEMPLATE
    files["groups.conf"] = """# Job groups: NAME LIMIT — at most LIMIT jobs of the group run at once (ms q add --group NAME, flow key group:)
# vivado_impl 3
# sims 6
"""
    files["resources.conf"] = """# Named resources for the job queue: NAME COUNT [check=COMMAND]
# A job asks with --res NAME=1 (or resources: {NAME: 1} in a flow). Count 1 = an exclusive lock.
# check= reads seats used outside the queue, e.g. from FlexLM:
# vivado_license 4 check=lmutil lmstat -c 2100@licserver -f Synthesis
# board_zcu104 1
# jtag_cable 1
# disk_io 2
# tool:vivado 8                 # at most 8 queued Vivado jobs at once
"""
    files["reservations.conf"] = """# Time windows when the queue holds back (e.g. you need the machine):
# DAYS HH:MM-HH:MM [keep_threads=N | no_new_jobs]
# Mon-Fri 09:00-12:00 keep_threads=12
# Sat 00:00-23:59 no_new_jobs
"""
    files["warnings_ignore.txt"] = """# Warnings to ignore when counting new/fixed issues (one regular expression per line)
# \\[Synth 8-7080\\]
# \\[Constraints 18-5210\\]
"""
    files["failure_kb.yaml"] = """# Known failure causes (checked before the built-in ones). Example:
# - name: "IP not upgraded"
#   pattern: '\\[IP_Flow 19-2162\\]'   # single quotes keep the backslashes
#   fix: "Run upgrade_ip [get_ips] once in the GUI or Tcl."
[]
"""
    return files


def write_initial(conf_dir=None, overwrite=False):
    conf_dir = Path(conf_dir or config.CONF)
    conf_dir.mkdir(parents=True, exist_ok=True)
    d = detect_all()
    written = []
    for name, text in initial_files(d).items():
        f = conf_dir / name
        if f.exists() and not overwrite:
            continue
        f.write_text(text)
        written.append(name)
    return d, written


def summary(d):
    lines = [f"Machine:   {d['hostname']} · {d['product']} · {d['os']} · {d['threads']} threads · "
             f"{d['mem_total'] / 2**30:.0f} GB RAM",
             f"Data disks: {', '.join(d['data_disks']) or 'none'}",
             f"Shares:    {', '.join(d['net_shares']) or 'none'}",
             f"Toolchains: {', '.join(f'{l} ({p})' for l, p in d['tools']) or 'none'}",
             f"Venvs:     {len(d['venvs'])} · Git projects: {len(d['projects'])}"]
    return "\n".join(lines)
