"""Hardware collector parsing, hardware findings and the full system report.
Run: python3 tests/test_hw.py"""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "hw"


def collector():
    loader = importlib.machinery.SourceFileLoader("ms_hwinfo", str(ROOT / "src/root/ms-hwinfo"))
    spec = importlib.util.spec_from_loader("ms_hwinfo", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def fake_hw():
    c = collector()
    nv = c.parse_smart(json.loads((FIX / "smart_nvme.json").read_text()))
    nv["device"] = "/dev/nvme0"
    hd = c.parse_smart(json.loads((FIX / "smart_ata.json").read_text()))
    hd["device"] = "/dev/sda"
    mem = c.parse_dmidecode_memory((FIX / "dmidecode17.txt").read_text())
    ups = c.parse_fwupd(json.loads((FIX / "fwupd.json").read_text()))
    return {"collected_at": int(time.time()), "version": 1, "storage": {"disks": [nv, hd]},
            "memory": dict(mem, max_capacity="128 GB"), "firmware": {"updates": ups}}


def test_parse():
    hw = fake_hw()
    nv, hd = hw["storage"]["disks"]
    assert nv["kind"] == "nvme" and nv["wear_pct"] == 3 and nv["unsafe_shutdowns"] == 203
    assert abs(nv["written_tb"] - 36.1) < 0.1, nv["written_tb"]
    assert nv["power_on_hours"] == 16160 and nv["passed"] is True
    assert hd["kind"] == "hdd" and hd["reallocated"] == 0 and hd["pending"] == 0 and hd["power_on_hours"] == 9912
    assert hd["temp_min_max"] == [28, 36] and hd["last_selftest"]["passed"] is True
    mods = hw["memory"]["modules"]
    assert len(mods) == 4 and hw["memory"]["slots"] == 5
    assert mods[0] == {"slot": "DIMM1", "size": "32 GB", "type": "DDR4", "maker": "Kingston",
                       "part": "KF3200C16D4/32GX", "speed": "2400 MT/s", "configured_speed": "2400 MT/s", "form": "DIMM"}
    assert "1234ABCD" not in json.dumps(hw), "serial numbers must not be stored"
    assert hw["firmware"]["updates"][0]["available"] == "1.37.0"


def test_findings():
    sys.path.insert(0, str(ROOT / "src/lib"))
    from mysys import hw
    good = fake_hw()["storage"]["disks"]
    assert all(lvl == "ok" for d in good for lvl, _ in hw.disk_findings(d))
    bad = dict(good[1], reallocated=8, pending=2, passed=False)
    lv = [lvl for lvl, _ in hw.disk_findings(bad)]
    assert lv.count("warn") == 3, hw.disk_findings(bad)
    worn = dict(good[0], wear_pct=93, media_errors=1)
    assert sum(1 for lvl, _ in hw.disk_findings(worn) if lvl == "warn") == 2
    note = hw.memory_note(fake_hw()["memory"]["modules"])
    assert "2 different module types" in note, note


def test_report():
    tmp = Path(tempfile.mkdtemp())
    ms = tmp / "My_System"
    for d in ("bin", "lib", "plugins"):
        shutil.copytree(ROOT / "src" / d, ms / d)
    for d in ("config", "state", "reports"):
        (ms / d).mkdir()
    (ms / "config/my_system.conf").write_text('MACHINE_LABEL="testbox"\nDATA_DISKS=""\n')
    (ms / "bin/notify.sh").write_text(f'#!/bin/sh\necho "$1|$2" >> {tmp}/notes\n')
    hwf = tmp / "hw.json"
    hwf.write_text(json.dumps(fake_hw()))
    day = ms / "reports" / time.strftime("%Y-%m-%d") / "inventory"
    day.mkdir(parents=True)
    (day / "apt_manual.txt").write_text("git\nvim\nborgbackup\n")
    (day / "tools.txt").write_text("# Tools\ngit            /usr/bin/git    git version 2.53\n\n# Toolchains (detected installs)\n")
    with open(ms / "reports/sizes.csv", "w") as f:
        f.write("date,folder,gb,disk,kind\n2026-10-01,~/Desktop,160.0,root,dir\n"
                f"{time.strftime('%Y-%m-%d')},~/Desktop,187.0,root,dir\n")
    # a previous snapshot to diff against
    (ms / "reports/system").mkdir(parents=True)
    prev = {"date": "2000-01-01", "time": "2000-01-01 00:00", "apt_manual": ["git", "vim", "libreoffice"],
            "kernel_running": "old-kernel", "toolchains": [], "venvs": [], "mounts": [], "failed_units": [],
            "hw": {"storage": {"disks": [dict(fake_hw()["storage"]["disks"][0], unsafe_shutdowns=190)]}}}
    (ms / "reports/system/2000-01-01.json").write_text(json.dumps(prev))
    env = dict(os.environ, MY_SYSTEM=str(ms), MS_HWINFO=str(hwf))
    r = subprocess.run([sys.executable, str(ms / "bin/ms"), "report", "--notify"], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    html = (ms / "reports/system/latest.html").read_text()
    md = (ms / "reports/system/latest.md").read_text()
    for s in ("Summary", "Memory and swap", "Disk health", "SK hynix PC711", "KF3200C16D4/32GX", "Changes since the last report",
              "Packages installed", "borgbackup", "libreoffice", "unsafe shutdowns 190 → 203", "1.37.0",
              "Open issues and recommendations", "~/Desktop", "+27.0 GB", "Capacity for heavy work"):
        assert s in md, f"missing in md: {s}"
    assert "<table>" in html and "prefers-color-scheme" in html
    snap = json.loads(next((ms / "reports/system").glob("20[!0]*.json")).read_text())
    assert snap["hw"]["storage"]["disks"][0]["unsafe_shutdowns"] == 203
    assert (tmp / "notes").read_text().startswith("Monthly system report ready|")
    # ms hw prints the same data
    out = subprocess.run([sys.executable, str(ms / "bin/ms"), "hw"], env=env, capture_output=True, text=True).stdout
    assert "SK hynix" in out and "healthy" in out and "firmware update available" in out, out


if __name__ == "__main__":
    t0 = time.time()
    for k, fn in list(globals().items()):
        if k.startswith("test_"):
            fn()
            print("ok", k)
    print(f"all passed in {time.time() - t0:.1f}s")
