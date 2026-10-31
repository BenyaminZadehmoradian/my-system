"""Phone status report: how the machine is, what runs, what waits, the last finished runs.

  ms brief            print it
  ms brief --send     send it to the phone (and desktop) now
Sent automatically at PHONE_REPORT_TIMES (my_system.conf, e.g. "08:00 13:00 19:00"; empty = off), and on request:
type  status  (or ?) as a message in your ntfy topic in the phone app — the answer arrives within a minute.
Plain text without emoji or symbols, short lines: readable on a phone without any formatting support.
"""
import json
import os
import subprocess
import time
from pathlib import Path

from . import config, probes

GiB = 2**30
MAX_RUNNING, MAX_WAITING, LAST_N = 6, 4, 3


def _ago(t):
    d = time.time() - t
    if d < 90:
        return "just now"
    if d < 3600:
        return f"{d / 60:.0f} min ago"
    if d < 86400 and time.strftime("%d", time.localtime(t)) == time.strftime("%d"):
        return "today " + time.strftime("%H:%M", time.localtime(t))
    if d < 2 * 86400:
        return "yesterday " + time.strftime("%H:%M", time.localtime(t))
    return time.strftime("%a %d %b %H:%M", time.localtime(t))


def _dur(s):
    s = int(s or 0)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{s % 3600 // 60:02d}m"


def _place(cwd, n=2):
    parts = Path(cwd or "").parts
    return "/".join(parts[-n:]) if parts else "?"


def _queue():
    try:
        from . import queue
        return queue, queue.all_jobs()
    except Exception:
        return None, []


def _tsp_waiting():
    """Jobs waiting in task-spooler (tsp), if it is used."""
    exe = next((x for x in ("tsp", "ts") if subprocess.run(["sh", "-c", f"command -v {x}"], capture_output=True).returncode == 0), None)
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "-l"], capture_output=True, text=True, timeout=5).stdout.splitlines()[1:]
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows = []
    for line in out:
        parts = line.split(None, 3)
        if len(parts) >= 3 and parts[1] == "queued":
            rows.append(parts[-1][:60])
    return rows


# ---------------------------------------------------------------- sections
def system_lines():
    th = probes.threads()
    l1 = probes.loadavg()[0]
    m = probes.meminfo()
    tot, av = m.get("MemTotal", 0), m.get("MemAvailable", 0)
    busy = min(999, l1 / th * 100) if th else 0
    word = "idle" if busy < 15 else "light" if busy < 50 else "busy" if busy < 110 else "OVERLOADED"
    t = probes.cpu_temp()
    lines = [f"CPU {busy:.0f}% {word} (load {l1:.1f} of {th})" + (f" · {t:.0f}°C" if t is not None else "")]
    lines.append(f"RAM {av / GiB:.0f} GB free of {tot / GiB:.0f}" + (" · swap in use" if m.get("SwapTotal", 0) - m.get("SwapFree", 0) > GiB else ""))
    d = probes.disk("/")
    disk = f"Disk {d[0] / d[2] * 100:.0f}% full ({d[1] / GiB:.0f} GB free)" if d else "Disk ?"
    st = {}
    try:
        st = json.loads((config.STATE / "watch.json").read_text()).get("mounts", {})
    except (OSError, ValueError):
        pass
    for mp in config.words("DATA_DISKS")[:1]:
        if st.get(mp, "ok") == "ok":
            dd = probes.disk(mp)
            if dd:
                disk += f" · data {dd[1] / 2**40:.1f} TB free"
    lines.append(disk)
    bst, btxt = config.backup_state()
    b = config.kv_file(config.STATE / "backup_status")
    ls = float(b.get("last_success", 0) or 0)
    lines.append("Backup " + {"ok": f"OK ({_ago(ls)})", "running": "running now", "first": "first backup running",
                              "off": "not set up"}.get(bst, f"PROBLEM: {btxt[:60]}"))
    return lines, busy


def attention():
    out = []
    try:
        latest = config.REPORTS / "latest" / "summary.txt"
        if time.time() - latest.stat().st_mtime < 36 * 3600:
            out += [l[6:].strip() for l in latest.read_text().splitlines() if l.startswith("WARN:")]
    except OSError:
        pass
    try:
        for line in (config.REPORTS / "crashes.log").read_text().splitlines()[-2:]:
            t, cause = line.split("\t")[:2]
            if time.time() - time.mktime(time.strptime(t, "%Y-%m-%d %H:%M")) < 3 * 86400:
                out.append(f"unexpected stop {t}: {cause}")
    except (OSError, ValueError):
        pass
    try:
        for mp, stt in json.loads((config.STATE / "watch.json").read_text()).get("mounts", {}).items():
            if stt != "ok":
                out.append(f"{mp} {'not answering' if stt == 'hung' else 'not mounted'}")
    except (OSError, ValueError):
        pass
    bst, btxt = config.backup_state()
    if bst in ("failed", "stale") and not any("backup" in x.lower() for x in out):
        out.append(f"backup: {btxt[:70]}")
    try:
        n = len(json.loads((config.STATE / "vulns.json").read_text()).get("findings") or [])
        if n:
            out.append(f"{n} Python package(s) with known security issues")
    except (OSError, ValueError):
        pass
    if probes.reboot_required() and not any("reboot" in x.lower() for x in out):
        out.append("reboot needed to finish updates")
    return list(dict.fromkeys(out))


def running():
    """[(sort_key, line)] for queue jobs and tracked runs (Vivado etc. started outside the queue)."""
    queue, js = _queue()
    rows, qcwd = [], set()
    for j in js:
        if j["state"] not in ("running", "suspended"):
            continue
        qcwd.add(j["cwd"])
        el = time.time() - (j.get("started") or time.time())
        bits = [_dur(el)]
        if j.get("progress") is not None and j["state"] == "running":
            bits.append(f"{j['progress']:.0f}%")
        try:
            usual = queue.usual_seconds(j)
            if usual:
                left = usual - el
                bits.append(f"~{_dur(left)} left" if left > 60 else "should end soon" if left > -usual * 0.5 else "slower than usual")
        except Exception:
            pass
        if j.get("rss"):
            bits.append(f"{j['rss'] / GiB:.1f} GB")
        st = " PAUSED" if j["state"] == "suspended" else ""
        rows.append((j.get("started") or 0, f"#{j['id']} {j['name']}{st} — " + ", ".join(bits)))
    try:
        st = json.loads((config.STATE / "watch.json").read_text())
        tracked = st.get("runs", {}).values()
    except (OSError, ValueError):
        tracked = []
    try:
        from . import capacity
        prof = capacity.profiles()
    except Exception:
        prof = {}
    for r in tracked:
        if r["cwd"] in qcwd:
            continue
        el = time.time() - r["start"]
        bits = [_dur(el)]
        mins = (prof.get(r["tool"]) or {}).get("minutes")
        if mins and (prof.get(r["tool"]) or {}).get("measured"):
            left = mins * 60 - el
            bits.append(f"usually {_dur(mins * 60)}" + (", running long" if left < -mins * 60 else ""))
        if r.get("rss"):
            bits.append(f"{r['rss'] / GiB:.1f} GB")
        rows.append((r["start"], f"{r['tool']} {_place(r['cwd'])} — " + ", ".join(bits)))
    rows.sort()
    return rows


def waiting():
    queue, js = _queue()
    rows = []
    for j in sorted((j for j in js if j["state"] in ("queued", "held", "approval")),
                    key=lambda j: (-int(j.get("priority") or 0), j["id"])):
        why = j.get("reason") or ("held by you" if j["state"] == "held" else "next in line")
        why = why.replace("waiting for ", "needs ").replace("waiting: ", "")
        if j["state"] == "approval":
            why = "needs your approval (ms q approve " + str(j["id"]) + ")"
        rows.append(f"#{j['id']} {j['name']} — {why[:70]}")
    rows += [f"tsp: {c}" for c in _tsp_waiting()]
    return rows


def finished(n=LAST_N):
    """Last n finished runs from the queue and from tracked runs, newest first, without duplicates."""
    queue, js = _queue()
    items = []
    for j in js:
        if j["state"] not in ("done", "warning", "failed", "timeout", "cancelled") or not j.get("ended") or not j.get("started"):
            continue
        ok = j["state"] in ("done", "warning")
        mark = "OK" if j["state"] == "done" else "WARN" if j["state"] == "warning" else "FAIL" if j["state"] != "cancelled" else "stopped"
        extra = []
        outs = j.get("outputs") or {}
        for k in ("wns", "wns_ns", "WNS"):
            if k in outs:
                extra.append(f"WNS {outs[k]}")
                break
        if not ok and j.get("reason"):
            extra.append(j["reason"].split(" — ")[0][:60])
        items.append((j["ended"], j["cwd"], f"{mark}  #{j['id']} {j['name']} — {_dur(j['ended'] - j['started'])}"
                      + (", " + ", ".join(extra) if extra else "") + f" · {_ago(j['ended'])}"))
    try:
        lines = (config.REPORTS / "runs.jsonl").read_text().splitlines()[-200:]
    except OSError:
        lines = []
    qends = [(i[0], i[1]) for i in items]
    for line in lines:
        try:
            r = json.loads(line)
            t = time.mktime(time.strptime(r["t"], "%Y-%m-%d %H:%M:%S"))
        except (ValueError, KeyError):
            continue
        if any(c == r.get("cwd") and abs(t - e) < 300 for e, c in qends):
            continue                                   # same run already listed from the queue
        res = r.get("results") or {}
        bad = (r.get("errors") or 0) > 0
        extra = []
        if res.get("wns_ns") is not None:
            extra.append(f"WNS {res['wns_ns']:+.3f} ns")
        if bad:
            extra.append(f"{r['errors']} ERROR(s)")
        items.append((t, r.get("cwd"), f"{'FAIL' if bad else 'OK'}  {r.get('tool')} {_place(r.get('cwd'))} — {_dur(r.get('seconds'))}"
                      + (", " + ", ".join(extra) if extra else "") + f" · {_ago(t)}"))
    items.sort(key=lambda x: -x[0])
    return [i[2] for i in items[:n]]


# ---------------------------------------------------------------- the message
def build():
    sysl, busy = system_lines()
    att = attention()
    run = running()
    wait = waiting()
    last = finished()
    state = "needs attention" if att else ("busy" if run else "idle")
    title = f"Status: {len(run)} running, {len(wait)} waiting" + (f", {len(att)} problem{'s' if len(att) > 1 else ''}" if att else ", all OK")
    out = ["SYSTEM"] + sysl
    if att:
        out += ["", f"NEEDS ATTENTION ({len(att)})"] + [f"- {a[:110]}" for a in att[:5]]
    out += ["", f"RUNNING ({len(run)})"]
    out += [f"- {l}" for _, l in run[:MAX_RUNNING]] or ["- nothing"]
    if len(run) > MAX_RUNNING:
        out.append(f"  + {len(run) - MAX_RUNNING} more")
    out += ["", f"WAITING ({len(wait)})"]
    out += [f"- {l}" for l in wait[:MAX_WAITING]] or ["- queue is empty"]
    if len(wait) > MAX_WAITING:
        out.append(f"  + {len(wait) - MAX_WAITING} more")
    out += ["", f"LAST {LAST_N} FINISHED"]
    out += [f"- {l}" for l in last] or ["- none yet"]
    out += ["", f"{time.strftime('%a %H:%M')} · reply 'status' for a new report"]
    text = "\n".join(out)
    if len(text.encode()) > 3900:                     # ntfy limit is 4096 bytes
        text = text.encode()[:3880].decode(errors="ignore") + "\n…"
    return title, text, {"state": state, "attention": att, "running": len(run), "waiting": len(wait)}


def send(prio=None):
    title, text, info = build()
    serious = any("unexpected stop" in a or "not answering" in a or "not mounted" in a for a in info["attention"])
    p = "high" if serious else (prio or ("default" if info["attention"] else "low"))
    subprocess.run([str(config.MS / "bin" / "notify.sh"), title, text, p, "bar_chart"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    return title, text


# ---------------------------------------------------------------- schedule and requests from the phone
def due(s, now=None):
    """True when one of PHONE_REPORT_TIMES has passed and was not yet sent today (checked every minute)."""
    now = now or time.time()
    times = config.words("PHONE_REPORT_TIMES")
    lt = time.localtime(now)
    sent = s.setdefault("brief_sent", {})
    today = time.strftime("%Y-%m-%d", lt)
    for hhmm in times:
        try:
            h, m = map(int, hhmm.split(":"))
        except ValueError:
            continue
        slot = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))
        if 0 <= now - slot < 1800 and sent.get(hhmm) != today:
            sent[hhmm] = today
            return True
    return False


REQUEST_WORDS = {"status", "?", "report", "وضعیت", "گزارش"}


def phone_requests(s):
    """Poll the ntfy topic for 'status' messages typed in the phone app (only since the last check)."""
    topic = (config.STATE / "ntfy_topic")
    if not topic.exists() or (config.STATE / "ntfy_off").exists() or config.get("PHONE_REQUESTS", "1") == "0":
        return False
    import urllib.request
    since = s.get("ntfy_since") or str(int(time.time()) - 120)
    url = f"{config.get('NTFY_SERVER') or 'https://ntfy.sh'}/{topic.read_text().strip()}/json?poll=1&since={since}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "my-system"}), timeout=10) as r:
            body = r.read().decode(errors="replace")
    except Exception:
        return False
    asked = False
    for line in body.splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("event") != "message":
            continue
        s["ntfy_since"] = m.get("id") or s.get("ntfy_since")
        if not m.get("title") and (m.get("message") or "").strip().lower() in REQUEST_WORDS:
            asked = True
    if "ntfy_since" not in s:
        s["ntfy_since"] = str(int(time.time()))
    return asked


def cli(a):
    if "--send" in a:
        title, text = send()
        print(f"sent: {title}\n\n{text}")
    else:
        title, text, _ = build()
        print(f"{title}\n\n{text}")
    return 0
