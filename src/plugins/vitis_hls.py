"""AMD/Xilinx Vitis HLS / Vivado HLS — errors + estimated clock, latency and resources from csynth.xml."""
import re
import xml.etree.ElementTree as ET

from mysys.plugins import count_log, find_log, newest

MATCH = re.compile(r"(?:^|/)(vitis_hls|vitis-run|vivado_hls|v\+\+)$")
LOGS = {"vitis_hls": ["vitis_hls.log"], "vitis-run": ["vitis_hls.log", "vitis-run.log", "vitis.log"],
        "vivado_hls": ["vivado_hls.log"], "v++": ["v++.log", "v++_link.log"]}
CLEANABLE = [".autopilot", "*.cache", "_x", ".ipcache"]


def log_summary(cwd, tool):
    f = find_log(cwd, LOGS.get(tool, []))
    return count_log(f) if f else None


def _f(root, path):
    el = root.find(path)
    try:
        return float(el.text) if el is not None and el.text not in (None, "undef") else None
    except ValueError:
        return None


def parse_csynth(text):
    root = ET.fromstring(text)
    out = {"clock_ns": _f(root, ".//SummaryOfTimingAnalysis/EstimatedClockPeriod"),
           "latency_cycles": _f(root, ".//SummaryOfOverallLatency/Worst-caseLatency")}
    res = root.find(".//AreaEstimates/Resources")
    if res is not None:
        for key, tags in (("lut", ["LUT"]), ("ff", ["FF"]), ("bram", ["BRAM_18K"]), ("dsp", ["DSP", "DSP48E"]),
                          ("uram", ["URAM"])):
            for t in tags:
                v = _f(res, t)
                if v is not None:
                    out[key] = v
                    break
    if out.get("clock_ns"):
        out["fmax_mhz"] = round(1000 / out["clock_ns"], 1)
    return {k: v for k, v in out.items() if v is not None}


def results(cwd, tool):
    f = newest(cwd, "*_csynth.xml", depth=5) or newest(cwd, "csynth.xml", depth=5)
    if not f:
        return None
    try:
        return parse_csynth(f.read_text(errors="replace")) or None
    except (OSError, ET.ParseError):
        return None
