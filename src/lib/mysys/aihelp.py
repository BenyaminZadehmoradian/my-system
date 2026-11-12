"""What an AI coding session should see: the rules, the reference pages and the live state — `ms ai ...`.

`ms ai status` is also printed into every new Claude Code session by the SessionStart hook of the managed policy,
so it must be fast (no ssh, no slow probes) and must never fail."""
import os
import time

from . import config

AI = config.MS / "ai"
DOCS = {"rules": AI / "AI_RULES.md", "log": AI / "docs" / "logging.md", "logging": AI / "docs" / "logging.md",
        "server": AI / "docs" / "server.md", "map": config.STATE / "SYSTEM_MAP.md",
        "spec": config.MS / "runlog" / "RUNLOG_SPEC.md"}

TEMPLATE = """cd RUN_FOLDER      # one folder per run; everything the job reads and writes is inside it
ms q add --name STUDY_WHAT_VARIANT --cores N --mem GB --metrics wns_ns,lut -- bash run.sh
# in run.sh / the Tcl / the Python:   echo "::ms stage synth"   echo "::ms metric wns_ns -0.084 ns"
ms q wait ID && ms q show ID      # finished? state done, metrics present"""


def _age(t):
    s = max(0, time.time() - t)
    return f"{s / 3600:.0f} h ago" if s >= 5400 else f"{max(1, s / 60):.0f} min ago"


def _jobs_lines(jobs):
    L = []
    act = [j for j in jobs if j["state"] in ("running", "suspended", "queued", "held", "approval")]
    run = [j for j in act if j["state"] == "running"]
    there = [j for j in run if j.get("remote")]
    wait = [j for j in act if j["state"] == "queued"]
    s = f"queue: {len(run)} running ({len(there)} on the server, {len(run) - len(there)} here), {len(wait)} waiting"
    other = len(act) - len(run) - len(wait)
    if other:
        s += f", {other} held or paused"
    L.append(s)
    if wait:
        why = {}
        for j in wait:
            r = (j.get("reason") or "").replace("waiting: ", "", 1).split("(")[0].strip()[:70] or "next in line"
            why[r] = why.get(r, 0) + 1
        top = sorted(why.items(), key=lambda kv: -kv[1])[:2]
        L.append("  waiting because: " + "; ".join(f"{n}× {r}" for r, n in top))
    day = time.time() - 86400
    bad = [j for j in jobs if j["state"] in ("failed", "warning", "timeout", "oom") and (j.get("ended") or 0) > day]
    mine = [j for j in bad if j.get("submitted_by") == "ai"] or bad
    if mine:
        L.append(f"ended badly in the last 24 h: {len(mine)} job(s) — look before starting the same thing again:")
        for j in sorted(mine, key=lambda j: -(j.get("ended") or 0))[:5]:
            L.append(f"  #{j['id']} {j['name'][:34]} — {j['state']}: {(j.get('reason') or '')[:90]} ({_age(j['ended'])})")
    return L


def _server_lines(jobs):
    from . import remote
    L = []
    for r in remote.remotes().values():
        st = remote._cache(r["name"])                 # the cached reading only: never ssh from here
        n, c = remote.running_here(r["name"], jobs), remote.cores_there(r["name"], jobs)
        s = f"server {r['name']} (offload = {r.get('offload') or 'never'}): {n} of {r.get('max_jobs') or '∞'} jobs"
        if r.get("max_cores"):
            s += f", {c:.0f} of {r['max_cores']} reserved cores"
        if st.get("t"):
            s += (f"; {st.get('idle_cpus', '?')} of {st.get('total_cpus', '?')} CPUs idle" if st.get("ok")
                  else f"; not reachable ({(st.get('error') or '')[:50]})") + f" ({_age(st['t'])})"
        L.append(s)
    try:
        u = remote.usage_summary(7, jobs)
        adv = [x for x in u["groups"] if x["advice"] and x["n"] >= 3][:4]
        if adv:
            L.append("reserve less for (measured on the server, 7 days):")
            L += [f"  {x['family']}…: used {x['used']:.1f} of {x['cores']:.0f} cores → {x['advice']}" for x in adv]
    except Exception:
        pass
    return L


def status_text(cwd=None):
    from . import queue
    L = [f"my-system {_version()} — live state for AI sessions ({time.strftime('%Y-%m-%d %H:%M')})"]
    try:
        jobs = queue.all_jobs()
    except Exception as e:
        jobs = []
        L.append(f"queue: not readable ({e})")
    for part in (_jobs_lines, _server_lines):
        try:
            L += part(jobs)
        except Exception as e:
            L.append(f"({part.__name__[1:-6]}: not available: {str(e)[:60]})")
    try:
        from . import pause
        if pause.active():
            L.append("paused by the user: " + pause.summary() + " — do not work around it")
    except Exception:
        pass
    try:
        block = queue.temp_block()
        if block:
            L.append(f"this machine is hot ({block.split(': ', 1)[-1]}): new local jobs wait; jobs for the server still start")
    except Exception:
        pass
    try:
        from . import versions
        v = versions.allowed_for(cwd or os.getcwd())
        if v and "*" not in v:
            L.append("Vivado/Vitis/ISE versions allowed in this folder: " + ", ".join(str(x) for x in v))
    except Exception:
        pass
    L.append("")
    L.append("Heavy or long work goes through the queue, and results are printed as `::ms` lines:")
    L.append(TEMPLATE)
    L.append("Rules: `ms ai rules` · logging: `ms ai log` · server jobs: `ms ai server` · this page: `ms ai status`")
    return "\n".join(L)


def _version():
    try:
        from . import __version__
        return __version__
    except Exception:
        return ""


def main(args):
    sub = args[0] if args else "status"
    if sub in ("-h", "--help", "help"):
        print("ms ai status     live state: queue, server, limits, what failed, how to run and log (shown at session start)\n"
              "ms ai rules      the rules for AI assistants on this machine\n"
              "ms ai log        how to write results into the run log (::ms lines)\n"
              "ms ai server     what to know about jobs that run on the server\n"
              "ms ai map        the system map (hardware, tools, projects, limits)\n"
              "ms ai spec       the run log file format")
        return 0
    if sub == "status":
        if "--hook" in args:                # SessionStart hook: never fail, never block the session
            try:
                print(status_text())
            except Exception as e:
                print(f"my-system: live state not available ({e}); see `ms ai rules`")
            return 0
        print(status_text())
        return 0
    p = DOCS.get(sub)
    if not p:
        print(f"ms ai: unknown page '{sub}' (status, rules, log, server, map, spec)")
        return 2
    try:
        print(p.read_text())
    except OSError as e:
        print(f"ms ai: cannot read {p}: {e}")
        return 1
    return 0
