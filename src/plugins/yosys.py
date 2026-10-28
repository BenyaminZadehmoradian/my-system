"""Open-source FPGA flow: yosys, nextpnr-*, openroad, OpenFPGA/VPR."""
import re

from mysys.plugins import count_log, find_log

MATCH = re.compile(r"(?:^|/)(yosys|nextpnr-\w+|openroad|vpr|openfpga)$")
LOGS = {"yosys": ["yosys.log", "synth.log"], "vpr": ["vpr_stdout.log"], "openfpga": ["openfpga_stdout.log"]}


def log_summary(cwd, tool):
    names = LOGS.get(tool, [f"{tool}.log", "log.txt"])
    if tool.startswith("nextpnr"):
        names = ["nextpnr.log", "pnr.log"]
    f = find_log(cwd, names)
    return count_log(f, error_rx=r"^(ERROR|Error)", crit_rx=r"^CRITICAL") if f else None


def results(cwd, tool):
    names = ["nextpnr.log", "pnr.log"] if tool.startswith("nextpnr") else LOGS.get(tool, [])
    f = find_log(cwd, names)
    if not f:
        return None
    try:
        text = f.read_text(errors="replace")
    except OSError:
        return None
    m = re.findall(r"Max frequency for clock[^:]*:\s*([\d.]+)\s*MHz", text)
    out = {"fmax_mhz": float(m[-1])} if m else {}
    m = re.findall(r"Final critical path delay:\s*([\d.]+)\s*ns", text)      # VPR
    if m:
        out["clock_ns"] = float(m[-1])
    return out or None
