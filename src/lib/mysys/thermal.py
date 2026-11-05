"""Keep the CPU below a temperature: status, daily check, and setting it up.

  ms thermal                  status: temperature now and today, the limits in force, what keeps them
  ms thermal check            what the daily check warns about (limits lost after a reboot, hotter than allowed)
  ms thermal setup [80]       keep the CPU below 80 °C on Intel CPUs (sudo): thermald trip point + a boot service
                              that caps short turbo bursts and the top frequency; MAX_TEMP_C for queue jobs
  ms thermal off              remove what setup installed and give the CPU its full speed back (sudo)

Three layers, as found on an i7-10700 (Dell Precision 3440, Nov 2026):
  * thermald (passive trip point, RAPL): holds the package at about LIMIT-8 °C under full load;
  * cpu-cool.service: PL2 (short turbo bursts) 80 W — the first seconds of a heavy load jumped 10 °C before
    thermald could react — and max_perf_pct 85 %, because a few cores at full turbo voltage run hotter than
    sixteen at a lower clock (89 °C right after a full load ended);
  * MAX_TEMP_C: queue jobs here wait / pause near the limit (they still go to servers).
"""
import os
import re
import subprocess
import time

from . import config, probes

THERMALD_CONF = "/etc/thermald/thermal-conf.xml"
COOL_SH = "/usr/local/sbin/cpu-cool.sh"
COOL_UNIT = "/etc/systemd/system/cpu-cool.service"
PSTATE = "/sys/devices/system/cpu/intel_pstate/max_perf_pct"
RAPL = "/sys/class/powercap/intel-rapl:0"
MARK = "Keep CPU below"


def _read(p):
    try:
        with open(p) as f:
            return f.read()
    except OSError:
        return ""


def _systemctl(*a):
    try:
        r = subprocess.run(["systemctl", *a], capture_output=True, text=True, errors="replace", timeout=10)
        return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def short_term_constraint():
    """Index of the RAPL 'short_term' (PL2) constraint, or None."""
    for i in range(4):
        if _read(f"{RAPL}/constraint_{i}_name").strip() == "short_term":
            return i
    return None


def current():
    """What the CPU uses now: max_perf_pct, PL2 (W)."""
    out = {"max_perf_pct": None, "pl2_w": None}
    v = _read(PSTATE).strip()
    out["max_perf_pct"] = int(v) if v.isdigit() else None
    i = short_term_constraint()
    if i is not None:
        v = _read(f"{RAPL}/constraint_{i}_power_limit_uw").strip()
        out["pl2_w"] = round(int(v) / 1e6) if v.isdigit() else None
    return out


def wanted():
    """What cpu-cool.sh sets (None when it is not installed)."""
    t = _read(COOL_SH)
    if not t:
        return None
    perf = re.search(r"echo\s+(\d+)\s*>\s*\S*max_perf_pct", t)
    pl2 = re.search(r"echo\s+(\d+)\s*>\s*\S*power_limit_uw", t)
    return {"max_perf_pct": int(perf.group(1)) if perf else None,
            "pl2_w": round(int(pl2.group(1)) / 1e6) if pl2 else None}


def trip_c():
    m = re.search(r"<Temperature>(\d+)</Temperature>", _read(THERMALD_CONF))
    return int(m.group(1)) / 1000 if m else None


def max_temp(hours=24, now=None):
    """Highest CPU temperature recorded in the last hours (minute samples of the watcher), or None."""
    from . import history
    now = now or time.time()
    best = None
    for d in range(int(hours // 24) + 2):
        day = time.strftime("%Y-%m-%d", time.localtime(now - d * 86400))
        for s in history._read_day(day):
            if s.get("temp") is not None and now - s.get("t", 0) <= hours * 3600:
                best = s["temp"] if best is None else max(best, s["temp"])
    return best


def limit():
    try:
        v = float(config.get("MAX_TEMP_C") or 0)
    except (TypeError, ValueError):
        v = 0
    return v or None


def status():
    w = wanted()
    return {"temp": probes.cpu_temp(), "max_24h": max_temp(24), "limit": limit(),
            "thermald": _systemctl("is-active", "thermald"), "thermald_ours": MARK in _read(THERMALD_CONF),
            "trip_c": trip_c(), "service": _systemctl("is-enabled", "cpu-cool.service") if w else "",
            "current": current(), "wanted": w}


def check(st=None):
    """[(level, text)] for the daily check: '!!' = warning, 'ok', '--' = information."""
    st = st or status()
    out = []
    if st["wanted"]:
        cur, w = st["current"], st["wanted"]
        lost = [k for k in ("max_perf_pct", "pl2_w") if w.get(k) is not None and cur.get(k) is not None and cur[k] != w[k]]
        if lost:
            out.append(("!!", "CPU cooling limits are not in force (" + ", ".join(
                f"{k} {cur[k]} instead of {w[k]}" for k in lost) + "): sudo systemctl restart cpu-cool.service"))
        else:
            out.append(("ok", f"CPU cooling limits in force (max {cur['max_perf_pct']}%, bursts {cur['pl2_w']} W)"))
        if st["service"] and st["service"] != "enabled":
            out.append(("!!", f"cpu-cool.service is {st['service']}: the limits are gone after the next reboot "
                              f"(sudo systemctl enable cpu-cool.service)"))
    if st["thermald_ours"] and st["thermald"] != "active":
        out.append(("!!", f"thermald is {st['thermald'] or 'not running'}: nothing holds the CPU temperature "
                          f"(sudo systemctl restart thermald)"))
    lim = st["limit"]
    if lim and st["max_24h"] is not None:
        if st["max_24h"] >= lim + 3:
            out.append(("!!", f"CPU reached {st['max_24h']:.0f}°C in the last 24 h (limit {lim:.0f}°C)"
                              + ("" if st["wanted"] else ": ms thermal setup " + f"{lim:.0f}")))
        else:
            out.append(("ok", f"CPU at most {st['max_24h']:.0f}°C in the last 24 h (limit {lim:.0f}°C)"))
    return out


def show(st=None):
    st = st or status()
    t = st["temp"]
    lines = [f"CPU now {t:.0f}°C" if t is not None else "CPU temperature: no sensor",
             f"highest in the last 24 h: {st['max_24h']:.0f}°C" if st["max_24h"] is not None else
             "highest in the last 24 h: no record yet",
             f"MAX_TEMP_C (queue jobs here wait/pause): {st['limit']:.0f}°C" if st["limit"] else
             "MAX_TEMP_C: off (queue jobs ignore the temperature)"]
    if st["thermald_ours"]:
        lines.append(f"thermald: {st['thermald']}, holds the CPU near {st['trip_c']:.0f}°C" if st["trip_c"] else
                     f"thermald: {st['thermald']}")
    else:
        lines.append(f"thermald: {st['thermald'] or 'not installed'} (no temperature trip point of my-system)")
    cur = st["current"]
    if st["wanted"]:
        lines.append(f"cpu-cool.service: {st['service'] or '?'} · now max {cur['max_perf_pct']}% · bursts {cur['pl2_w']} W "
                     f"(set: {st['wanted']['max_perf_pct']}% · {st['wanted']['pl2_w']} W)")
    else:
        lines.append(f"cpu-cool.service: not installed · max {cur['max_perf_pct']}% · bursts {cur['pl2_w']} W")
    for lvl, txt in check(st):
        if lvl != "ok":
            lines.append(f"{lvl} {txt}")
    return "\n".join(lines)


def thermald_xml(trip):
    return f"""<?xml version="1.0"?>
<ThermalConfiguration>
 <Platform>
  <Name>{MARK} {trip + 8:.0f}C (my-system)</Name>
  <ProductName>*</ProductName>
  <Preference>QUIET</Preference>
  <ThermalZones>
   <ThermalZone>
    <Type>cpu</Type>
    <TripPoints>
     <TripPoint>
      <SensorType>x86_pkg_temp</SensorType>
      <Temperature>{int(trip * 1000)}</Temperature>
      <type>passive</type>
      <ControlType>SEQUENTIAL</ControlType>
      <CoolingDevice>
       <index>1</index>
       <type>rapl_controller</type>
       <influence>100</influence>
       <SamplingPeriod>1</SamplingPeriod>
      </CoolingDevice>
     </TripPoint>
    </TripPoints>
   </ThermalZone>
  </ThermalZones>
 </Platform>
</ThermalConfiguration>
"""


def cool_sh(perf, pl2_w):
    return f"""#!/bin/sh
# my-system: keep the CPU cool. Short turbo bursts at most {pl2_w} W, at most {perf}% of the top frequency.
C={RAPL}
i=$(grep -l short_term $C/constraint_*_name | sed 's/.*constraint_\\([0-9]\\)_name/\\1/')
[ -n "$i" ] && echo {int(pl2_w * 1e6)} > $C/constraint_${{i}}_power_limit_uw
echo {perf} > {PSTATE}
"""


COOL_UNIT_TEXT = """[Unit]
Description=Keep the CPU cool (my-system: cpu-cool.sh)
After=thermald.service suspend.target hibernate.target hybrid-sleep.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/cpu-cool.sh

[Install]
WantedBy=multi-user.target suspend.target hibernate.target hybrid-sleep.target
"""


def _sudo_write(path, text, mode="644"):
    r = subprocess.run(["sudo", "tee", path], input=text, text=True, stdout=subprocess.DEVNULL)
    if r.returncode == 0:
        subprocess.run(["sudo", "chmod", mode, path])
    return r.returncode == 0


def setup(lim=80.0, perf=85, pl2_w=80, run=None):
    """Install the three layers (sudo). run: for tests, a function(cmd list) instead of real sudo calls."""
    if not os.path.exists(PSTATE) and run is None:
        print("this CPU has no intel_pstate driver: only the thermald part and MAX_TEMP_C apply")
    steps = [("thermald trip point", THERMALD_CONF, thermald_xml(lim - 8), "644"),
             ("cpu-cool.sh", COOL_SH, cool_sh(perf, pl2_w), "755"),
             ("cpu-cool.service", COOL_UNIT, COOL_UNIT_TEXT, "644")]
    for what, path, text, mode in steps:
        ok = run(["write", path, text]) if run else _sudo_write(path, text, mode)
        print(f"{'ok ' if ok else 'NO '} {what}: {path}")
        if not ok:
            return False
    for cmd in (["systemctl", "daemon-reload"], ["systemctl", "restart", "thermald"],
                ["systemctl", "enable", "--now", "cpu-cool.service"]):
        rc = run(cmd) if run else subprocess.run(["sudo", *cmd]).returncode == 0
        print(f"{'ok ' if rc else 'NO '} {' '.join(cmd)}")
    print(f"\nnow set MAX_TEMP_C={lim:.0f} in my_system.conf (ms unlock; edit; ms lock) so queue jobs respect it too;"
          f"\ncheck with: ms thermal")
    return True


def off(run=None):
    cmds = [["systemctl", "disable", "--now", "cpu-cool.service"], ["rm", "-f", COOL_UNIT, COOL_SH, THERMALD_CONF],
            ["systemctl", "daemon-reload"], ["systemctl", "restart", "thermald"]]
    for cmd in cmds:
        ok = run(cmd) if run else subprocess.run(["sudo", *cmd]).returncode == 0
        print(f"{'ok ' if ok else 'NO '} {' '.join(cmd)}")
    if run is None:
        subprocess.run(["sudo", "sh", "-c", f"echo 100 > {PSTATE}"], stderr=subprocess.DEVNULL)
    print("full speed again (the short-burst limit returns to the firmware value after the next reboot)")
    return True


def cli(a):
    sub = a[0] if a else "status"
    if sub in ("status", "show"):
        print(show())
        return 0
    if sub == "check":
        bad = False
        for lvl, txt in check():
            print(f"  {lvl} {txt}")
            bad = bad or lvl == "!!"
        return 1 if bad else 0
    if sub == "setup":
        try:
            lim = float(a[1]) if len(a) > 1 else 80.0
        except ValueError:
            print("usage: ms thermal setup [LIMIT_C]")
            return 2
        return 0 if setup(lim) else 1
    if sub == "off":
        return 0 if off() else 1
    print(__doc__)
    return 0 if sub in ("help", "-h", "--help") else 2
