"""Hardware health from the root collector (state in /var/lib/my-system/hw.json) plus sysfs probes.

Used by the daily health check, `ms report`, `ms doctor` and the desktop app.
"""
import json
import time

from . import config, probes

PREV = config.STATE / "hw_prev.json"


def human_bytes(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or u == "TB":
            return f"{n:.0f} {u}" if u in ("B", "KB") else f"{n:.1f} {u}"
        n /= 1000


def disk_label(d):
    cap = d.get("capacity_bytes")
    return f"{d.get('model', '?')} ({human_bytes(cap)}, {d.get('kind', '?').upper()})" if cap else d.get("model", "?")


def disk_findings(d):
    """[(level, text)] for one disk; level in ok | warn | info."""
    out = []
    name = f"{d.get('model', '?')} {d.get('device', '')}".strip()
    if d.get("passed") is False:
        out.append(("warn", f"{name}: SMART health check FAILED — back up now and replace the disk"))
    for key, what in (("media_errors", "media errors"), ("reallocated", "reallocated sectors"),
                      ("pending", "sectors pending reallocation"), ("offline_uncorrectable", "uncorrectable sectors"),
                      ("uncorrectable", "uncorrectable errors")):
        v = d.get(key)
        if isinstance(v, int) and v > 0:
            out.append(("warn", f"{name}: {v} {what}"))
    if d.get("critical_warning"):
        out.append(("warn", f"{name}: NVMe critical warning flag {d['critical_warning']}"))
    w = d.get("wear_pct")
    if isinstance(w, int):
        if w >= 90:
            out.append(("warn", f"{name}: {w}% of rated write endurance used — plan a replacement"))
        elif w >= 70:
            out.append(("info", f"{name}: {w}% of rated write endurance used"))
    sp, th = d.get("spare_pct"), d.get("spare_threshold_pct")
    if isinstance(sp, int) and isinstance(th, int) and sp <= th + 5:
        out.append(("warn", f"{name}: spare blocks low ({sp}%, threshold {th}%)"))
    t = d.get("temperature")
    lim = 55 if d.get("kind") == "hdd" else 75
    if isinstance(t, int) and t >= lim:
        out.append(("warn", f"{name}: {t}°C (limit {lim}°C)"))
    crc = d.get("crc_errors")
    if isinstance(crc, int) and crc > 0:
        out.append(("info", f"{name}: {crc} cable/interface (CRC) errors — check the SATA cable if it grows"))
    if not out:
        out.append(("ok", f"{name}: healthy"))
    return out


def collector_state():
    """('ok'|'stale'|'missing', hw dict, age_hours)."""
    hw = probes.hwinfo()
    if not hw:
        return "missing", {}, None
    age = (time.time() - hw.get("collected_at", 0)) / 3600
    return ("stale" if age > 72 else "ok"), hw, age


def unsafe_delta(hw):
    """{device: increase} of unsafe shutdowns since the previous daily check; updates the stored values."""
    try:
        prev = json.loads(PREV.read_text())
    except (OSError, ValueError):
        prev = {}
    cur, delta = {}, {}
    for d in (hw.get("storage") or {}).get("disks", []):
        v = d.get("unsafe_shutdowns")
        if isinstance(v, int):
            key = d.get("model", "") + d.get("device", "")
            cur[key] = v
            if key in prev and v > prev[key]:
                delta[d.get("model", "?")] = v - prev[key]
    if cur:
        try:
            PREV.write_text(json.dumps(cur))
        except OSError:
            pass
    return delta


def memory_note(mods):
    """Explain mixed modules / reduced speed, or ''."""
    if not mods:
        return ""
    conf = {m.get("configured_speed") for m in mods if m.get("configured_speed")}
    parts = {(m.get("maker"), m.get("part")) for m in mods}
    rated = {m.get("speed") for m in mods}
    notes = []
    if len(parts) > 1:
        notes.append(f"{len(parts)} different module types are installed; all run at the speed of the slowest")
    if conf and rated and conf != rated and len(conf) == 1:
        notes.append(f"configured speed {next(iter(conf))} (rated {', '.join(sorted(r for r in rated if r))})")
    return "; ".join(notes)


def health_lines(update_prev=True):
    """Print the '## Hardware' part of the daily health check ('  !! ' lines are warnings)."""
    st, hw, age = collector_state()
    if st == "missing":
        print("  -- hardware collector not installed (SMART, memory modules, firmware): re-run ./install.sh")
    else:
        if st == "stale":
            print(f"  !! hardware data is {age / 24:.0f} days old (sudo systemctl status my-system-hwinfo)")
        disks = (hw.get("storage") or {}).get("disks", [])
        if (hw.get("storage") or {}).get("error"):
            print(f"  -- disks: {hw['storage']['error']}")
        for d in disks:
            extra = []
            if d.get("wear_pct") is not None:
                extra.append(f"wear {d['wear_pct']}%")
            if d.get("power_on_hours") is not None:
                extra.append(f"{d['power_on_hours']:,} h on")
            if d.get("temperature") is not None:
                extra.append(f"{d['temperature']}°C")
            print(f"     {disk_label(d)}: {', '.join(extra)}")
            for lvl, txt in disk_findings(d):
                print({"warn": "  !! ", "info": "  -- ", "ok": "  ok "}[lvl] + txt)
        if update_prev:
            for model, n in unsafe_delta(hw).items():
                print(f"  -- {model}: {n} new unsafe shutdown(s) since last check (power loss or forced power-off)")
        ups = (hw.get("firmware") or {}).get("updates") or []
        for u in ups:
            print(f"  -- firmware update available: {u['device']} {u['current']} → {u['available']}")
        mods = (hw.get("memory") or {}).get("modules") or []
        if mods:
            print(f"     memory: {len(mods)} modules — " + ", ".join(f"{m['slot']} {m['size']} {m['maker']}" for m in mods))
            n = memory_note(mods)
            if n:
                print(f"  -- memory: {n}")
    for lab, rpm, mx in probes.fans():
        print(f"     fan {lab}: {rpm} rpm" + (f" (max {mx})" if mx else ""))
    for z in probes.zram():
        print(f"     zram {z['device']}: {human_bytes(z['size'])} {z['algorithm']}, {human_bytes(z['stored'])} stored")
