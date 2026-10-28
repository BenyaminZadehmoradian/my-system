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
}


def _expand(v):
    return os.path.expandvars(os.path.expanduser(v))


def load():
    conf = dict(DEFAULTS)
    f = CONF / "my_system.conf"
    try:
        lines = f.read_text().splitlines()
    except OSError:
        lines = []
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
