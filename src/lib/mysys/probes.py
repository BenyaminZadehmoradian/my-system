"""Read-only probes of the running system (no external commands where avoidable)."""
import os
import platform
import re
import shutil
import socket
from pathlib import Path


def disk(path):
    """(used, free, total) bytes, or None if not mounted / unreadable."""
    try:
        if path not in ("/", "") and not os.path.ismount(path):
            return None
        u = shutil.disk_usage(path or "/")
        # like df: percentages relative to the space usable by you (excludes root-reserved blocks)
        return u.used, u.free, u.used + u.free
    except OSError:
        return None


def meminfo():
    out = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                out[k] = int(v.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return out


def cpu_temp():
    """Package temperature in °C from the best available sensor."""
    best = None
    for hw in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            name = (hw / "name").read_text().strip()
        except OSError:
            continue
        if name in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
            for f in sorted(hw.glob("temp*_input")):
                try:
                    return int(f.read_text()) // 1000
                except (OSError, ValueError):
                    continue
        if name == "acpitz" and best is None:
            try:
                best = int((hw / "temp1_input").read_text()) // 1000
            except (OSError, ValueError):
                pass
    return best


def threads():
    return os.cpu_count() or 1


def loadavg():
    try:
        return os.getloadavg()
    except OSError:
        return (0.0, 0.0, 0.0)


def boot_time():
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def uptime_s():
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def product():
    for f in ("product_name", "board_name"):
        try:
            v = Path("/sys/class/dmi/id", f).read_text().strip()
            if v and "To be filled" not in v:
                return v
        except OSError:
            pass
    return platform.machine()


def os_name():
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    return platform.system()


def hostname():
    return socket.gethostname()


def reboot_required():
    return Path("/var/run/reboot-required").exists()


def mounts():
    """[(device, mountpoint, fstype)] from /proc/mounts (octal escapes decoded)."""
    out = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                p = line.split()
                if len(p) >= 3:
                    out.append((p[0], p[1].encode().decode("unicode_escape"), p[2]))
    except OSError:
        pass
    return out


def processes(uid=None):
    """Yield dicts for processes of `uid` (default: current user)."""
    uid = os.getuid() if uid is None else uid
    hz = os.sysconf("SC_CLK_TCK")
    page = os.sysconf("SC_PAGE_SIZE")
    bt = boot_time()
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            if p.stat().st_uid != uid:
                continue
            argv = [a.decode(errors="replace") for a in (p / "cmdline").read_bytes().split(b"\0") if a]
            if not argv:
                continue
            stat = (p / "stat").read_text()
            f = stat[stat.rfind(")") + 2:].split()
            yield {
                "pid": int(p.name),
                "ppid": int(f[1]),
                "argv": argv,
                "comm": stat[stat.find("(") + 1:stat.rfind(")")],
                "cpu_ticks": int(f[11]) + int(f[12]),
                "start": bt + int(f[19]) / hz,
                "rss": int(f[21]) * page,
                "cwd": os.readlink(p / "cwd"),
            }
        except (OSError, ValueError, IndexError):
            continue


# ---------------- extra read-only probes (no root needed) ----------------
HWINFO = Path(os.environ.get("MS_HWINFO", "/var/lib/my-system/hw.json"))


def _read(p, default=""):
    try:
        return Path(p).read_text().strip()
    except OSError:
        return default


def _cmd(args, timeout=10):
    import subprocess
    try:
        r = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout, env=dict(os.environ, LC_ALL="C"))
        return r.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def hwinfo():
    """Data of the root hardware collector, or {} when it is not installed."""
    import json
    try:
        return json.loads(HWINFO.read_text())
    except (OSError, ValueError):
        return {}


def cpu_info():
    model, cores = "", set()
    phys = core = None
    try:
        for line in open("/proc/cpuinfo"):
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            if k == "model name" and not model:
                model = v
            elif k == "physical id":
                phys = v
            elif k == "core id":
                core = v
            elif not k and phys is not None:
                cores.add((phys, core))
                phys = core = None
        if phys is not None:
            cores.add((phys, core))
    except OSError:
        pass
    mhz = _read("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    return {"model": re.sub(r"\s+", " ", model), "cores": len(cores) or None, "threads": threads(),
            "max_ghz": round(int(mhz) / 1e6, 1) if mhz.isdigit() else None}


def bios():
    d = "/sys/class/dmi/id/"
    return {"vendor": _read(d + "sys_vendor"), "product": _read(d + "product_name"),
            "sku": _read(d + "product_sku"), "bios_version": _read(d + "bios_version"),
            "bios_date": _read(d + "bios_date"), "board": _read(d + "board_name")}


def fans():
    """[(label, rpm, max_rpm)] from hwmon."""
    out = []
    for hw in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        for f in sorted(hw.glob("fan*_input")):
            n = f.name.split("_")[0]
            rpm = _read(f)
            if not rpm.isdigit():
                continue
            lab = _read(hw / f"{n}_label") or f"{_read(hw / 'name')} {n}"
            mx = _read(hw / f"{n}_max")
            out.append((lab, int(rpm), int(mx) if mx.isdigit() and int(mx) > 0 else None))
    return out


def temps():
    """[(sensor, °C, high, critical)] for CPU package / NVMe / drive sensors."""
    out = []
    for hw in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        name = _read(hw / "name")
        if name not in ("coretemp", "k10temp", "zenpower", "nvme", "drivetemp", "acpitz", "amdgpu", "dell_smm"):
            continue
        for f in sorted(hw.glob("temp*_input")):
            n = f.name.split("_")[0]
            v = _read(f)
            if not v.lstrip("-").isdigit():
                continue
            lab = _read(hw / f"{n}_label") or n
            if name == "coretemp" and not lab.startswith("Package"):
                continue
            hi, cr = _read(hw / f"{n}_max"), _read(hw / f"{n}_crit")
            out.append((f"{name}: {lab}", int(v) // 1000, int(hi) // 1000 if hi.lstrip("-").isdigit() else None,
                        int(cr) // 1000 if cr.lstrip("-").isdigit() else None))
    return out


def swaps():
    """[(device, type, size_bytes, used_bytes, priority)]."""
    out = []
    for line in _read("/proc/swaps").splitlines()[1:]:
        p = line.split()
        if len(p) >= 5:
            out.append((p[0], p[1], int(p[2]) * 1024, int(p[3]) * 1024, int(p[4])))
    return out


def zram():
    out = []
    for z in sorted(Path("/sys/block").glob("zram*")):
        size = _read(z / "disksize")
        if not size.isdigit() or int(size) == 0:
            continue
        algo = re.search(r"\[(\w+)\]", _read(z / "comp_algorithm"))
        mm = _read(z / "mm_stat").split()
        out.append({"device": z.name, "size": int(size), "algorithm": algo.group(1) if algo else "",
                    "stored": int(mm[0]) if mm else 0, "compressed": int(mm[1]) if len(mm) > 1 else 0,
                    "ram_used": int(mm[2]) if len(mm) > 2 else 0})
    return out


def secure_boot():
    """'on', 'off', 'legacy BIOS' or 'unknown'."""
    if not Path("/sys/firmware/efi").exists():
        return "legacy BIOS"
    for f in Path("/sys/firmware/efi/efivars").glob("SecureBoot-*"):
        try:
            b = f.read_bytes()
            return "on" if b and b[-1] == 1 else "off"
        except OSError:
            break
    return "unknown"


def apt_repos():
    """[(name, uri, suites, enabled)] from sources.list and sources.list.d."""
    out = []
    base = Path("/etc/apt")
    files = [base / "sources.list"] + sorted((base / "sources.list.d").glob("*"))
    for f in files:
        txt = _read(f)
        if not txt:
            continue
        if f.suffix == ".sources":
            for block in re.split(r"\n\s*\n", txt):
                kv = {}
                for line in block.splitlines():
                    if line.startswith("#") or ":" not in line or line.startswith(" "):
                        continue
                    k, _, v = line.partition(":")
                    kv[k.strip().lower()] = v.strip()
                if "uris" in kv:
                    out.append((f.name, kv["uris"], kv.get("suites", ""),
                                kv.get("enabled", "yes").lower() not in ("no", "false")))
        elif f.suffix == ".list" or f.name == "sources.list":
            for line in txt.splitlines():
                s = line.strip()
                enabled = not s.startswith("#")
                s = s.lstrip("# ").strip()
                m = re.match(r"deb\s+(?:\[[^\]]*\]\s+)?(\S+)\s+(\S+)", s)
                if m and (enabled or line.lstrip().startswith("# deb")):
                    out.append((f.name, m.group(1), m.group(2), enabled))
    return out


def kernels():
    run = os.uname().release
    inst = sorted({p.name[len("vmlinuz-"):] for p in Path("/boot").glob("vmlinuz-*")}, reverse=True)
    return run, inst


def power_profile():
    return _cmd(["powerprofilesctl", "get"], 5).strip()


def gpus():
    """[(name, driver)] from lspci."""
    out = []
    for block in _cmd(["lspci", "-vmmk"]).split("\n\n"):
        kv = dict(l.split(":\t", 1) for l in block.splitlines() if ":\t" in l)
        if kv.get("Class", "").startswith(("VGA", "3D", "Display")):
            out.append((f"{kv.get('Vendor', '')} {kv.get('Device', '')}".strip(), kv.get("Driver", "")))
    return out


def net_links():
    """[(iface, state, speed Mb/s)] for physical interfaces."""
    out = []
    for n in sorted(Path("/sys/class/net").iterdir()):
        if not (n / "device").exists():
            continue
        sp = _read(n / "speed")
        out.append((n.name, _read(n / "operstate"), int(sp) if sp.lstrip("-").isdigit() and int(sp) > 0 else None))
    return out


def failed_units():
    out = _cmd(["systemctl", "--failed", "--no-legend", "--plain"])
    return [(l.split()[0], l.split()[1]) for l in out.splitlines() if l.strip() and len(l.split()) > 1]


def apt_upgradable():
    out = _cmd(["apt", "list", "--upgradable"], 60)
    return [l.split("/")[0] for l in out.splitlines() if "upgradable" in l]


# ---------------------------------------------------------------- pressure, responsiveness, processes (1.6)
PSI_DIR = os.environ.get("MS_PSI_DIR", "/proc/pressure")


def psi():
    """Pressure stall information (kernel PSI): {'cpu': {'some': avg10, ...}, 'memory': {'some', 'full'}, 'io': ...}.
    'full' memory pressure = every task waited for memory (thrashing). Empty dict when the kernel has no PSI."""
    out = {}
    for res in ("cpu", "memory", "io"):
        try:
            with open(os.path.join(PSI_DIR, res)) as f:
                for line in f:
                    kind, *kv = line.split()
                    vals = dict(x.split("=") for x in kv)
                    out.setdefault(res, {})[kind] = float(vals.get("avg10", 0))
                    out[res][kind + "60"] = float(vals.get("avg60", 0))
        except (OSError, ValueError):
            continue
    return out


def responsive(path, timeout=5.0):
    """True / False (hung, e.g. a network share that stopped answering) / None (not there).
    The check runs in a thread so a hung mount cannot block the caller."""
    import threading
    res = {}

    def look():
        try:
            os.statvfs(path)
            res["ok"] = True
        except FileNotFoundError:
            res["ok"] = None
        except OSError:
            res["ok"] = False
    t = threading.Thread(target=look, daemon=True)
    t.start()
    t.join(timeout)
    return res.get("ok", False) if not t.is_alive() else False


def boot_id():
    return _read("/proc/sys/kernel/random/boot_id").strip()


def top_procs(n=8, prev=None):
    """Biggest processes by RAM: [(pid, name, rss_bytes, cpu_ticks)]. `prev` is unused (kept for callers)."""
    page = os.sysconf("SC_PAGE_SIZE")
    rows = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            st = (p / "stat").read_text()
            name = st[st.find("(") + 1:st.rfind(")")]
            f = st[st.rfind(")") + 2:].split()
            rows.append((int(p.name), name, int(f[21]) * page, int(f[11]) + int(f[12])))
        except (OSError, ValueError, IndexError):
            continue
    rows.sort(key=lambda r: -r[2])
    return rows[:n]


def cpu_siblings():
    """[[cpu, sibling], ...] one list per physical core (hyper-threads together)."""
    seen, cores = set(), []
    for d in sorted(Path("/sys/devices/system/cpu").glob("cpu[0-9]*"), key=lambda x: int(x.name[3:])):
        try:
            txt = (d / "topology" / "thread_siblings_list").read_text().strip()
        except OSError:
            continue
        ids = []
        for part in txt.split(","):
            a, _, b = part.partition("-")
            ids += list(range(int(a), int(b or a) + 1))
        key = tuple(sorted(ids))
        if key not in seen:
            seen.add(key)
            cores.append(list(key))
    return cores or [[i] for i in range(threads())]
