"""Long simulations: gem5, Sniper, Verilator/Icarus binaries, ModelSim/Questa, ngspice."""
import re

from mysys.plugins import count_log, find_log

MATCH = re.compile(r"(?:^|/)(gem5\.\w+|run-sniper|sniper|vvp|vsim|qrun|ngspice|xyce|Vtop|V\w+_tb)$")
LOGS = {"vsim": ["transcript", "vsim.log"], "qrun": ["qrun.log"], "run-sniper": ["sim.out", "sniper.log"],
        "sniper": ["sim.out"]}


def log_summary(cwd, tool):
    f = find_log(cwd, LOGS.get(tool, []))
    if tool.startswith("gem5"):
        f = find_log(cwd, ["m5out/simerr", "simerr"])
    return count_log(f, error_rx=r"(^\*\* Error|^ERROR|^Error|panic:|fatal:)", crit_rx=r"^\*\* Warning") if f else None
