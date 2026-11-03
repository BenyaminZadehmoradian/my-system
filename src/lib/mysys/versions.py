"""Only the allowed version of the AMD/Xilinx tools may be used (by queue jobs and by AI assistants).

config/my_system.conf:  XILINX_ALLOWED="2025.2"     # space-separated; "2025.2" also allows 2025.2.1 (its update)
                                                     # empty = every installed version may be used

Every other installed Vivado, Vitis, Vitis HLS, ISE or SDK version is "not allowed":
  - a queue job whose command names it does not start, and a job that runs it anyway (e.g. from a script) is
    stopped within seconds, with the reason in ms q show;
  - the AI guard blocks shell commands that name it;
  - the AI system map lists which version to use.
"""
import os
import re

from . import config

XILINX_LABELS = re.compile(r"vivado|vitis|ise\b|ise_|xilinx|sdk|petalinux|model_composer", re.I)
# a version-named folder of a Xilinx tool anywhere in a path: .../Vivado/2023.2/..., .../Vitis_HLS/2023.2, /opt/Xilinx/14.7
PATH_VER = re.compile(r"/(?:Xilinx|AMD|Vivado|Vitis|Vitis_HLS|Vivado_HLS|SDK|ISE|Apps|PetaLinux)/+(\d{4}\.\d(?:\.\d)?|1\d\.\d)(?=/|\s|$|['\"])",
                      re.I)
# settings scripts and tool names with a version in them: Vivado_2023.2, vitis_hls_2023.2
NAME_VER = re.compile(r"\b(?:vivado|vitis|vitis_hls|ise|sdk)[_-](\d{4}\.\d(?:\.\d)?|1\d\.\d)\b", re.I)


def allowed_prefixes():
    return config.words("XILINX_ALLOWED")


def allowed(ver):
    pre = allowed_prefixes()
    if not pre or not ver:
        return True
    return any(ver == p or ver.startswith(p + ".") for p in pre)


def installs():
    """[(label, root, version)] of installed Xilinx/AMD tools."""
    try:
        from . import detect
        d = detect.load_state()
        tools = d.get("tools", [])
    except Exception:
        tools = []
    out = []
    for label, root in tools:
        if not XILINX_LABELS.search(label):
            continue
        m = re.search(r"(\d{4}\.\d(?:\.\d)?|1\d\.\d)", label + " " + root)
        out.append((label, os.path.realpath(root), m.group(1) if m else ""))
    return out


def forbidden_roots():
    if not allowed_prefixes():
        return []
    return [(root, ver) for _, root, ver in installs() if ver and not allowed(ver)]


def check_text(text):
    """Version that is not allowed and that `text` (a command line or script) uses, or None."""
    if not text or not allowed_prefixes():
        return None
    for root, ver in forbidden_roots():
        if root and root in text:
            return ver
    for rx in (PATH_VER, NAME_VER):
        for m in rx.finditer(text):
            if not allowed(m.group(1)):
                return m.group(1)
    return None


def check_proc(pid):
    """Version that is not allowed and that process `pid` runs (its program or the script it runs), or None."""
    if not allowed_prefixes():
        return None
    parts = []
    try:
        parts.append(os.readlink(f"/proc/{pid}/exe"))
    except OSError:
        pass
    try:
        args = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")[:3]
        parts += [a.decode(errors="replace") for a in args if a]
    except OSError:
        pass
    return check_text(" ".join(parts))


def message(ver):
    pre = " or ".join(allowed_prefixes())
    return (f"AMD/Xilinx tools version {ver} is not allowed on this machine; use {pre} "
            f"(set by the user in XILINX_ALLOWED)")


def map_lines():
    """For the AI system map."""
    pre = allowed_prefixes()
    if not pre:
        return []
    ok = [f"`{root}` ({ver})" for _, root, ver in installs() if allowed(ver)]
    bad = [f"`{root}` ({ver})" for _, root, ver in installs() if ver and not allowed(ver)]
    out = ["", f"## AMD/Xilinx tool version: only {' / '.join(pre)}",
           f"The user allows only version {' / '.join(pre)} (and its updates, e.g. {pre[0]}.1). Use: "
           + (", ".join(ok) or "(not installed)") + "."]
    if bad:
        out.append("Installed but NOT allowed (commands and jobs that use them are blocked or stopped): "
                   + ", ".join(bad) + ".")
    return out
