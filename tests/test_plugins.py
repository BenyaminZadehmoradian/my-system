import sys, importlib.util
from pathlib import Path
HERE = Path(__file__).parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC / "lib"))

def load(name):
    spec = importlib.util.spec_from_file_location(name, SRC / "plugins" / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_vivado_results():
    v = load("vivado")
    r = v.results(HERE / "fixtures" / "vivado_run", "vivado")
    assert r["wns_ns"] == -0.231 and r["tns_ns"] == -4.512 and r["whs_ns"] == 0.052
    assert r["lut"] == 12456 and r["ff"] == 20100 and r["bram"] == 45.5 and r["dsp"] == 220
    assert r["power_w"] == 0.473

def test_vivado_log_fallback():
    v = load("vivado")
    assert v.parse_log_timing((HERE / "fixtures/vivado_run/vivado.log").read_text())["wns_ns"] == -0.231
    s = v.log_summary(HERE / "fixtures" / "vivado_run", "vivado")
    assert s["errors"] == 0 and s["critical"] == 1

def test_vivado_match():
    v = load("vivado")
    assert v.MATCH.search("/mnt/data/Apps/2025.2.1/Vivado/bin/unwrapped/lnx64.o/vivado").group(1) == "vivado"
    assert not v.MATCH.search("/usr/bin/vivaldi")

def test_hls():
    h = load("vitis_hls")
    r = h.results(HERE / "fixtures" / "hls_run", "vitis_hls")
    assert r == {"clock_ns": 7.3, "latency_cycles": 4100.0, "lut": 5678.0, "ff": 3456.0, "bram": 4.0,
                 "dsp": 12.0, "uram": 0.0, "fmax_mhz": 137.0}
    assert h.log_summary(HERE / "fixtures" / "hls_run", "vitis_hls")["errors"] == 1

if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); print("ok", k)
