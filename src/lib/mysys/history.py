"""Per-minute record of what the machine was doing (below/atop style "time travel") and a report after an
unexpected stop (power loss, freeze, out-of-memory hang).

  state/history/YYYY-MM-DD.jsonl   one sample per minute: load, RAM, swap, temperature, pressure,
                                   biggest processes (RAM, CPU %), tracked jobs, queue jobs
  state/lastseen.json              the newest sample + boot id (survives a crash)
  state/clean_stop.json            written by the queue service when systemd stops it cleanly
  reports/crashes.log              one line per unexpected stop

`ms replay "2026-10-31 07:30" [--span 10m]` shows the samples around that time.
"""
import gzip
import json
import os
import time
from pathlib import Path

from . import config, probes

DIR = config.STATE / "history"
LAST = config.STATE / "lastseen.json"
CLEAN = config.STATE / "clean_stop.json"
CRASHES = config.REPORTS / "crashes.log"
GiB = 2**30


def sample(s, tracked=(), now=None):
    """Build one sample. `s` is the watcher state (keeps CPU ticks between samples)."""
    now = now or time.time()
    m = probes.meminfo()
    ps = probes.psi()
    hz = os.sysconf("SC_CLK_TCK")
    prev = s.get("hist_ticks") or {}
    dt = max(1.0, now - (s.get("hist_t") or now - 60))
    top, ticks = [], {}
    for pid, name, rss, tk in probes.top_procs(10):
        ticks[str(pid)] = tk
        cpu = (tk - prev[str(pid)]) / hz / dt * 100 if str(pid) in prev else None
        top.append([name, round(rss / 2**20), None if cpu is None else round(cpu)])
    s["hist_ticks"], s["hist_t"] = ticks, now
    q = []
    try:
        from . import queue
        q = [f"#{j['id']} {j['name']}" for j in queue.all_jobs() if j["state"] in queue.ACTIVE]
    except Exception:
        pass
    return {"t": int(now), "load": round(probes.loadavg()[0], 1), "mem_avail_gb": round(m.get("MemAvailable", 0) / GiB, 1),
            "mem_total_gb": round(m.get("MemTotal", 0) / GiB, 1),
            "swap_used_gb": round((m.get("SwapTotal", 0) - m.get("SwapFree", 0)) / GiB, 1),
            "temp": probes.cpu_temp(), "psi_mem_full": ps.get("memory", {}).get("full"),
            "psi_io": ps.get("io", {}).get("some"), "psi_cpu": ps.get("cpu", {}).get("some"),
            "top": top, "jobs": list(tracked)[:12], "queue": q[:12]}


def record(s, tracked=()):
    now = time.time()
    if now - s.get("hist_last", 0) < 55:
        return None
    s["hist_last"] = now
    smp = sample(s, tracked, now)
    DIR.mkdir(parents=True, exist_ok=True)
    with open(DIR / time.strftime("%Y-%m-%d.jsonl", time.localtime(now)), "a") as f:
        f.write(json.dumps(smp) + "\n")
    tmp = LAST.with_suffix(".tmp")
    tmp.write_text(json.dumps({"boot_id": probes.boot_id(), "sample": smp}))
    tmp.replace(LAST)
    return smp


def housekeeping(keep_days=None):
    """gzip finished days, delete days older than HISTORY_KEEP_DAYS."""
    keep = keep_days if keep_days is not None else int(config.num("HISTORY_KEEP_DAYS") or 14)
    today = time.strftime("%Y-%m-%d")
    for f in sorted(DIR.glob("*.jsonl*")):
        day = f.name[:10]
        try:
            age = (time.time() - time.mktime(time.strptime(day, "%Y-%m-%d"))) / 86400
        except ValueError:
            continue
        if age > keep + 1:
            f.unlink(missing_ok=True)
        elif f.suffix == ".jsonl" and day != today:
            with open(f, "rb") as a, gzip.open(str(f) + ".gz", "wb") as b:
                b.write(a.read())
            f.unlink()


def _read_day(day):
    out = []
    for f in (DIR / f"{day}.jsonl", DIR / f"{day}.jsonl.gz"):
        try:
            op = gzip.open if f.suffix == ".gz" else open
            with op(f, "rt") as fh:
                for line in fh:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass
    return out


def around(t, span_s=600):
    days = {time.strftime("%Y-%m-%d", time.localtime(x)) for x in (t - span_s, t, t + span_s)}
    rows = [r for d in sorted(days) for r in _read_day(d)]
    return [r for r in rows if t - span_s <= r["t"] <= t + span_s]


def parse_time(txt):
    txt = txt.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return time.mktime(time.strptime(txt, fmt))
        except ValueError:
            pass
    if len(txt) == 5 and txt[2] == ":":
        lt = time.localtime()
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, int(txt[:2]), int(txt[3:]), 0, 0, 0, -1))
        return t if t <= time.time() else t - 86400
    if txt.endswith(("m", "h")) and txt[:-1].isdigit():
        return time.time() - int(txt[:-1]) * (60 if txt.endswith("m") else 3600)
    raise ValueError(f"bad time {txt!r} (use '2026-10-31 07:30', '07:30' or '90m' for 90 minutes ago)")


def fmt(r, wide=True):
    ts = time.strftime("%m-%d %H:%M", time.localtime(r["t"]))
    temp = f"{r['temp']}°C" if r.get("temp") is not None else "–"
    psi = f" memstall {r['psi_mem_full']:.0f}%" if r.get("psi_mem_full") else ""
    line = (f"{ts}  load {r['load']:>5}  RAM free {r['mem_avail_gb']:>5} GB  swap {r['swap_used_gb']} GB  {temp}{psi}")
    if wide:
        top = ", ".join(f"{n} {mb / 1024:.1f}G" + (f" {c}%" if c is not None else "") for n, mb, c in r.get("top", [])[:5])
        line += f"\n            top: {top}"
        if r.get("queue") or r.get("jobs"):
            line += "\n            jobs: " + ", ".join((r.get("queue") or []) + (r.get("jobs") or []))
    return line


# ---------------------------------------------------------------- unexpected stop
def _clean_stops():
    """{boot_id: time of its clean stop} for the last few boots."""
    try:
        d = json.loads(CLEAN.read_text())
    except (OSError, ValueError):
        return {}
    stops = dict(d.get("stops") or {})
    if d.get("boot_id") and d["boot_id"] not in stops:        # file written by versions before 1.22
        stops[d["boot_id"]] = d.get("t", 0)
    return stops


def mark_clean_stop():
    """Called when the queue service is stopped by systemd (shutdown, reboot, logout). One entry per boot: two
    reboots within a minute (e.g. into the BIOS and back) must not hide that the first one was clean."""
    stops = _clean_stops()
    stops[probes.boot_id()] = time.time()
    stops = dict(sorted(stops.items(), key=lambda kv: kv[1])[-8:])
    tmp = CLEAN.with_suffix(".tmp")
    tmp.write_text(json.dumps({"t": time.time(), "boot_id": probes.boot_id(), "stops": stops}))
    tmp.replace(CLEAN)


def check_unclean(notify):
    """Run once per watcher tick: if the previous boot ended without a clean stop, report what was going on."""
    try:
        last = json.loads(LAST.read_text())
    except (OSError, ValueError):
        return None
    cur = probes.boot_id()
    if not last.get("boot_id") or last["boot_id"] == cur:
        return None
    smp = last.get("sample") or {}
    stops = _clean_stops()
    # mark as handled first, so a failure below never repeats the report
    LAST.write_text(json.dumps({"boot_id": cur, "sample": smp, "handled_prev": last["boot_id"]}))
    # clean only if the stop came at the very end (a restart of the service earlier that day does not count)
    if stops.get(last["boot_id"], 0) >= smp.get("t", 0) - 120:
        return {"clean": True}
    try:
        from . import queue
        d = json.loads(queue.DAEMON.read_text())
        daemon_ran = abs(d.get("t", 0) - smp.get("t", 0)) < 600
    except (OSError, ValueError, ImportError):
        daemon_ran = False
    if not daemon_ran:
        return {"clean": None}            # cannot tell (the queue service was not running then)
    t = smp.get("t", 0)
    cause = "power loss, a hard freeze or a forced power-off"
    if smp.get("mem_total_gb") and smp.get("mem_avail_gb", 99) < smp["mem_total_gb"] * 0.03:
        cause = "most likely the machine ran out of memory and froze"
    elif smp.get("psi_mem_full") and smp["psi_mem_full"] > 30:
        cause = "most likely memory thrashing (the machine was stalled waiting for memory)"
    elif smp.get("temp") and smp["temp"] >= float(config.num("HOT_C") or 90):
        cause = "possibly overheating"
    what = ", ".join((smp.get("queue") or []) + (smp.get("jobs") or [])) or "no tracked job"
    msg = (f"Last sign of life {time.strftime('%Y-%m-%d %H:%M', time.localtime(t))}: {cause}. "
           f"Running then: {what}. RAM free {smp.get('mem_avail_gb')} GB, load {smp.get('load')}, "
           f"CPU {smp.get('temp')}°C. Details: ms replay '{time.strftime('%Y-%m-%d %H:%M', time.localtime(t))}'")
    config.REPORTS.mkdir(parents=True, exist_ok=True)
    with open(CRASHES, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(t))}\t{cause}\t{what}\n")
    notify("The computer stopped unexpectedly", msg, "high", "warning")
    return {"clean": False, "cause": cause, "t": t, "msg": msg}


def cli(a):
    if not a or a[0] in ("-h", "--help"):
        print("usage: ms replay TIME [--span 10m] [--brief]   TIME: '2026-10-31 07:30', '07:30', '90m' (ago)\n"
              "       ms replay crashes                         unexpected stops recorded so far")
        return 0 if a else 2
    if a[0] == "crashes":
        try:
            print(CRASHES.read_text().rstrip() or "none recorded")
        except OSError:
            print("none recorded")
        return 0
    span = 600
    if "--span" in a:
        v = a[a.index("--span") + 1]
        span = int(v[:-1]) * (3600 if v.endswith("h") else 60) if v[-1] in "mh" else int(v)
        a = [x for x in a if x not in ("--span", v)]
    brief = "--brief" in a
    a = [x for x in a if x != "--brief"]
    try:
        t = parse_time(" ".join(a))
    except ValueError as e:
        print(e)
        return 2
    rows = around(t, span)
    if not rows:
        print("no samples recorded for that time (history keeps "
              f"{int(config.num('HISTORY_KEEP_DAYS') or 14)} days and starts with version 1.6)")
        return 1
    for r in rows:
        print(fmt(r, not brief))
    return 0
