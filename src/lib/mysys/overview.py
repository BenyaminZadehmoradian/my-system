"""Data for the app's Overview page: machine specification, health with levels, activity, overall status.
Read-only and fast enough to call every 15 s (the specification is cached for 10 minutes)."""
import json
import os
import re
import time

from . import config, probes

GiB = 2**30
_SPEC = {}


def _h(n):
    for unit, f in (("TB", 2**40), ("GB", GiB), ("MB", 2**20)):
        if n >= f:
            v = n / f
            return f"{v:.1f} {unit}" if v < 100 else f"{v:.0f} {unit}"
    return f"{n} B"


def _short_cpu(model):
    m = re.sub(r"\(R\)|\(TM\)|CPU|Processor|@.*$", "", model or "").strip()
    return re.sub(r"\s+", " ", m)


def _short_gpu(name):
    n = re.sub(r"Corporation|Integrated Graphics Controller|\[|\]", "", name or "")
    n = re.sub(r"\s+", " ", n).strip()
    return n


def uptime_text():
    s = int(probes.uptime_s() or 0)
    d, h = s // 86400, s % 86400 // 3600
    return f"{d} d {h} h" if d else f"{h} h {s % 3600 // 60} min"


def specs():
    """[(label, value)] describing this computer."""
    if time.time() - _SPEC.get("t", 0) < 600:
        return _SPEC["rows"]
    from . import capacity
    s = capacity.static_specs()
    cpu = s["cpu"]
    rows = [("Computer", f"{probes.product()} · {config.get('MACHINE_LABEL')}")]
    c = _short_cpu(cpu.get("model"))
    bits = [f"{cpu['cores']} cores / {cpu['threads']} threads" if cpu.get("cores") else f"{cpu.get('threads')} threads"]
    if cpu.get("max_ghz"):
        bits.append(f"up to {cpu['max_ghz']} GHz")
    rows.append(("Processor", f"{c} · " + " · ".join(bits)))
    mem = _h(s["mem_total"])
    hw = probes.hwinfo()
    mods = (hw.get("memory") or {}).get("modules") or []
    if mods:
        sizes = "+".join(str(m.get("size", "?")).replace(" GB", "") for m in mods)
        typ = mods[0].get("type", "")
        spd = (mods[0].get("configured_speed") or mods[0].get("speed") or "").replace(" MT/s", "")
        slots = (hw.get("memory") or {}).get("slots")
        mem += f" · {len(mods)} modules ({sizes} GB) {typ}" + (f" at {spd} MT/s" if spd else "") + \
            (f" · {slots - len(mods)} slot{'s' if slots - len(mods) != 1 else ''} free" if slots and slots > len(mods) else "")
    rows.append(("Memory", mem))
    if s["gpus"]:
        rows.append(("Graphics", " · ".join(_short_gpu(g[0]) + ("" if g[1] else " (no driver)") for g in s["gpus"])))
    for d in s["disks"]:
        kind = "HDD" if d.get("rotational") else "SSD" if d.get("rotational") is False else "disk"
        rows.append((f"Disk {d['mount']}", f"{kind} · {_h(d['total'])} · {_h(d['free'])} free" + (f" · {d['fs']}" if d.get("fs") else "")))
    for sh in s.get("shares") or []:
        rows.append(("Network share", sh))
    rows.append(("System", f"{probes.os_name()} · kernel {os.uname().release}"))
    if s.get("swap_total"):
        rows.append(("Swap", _h(s["swap_total"]) + (" (incl. zram)" if s.get("zram") else "")))
    _SPEC.update(t=time.time(), rows=rows)
    return rows


def health():
    """Live values with honest levels: [{key, label, value, detail, level, pct}]."""
    out = []
    th = probes.threads()
    l1, l5, _ = probes.loadavg()
    busy = l1 / th * 100 if th else 0
    out.append({"key": "cpu", "label": "CPU", "value": f"{busy:.0f}%", "pct": min(100, busy),
                "detail": f"load {l1:.1f} on {th} threads" + (" — overloaded, jobs wait for CPU" if busy > 110 else ""),
                "level": "ok" if busy < 90 else "warn" if busy < 150 else "bad"})
    m = probes.meminfo()
    tot, av = m.get("MemTotal", 0), m.get("MemAvailable", 0)
    used = (tot - av) / tot * 100 if tot else 0
    sw = m.get("SwapTotal", 0) - m.get("SwapFree", 0)
    out.append({"key": "mem", "label": "Memory", "value": f"{av / GiB:.0f} GB free", "pct": used,
                "detail": f"{used:.0f}% of {tot / GiB:.0f} GB in use" + (f" · swap {sw / GiB:.1f} GB" if sw > GiB else ""),
                "level": "ok" if av > tot * 0.2 else "warn" if av > tot * 0.08 else "bad"})
    t = probes.cpu_temp()
    if t is not None:
        hot = float(config.num("HOT_C") or 90)
        out.append({"key": "temp", "label": "CPU temperature", "value": f"{t:.0f} °C", "pct": min(100, t),
                    "detail": "normal under full load" if t < 80 else "warm — check the fans and dust" if t < hot else "too hot",
                    "level": "ok" if t < 80 else "warn" if t < hot else "bad"})
    d = probes.disk("/")
    if d:
        pct = d[0] / d[2] * 100
        from . import watcher
        days, _, _ = watcher.forecast()
        lim = float(config.num("SSD_WARN_PCT") or 93)
        lvl = "bad" if pct >= lim else "warn" if pct >= 85 or (days is not None and days < 30) else "ok"
        out.append({"key": "root", "label": "Root disk /", "value": f"{pct:.0f}% full", "pct": pct,
                    "detail": f"{d[1] / GiB:.0f} GB free" + (f" · full ({lim:.0f}%) in about {days:.0f} days" if days else ""),
                    "level": lvl})
    try:
        mst = json.loads((config.STATE / "watch.json").read_text()).get("mounts", {})
    except (OSError, ValueError):
        mst = {}
    for label, mp in [("Data disk", x) for x in config.words("DATA_DISKS")[:1]] + \
                     [("Network share", x) for x in config.words("NET_SHARES")[:1]]:
        st = mst.get(mp, "ok")
        if st == "ok" and label == "Network share" and probes.responsive(mp, 2) and os.path.ismount(mp):
            out.append({"key": mp, "label": f"{label} {mp}", "value": "connected", "pct": None,
                        "detail": "answering normally", "level": "ok"})
            continue
        dd = probes.disk(mp) if st == "ok" and probes.responsive(mp, 2) else None
        if dd:
            pct = dd[0] / dd[2] * 100
            out.append({"key": mp, "label": f"{label} {mp}", "value": f"{_h(dd[1])} free", "pct": pct,
                        "detail": f"{pct:.0f}% of {_h(dd[2])} used", "level": "ok" if pct < 90 else "warn"})
        else:
            out.append({"key": mp, "label": f"{label} {mp}", "value": "not answering" if st == "hung" else "not mounted",
                        "pct": None, "detail": mp, "level": "bad"})
    bst, btxt = config.backup_state()
    b = config.kv_file(config.STATE / "backup_status")
    ls = float(b.get("last_success", 0) or 0)
    out.append({"key": "backup", "label": "Backup", "pct": None,
                "value": {"running": "running", "first": "first run", "off": "not set up"}.get(bst, _ago(ls) if ls else "never"),
                "detail": btxt, "level": {"ok": "ok", "running": "info", "first": "info", "off": "warn"}.get(bst, "bad")})
    return out


def _ago(t):
    d = time.time() - t
    if d < 3600:
        return f"{d / 60:.0f} min ago"
    if d < 86400:
        return f"{d / 3600:.0f} h ago"
    return f"{d / 86400:.0f} days ago"


def _dur(s):
    s = int(s or 0)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m" if s >= 60 else f"{s}s"


def activity(n_done=5):
    """{'running': [...], 'waiting': [...], 'finished': [...]} with dicts ready for display."""
    from . import brief, queue
    js = queue.all_jobs()
    now = time.time()
    running, qcwd = [], set()
    for j in js:
        if j["state"] not in ("running", "suspended"):
            continue
        qcwd.add(j["cwd"])
        el = now - (j.get("started") or now)
        usual = queue.usual_seconds(j)
        where = brief._place(j["cwd"])
        if j.get("remote"):
            where += f" · on {j['remote']}" + (f" ({j['remote_node']})" if j.get("remote_node") else "") + \
                (f", {j['remote_state'].lower()}" if j.get("remote_state") and j["remote_state"] != "RUNNING" else "")
        running.append({"name": f"#{j['id']} {j['name']}", "where": where, "elapsed": _dur(el),
                        "progress": j.get("progress"), "ram": (j.get("rss") or 0) / GiB, "source": "queue",
                        "paused": j["state"] == "suspended", "start": j.get("started") or 0,
                        "left": _dur(usual - el) if usual and usual > el else None})
    try:
        tracked = json.loads((config.STATE / "watch.json").read_text()).get("runs", {}).values()
    except (OSError, ValueError):
        tracked = []
    from . import capacity
    prof = capacity.profiles()
    brief._CACHE.clear()
    # one row per run folder: helper tools in its subfolders (vitis-run → vitis_hls → xelab) belong to it
    tracked = sorted((r for r in tracked if r["cwd"] not in qcwd), key=lambda r: len(r["cwd"]))
    groups = []
    for r in tracked:
        home = next((g_ for g_ in groups if r["cwd"] == g_["cwd"] or r["cwd"].startswith(g_["cwd"] + "/")), None)
        if home and home["cwd"] != str(config.HOME):
            home["members"].append(r)
        else:
            groups.append({"cwd": r["cwd"], "members": [r]})
    for g_ in groups:
        ms = g_["members"]
        lead = min(ms, key=lambda r: r["start"])
        el = now - lead["start"]
        usual = brief._usual_tracked(lead, prof)
        tools = list(dict.fromkeys(r["tool"] for r in sorted(ms, key=lambda r: r["start"])))
        name = os.path.basename(g_["cwd"]) or g_["cwd"]
        running.append({"name": f"{name}", "where": brief._place(os.path.dirname(g_["cwd"])) + " · " + " → ".join(tools),
                        "elapsed": _dur(el), "progress": None, "ram": sum(r.get("rss") or 0 for r in ms) / GiB,
                        "source": "started by you", "paused": False, "start": lead["start"],
                        "left": _dur(usual - el) if usual and usual > el else ("longer than usual" if usual and el > 2 * usual else None)})
    running.sort(key=lambda x: x["start"])
    waiting = []
    for j in sorted((j for j in js if j["state"] in ("queued", "held", "approval")),
                    key=lambda j: (-int(j.get("priority") or 0), j["id"])):
        why = j.get("reason") or ("held by you" if j["state"] == "held" else "next in line")
        why = why.replace("waiting for ", "needs ").replace("waiting: ", "")
        waited = now - max(j.get("created") or now, j.get("not_before") or 0)
        waiting.append({"name": f"#{j['id']} {j['name']}", "where": brief._place(j["cwd"]), "why": why,
                        "waited": _dur(waited), "long": waited > 2 * 3600})
    finished = []
    for line in brief.finished(n_done):
        m = re.match(r"(OK|WARN|FAIL|stopped)\s+(.*?) — (.*?) · (.*)$", line)
        if m:
            finished.append({"result": m.group(1), "name": m.group(2), "detail": m.group(3), "when": m.group(4)})
    return {"running": running, "waiting": waiting, "finished": finished}


def overall(h=None, act=None, attention=None):
    """(headline, level): one honest sentence about the machine right now."""
    from . import brief
    h = h if h is not None else health()
    act = act if act is not None else activity()
    att = attention if attention is not None else brief.attention()
    bad = [x for x in h if x["level"] == "bad"]
    warn = [x for x in h if x["level"] == "warn"]
    long_wait = [w for w in act["waiting"] if w["long"]]
    parts = []
    n = len(act["running"])
    parts.append(f"{n} run{'s' if n != 1 else ''} going" if n else "idle")
    if act["waiting"]:
        parts.append(f"{len(act['waiting'])} waiting" + (f" ({len(long_wait)} for hours)" if long_wait else ""))
    for x in bad + warn:
        parts.append(f"{x['label'].split(' /')[0].lower()} {x['value']}")
    level = "bad" if bad or any("unexpected" in a or "not answering" in a for a in att) else \
        "warn" if warn or long_wait or att else "ok"
    head = {"ok": "All good", "warn": "Needs a look", "bad": "Problem"}[level]
    return head, " · ".join(parts), level
