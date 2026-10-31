"""How much work this machine can take — for people and for AI assistants.

  static_specs()   what the machine is (CPU, ISA, caches, RAM, GPU, disks)
  profiles()       measured cost of past jobs per tool (peak RAM, CPU cores, duration)
  now()            live headroom and how many more jobs of each kind fit right now
  guide_md()       the "how to run heavy work here" section of the AI system map

`ms capacity [TOOL]` prints now(); AI assistants are told to run it before starting heavy work.
"""
import json
import math
import os
import statistics
from pathlib import Path

from . import config, probes

GiB = 2**30

# Used only until real runs of a tool have been measured on this machine.
DEFAULT_PROFILES = {
    "vivado": {"peak_gb": 8.0, "cores": 2.0, "note": "synthesis + implementation; placement/routing peaks highest"},
    "vitis_hls": {"peak_gb": 4.0, "cores": 1.5, "note": "C synthesis"},
    "xsim": {"peak_gb": 2.0, "cores": 1.0, "note": "single-threaded simulation"},
    "yosys": {"peak_gb": 2.0, "cores": 1.0, "note": ""},
    "nextpnr": {"peak_gb": 2.0, "cores": 1.0, "note": ""},
    "verilator": {"peak_gb": 2.0, "cores": 1.0, "note": "compile step can use many cores with -j"},
    "gem5": {"peak_gb": 4.0, "cores": 1.0, "note": "one simulation per core"},
    "sniper": {"peak_gb": 4.0, "cores": 1.0, "note": ""},
}


# ---------------------------------------------------------------- static
def cpu_flags():
    try:
        for line in open("/proc/cpuinfo"):
            if line.startswith("flags"):
                return set(line.split(":", 1)[1].split())
    except OSError:
        pass
    return set()


def cache_l3():
    for idx in Path("/sys/devices/system/cpu/cpu0/cache").glob("index*"):
        try:
            if (idx / "level").read_text().strip() == "3":
                v = (idx / "size").read_text().strip()
                if v.endswith("K") and v[:-1].isdigit() and int(v[:-1]) >= 1024:
                    v = f"{int(v[:-1]) / 1024:.0f} MB"
                return v
        except OSError:
            pass
    return ""


def rotational(mount):
    """True for spinning disks, False for SSD/NVMe, None if unknown."""
    dev = next((d for d, m, _ in probes.mounts() if m == mount), "")
    if not dev.startswith("/dev/"):
        return None
    name = os.path.basename(os.path.realpath(dev))
    for cand in (name, name.rstrip("0123456789"), name.split("p")[0] if name.startswith("nvme") else name):
        f = Path("/sys/block", cand, "queue/rotational")
        if f.exists():
            return f.read_text().strip() == "1"
    for blk in Path("/sys/block").iterdir():
        if (blk / name).exists() and (blk / "queue/rotational").exists():
            return (blk / "queue/rotational").read_text().strip() == "1"
    return None


def static_specs():
    cpu = probes.cpu_info()
    fl = cpu_flags()
    isa = [x for x in ("sse4_2", "avx", "avx2", "fma", "avx512f", "avx512bw", "avx512_vnni", "amx_tile") if x in fl]
    m = probes.meminfo()
    gpus = probes.gpus()
    disks = []
    for mp in ["/"] + config.words("DATA_DISKS"):
        d = probes.disk(mp)
        if d:
            fs = next((f for _, m_, f in probes.mounts() if m_ == mp), "")
            disks.append({"mount": mp, "free": d[1], "total": d[2], "fs": fs, "rotational": rotational(mp)})
    return {
        "cpu": cpu, "isa": isa, "l3": cache_l3(), "mem_total": m.get("MemTotal", 0),
        "swap_total": m.get("SwapTotal", 0), "zram": probes.zram(), "gpus": gpus,
        "cuda_ok": any(g[1] == "nvidia" for g in gpus), "disks": disks,
        "shares": config.words("NET_SHARES"),
    }


def reserve_bytes(total):
    """RAM kept free for the desktop, file cache and the AI assistant itself."""
    return max(min(6 * GiB, int(total * 0.25)), int(total * 0.10))


# ---------------------------------------------------------------- measured
def profiles():
    """{tool: {runs, peak_gb (p90), cores (median), core_max, minutes (median), measured}}"""
    try:
        raw = json.loads((config.STATE / "job_profiles.json").read_text())
    except (OSError, ValueError):
        raw = {}
    out = {}
    for tool, all_runs in raw.items():
        # runs first seen mid-way (e.g. already running at install) miss their early peak: use them only
        # when nothing complete exists, and never below the default estimate
        full = [r for r in all_runs if r.get("partial") is False]
        runs = full or all_runs
        peaks = sorted(r["peak_gb"] for r in runs if r.get("peak_gb"))
        # cores a job uses when it gets them: its busiest minute, not its average on a crowded machine
        cores = [max(r.get("core_max") or 0, r.get("cores") or 0) for r in runs if r.get("core_max") or r.get("cores")]
        mins = [r["minutes"] for r in runs if r.get("minutes")]
        if not peaks:
            continue
        p90 = peaks[min(len(peaks) - 1, int(math.ceil(0.9 * len(peaks))) - 1)]
        d = DEFAULT_PROFILES.get(tool, {})
        if not full:
            p90 = max(p90, d.get("peak_gb", 0))
        c = statistics.median(cores) if cores else d.get("cores")
        if not full and d.get("cores"):
            c = max(c or 0, d["cores"])
        out[tool] = {"runs": len(runs), "complete": bool(full), "peak_gb": round(p90, 1), "max_gb": round(peaks[-1], 1),
                     "cores": round(max(1.0, c), 1) if c else None,
                     "minutes": round(statistics.median(mins)) if mins else None, "measured": True}
    for tool, d in DEFAULT_PROFILES.items():
        out.setdefault(tool, dict(d, runs=0, measured=False))
    return out


def live_samples(jobs):
    """Current per-tool memory/CPU of running jobs (used when nothing is measured yet)."""
    by = {}
    for j in jobs:
        by.setdefault(j["tool"], []).append(j)
    return {t: {"n": len(js), "rss_gb": max(j["rss"] for j in js) / GiB,
                "cores": statistics.median([j["cores"] for j in js if j.get("cores")]) if any(j.get("cores") for j in js) else None}
            for t, js in by.items()}


def fit(free_threads, free_mem, peak_gb, cores):
    by_cpu = free_threads / max(1.0, cores or 1.0)          # a running job always holds at least one thread
    by_mem = free_mem / (max(0.5, peak_gb) * GiB)
    return max(0, int(min(by_cpu, by_mem))), ("memory" if by_mem < by_cpu else "CPU")


# ---------------------------------------------------------------- live
def now(tool=None):
    from . import watcher
    th = probes.threads()
    m = probes.meminfo()
    total, avail = m.get("MemTotal", 0), m.get("MemAvailable", 0)
    swap_used = m.get("SwapTotal", 0) - m.get("SwapFree", 0)
    l1, l5, _ = probes.loadavg()
    res = reserve_bytes(total)
    jobs = watcher.jobs()
    try:
        state = watcher.load_state().get("runs", {})
        for j in jobs:
            j["cores"] = (state.get(j["key"]) or {}).get("cores")
    except Exception:
        pass
    free_threads = max(0.0, th - max(l1, l5) * 0.95)
    free_mem = max(0, avail - res)
    prof = profiles()
    live = live_samples(jobs)
    tools = [tool] if tool else sorted(set(prof) | set(live))
    rec = {}
    for t in tools:
        p = prof.get(t, {"peak_gb": 2.0, "cores": 1.0, "measured": False, "runs": 0})
        peak, cores = p["peak_gb"], p.get("cores") or 1.0
        src = (f"{p['runs']} measured runs" + ("" if p.get("complete") else " (partial, floored at estimate)")
               if p.get("measured") else "default estimate")
        if not p.get("measured") and t in live:
            peak, cores = max(peak, live[t]["rss_gb"] * 1.3), max(1.0, live[t]["cores"] or cores)
            src = f"from {live[t]['n']} running job(s)"
        n, limit = fit(free_threads, free_mem, peak, cores)
        idle_n, idle_limit = fit(th, total - res, peak, cores)
        rec[t] = {"more_now": n, "limited_by": limit, "idle_max": idle_n, "idle_limited_by": idle_limit,
                  "peak_gb": round(peak, 1), "cores": round(cores, 1), "source": src, "running": live.get(t, {}).get("n", 0)}
    busy = l1 > th * 0.9 or avail < res * 1.5 or swap_used > 4 * GiB
    make_j = max(1, min(int(free_threads + 0.5), int(free_mem / (1.5 * GiB)))) if not busy else 1
    root = probes.disk("/")
    return {"threads": th, "load1": l1, "load5": l5, "mem_total": total, "mem_avail": avail, "reserve": res,
            "swap_used": swap_used, "free_threads": round(free_threads, 1), "free_mem": free_mem, "busy": busy,
            "jobs": [(j["tool"], j["cwd"]) for j in jobs], "tools": rec, "make_j": make_j,
            "root_free": root[1] if root else None}


def now_text(tool=None):
    n = now(tool)
    lines = [f"Machine now: load {n['load1']:.1f} on {n['threads']} threads · {n['mem_avail'] / GiB:.0f} GB RAM available "
             f"(keep {n['reserve'] / GiB:.0f} GB free) · swap used {n['swap_used'] / GiB:.1f} GB"
             + (f" · root disk {n['root_free'] / GiB:.0f} GB free" if n["root_free"] is not None else "")]
    if n["jobs"]:
        cnt = {}
        for t, _ in n["jobs"]:
            cnt[t] = cnt.get(t, 0) + 1
        lines.append("Running: " + ", ".join(f"{v}× {k}" for k, v in cnt.items()))
    lines.append(("BUSY — do not start heavy work now; queue it with  ms q add -- <command>  and it starts when there is room."
                  if n["busy"] else f"Headroom: ~{n['free_threads']:.0f} free threads, {n['free_mem'] / GiB:.0f} GB usable RAM."))
    lines.append(f"Generic parallel builds right now: make -j{n['make_j']} (OMP_NUM_THREADS={n['make_j']}).")
    lines.append("")
    lines.append(f"{'tool':<11} {'more now':>8} {'idle max':>8}  {'per job':<18} basis")
    for t, r in sorted(n["tools"].items(), key=lambda x: (-x[1]["running"], x[0])):
        if tool is None and not r["running"] and r["source"] == "default estimate" and t not in ("vivado", "vitis_hls", "xsim"):
            continue
        lines.append(f"{t:<11} {r['more_now'] if not n['busy'] else 0:>8} {r['idle_max']:>8}  "
                     f"{r['peak_gb']:>5.1f} GB, {r['cores']:.1f} cores  {r['source']}; limited by {r['limited_by']}")
    lines.append("")
    lines.append("Start at most 'more now' jobs; better: queue them all with  ms q add -- <command>  (starts each when it fits)")
    return "\n".join(lines)


# ---------------------------------------------------------------- AI guide
def guide_md():
    s = static_specs()
    cpu = s["cpu"]
    th = cpu["threads"]
    total = s["mem_total"]
    res = reserve_bytes(total)
    L = ["## This machine and how to run heavy work on it",
         "",
         "### Specification"]
    L.append(f"- CPU: {cpu['model']} — {cpu['cores'] or '?'} cores / {th} threads"
             + (f", up to {cpu['max_ghz']} GHz" if cpu.get("max_ghz") else "") + (f", L3 {s['l3']}" if s["l3"] else ""))
    if s["isa"]:
        has512 = any(x.startswith("avx512") for x in s["isa"])
        L.append(f"- Vector ISA: {', '.join(s['isa'])} — compile with `-O2 -march=native`"
                 + ("" if has512 else "; **no AVX-512** (do not enable AVX-512 code paths)"))
    L.append(f"- RAM: {total / GiB:.0f} GB; keep at least {res / GiB:.0f} GB free for the desktop, file cache and you")
    sw = f"{s['swap_total'] / GiB:.0f} GB swap"
    if s["zram"]:
        sw += f" (incl. {s['zram'][0]['size'] / GiB:.0f} GB compressed zram in RAM)"
    L.append(f"- Swap: {sw}. Swapping makes EDA tools 10× slower — stay below the RAM limit instead")
    if s["gpus"]:
        for name, drv in s["gpus"]:
            L.append(f"- GPU: {name} — driver: {drv or 'none'}")
        if not s["cuda_ok"]:
            L.append("- **No usable CUDA GPU**: do not use CUDA / GPU builds; use the CPU")
    for d in s["disks"]:
        kind = {True: "HDD", False: "SSD", None: "disk"}[d["rotational"]]
        role = "system + home; small — keep big outputs off it" if d["mount"] == "/" else "large outputs, run folders, scratch"
        L.append(f"- `{d['mount']}`: {kind}, {d['fs']}, {d['free'] / GiB:.0f} GB free of {d['total'] / GiB:.0f} GB — {role}")
    for sh in s["shares"]:
        L.append(f"- `{sh}`: network share (slow, shared) — never use for build or run output")

    prof = profiles()
    L += ["", "### Measured cost of jobs on this machine",
          "Peak RAM is the 90th percentile of real runs; 'max parallel' assumes an otherwise idle machine.",
          "", "| Tool | Runs measured | Peak RAM / job | CPU cores / job | Typical time | Max parallel (idle) |",
          "|---|---|---|---|---|---|"]
    for t, p in sorted(prof.items(), key=lambda x: (not x[1]["measured"], x[0])):
        if not p["measured"] and t not in ("vivado", "vitis_hls", "xsim"):
            continue
        n, lim = fit(th, total - res, p["peak_gb"], p.get("cores") or 1.0)
        L.append(f"| {t} | {(str(p['runs']) + ('' if p.get('complete') else ' partial')) if p['measured'] else 'none yet (estimate)'} | {p['peak_gb']} GB | {p.get('cores') or '?'} | "
                 f"{str(p['minutes']) + ' min' if p.get('minutes') else '–'} | {n} (by {lim}) |")
    gen = max(1, min(th - 2, int((total - res) / (1.5 * GiB))))
    L += ["",
          "### Rules for running code here",
          "1. **Check before you start anything heavy** (builds, synthesis, simulations, sweeps, training):",
          "   run `ms capacity` (or `ms capacity vivado`). It prints the live load, free RAM and how many more",
          "   jobs of each kind fit *right now*. Never start more than it says. If it says BUSY, queue and wait.",
          "2. **Queue, don't flood.** Put heavy jobs in the my-system queue: `ms q add --name N -- <command>`",
          "   (or the `queue_submit` MCP tool). It starts each job only when its CPU and RAM are free, pauses jobs",
          "   when RAM runs low, retries out-of-memory runs and writes the run log. Sweeps and multi-step work:",
          "   a flow file (`ms flow example`), checked with `ms flow check FILE`, started with `ms flow run FILE`.",
          "3. **Cap threads explicitly** — never rely on 'use all cores': `make -j<N>` / `cmake --build -j <N>`,",
          "   `OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `NUMEXPR_MAX_THREADS`, Python `Pool(processes=N)`, `pytest -n N`.",
          f"   On an idle machine N ≤ {max(1, th - 2)} for one job; with other jobs running use the number from `ms capacity`.",
          "4. **Vivado / Vitis**: run in batch mode (`vivado -mode batch -source x.tcl -nojournal`), put",
          "   `set_param general.maxThreads <k>` in the Tcl (default 8; with several parallel runs use 2),",
          "   and give every run its own folder on the data disk. Parallel runs are limited by CPU or RAM — see the table.",
          "5. **Long jobs survive the terminal**: start them through `ms q add` (or `tmux`), never as a plain",
          "   background `&` from your shell session.",
          "6. **Be nice to the desktop**: prefix CPU-heavy commands with `nice -n 10`, disk-heavy ones with `ionice -c3`.",
          "7. **Outputs and scratch go to the data disk** (see above), not to `/` or the network share.",
          "   Set `TMPDIR` to a folder there for tools that write huge temporary files.",
          "8. **Watch memory, not just CPU.** If a job may exceed the per-job peak above, run fewer in parallel.",
          "   Never let RAM available drop under the reserve; the machine then swaps and everything stalls.",
          "9. **Never stop or renice jobs you did not start**, even if they slow you down; tell the user instead.",
          f"10. Quick defaults when `ms capacity` cannot be run: idle machine → `make -j{gen}`; busy machine → `-j2`.",
          ]
    return "\n".join(L)
