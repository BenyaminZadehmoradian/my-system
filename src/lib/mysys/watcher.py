"""The one-minute watcher (`ms watch`). Reads the system, writes only into ~/My_System.

Per tick:
  * root disk: fast-fill and nearly-full alerts
  * CPU temperature alert
  * long jobs (via plugins): start/finish tracking, per-job CPU use, error alerts at once,
    one summary per batch folder, results extraction to reports/results.csv
  * CPU overload advice (load/threads above OVERLOAD_RATIO for OVERLOAD_MINUTES)
  * "good moment to reboot" when an update needs a reboot and nothing has run for 10 min
  * watchdog: daily report / backup stopped happening
  * hourly sample to reports/metrics.csv
"""
import csv
import json
import math
import os
import subprocess
import time
from pathlib import Path

from . import config, plugins, probes

STATE_FILE = config.STATE / "watch.json"
RUNS = config.REPORTS / "runs.jsonl"
RESULTS = config.REPORTS / "results.csv"
METRICS = config.REPORTS / "metrics.csv"
VERSION = 3


# ---------------- helpers ----------------
def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(s):
    config.STATE.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s))
    tmp.replace(STATE_FILE)


def notify(title, msg, prio="default", tag="computer"):
    subprocess.run([str(config.MS / "bin" / "notify.sh"), title, msg, prio, tag],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def rate_ok(s, key, seconds):
    a = s.setdefault("alerts", {})
    if time.time() - a.get(key, 0) >= seconds:
        a[key] = time.time()
        return True
    return False


def hms(sec):
    sec = int(sec)
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m"


def short(path, n=3):
    return "/".join(Path(path).parts[-n:])


# ---------------- jobs ----------------
def jobs():
    """Long-running jobs recognised by a plugin, one entry per job (helpers merged)."""
    found = []
    for p in probes.processes():
        plug, tool = plugins.match(p["argv"])
        if not plug:
            continue
        p["tool"], p["plugin"] = tool, plug.NAME
        found.append(p)
    merged = []
    for r in sorted(found, key=lambda r: r["start"]):
        twin = next((j for j in merged if j["tool"] == r["tool"] and j["cwd"] == r["cwd"]
                     and abs(j["start"] - r["start"]) <= 30), None)
        if twin:
            twin["rss"] += r["rss"]
            twin["cpu_ticks"] += r["cpu_ticks"]
            twin["pids"].append(r["pid"])
        else:
            r["pids"] = [r["pid"]]
            merged.append(r)
    for j in merged:
        j["key"] = f"{j['tool']}|{j['cwd']}|{int(j['start'])}"
    return merged


def plugin_by_name(name):
    return next((p for p in plugins.load() if p.NAME == name), None)


def record_result(r, dur, summ, status):
    plug = plugin_by_name(r.get("plugin", ""))
    res = {}
    if plug and hasattr(plug, "results"):
        try:
            res = plug.results(r["cwd"], r["tool"]) or {}
        except Exception:
            res = {}
    row = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "tool": r["tool"], "batch": Path(r["cwd"]).parent.name,
           "run": Path(r["cwd"]).name, "folder": r["cwd"], "status": status, "seconds": int(dur),
           "errors": summ.get("errors") if summ else "", "critical": summ.get("critical") if summ else ""}
    row.update({k: (round(v, 4) if isinstance(v, float) else v) for k, v in res.items()})
    new = not RESULTS.exists()
    with open(RESULTS, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=plugins.RESULT_COLUMNS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)
    return res


def track_jobs(s, now, conf):
    cur = jobs()
    seen = {j["key"]: j for j in cur}
    if s.get("v") != VERSION:              # older state format: adopt running jobs silently
        s.clear()
        s["v"] = VERSION
        s["runs"] = {k: {x: j[x] for x in ("tool", "cwd", "start", "plugin", "cpu_ticks")} for k, j in seen.items()}
        s["pending"] = {}
    known = s.setdefault("runs", {})
    pending = s.setdefault("pending", {})
    min_run, quiet = conf["MIN_RUN"], conf["BATCH_QUIET"]
    hz = os.sysconf("SC_CLK_TCK")

    # per-job CPU use (cores) since the previous tick
    for k, j in seen.items():
        prev = known.get(k)
        if prev and s.get("last_tick"):
            dt = max(1.0, now - s["last_tick"])
            j["cores"] = max(0.0, (j["cpu_ticks"] - prev.get("cpu_ticks", j["cpu_ticks"])) / hz / dt)
        else:
            j["cores"] = None

    for key, r in known.items():
        if key in seen:
            continue
        dur = now - r["start"]
        if dur < min_run:
            continue
        plug = plugin_by_name(r.get("plugin", ""))
        summ = None
        if plug and hasattr(plug, "log_summary"):
            try:
                summ = plug.log_summary(r["cwd"], r["tool"])
            except Exception:
                summ = None
        errs = (summ or {}).get("errors") or 0
        status = "failed" if errs else "ok" if summ else "ended"
        res = record_result(r, dur, summ, status)
        with open(RUNS, "a") as f:
            f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "tool": r["tool"], "cwd": r["cwd"],
                                "seconds": int(dur), "errors": summ.get("errors") if summ else None,
                                "log": summ.get("log", "") if summ else "", "via": "watch",
                                "results": res}) + "\n")
        if errs:
            notify(f"{r['tool']} failed: {errs} ERROR(s)",
                   f"{short(r['cwd'])} · {hms(dur)} · {summ.get('first_error', '')}", "high", "x")
        b = pending.setdefault(str(Path(r["cwd"]).parent),
                               {"tool": r["tool"], "n": 0, "err": 0, "crit": 0, "longest": 0, "last_end": now,
                                "best_wns": None})
        b["n"] += 1
        b["err"] += 1 if errs else 0
        b["crit"] += (summ or {}).get("critical") or 0
        b["longest"] = max(b["longest"], dur)
        b["last_end"] = now
        if res.get("wns_ns") is not None:
            b["best_wns"] = res["wns_ns"] if b["best_wns"] is None else max(b["best_wns"], res["wns_ns"])

    busy = {str(Path(j["cwd"]).parent) for j in cur}
    for bkey in list(pending):
        b = pending[bkey]
        if bkey in busy or now - b["last_end"] < quiet:
            continue
        ok_n = b["n"] - b["err"]
        what = f"{b['n']} {b['tool']} run{'s' if b['n'] > 1 else ''}"
        detail = (f"{ok_n} OK" + (f", {b['err']} failed" if b["err"] else "") +
                  (f", {b['crit']} critical warnings" if b["crit"] else "") + f" · longest {hms(b['longest'])}" +
                  (f" · best WNS {b['best_wns']:+.3f} ns" if b.get("best_wns") is not None else ""))
        notify(f"Finished: {what} in {Path(bkey).name}/", detail,
               "high" if b["err"] else "default", "warning" if b["err"] else "white_check_mark")
        del pending[bkey]

    s["runs"] = {k: {"tool": j["tool"], "cwd": j["cwd"], "start": j["start"], "plugin": j["plugin"],
                     "cpu_ticks": j["cpu_ticks"], "cores": j.get("cores"), "rss": j["rss"]}
                 for k, j in seen.items()}
    return cur


def advise_parallel(cur, s):
    """Suggested number of parallel jobs from measured per-job CPU and RAM."""
    cores = [j.get("cores") for j in cur if j.get("cores")]
    rss = [j["rss"] for j in cur if j["rss"]]
    th = probes.threads()
    by_cpu = th / max(1.0, sum(cores) / len(cores)) if cores else th
    mem = probes.meminfo().get("MemTotal", 0)
    by_mem = mem * 0.85 / (sum(rss) / len(rss)) if rss else by_cpu
    return max(1, int(min(by_cpu, by_mem)))


# ---------------- tick ----------------
def tick():
    c = config.load()
    conf = {k: float(c.get(k, config.DEFAULTS.get(k, 0)) or 0) for k in
            ("SSD_WARN_PCT", "FAST_FILL_GB", "HOT_C", "MIN_RUN", "BATCH_QUIET", "OVERLOAD_RATIO",
             "OVERLOAD_MINUTES", "REBOOT_HINT")}
    s = load_state()
    now = time.time()
    config.REPORTS.mkdir(parents=True, exist_ok=True)

    d = probes.disk("/")
    if d:
        used, free, total = d
        pct = used / total * 100
        prev = s.get("ssd_free")
        if prev and prev - free > conf["FAST_FILL_GB"] * 2**30 and rate_ok(s, "fastfill", 600):
            running = ", ".join(f"{j['tool']} in {short(j['cwd'], 2)}" for j in jobs()[:4]) or "no tracked job"
            notify("Disk filling fast", f"-{(prev - free) / 2**30:.1f} GB in 1 min, {free / 2**30:.0f} GB left. "
                                        f"Running: {running}", "urgent", "warning")
        if pct >= conf["SSD_WARN_PCT"] and rate_ok(s, "ssdfull", 3600):
            notify("Disk almost full", f"/ is {pct:.0f}% used, {free / 2**30:.0f} GB free", "high", "warning")
        s["ssd_free"] = free

    t = probes.cpu_temp()
    if t and t >= conf["HOT_C"] and rate_ok(s, "hot", 1800):
        notify("CPU very hot", f"{t} °C", "high", "fire")

    cur = track_jobs(s, now, conf)

    # overload advice
    l1 = probes.loadavg()[0]
    th = probes.threads()
    if l1 / th > conf["OVERLOAD_RATIO"] and len(cur) >= 2:
        s["over_min"] = s.get("over_min", 0) + 1
    else:
        s["over_min"] = 0
    if s["over_min"] >= conf["OVERLOAD_MINUTES"] and rate_ok(s, "overload", 6 * 3600):
        k = advise_parallel(cur, s)
        notify("CPU overloaded", f"load {l1:.0f} on {th} threads with {len(cur)} jobs for {s['over_min']} min. "
                                 f"Jobs wait for CPU and each one runs slower. About {k} jobs at a time would "
                                 f"finish the same work sooner (e.g. tsp -S {k}).", "default", "hourglass")

    # reboot hint
    if cur:
        s["idle_since"] = None
    elif not s.get("idle_since"):
        s["idle_since"] = now
    if (conf["REBOOT_HINT"] and probes.reboot_required() and s.get("idle_since")
            and now - s["idle_since"] >= 600 and rate_ok(s, "reboot", 12 * 3600)):
        notify("Good moment to reboot", "An update is waiting for a reboot and no tracked job is running.",
               "default", "arrows_counterclockwise")

    # watchdog: daily check and backups
    latest = config.REPORTS / "history.log"
    if latest.exists() and now - latest.stat().st_mtime > 30 * 3600 and rate_ok(s, "daily_stale", 12 * 3600):
        notify("Daily check stopped", "No health report for 30 h — check: systemctl --user status my-system-daily.timer",
               "high", "warning")
    b = config.kv_file(config.STATE / "backup_status")
    if b and now - float(b.get("last_success", 0) or 0) > 50 * 3600 and not config.backup_running() and rate_ok(s, "backup_stale", 12 * 3600):
        notify("Backup is behind", f"Last successful backup: {b.get('last_success') and time.strftime('%Y-%m-%d %H:%M', time.localtime(float(b['last_success']))) or 'never'}",
               "high", "floppy_disk")

    # hourly metrics
    if now - s.get("last_metric", 0) >= 3600 - 30:
        s["last_metric"] = now
        m = probes.meminfo()
        dd = [probes.disk(x) for x in config.words("DATA_DISKS")]
        dd = next((x for x in dd if x), None)
        row = [time.strftime("%Y-%m-%d %H:%M"),
               f"{d[0] / d[2] * 100:.1f}" if d else "", f"{d[1] / 2**30:.1f}" if d else "",
               f"{dd[1] / 2**40:.2f}" if dd else "",
               f"{m.get('MemAvailable', 0) / 2**30:.1f}",
               f"{(m.get('SwapTotal', 0) - m.get('SwapFree', 0)) / 2**30:.1f}",
               t if t is not None else "", f"{l1:.1f}", len(cur)]
        new = not METRICS.exists()
        with open(METRICS, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "ssd_used_pct", "ssd_free_gb", "data_free_tb", "mem_avail_gb",
                            "swap_used_gb", "cpu_temp", "load1", "jobs"])
            w.writerow(row)
    s["last_tick"] = now
    save_state(s)


# ---------------- metrics / forecast ----------------
def read_metrics(days=30):
    if not METRICS.exists():
        return []
    cutoff = time.time() - days * 86400
    out = []
    with open(METRICS) as f:
        for r in csv.DictReader(f):
            try:
                r["ts"] = time.mktime(time.strptime(r["time"], "%Y-%m-%d %H:%M"))
            except (ValueError, KeyError, TypeError):
                continue
            if r["ts"] >= cutoff:
                out.append(r)
    return out


def forecast(target=None):
    target = target or config.num("SSD_WARN_PCT")
    pts = []
    for r in read_metrics(14):
        try:
            pts.append((r["ts"], float(r["ssd_used_pct"])))
        except (ValueError, KeyError, TypeError):
            pass
    if len(pts) < 24:
        return None, None, len(pts)
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    var = sum((p[0] - mx) ** 2 for p in pts)
    if var == 0:
        return None, 0.0, n
    slope = sum((p[0] - mx) * (p[1] - my) for p in pts) / var * 86400
    cur = pts[-1][1]
    if cur >= target:
        return 0, slope, n
    if slope <= 0.01:
        return None, slope, n
    return (target - cur) / slope, slope, n


def forecast_text():
    days, slope, n = forecast()
    t = config.num("SSD_WARN_PCT")
    if slope is None:
        return f"collecting data ({n} of 24 hourly samples)"
    if days is None:
        return f"stable ({slope:+.2f} %/day)"
    if days == 0:
        return f"already above {t:.0f}%"
    return f"{slope:+.2f} %/day → reaches {t:.0f}% in about {math.ceil(days)} days"


# ---------------- daily folder sizes ----------------
def size_folders():
    entries = config.lst("size_folders.txt") or ["AUTO"]
    out = []
    for e in entries:
        if e.strip().upper() == "AUTO":
            for base in [str(config.HOME)] + config.words("DATA_DISKS"):
                try:
                    for name in sorted(os.listdir(base)):
                        p = os.path.join(base, name)
                        if name.startswith(".") and name != ".cache":
                            continue
                        if name in ("lost+found",) or not os.path.isdir(p):
                            continue
                        out.append(p)
                except OSError:
                    pass
            out.append(str(config.HOME / ".cache"))
        else:
            out.append(e)
    return list(dict.fromkeys(out))


def daily_sizes():
    path = config.REPORTS / "sizes.csv"
    rows = []
    for p in size_folders():
        if not os.path.exists(p):
            continue
        try:
            r = subprocess.run(["du", "-sb", "--one-file-system", p], capture_output=True, text=True, timeout=3600)
            b = int(r.stdout.split()[0])
        except (subprocess.TimeoutExpired, ValueError, IndexError, OSError):
            continue
        real = os.path.realpath(p)
        disk = "root" if not any(real.startswith(dd + "/") or real == dd for dd in config.words("DATA_DISKS")) else "data"
        label = "~" + p[len(str(config.HOME)):] if p.startswith(str(config.HOME)) else p
        rows.append([time.strftime("%Y-%m-%d"), label, f"{b / 2**30:.2f}", disk, "link" if os.path.islink(p) else "dir"])
    new = not path.exists()
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "folder", "gb", "disk", "kind"])
        w.writerows(rows)
    return sorted(rows, key=lambda r: -float(r[2]))
