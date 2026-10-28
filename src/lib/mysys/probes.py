"""Read-only probes of the running system (no external commands where avoidable)."""
import os
import platform
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
