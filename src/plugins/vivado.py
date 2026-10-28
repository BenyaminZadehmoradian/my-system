"""AMD/Xilinx Vivado (implementation, simulation with xsim) — errors + timing/utilization/power."""
import re

from mysys.plugins import count_log, find_log, newest

MATCH = re.compile(r"(?:^|/)(vivado|xsim|xelab|xvlog|xvhdl|vivado_lab)$")
LOGS = {"vivado": ["vivado.log"], "xsim": ["simulate.log", "xsim.log"], "xelab": ["elaborate.log", "xelab.log"],
        "xvlog": ["compile.log", "xvlog.log"], "xvhdl": ["compile.log", "xvhdl.log"]}
CLEANABLE = [".Xil", "*.cache", "xsim.dir", "*.hw", "*.ip_user_files", ".Xil*"]


def log_summary(cwd, tool):
    f = find_log(cwd, LOGS.get(tool, [f"{tool}.log"]))
    return count_log(f) if f else None


def _num(s):
    try:
        return float(s.replace(",", ""))
    except (AttributeError, ValueError):
        return None


def parse_timing(text):
    """WNS/TNS/WHS from a report_timing_summary report (Design Timing Summary table)."""
    # Header words contain spaces ("TNS Failing Endpoints"), so use the fixed column order:
    # WNS TNS TNS-failing TNS-total WHS THS ...
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("WNS(ns)") and "TNS(ns)" in line:
            for row in lines[i + 1:i + 5]:
                vals = row.split()
                if len(vals) >= 2 and re.match(r"^-?(\d|inf)", vals[0]):
                    return {"wns_ns": _num(vals[0]), "tns_ns": _num(vals[1]),
                            "whs_ns": _num(vals[4]) if len(vals) > 4 else None}
    return {}


def parse_util(text):
    out = {}
    rows = {"lut": r"(?:Slice|CLB) LUTs\*?", "ff": r"(?:Slice|CLB) Registers|Register as Flip Flop",
            "bram": r"Block RAM Tile", "uram": r"URAM", "dsp": r"DSPs?"}
    for key, rx in rows.items():
        m = re.search(rf"^\|\s*(?:{rx})\s*\|\s*([\d.]+)", text, re.M)
        if m:
            out[key] = _num(m.group(1))
    return out


def parse_power(text):
    m = re.search(r"Total On-Chip Power \(W\)\s*\|\s*([\d.]+)", text)
    return {"power_w": _num(m.group(1))} if m else {}


def parse_log_timing(text):
    """Fallback: last 'Estimated Timing Summary | WNS=… | TNS=… | WHS=…' line of vivado.log."""
    hits = re.findall(r"WNS=(-?[\d.]+)\s*\|\s*TNS=(-?[\d.]+)(?:\s*\|\s*WHS=(-?[\d.]+))?", text)
    if not hits:
        return {}
    w, t, h = hits[-1]
    return {"wns_ns": _num(w), "tns_ns": _num(t), "whs_ns": _num(h) if h else None}


def results(cwd, tool):
    if tool != "vivado":
        return None
    out = {}
    for pat, fn in (("*timing_summary*.rpt", parse_timing), ("*utilization*.rpt", parse_util),
                    ("*power*.rpt", parse_power)):
        f = newest(cwd, pat)
        if f:
            try:
                out.update({k: v for k, v in fn(f.read_text(errors="replace")).items() if v is not None})
            except OSError:
                pass
    if "wns_ns" not in out:
        log = find_log(cwd, ["vivado.log"])
        if log:
            try:
                out.update({k: v for k, v in parse_log_timing(log.read_text(errors="replace")).items() if v is not None})
            except OSError:
                pass
    return out or None
