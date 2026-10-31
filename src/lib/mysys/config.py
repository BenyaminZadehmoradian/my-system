"""Configuration: one shell-compatible main file plus simple list files.

  config/my_system.conf      KEY="value" lines (also sourced by the shell scripts)
  config/<name>.txt|.conf    one entry per line, '#' comments, ~ and $VARS expanded

Everything has a sensible default, so a missing file never breaks anything.
"""
import os
import shlex
import socket
from pathlib import Path

HOME = Path.home()
MS = Path(os.environ.get("MY_SYSTEM", HOME / "My_System"))
CONF = MS / "config"
REPORTS = MS / "reports"
STATE = MS / "state"
PLUGINS = MS / "plugins"

DEFAULTS = {
    "MACHINE_LABEL": socket.gethostname(),
    "DATA_DISKS": "",            # space separated mount points, e.g. "/mnt/data"
    "NET_SHARES": "",            # space separated mount points of network shares
    "BIG_FOLDER_DEST": "",       # where "Move to HDD" puts folders, e.g. /mnt/data/$USER
    "SSD_WARN_PCT": "93",
    "FAST_FILL_GB": "5",
    "HOT_C": "90",
    "MIN_RUN": "60",
    "BATCH_QUIET": "300",
    "OVERLOAD_RATIO": "1.5",
    "OVERLOAD_MINUTES": "15",
    "REBOOT_HINT": "1",
    "REPORT_KEEP_DAYS": "60",
    "NTFY_SERVER": "https://ntfy.sh",
    # 1.6: pressure, fairness, safety
    "QUEUE_PSI_MEM_FULL": "10",     # % of time all tasks stalled on memory (avg 10 s) — no new jobs above this
    "QUEUE_PSI_IO_SOME": "80",      # % of time some task stalled on disk I/O — no new jobs above this
    "QUEUE_STARVE_MIN": "30",       # a job waiting this long for RAM/CPU gets room kept free for it
    "QUEUE_BACKFILL_MIN": "20",     # ... but jobs expected to finish within this many minutes may still use the room
    "QUEUE_PIN_CPUS": "0",          # 1 = give each job its own CPU cores (taskset)
    "QUEUE_OOM_SCORE_ADJ": "300",   # queue jobs are killed before your desktop / interactive tools if RAM runs out
    "QUEUE_SLOW_FACTOR": "2",       # tell me when a job runs this many times longer than usual
    "LOW_MEM_PCT": "5",             # urgent warning when available RAM drops below this
    "PSI_ALERT_MEM_FULL": "20",     # warning when the machine is thrashing (memory full pressure, % of time)
    "HISTORY_KEEP_DAYS": "14",      # per-minute record of load/RAM/top processes (ms replay)
    "AUDIT_KEEP_MONTHS": "12",
    "NOTIFY_LOG_MAX_MB": "5",
    "OSV_AUDIT": "1",               # weekly check of Python packages against the OSV vulnerability database
    "BACKUP_SPOT_FILES": "20",
    "BACKUP_COUNT_TOLERANCE_PCT": "10",
    "BACKUP_REPO_CHECK_DAYS": "90",
    "PHONE_REPORT_TIMES": "08:00 13:00 19:00",   # status report to the phone (ms brief); empty = only on request
    "PHONE_REQUESTS": "1",          # 1 = answer "status" typed in the ntfy app with a status report
    # dead-man's-switch URLs (healthchecks.io or compatible, optional): PING_WATCH PING_DAILY PING_BACKUP PING_VERIFY
}


def _expand(v):
    return os.path.expandvars(os.path.expanduser(v))


def load():
    conf = dict(DEFAULTS)
    lines = []
    for f in (CONF / "my_system.conf", CONF / "queue.conf"):
        try:
            lines += f.read_text().splitlines()
        except OSError:
            pass
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip().removeprefix("export ").strip()
        try:
            parts = shlex.split(v, comments=True)
            v = parts[0] if parts else ""
        except ValueError:
            v = v.strip().strip('"\'')
        conf[k] = _expand(v)
    return conf


def get(key, default=None):
    return load().get(key, default)


def num(key):
    try:
        return float(load().get(key, DEFAULTS.get(key, "0")))
    except ValueError:
        return float(DEFAULTS.get(key, "0"))


def words(key):
    return [w for w in load().get(key, "").split() if w]


def lst(name):
    """Entries of config/<name> (one per line), comments stripped, paths expanded."""
    f = CONF / name
    out = []
    try:
        for line in f.read_text().splitlines():
            line = line.split(" #")[0].strip()
            if line and not line.startswith("#"):
                out.append(_expand(line))
    except OSError:
        pass
    return out


def pairs(name):
    """label|path files."""
    out = []
    for e in lst(name):
        label, _, path = e.partition("|")
        if path:
            out.append((label.strip(), _expand(path.strip())))
    return out


def kv_file(path):
    out = {}
    try:
        for line in Path(path).read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip():
                out[k.strip()] = v.strip().strip('"')
    except OSError:
        pass
    return out


def backup_repo():
    """Repository path: config/backup.conf REPO, else state/backup_repo (set by backup_setup)."""
    r = kv_file(CONF / "backup.conf").get("REPO", "")
    if not r:
        try:
            r = (STATE / "backup_repo").read_text().strip()
        except OSError:
            r = ""
    return _expand(r)


def backup_running():
    """True while a backup holds state/backup.lock (works for runs started by any version)."""
    import fcntl
    try:
        with open(STATE / "backup.lock") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        pass
    return False


def backup_state():
    """(state, text) with state in ok | running | first | failed | stale | off."""
    import time
    b = kv_file(STATE / "backup_status")
    last = float(b.get("last_success", 0) or 0)
    if backup_running():
        try:
            start = (STATE / "backup.lock").stat().st_mtime
            since = time.strftime("%H:%M", time.localtime(start))
        except OSError:
            since = "?"
        first = "first backup " if not last else ""
        return "running", f"{first}running (since {since})"
    if b.get("last_result"):
        res = b["last_result"]
        if res.startswith("failed"):
            return "failed", res
        if last and time.time() - last > 48 * 3600:
            return "stale", f"last success {(time.time() - last) / 86400:.0f} days ago"
        return "ok", res
    if backup_repo():
        return "first", "configured, first backup not finished yet"
    return "off", "not set up (ms backup-setup)"


def ping(name, what=""):
    """Healthchecks-style ping: PING_<NAME> url [+ /start | /fail | /<exit code>]. Never raises, never blocks long."""
    url = (load().get(f"PING_{name.upper()}") or "").strip()
    if not url.startswith(("https://", "http://")):
        return False
    if what and what not in ("ok", "0"):
        url = url.rstrip("/") + "/" + str(what)
    try:
        import urllib.request
        urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "my-system"}), timeout=10).read(64)
        return True
    except Exception:
        return False
