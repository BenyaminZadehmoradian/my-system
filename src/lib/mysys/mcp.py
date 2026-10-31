"""Minimal MCP server (stdio, JSON-RPC 2.0) so AI assistants can ask my-system directly.

Registered for Claude Code by install.sh as `my-system` (command: ms mcp). Tools:
  capacity, queue_list, queue_submit, queue_cancel, queue_explain, runs_list, run_detail, runs_compare, flow_check,
  flow_run, software_uses, software_updates, vulnerabilities, replay, system_map
Submitting goes through the same queue as everything else, so an assistant can never overload the machine.
"""
import json
import os
import sys
import time

from . import capacity, config, flow, queue, runlog

PROTOCOL = "2024-11-05"


def _home(p):
    return str(p).replace(str(config.HOME), "~")


def t_capacity(tool=None):
    return capacity.now_text(tool)


def t_queue_list(all=False):
    out = []
    for j in queue.all_jobs():
        if all or j["state"] not in queue.TERMINAL or time.time() - (j.get("ended") or 0) < 3600:
            e = queue.efficiency(j)
            out.append({"id": j["id"], "name": j["name"], "state": j["state"], "reason": j.get("reason", ""),
                        "cwd": _home(j["cwd"]), "mem_gb": j["mem_gb"], "cores": j["cores"], "seconds": e["seconds"],
                        "flow": (j.get("flow") or {}).get("id")})
    return json.dumps({"scheduler": queue.daemon_state()[0], "jobs": out}, indent=1)


def _inside_home(p):
    real = os.path.realpath(os.path.expanduser(p))
    allowed = [str(config.HOME)] + [os.path.realpath(os.path.expanduser(x)) for x in
                                    [config.get("BIG_FOLDER_DEST") or ""] + config.words("DATA_DISKS") if x]
    protected = [os.path.realpath(os.path.expanduser(x)) for x in config.lst("protected_paths.txt")]
    if any(real == pr or real.startswith(pr + "/") for pr in protected):
        return False
    return any(real == a or real.startswith(a + "/") for a in allowed)


def t_queue_submit(cmd, cwd, name=None, mem_gb=None, cores=None, after=None, retries=0, timeout=None, priority=0):
    if not _inside_home(cwd):
        raise ValueError(f"cwd must be inside your home or the data disk and not protected: {cwd}")
    j = queue.submit(cmd, cwd=cwd, name=name, mem_gb=mem_gb, cores=cores, depends=after or [], retries=retries,
                     timeout_s=timeout, priority=min(int(priority or 0), 0), submitted_by="ai")
    return f"queued job {j['id']} ({j['mem_gb']} GB, {j['cores']} cores). Check with queue_list."


def t_queue_cancel(id):
    j = queue.load(int(id))
    if not j:
        return f"no job {id}"
    if j.get("submitted_by") != "ai":
        return "refused: this job was not submitted by an AI assistant; ask the user to cancel it."
    return "cancelled" if queue.cancel(int(id)) else f"not cancelled (state {j['state']})"


def t_runs_list(limit=30, project=None):
    out = []
    for r in runlog.all_runs(500):
        if project and project.lower() not in (r["project"] or "").lower():
            continue
        out.append({"run": r["run"], "name": r["name"], "project": r["project"], "status": r["status"],
                    "folder": _home(r["folder"]), "seconds": r["seconds"], "metrics": r["metrics"],
                    "errors": r["errors"], "first_error": r["first_error"]})
        if len(out) >= int(limit):
            break
    return json.dumps(out, indent=1, default=str)


def t_run_detail(folder, run=None):
    f = os.path.join(os.path.expanduser(folder), runlog.FILENAME)
    runs = runlog.read(f)
    if not runs:
        return f"no run log in {folder}"
    r = runs.get(run) if run else sorted(runs.values(), key=lambda x: x["start_ts"] or 0)[-1]
    return runlog.timeline(f, r["run"])


def t_flow_check(file):
    f, specs, errs = flow.check(file)
    if errs:
        return "Problems:\n" + "\n".join(f"- {e}" for e in errs)
    plan, total, peak = flow.estimate(specs)
    return (f"OK: {len(specs)} jobs\n{flow.dag_text(specs)}\nestimated {flow.queue_hms(total * 60)} on an idle machine, "
            f"up to {peak} in parallel")


def t_flow_run(file, resume=True):
    f, specs, errs = flow.check(file)
    if errs:
        return "Problems:\n" + "\n".join(f"- {e}" for e in errs)
    if not _inside_home(f.get("cwd", ".")):
        return "refused: flow cwd must be inside your home or the data disk"
    rec, _ = flow.submit_flow(file, resume=resume)
    return f"flow {rec['id']} queued with {len(rec['jobs'])} jobs"


def t_software_uses(name):
    from . import software
    d = software.load()
    if not d:
        return "software not scanned yet (ms software scan)"
    r = software.uses(d, name)
    return "\n".join(r) if r else f"nothing found that uses {name}"


def t_software_updates():
    from . import software
    d = software.load()
    return software.summary_text(d) if d else "software not scanned yet (ms software scan)"


def t_queue_explain(id):
    return "\n".join(f"{'ok ' if ok else 'NO '} {c}: {d}" for ok, c, d in queue.explain(int(id)))


def t_replay(time_text, span_minutes=10):
    from . import history
    try:
        t = history.parse_time(time_text)
    except ValueError as e:
        return str(e)
    rows = history.around(t, int(span_minutes) * 60)
    return "\n".join(history.fmt(r) for r in rows) or "no samples recorded for that time"


def t_runs_compare(paths=None, sort=None, desc=False, name=None):
    runs = runlog.collect(paths or [], None, name)
    if not runs:
        return "no runs found"
    h, rows = runlog.compare_table(runs, sort, desc)
    return json.dumps({"columns": h, "rows": rows[-200:]}, default=str)


def t_vulnerabilities():
    from . import software
    return software.audit_text(software.load_audit())


def t_system_map():
    try:
        return (config.STATE / "SYSTEM_MAP.md").read_text()
    except OSError:
        return "system map not generated yet"


S = {"type": "string"}
I = {"type": "integer"}
N = {"type": "number"}
B = {"type": "boolean"}
TOOLS = {
    "capacity": (t_capacity, "Live load, free RAM and how many more jobs of each tool fit right now. Call before heavy work.",
                 {"tool": S}, []),
    "queue_list": (t_queue_list, "Jobs in the my-system queue (running, waiting, recently finished).", {"all": B}, []),
    "queue_submit": (t_queue_submit, "Queue a long or heavy shell command. It starts only when CPU and RAM are free; "
                                     "it writes a standard run log. Use instead of starting heavy jobs directly.",
                     {"cmd": S, "cwd": S, "name": S, "mem_gb": N, "cores": N, "after": {"type": "array", "items": I},
                      "retries": I, "timeout": S, "priority": I}, ["cmd", "cwd"]),
    "queue_cancel": (t_queue_cancel, "Cancel a job that an AI assistant submitted.", {"id": I}, ["id"]),
    "runs_list": (t_runs_list, "Recent runs from standard run logs, with status, metrics and first error.",
                  {"limit": I, "project": S}, []),
    "run_detail": (t_run_detail, "Timeline of a run (stages, errors, metrics, files) from its folder.",
                   {"folder": S, "run": S}, ["folder"]),
    "flow_check": (t_flow_check, "Validate a flow YAML file and show the plan and time estimate.", {"file": S}, ["file"]),
    "flow_run": (t_flow_run, "Queue all jobs of a flow YAML file (steps already done are skipped).",
                 {"file": S, "resume": B}, ["file"]),
    "software_uses": (t_software_uses, "Who uses a project, Python package, system library, tool or environment — "
                                       "check this before changing or updating anything.", {"name": S}, ["name"]),
    "software_updates": (t_software_updates, "Available updates grouped by advice (safe / test / wait / reboot).", {}, []),
    "queue_explain": (t_queue_explain, "Why a waiting job has not started: every condition (RAM, CPU, licenses, "
                                       "dependencies, groups, pressure) with pass/fail.", {"id": I}, ["id"]),
    "replay": (t_replay, "What the machine was doing at a past time (load, free RAM, biggest programs, jobs); "
                         "time like '2026-10-31 07:30', '07:30' or '90m' (ago).", {"time_text": S, "span_minutes": I}, ["time_text"]),
    "runs_compare": (t_runs_compare, "Params and metrics of many standard-log runs side by side (only differing columns), "
                                     "sorted by a metric — e.g. which seed/strategy gave the best WNS.",
                     {"paths": {"type": "array", "items": S}, "sort": S, "desc": B, "name": S}, []),
    "vulnerabilities": (t_vulnerabilities, "Python packages with known security vulnerabilities and which projects use them.",
                        {}, []),
    "system_map": (t_system_map, "This machine: hardware, capacity rules, toolchains, environments, projects, protected paths.",
                   {}, []),
}


def handle(msg):
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "my-system", "version": __import__("mysys").__version__}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": n, "description": d, "inputSchema": {"type": "object", "properties": p, "required": r}}
            for n, (_, d, p, r) in TOOLS.items()]}}
    if method == "tools/call":
        name = msg["params"]["name"]
        args = msg["params"].get("arguments") or {}
        try:
            fn = TOOLS[name][0]
            text = fn(**args)
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": str(text)}]}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if mid is None:
        return None          # notification
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}


def serve():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        out = handle(msg)
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
