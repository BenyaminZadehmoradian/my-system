"""Named resources, tool caps, reservations and machine-pressure signals for the queue.

config/resources.conf (one per line, '#' comments):

    vivado_license 4                          # 4 seats; a job asks with resources: {vivado_license: 1}
    vivado_license 4 check=lmutil lmstat -c 2100@lic -f Vivado_System_Edition
                                              # seats used outside the queue are read from lmstat
    board_zcu104 1                            # count 1 = an exclusive lock (board, JTAG cable)
    disk_io 2                                 # any counting semaphore
    tool:vivado 4                             # at most 4 queued Vivado jobs at once, whatever RAM is free

config/reservations.conf:

    Mon-Fri 09:00-12:00 keep_threads=12       # leave 12 threads free in this window
    Sat 00:00-23:59 no_new_jobs               # start nothing new
"""
import fcntl
import os
import re
import subprocess
import time
from pathlib import Path

from . import config

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def load():
    """{name: {"count": N, "check": cmd|None}} plus {"tool:x": ...} caps."""
    out = {}
    for line in config.lst("resources.conf"):
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        name = parts[0]
        try:
            n = int(parts[1])
        except ValueError:
            continue
        chk = None
        if len(parts) > 2 and parts[2].startswith("check="):
            chk = parts[2][6:]
        out[name] = {"count": n, "check": chk}
    return out


_ext_cache = {}


def external_use(name, cmd):
    """Seats in use outside the queue, from an lmstat-like command (cached 60 s)."""
    now = time.time()
    c = _ext_cache.get(name)
    if c and now - c[0] < 60:
        return c[1]
    used = 0
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, errors="replace", timeout=30).stdout
        m = re.search(r"Total of (\d+) licenses? in use", out)
        if m:
            used = int(m.group(1))
        else:
            nums = re.findall(r"^\s*(\d+)\s*$", out, re.M)
            used = int(nums[-1]) if nums else 0
    except (OSError, subprocess.SubprocessError, ValueError):
        used = 0
    _ext_cache[name] = (now, used)
    return used


def in_use(jobs):
    """{resource: units held by running/suspended queue jobs} (licenses stay held while suspended)."""
    use = {}
    for j in jobs:
        if j["state"] in ("running", "suspended") and not j.get("remote"):   # server jobs use the server's
            for k, v in (j.get("resources") or {}).items():
                use[k] = use.get(k, 0) + int(v)
            if j.get("tool"):
                use[f"tool:{j['tool']}"] = use.get(f"tool:{j['tool']}", 0) + 1
    return use


def fits(job, jobs, res=None):
    """(ok, reason) for named resources and tool caps."""
    res = res if res is not None else load()
    used = in_use(jobs)
    own_queue_use = {}
    for k, v in (job.get("resources") or {}).items():
        own_queue_use[k] = int(v)
    if job.get("tool"):
        own_queue_use[f"tool:{job['tool']}"] = 1
    for k, need in own_queue_use.items():
        if k not in res:
            if k.startswith("tool:"):
                continue
            return False, f"unknown resource '{k}' (define it in config/resources.conf)"
        cap = res[k]["count"]
        ext = external_use(k, res[k]["check"]) if res[k].get("check") else 0
        if ext:
            ext = max(0, ext - used.get(k, 0))        # our own jobs also appear in lmstat
        if used.get(k, 0) + ext + need > cap:
            what = "tool limit" if k.startswith("tool:") else "resource"
            return False, f"waiting for {what} {k} ({used.get(k, 0) + ext}/{cap} in use)"
    return True, ""


# ---------------------------------------------------------------- slots ($MSQ_SLOT)
def free_slot(jobs):
    taken = {j.get("slot") for j in jobs if j["state"] in ("running", "suspended")}
    n = 1
    while n in taken:
        n += 1
    return n


# ---------------------------------------------------------------- semaphores (ms sem)
SEM = config.STATE / "sem"


def sem_acquire(name, count=1, wait=True):
    """Hold one of `count` slots of semaphore `name`; returns an open file (keep it open) or None."""
    SEM.mkdir(parents=True, exist_ok=True)
    while True:
        for i in range(max(1, count)):
            f = open(SEM / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', name)}.{i}", "a")
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return f
            except OSError:
                f.close()
        if not wait:
            return None
        time.sleep(1)


# ---------------------------------------------------------------- reservations
def _parse_days(s):
    out = set()
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-")
            ia, ib = DAYS.index(a[:3].title()), DAYS.index(b[:3].title())
            rng = range(ia, ib + 1) if ia <= ib else list(range(ia, 7)) + list(range(0, ib + 1))
            out |= set(rng)
        elif part[:3].title() in DAYS:
            out.add(DAYS.index(part[:3].title()))
        elif part in ("*", "all", "daily"):
            out |= set(range(7))
    return out


def reservations():
    out = []
    for line in config.lst("reservations.conf"):
        p = line.split()
        if len(p) < 2:
            continue
        try:
            days = _parse_days(p[0])
            a, b = p[1].split("-")
            ha, ma = map(int, a.split(":"))
            hb, mb = map(int, b.split(":"))
        except (ValueError, IndexError):
            continue
        opts = {"keep_threads": None, "no_new_jobs": False}
        for o in p[2:]:
            if o == "no_new_jobs":
                opts["no_new_jobs"] = True
            elif o.startswith("keep_threads="):
                opts["keep_threads"] = int(o.split("=")[1])
        out.append({"days": days, "start": ha * 60 + ma, "end": hb * 60 + mb, **opts, "text": line})
    return out


def active_reservation(t=None):
    t = time.localtime(t or time.time())
    m = t.tm_hour * 60 + t.tm_min
    for r in reservations():
        if t.tm_wday in r["days"] and r["start"] <= m < r["end"]:
            return r
    return None


def minutes_to_next_reservation(t=None):
    """Minutes until the next reservation window starts (within 7 days), or None."""
    now = t or time.time()
    best = None
    for r in reservations():
        for d in range(8):
            day = time.localtime(now + d * 86400)
            if day.tm_wday not in r["days"]:
                continue
            start = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, r["start"] // 60, r["start"] % 60, 0, 0, 0, -1))
            if start > now:
                mins = (start - now) / 60
                best = mins if best is None else min(best, mins)
                break
    return best


# ---------------------------------------------------------------- swapping / disk pressure
_prev = {}


def swapping():
    """True when the machine is swapping both in and out right now (between two calls)."""
    v = {}
    try:
        for line in open("/proc/vmstat"):
            k, val = line.split()
            if k in ("pswpin", "pswpout"):
                v[k] = int(val)
    except (OSError, ValueError):
        return False
    old = _prev.get("swap")
    _prev["swap"] = (time.time(), v)
    if not old or time.time() - old[0] > 120:
        return False
    return v.get("pswpin", 0) - old[1].get("pswpin", 0) > 200 and v.get("pswpout", 0) - old[1].get("pswpout", 0) > 200


def disk_busy(path="/", threshold=90):
    """Percentage of time the disk holding `path` was busy since the previous call (or None)."""
    try:
        dev = os.stat(path).st_dev
        major, minor = os.major(dev), os.minor(dev)
        name = Path(f"/sys/dev/block/{major}:{minor}").resolve().name
        if Path(f"/sys/class/block/{name}/partition").exists():
            name = Path(f"/sys/class/block/{name}").resolve().parent.name
        ticks = None
        for line in open("/proc/diskstats"):
            p = line.split()
            if p[2] == name:
                ticks = int(p[12])
                break
    except (OSError, ValueError, IndexError):
        return None
    if ticks is None:
        return None
    key = f"disk:{name}"
    old = _prev.get(key)
    _prev[key] = (time.time(), ticks)
    if not old:
        return None
    dt = (time.time() - old[0]) * 1000
    return min(100.0, (ticks - old[1]) / dt * 100) if dt > 0 else None
