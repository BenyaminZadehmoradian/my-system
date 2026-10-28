"""`ms report`: one complete, readable report of the whole machine.

Writes reports/system/<date>.html (for people), .md (for AI assistants and diffs) and .json
(a snapshot, so the next report can say what changed). Read-only: nothing is changed.
"""
import csv
import datetime as dt
import html
import json
import os
import re
import subprocess
import time

from . import __version__, config, detect, gitstat, hw, probes, watcher

OUT = config.REPORTS / "system"


def _cmd(args, timeout=30):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              env=dict(os.environ, LC_ALL="C")).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def gb(n):
    return f"{n / 1e9:.0f} GB" if n < 1e12 else f"{n / 1e12:.1f} TB"


def gib(n):
    return f"{n / 2**30:.0f} GiB"


# ---------------------------------------------------------------- collect
def latest_inventory():
    for d in sorted(config.REPORTS.glob("20??-??-??"), reverse=True):
        if (d / "inventory").is_dir():
            return d / "inventory"
    return None


def tools_from_inventory(inv):
    rows = []
    if inv and (inv / "tools.txt").exists():
        for line in (inv / "tools.txt").read_text().splitlines():
            if not line.strip() or line.startswith("#"):
                if line.startswith("# Toolchains"):
                    break
                continue
            p = line.split(None, 2)
            if len(p) >= 2:
                rows.append((p[0], p[1], p[2] if len(p) > 2 else ""))
    return rows


def venv_packages(inv, path):
    if not inv:
        return None
    for f in inv.glob("venv_*.txt"):
        try:
            lines = f.read_text().splitlines()
        except OSError:
            continue
        if lines and lines[0].startswith(f"# {path} "):
            return len([l for l in lines[1:] if "==" in l])
    return None


def sizes_history():
    """{folder: [(date, gb)]} from reports/sizes.csv."""
    out = {}
    p = config.REPORTS / "sizes.csv"
    if p.exists():
        with open(p) as f:
            for r in csv.DictReader(f):
                try:
                    out.setdefault(r["folder"], []).append((r["date"], float(r["gb"]), r.get("disk", "")))
                except (KeyError, ValueError):
                    pass
    return out


def docker_images():
    out = _cmd(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}\t{{.Size}}"], 20)
    return [tuple(l.split("\t", 1)) for l in out.splitlines() if "\t" in l and not l.startswith("<none>")]


def mounts_table():
    rows = []
    seen = set()
    for dev, mp, fs in probes.mounts():
        if fs in ("tmpfs", "devtmpfs", "proc", "sysfs", "cgroup2", "overlay", "squashfs", "securityfs", "pstore",
                  "efivarfs", "bpf", "tracefs", "debugfs", "configfs", "fusectl", "mqueue", "hugetlbfs", "autofs",
                  "devpts", "binfmt_misc", "ramfs", "nsfs", "fuse.portal", "fuse.gvfsd-fuse") or mp in seen:
            continue
        if mp.startswith(("/snap/", "/run/", "/sys/", "/proc/", "/dev/", "/var/snap/")):
            continue
        d = probes.disk(mp)
        if not d or d[2] < 100e6:
            continue
        seen.add(mp)
        rows.append({"mount": mp, "device": dev, "fs": fs, "used": d[0], "free": d[1], "total": d[2]})
    return rows


def collect():
    det = detect.load_state() or detect.detect_all()
    inv = latest_inventory()
    st, hwd, age = hw.collector_state()
    m = probes.meminfo()
    run_k, inst_k = probes.kernels()
    git = []
    try:
        git = json.loads((config.STATE / "git_status.json").read_text())
    except (OSError, ValueError):
        git = gitstat.run()
    apt_manual = []
    if inv and (inv / "apt_manual.txt").exists():
        apt_manual = inv.joinpath("apt_manual.txt").read_text().split()
    metrics = watcher.read_metrics(30)

    def mx(key):
        vals = [float(r[key]) for r in metrics if r.get(key) not in (None, "")]
        return (max(vals), sum(vals) / len(vals)) if vals else (None, None)

    b = config.kv_file(config.STATE / "backup_status")
    v = config.kv_file(config.STATE / "verify_status")
    return {
        "date": time.strftime("%Y-%m-%d"), "time": time.strftime("%Y-%m-%d %H:%M"), "version": __version__,
        "host": probes.hostname(), "label": config.get("MACHINE_LABEL"), "os": probes.os_name(),
        "kernel_running": run_k, "kernels": inst_k, "reboot_required": probes.reboot_required(),
        "uptime_days": round(probes.uptime_s() / 86400, 1),
        "cpu": probes.cpu_info(), "bios": probes.bios(), "gpus": probes.gpus(), "net": probes.net_links(),
        "mem_total": m.get("MemTotal", 0), "mem_avail": m.get("MemAvailable", 0),
        "swaps": probes.swaps(), "zram": probes.zram(),
        "temps": probes.temps(), "fans": probes.fans(), "cpu_temp": probes.cpu_temp(),
        "load": probes.loadavg(), "temp_30d": mx("cpu_temp"), "swap_30d": mx("swap_used_gb"), "load_30d": mx("load1"),
        "secure_boot": probes.secure_boot(), "power_profile": probes.power_profile(),
        "repos": probes.apt_repos(), "upgradable": probes.apt_upgradable(), "failed_units": probes.failed_units(),
        "mounts": mounts_table(), "data_disks": config.words("DATA_DISKS"), "net_shares": config.words("NET_SHARES"),
        "forecast": watcher.forecast_text(),
        "hw_state": st, "hw_age_h": age, "hw": hwd,
        "toolchains": det.get("tools", []), "venvs": [dict(x, packages=venv_packages(inv, x["path"])) for x in det.get("venvs", [])],
        "tools": tools_from_inventory(inv), "docker_images": docker_images(), "apt_manual": apt_manual,
        "sizes": {k: v[-1] for k, v in sizes_history().items()}, "sizes_hist": sizes_history(),
        "git": git, "backup": {"state": config.backup_state(), "last_success": b.get("last_success"),
                              "last_size": b.get("last_size"), "verify": v.get("last_result"), "verify_run": v.get("last_run")},
        "jobs": len(watcher.jobs()),
    }


# ---------------------------------------------------------------- analyse
def previous_snapshot(today):
    snaps = sorted(p for p in OUT.glob("20??-??-??.json") if p.stem < today)
    if not snaps:
        return None
    try:
        return json.loads(snaps[-1].read_text())
    except (OSError, ValueError):
        return None


def growth(c, days=30):
    """[(folder, now_gb, delta_gb, since_date, disk)] sorted by growth."""
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out = []
    for f, hist in c["sizes_hist"].items():
        if not hist:
            continue
        old = next((h for h in hist if h[0] >= cutoff), hist[0])
        out.append((f, hist[-1][1], hist[-1][1] - old[1], old[0], hist[-1][2]))
    return sorted(out, key=lambda r: -r[2])


def disks_of(c):
    return (c["hw"].get("storage") or {}).get("disks", [])


def changes(c, prev):
    if not prev:
        return []
    out = []
    a, b = set(prev.get("apt_manual", [])), set(c["apt_manual"])
    if a and b:
        if b - a:
            out.append(("Packages installed", ", ".join(sorted(b - a)[:25]) + (" …" if len(b - a) > 25 else "")))
        if a - b:
            out.append(("Packages removed", ", ".join(sorted(a - b)[:25]) + (" …" if len(a - b) > 25 else "")))
    if prev.get("kernel_running") != c["kernel_running"]:
        out.append(("Kernel", f"{prev.get('kernel_running')} → {c['kernel_running']}"))
    pt, ct = {t[0] for t in prev.get("toolchains", [])}, {t[0] for t in c["toolchains"]}
    if ct - pt:
        out.append(("Toolchains added", ", ".join(sorted(ct - pt))))
    if pt - ct:
        out.append(("Toolchains gone", ", ".join(sorted(pt - ct))))
    pv, cv = {v["path"] for v in prev.get("venvs", [])}, {v["path"] for v in c["venvs"]}
    if cv - pv:
        out.append(("Python environments added", ", ".join(sorted(cv - pv))))
    if pv - cv:
        out.append(("Python environments gone", ", ".join(sorted(pv - cv))))
    pr = next((m for m in prev.get("mounts", []) if m["mount"] == "/"), None)
    cr = next((m for m in c["mounts"] if m["mount"] == "/"), None)
    if pr and cr:
        d = (cr["free"] - pr["free"]) / 1e9
        out.append(("Root disk free space", f"{pr['free'] / 1e9:.0f} GB → {cr['free'] / 1e9:.0f} GB ({d:+.0f} GB)"))
    pd = {x.get("model"): x for x in (prev.get("hw", {}).get("storage") or {}).get("disks", [])}
    for d in disks_of(c):
        p = pd.get(d.get("model"))
        if not p:
            continue
        bits = []
        for k, lab in (("wear_pct", "wear %"), ("written_tb", "TB written"), ("unsafe_shutdowns", "unsafe shutdowns"),
                       ("reallocated", "reallocated sectors"), ("media_errors", "media errors")):
            if isinstance(d.get(k), (int, float)) and isinstance(p.get(k), (int, float)) and d[k] != p[k]:
                bits.append(f"{lab} {p[k]} → {d[k]}")
        if bits:
            out.append((f"Disk {d.get('model')}", "; ".join(bits)))
    pf = {x[0] for x in prev.get("failed_units", [])}
    nf = [u for u, _ in c["failed_units"] if u not in pf]
    if nf:
        out.append(("Newly failed services", ", ".join(nf)))
    return out


def recommendations(c):
    """[(priority, text)] priority: now | when idle | sometime."""
    r = []
    root = next((m for m in c["mounts"] if m["mount"] == "/"), None)
    warn_pct = config.num("SSD_WARN_PCT")
    if root:
        pct = root["used"] / root["total"] * 100
        days, _, _ = watcher.forecast()
        if pct >= warn_pct - 5 or (days is not None and days < 30):
            top = [g for g in growth(c) if g[4] == "root" and g[2] > 1][:3]
            hint = "; fastest growing: " + ", ".join(f"{g[0]} (+{g[2]:.0f} GB)" for g in top) if top else ""
            r.append(("now", f"Root disk is {pct:.0f}% full ({c['forecast']}). Move big folders to the data disk "
                             f"with `ms move FOLDER`{hint}."))
    for d in disks_of(c):
        for lvl, txt in hw.disk_findings(d):
            if lvl == "warn":
                r.append(("now", txt))
            elif lvl == "info":
                r.append(("sometime", txt))
    real_failed = [u for u, load in c["failed_units"] if load != "not-found"]
    stale = [u for u, load in c["failed_units"] if load == "not-found"]
    if real_failed:
        r.append(("now", "Failed services: " + ", ".join(real_failed) + " — see `systemctl status <name>`."))
    if stale:
        r.append(("sometime", "Leftover failed units of removed software: " + ", ".join(stale) +
                  " — clear with `sudo systemctl reset-failed`."))
    bst = c["backup"]["state"][0]
    if bst in ("failed", "stale", "off"):
        r.append(("now", f"Backup: {c['backup']['state'][1]}."))
    if c["backup"]["verify"] not in (None, "ok") and bst != "off":
        r.append(("now", f"Last restore test: {c['backup']['verify']}."))
    if c["reboot_required"]:
        r.append(("when idle", f"Reboot to activate the new kernel ({c['kernels'][0] if c['kernels'] else 'installed'}; "
                               f"running {c['kernel_running']})."))
    if c["upgradable"]:
        r.append(("when idle", f"{len(c['upgradable'])} package update(s) waiting: `sudo apt upgrade`."))
    for u in (c["hw"].get("firmware") or {}).get("updates") or []:
        r.append(("when idle", f"Firmware update for {u['device']}: {u['current']} → {u['available']} (`fwupdmgr update`)."))
    sw_max, _ = c["swap_30d"]
    if sw_max and sw_max > 4:
        r.append(("sometime", f"Swap use peaked at {sw_max:.1f} GB this month — run fewer heavy jobs in parallel."))
    t_max, _ = c["temp_30d"]
    if t_max and t_max >= config.num("HOT_C") - 5:
        r.append(("sometime", f"CPU reached {t_max:.0f}°C this month — clean fans and air vents."))
    unsafe = [d for d in disks_of(c) if isinstance(d.get("unsafe_shutdowns"), int) and d["unsafe_shutdowns"] > 50]
    for d in unsafe:
        r.append(("sometime", f"{d['unsafe_shutdowns']} unsafe shutdowns recorded on {d.get('model')} — shut down "
                              "from the menu; hold the power button only when the system is frozen."))
    note = hw.memory_note((c["hw"].get("memory") or {}).get("modules") or [])
    if note:
        r.append(("sometime", f"Memory: {note}. Matched modules would run at full speed."))
    kinds = {}
    for g in c["git"]:
        for p in gitstat.problems(g):
            k = re.sub(r"^\d+ ", "", p)
            kinds[k] = kinds.get(k, 0) + 1
    if kinds:
        r.append(("sometime", "Git projects — " + ", ".join(f"{k.replace('(s)', 's')}: {n}" for k, n in
                                                          sorted(kinds.items(), key=lambda x: -x[1]))
                  + " (see the Projects page; add important ones to config/backup_sources.txt)."))
    if c["hw_state"] == "missing":
        r.append(("sometime", "Hardware collector not installed: re-run ./install.sh to get disk health and memory details."))
    elif c["hw_state"] == "stale":
        r.append(("now", "Hardware data is stale: `sudo systemctl status my-system-hwinfo`."))
    return r


# ---------------------------------------------------------------- document model
def build(c, prev):
    """List of sections: (title, intro, [blocks]); block = ('p', text) | ('table', head, rows) | ('list', items)."""
    S = []
    recs = recommendations(c)
    now = [t for p, t in recs if p == "now"]
    root = next((m for m in c["mounts"] if m["mount"] == "/"), None)
    disks = disks_of(c)
    health = "all disks healthy" if disks and all(d.get("passed") is not False for d in disks) else \
        ("disk health unknown (collector not installed)" if not disks else "a disk reports problems")
    machine = f"{c['bios']['vendor']} {c['bios']['product']}".strip() or "this machine"
    summary = (f"{c['os']} on {machine}, {c['cpu']['threads']} threads, "
               f"{gib(c['mem_total'])} RAM; {health}. "
               + (f"Root disk {root['used'] / root['total'] * 100:.0f}% used ({gb(root['free'])} free, {c['forecast']}). "
                  if root else "")
               + (f"{len(now)} item(s) need attention now." if now else "Nothing urgent."))
    items = [f"**{p}** — {t}" for p, t in recs[:8]]
    S.append(("Summary", summary, [("list", items)] if items else []))

    hwrows = [("Model", f"{c['bios']['vendor']} {c['bios']['product']}" + (f" (SKU {c['bios']['sku']})" if c['bios']['sku'] else "")),
              ("BIOS", f"{c['bios']['bios_version']} ({c['bios']['bios_date']})"),
              ("CPU", f"{c['cpu']['model']}, {c['cpu']['cores'] or '?'} cores / {c['cpu']['threads']} threads"
                      + (f", up to {c['cpu']['max_ghz']} GHz" if c['cpu']['max_ghz'] else ""))]
    for i, (name, drv) in enumerate(c["gpus"], 1):
        hwrows.append((f"Graphics {i}", f"{name} — driver: {drv or 'none'}"))
    for n, stt, sp in c["net"]:
        hwrows.append((f"Network {n}", f"{stt}" + (f", {sp} Mb/s" if sp else "")))
    hwrows = [r for r in hwrows if r[1].strip(" ()")]
    S.append(("Hardware", "", [("table", ["Part", "Details"], hwrows)]))

    mem = c["hw"].get("memory") or {}
    blocks = []
    mods = mem.get("modules") or []
    if mods:
        blocks.append(("table", ["Slot", "Size", "Type", "Maker", "Part number", "Rated", "Running at"],
                       [(m["slot"], m["size"], m["type"], hw.maker(m["maker"]), m["part"], m["speed"], m["configured_speed"]) for m in mods]))
        n = hw.memory_note(mods)
        if n:
            blocks.append(("p", n[0].upper() + n[1:] + "."))
    else:
        blocks.append(("p", "Module details need the hardware collector (" + (mem.get("error") or "not installed") + ")."))
    sw = [(s[0], s[1], gib(s[2]), gib(s[3]) if s[3] >= 2**30 else f"{s[3] / 2**20:.0f} MiB", str(s[4])) for s in c["swaps"]]
    if sw:
        blocks.append(("table", ["Swap", "Type", "Size", "Used", "Priority"], sw))
    for z in c["zram"]:
        ratio = z["stored"] / z["compressed"] if z["compressed"] else 0
        blocks.append(("p", f"zram {z['device']}: {gib(z['size'])} compressed swap in RAM ({z['algorithm']}), "
                            f"{hw.human_bytes(z['stored'])} stored" + (f", ratio {ratio:.1f}×" if ratio else "") + "."))
    smax, savg = c["swap_30d"]
    blocks.append(("p", f"Now: {gib(c['mem_total'] - c['mem_avail'])} of {gib(c['mem_total'])} in use, "
                        f"{gib(c['mem_avail'])} available." + (f" Swap peak in 30 days: {smax:.1f} GB." if smax is not None else "")))
    S.append(("Memory and swap", f"{gib(c['mem_total'])} RAM" + (f" in {len(mods)} of {mem.get('slots')} slots"
                                                                   + (f" (max {mem['max_capacity']})" if mem.get("max_capacity") else "") if mods else "") + ".",
              blocks))

    rows = []
    for m in c["mounts"]:
        role = "system" if m["mount"] == "/" else "data disk" if m["mount"] in c["data_disks"] else \
            "network share" if m["mount"] in c["net_shares"] else ""
        rows.append((m["mount"], m["device"], m["fs"], gb(m["total"]), f"{gb(m['used'])} ({m['used'] / m['total'] * 100:.0f}%)",
                     gb(m["free"]), role))
    blocks = [("table", ["Mount", "Device", "Type", "Size", "Used", "Free", "Role"], rows),
              ("p", f"Root disk trend: {c['forecast']}.")]
    if disks:
        cols = ["", *[f"{d.get('model')} ({d.get('device')})" for d in disks]]

        def row(label, f):
            return (label, *[f(d) for d in disks])
        drows = [row("Health (SMART)", lambda d: "PASSED" if d.get("passed") else "FAILED" if d.get("passed") is False else "?"),
                 row("Type / size", lambda d: f"{d.get('kind', '?').upper()} {hw.human_bytes(d.get('capacity_bytes'))}"),
                 row("Firmware", lambda d: d.get("firmware", "")),
                 row("Power-on time", lambda d: f"{d['power_on_hours']:,} h ({d['power_on_hours'] / 8760:.1f} years)" if d.get("power_on_hours") else "–"),
                 row("Wear", lambda d: f"{d['wear_pct']}%" if d.get("wear_pct") is not None else "–"),
                 row("Written", lambda d: f"{d['written_tb']} TB" if d.get("written_tb") is not None else "–"),
                 row("Bad / pending sectors", lambda d: f"{d.get('reallocated', '–')} / {d.get('pending', '–')}" if d.get("kind") != "nvme" else "–"),
                 row("Media errors", lambda d: str(d.get("media_errors", d.get("uncorrectable", "–")))),
                 row("Unsafe shutdowns", lambda d: str(d["unsafe_shutdowns"]) if "unsafe_shutdowns" in d else
                     (f"{d['power_off_retracts']} emergency head retracts" if d.get("power_off_retracts") is not None else "–")),
                 row("Temperature", lambda d: f"{d['temperature']}°C" if d.get("temperature") is not None else "–")]
        blocks.append(("h3", "Disk health"))
        blocks.append(("table", cols, drows))
        fnd = [t for d in disks for lvl, t in hw.disk_findings(d) if lvl != "ok"]
        if fnd:
            blocks.append(("list", fnd))
    else:
        blocks.append(("p", "Disk health (SMART) needs the hardware collector."))
    g = growth(c)
    if g:
        blocks.append(("h3", "Largest folders"))
        since = min(x[3] for x in g)
        blocks.append(("table", ["Folder", "Size", f"Change since {since}", "Disk"],
                       [(f, f"{now_:.1f} GB", f"{d:+.1f} GB", disk) for f, now_, d, _, disk in sorted(g, key=lambda r: -r[1])[:20]]))
    S.append(("Storage", "", blocks))

    trows = [(s, f"{t}°C", f"{h}°C" if h else "–", f"{cr}°C" if cr else "–") for s, t, h, cr in c["temps"]]
    blocks = [("table", ["Sensor", "Now", "High", "Critical"], trows)] if trows else []
    if c["fans"]:
        blocks.append(("table", ["Fan", "Speed", "Max"], [(lab, f"{rpm} rpm", f"{mx_} rpm" if mx_ else "–") for lab, rpm, mx_ in c["fans"]]))
    tmax, tavg = c["temp_30d"]
    intro = f"CPU now {c['cpu_temp']}°C" if c["cpu_temp"] is not None else "No CPU sensor"
    if tmax is not None:
        intro += f"; last 30 days: peak {tmax:.0f}°C, average {tavg:.0f}°C"
    lmax, lavg = c["load_30d"]
    if lmax is not None:
        intro += f". Load average peak {lmax:.0f} on {c['cpu']['threads']} threads (average {lavg:.1f})"
    S.append(("Temperature and cooling", intro + ".", blocks))

    rows = [("Release", c["os"]), ("Kernel running", c["kernel_running"]),
            ("Kernels installed", ", ".join(c["kernels"]) or "–"),
            ("Reboot", "required" if c["reboot_required"] else "not needed"), ("Uptime", f"{c['uptime_days']} days"),
            ("Secure Boot", c["secure_boot"]), ("Power profile", c["power_profile"] or "–"),
            ("Updates waiting", f"{len(c['upgradable'])}" + (": " + ", ".join(c["upgradable"][:12]) if c["upgradable"] else "")),
            ("Failed services", ", ".join(u for u, _ in c["failed_units"]) or "none")]
    fw = c["hw"].get("firmware") or {}
    rows.append(("Firmware updates", ", ".join(f"{u['device']} → {u['available']}" for u in fw.get("updates", [])) or
                 ("none" if "updates" in fw else fw.get("error", "unknown"))))
    blocks = [("table", ["Item", "State"], rows)]
    if c["repos"]:
        blocks.append(("h3", "Package sources"))
        blocks.append(("table", ["File", "Address", "Suites", "Enabled"],
                       [(f, u, s, "yes" if e else "no") for f, u, s, e in c["repos"]]))
    S.append(("Operating system and security", "", blocks))

    blocks = []
    if c["toolchains"]:
        blocks.append(("table", ["Toolchain", "Location"], [(a, b) for a, b in c["toolchains"]]))
    if c["tools"]:
        blocks.append(("h3", "Command-line tools"))
        blocks.append(("table", ["Tool", "Path", "Version"], c["tools"]))
    if c["docker_images"]:
        blocks.append(("h3", "Docker images"))
        blocks.append(("table", ["Image", "Size"], c["docker_images"]))
    S.append(("Software", f"{len(c['apt_manual'])} packages installed by hand; {len(c['toolchains'])} toolchains detected.", blocks))

    rows = [(v["path"].replace(str(config.HOME), "~"), v.get("python") or "?",
             str(v["packages"]) if v.get("packages") is not None else "–") for v in c["venvs"]]
    S.append(("Python environments", f"{len(rows)} environments found.",
              [("table", ["Environment", "Python", "Packages"], rows)] if rows else []))

    gp = [(g["path"].replace(str(config.HOME), "~"), g.get("branch", ""), ", ".join(gitstat.problems(g)) or "ok") for g in c["git"]]
    S.append(("Projects", f"{len(gp)} git repositories; {sum(1 for x in gp if x[2] != 'ok')} need attention.",
              [("table", ["Repository", "Branch", "State"], sorted(gp, key=lambda r: (r[2] == "ok", r[0])))] if gp else []))

    bk = c["backup"]
    rows = [("State", bk["state"][1]),
            ("Last success", dt.datetime.fromtimestamp(float(bk["last_success"])).strftime("%Y-%m-%d %H:%M")
             if bk.get("last_success") and float(bk["last_success"]) > 0 else "–"),
            ("New data last time", bk.get("last_size") or "–"), ("Repository", config.backup_repo() or "–"),
            ("Restore test", (bk.get("verify") or "not yet") + (
                f" ({dt.datetime.fromtimestamp(float(bk['verify_run'])):%Y-%m-%d})" if bk.get("verify_run") else ""))]
    S.append(("Backup", "", [("table", ["Item", "State"], rows)]))

    ch = changes(c, prev)
    if prev:
        S.append(("Changes since the last report", f"Compared with {prev.get('time', prev.get('date'))}.",
                  [("table", ["What", "Change"], ch)] if ch else [("p", "No notable changes.")]))

    grouped = []
    for pr in ("now", "when idle", "sometime"):
        items = [t for p, t in recs if p == pr]
        if items:
            grouped.append(("h3", {"now": "Now", "when idle": "When nothing is running", "sometime": "When there is time"}[pr]))
            grouped.append(("list", items))
    S.append(("Open issues and recommendations", "" if recs else "Nothing to do.", grouped))
    return S


# ---------------------------------------------------------------- render
def _md_cell(x):
    return str(x).replace("|", "\\|").replace("\n", " ")


def to_md(c, S):
    out = [f"# System report — {c['label']} — {c['time']}", "",
           f"_my-system {c['version']} · {c['host']} · read-only snapshot_", ""]
    for title, intro, blocks in S:
        out += [f"## {title}", ""]
        if intro:
            out += [intro, ""]
        for b in blocks:
            if b[0] == "p":
                out += [b[1], ""]
            elif b[0] == "h3":
                out += [f"### {b[1]}", ""]
            elif b[0] == "list":
                out += [f"- {x}" for x in b[1]] + [""]
            elif b[0] == "table":
                out += ["| " + " | ".join(map(_md_cell, b[1])) + " |", "|" + "---|" * len(b[1])]
                out += ["| " + " | ".join(map(_md_cell, r)) + " |" for r in b[2]] + [""]
    return "\n".join(out)


def _inline(t):
    t = html.escape(str(t))
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", t)


CSS = """
:root{--bg:#f7f7f5;--card:#fff;--fg:#1d1d1f;--mut:#6b6b70;--line:#e3e3e0;--acc:#2f6fdb;--warn:#b25b00;--bad:#c62828;--ok:#2e7d32}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1c1c1f;--fg:#ececee;--mut:#9a9aa2;--line:#2c2c31;--acc:#7aa7ff;--warn:#f0a040;--bad:#ff6b6b;--ok:#6fcf73}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Segoe UI",Ubuntu,sans-serif}
main{max-width:1040px;margin:0 auto;padding:28px 18px 60px}h1{font-size:26px;margin:0 0 4px}
.sub{color:var(--mut);margin-bottom:22px}nav{display:flex;flex-wrap:wrap;gap:6px 14px;margin:0 0 22px;font-size:13px}
nav a{color:var(--acc);text-decoration:none}section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin:0 0 16px}
h2{font-size:18px;margin:0 0 8px}h3{font-size:14px;margin:18px 0 6px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.tw{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13.5px;margin:6px 0}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--mut);font-weight:600}
td:first-child{font-weight:500}code{font:12.5px ui-monospace,Menlo,monospace;background:var(--bg);padding:1px 5px;border-radius:5px}
ul{margin:6px 0;padding-left:20px}li{margin:3px 0}.now{color:var(--bad)}.idle{color:var(--warn)}
"""


def to_html(c, S):
    nav = " ".join(f'<a href="#s{i}">{html.escape(t)}</a>' for i, (t, _, _) in enumerate(S))
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>System report {html.escape(c['label'])} {c['date']}</title><style>{CSS}</style></head><body><main>",
             f"<h1>System report — {html.escape(c['label'])}</h1>",
             f"<div class='sub'>{c['time']} · {html.escape(c['os'])} · my-system {c['version']} · read-only snapshot</div><nav>{nav}</nav>"]
    for i, (title, intro, blocks) in enumerate(S):
        parts.append(f"<section id='s{i}'><h2>{html.escape(title)}</h2>")
        if intro:
            parts.append(f"<p>{_inline(intro)}</p>")
        for b in blocks:
            if b[0] == "p":
                parts.append(f"<p>{_inline(b[1])}</p>")
            elif b[0] == "h3":
                parts.append(f"<h3>{html.escape(b[1])}</h3>")
            elif b[0] == "list":
                lis = []
                for x in b[1]:
                    cls = " class='now'" if str(x).startswith("**now**") else " class='idle'" if str(x).startswith("**when idle**") else ""
                    lis.append(f"<li{cls}>{_inline(x)}</li>")
                parts.append("<ul>" + "".join(lis) + "</ul>")
            elif b[0] == "table":
                head = "".join(f"<th>{html.escape(str(h))}</th>" for h in b[1])
                body = "".join("<tr>" + "".join(f"<td>{_inline(x)}</td>" for x in r) + "</tr>" for r in b[2])
                parts.append(f"<div class='tw'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>")
        parts.append("</section>")
    parts.append("</main></body></html>")
    return "".join(parts)


def snapshot(c):
    s = {k: v for k, v in c.items() if k not in ("sizes_hist",)}
    return json.loads(json.dumps(s, default=list))


def run(notify=False):
    OUT.mkdir(parents=True, exist_ok=True)
    c = collect()
    prev = previous_snapshot(c["date"])
    S = build(c, prev)
    base = OUT / c["date"]
    base.with_suffix(".md").write_text(to_md(c, S))
    base.with_suffix(".html").write_text(to_html(c, S))
    base.with_suffix(".json").write_text(json.dumps(snapshot(c), indent=1))
    for ext in (".html", ".md"):
        link = OUT / f"latest{ext}"
        try:
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(base.name + ext)
        except OSError:
            pass
    recs = recommendations(c)
    if notify:
        now = [t for p, t in recs if p == "now"]
        msg = (f"{len(now)} item(s) need attention: " + "; ".join(now[:3])) if now else "Nothing urgent."
        subprocess.call([str(config.MS / "bin" / "notify.sh"), "Monthly system report ready", msg[:400], "default", "page_facing_up"])
    return base.with_suffix(".html"), recs
