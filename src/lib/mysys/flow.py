"""Flows: a YAML file that describes steps, parameter sweeps and their dependencies.

    name: s18_doe
    cwd: /mnt/data/benyamin/efpga/S18
    defaults: {tool: vivado, mem_gb: 8, cores: 2, retries: 1, retry_on: [oom], timeout: auto}
    params: {part: xc7z020clg400-1}
    require: ["mounted /mnt/data"]
    steps:
      - name: synth
        matrix: {seed: [1, 2, 3], strategy: [Explore, Default]}
        folder: "doe_{{seed}}_{{strategy}}"
        cmd: "vivado -mode batch -source ../synth.tcl -tclargs {{part}} {{seed}} {{strategy}}"
        inputs: ["../rtl/*.v", "../synth.tcl"]
        outputs: ["post_synth.dcp"]
      - name: impl
        matrix: synth                 # same items as synth -> each impl waits for its own synth
        depends: [synth]
        folder: "doe_{{seed}}_{{strategy}}"
        cmd: "vivado -mode batch -source ../impl.tcl"
        temp: ["*.jou", ".Xil"]       # deleted after the whole flow succeeded
    on_success: "python3 summarize.py"
    on_failure: ""

Placeholders are {{name}} (double braces, so Tcl braces are safe).
"""
import glob
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import capacity, probes, queue

FLOWS = queue.Q / "flows"
SCHED = queue.Q / "schedules.json"
TOP_KEYS = {"name", "cwd", "defaults", "params", "require", "env", "steps", "on_success", "on_failure", "notify",
            "description", "schedule"}
STEP_KEYS = {"name", "cmd", "matrix", "folder", "depends", "dep_mode", "inputs", "outputs", "temp", "require", "mem_gb",
             "cores", "gpus", "priority", "retries", "retry_on", "timeout", "nice", "limit_memory", "container", "host",
             "when_idle", "preempt", "env", "tool", "incremental", "params", "description"}
DEFAULT_KEYS = STEP_KEYS - {"name", "cmd", "matrix", "folder", "depends", "description"}
PH = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class FlowError(Exception):
    pass


def load_file(path):
    p = Path(path).expanduser()
    txt = p.read_text()
    if p.suffix in (".json",):
        data = json.loads(txt)
    elif p.suffix in (".toml",):
        import tomllib
        data = tomllib.loads(txt)
    else:
        import yaml
        data = yaml.safe_load(txt)
    if not isinstance(data, dict):
        raise FlowError("the file must contain a mapping (name:, steps:, …)")
    data.setdefault("cwd", str(p.parent.resolve()))
    data["_file"] = str(p.resolve())
    return data


def _fill(text, values, where, errors):
    def rep(m):
        k = m.group(1)
        if k not in values:
            errors.append(f"{where}: unknown placeholder {{{{{k}}}}}")
            return m.group(0)
        return str(values[k])
    return PH.sub(rep, str(text))


def _matrix_items(m, where, errors):
    if m is None:
        return [{}]
    if isinstance(m, list):
        if not all(isinstance(x, dict) for x in m):
            errors.append(f"{where}: matrix list must contain mappings")
            return [{}]
        return [dict(x) for x in m]
    if isinstance(m, dict):
        keys = list(m)
        vals = [v if isinstance(v, list) else [v] for v in m.values()]
        return [dict(zip(keys, combo)) for combo in itertools.product(*vals)]
    errors.append(f"{where}: matrix must be a mapping of lists, a list of mappings, or the name of another step")
    return [{}]


def _slug(item):
    return "_".join(f"{v}" for v in item.values()) if item else ""


def expand(flow, overrides=None):
    """Validate and expand a flow into job specs. Returns (jobs, errors)."""
    errors = []
    for k in flow:
        if k not in TOP_KEYS and not k.startswith("_") and not k.startswith("x_"):
            errors.append(f"unknown top-level key '{k}'")
    name = flow.get("name")
    if not name or not re.match(r"^[A-Za-z0-9_.+-]+$", str(name)):
        errors.append("name: required, letters/digits/_ . + - only")
        name = "flow"
    base = os.path.expanduser(str(flow.get("cwd") or "."))
    params = dict(flow.get("params") or {})
    params.update(overrides or {})
    defaults = dict(flow.get("defaults") or {})
    for k in defaults:
        if k not in DEFAULT_KEYS:
            errors.append(f"defaults: unknown key '{k}'")
    steps = flow.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("steps: at least one step is required")
        return [], errors
    names = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            errors.append(f"steps[{i}]: must be a mapping")
            continue
        n = s.get("name")
        if not n or not re.match(r"^[A-Za-z0-9_.+-]+$", str(n)):
            errors.append(f"steps[{i}]: name required (letters/digits/_ . + -)")
        elif n in names:
            errors.append(f"step '{n}': name used twice")
        names.append(n)
        for k in s:
            if k not in STEP_KEYS and not k.startswith("x_"):
                errors.append(f"step '{n}': unknown key '{k}'")
        if not s.get("cmd"):
            errors.append(f"step '{n}': cmd is required")
        for d in s.get("depends") or []:
            if d not in [x.get("name") for x in steps if isinstance(x, dict)]:
                errors.append(f"step '{n}': depends on unknown step '{d}'")
        for key in ("timeout",):
            if key in s or key in defaults:
                try:
                    queue.parse_duration(s.get(key, defaults.get(key)))
                except ValueError as e:
                    errors.append(f"step '{n}': {e}")
    # cycles
    graph = {s.get("name"): list(s.get("depends") or []) for s in steps if isinstance(s, dict)}
    state = {}

    def visit(n, stack):
        if state.get(n) == 1:
            errors.append("dependency cycle: " + " → ".join(stack + [n]))
            return
        if state.get(n) == 2:
            return
        state[n] = 1
        for d in graph.get(n, []):
            visit(d, stack + [n])
        state[n] = 2
    for n in graph:
        visit(n, [])
    if errors:
        return [], errors

    items_of = {}
    jobs = []
    by_step = {}
    for s in steps:
        n = s["name"]
        m = s.get("matrix")
        same = isinstance(m, str)
        if same:
            if m not in items_of:
                errors.append(f"step '{n}': matrix '{m}' must name an EARLIER step")
                continue
            items = items_of[m]
        else:
            items = _matrix_items(m, f"step '{n}'", errors)
        items_of[n] = items
        opts = dict(defaults)
        opts.update({k: v for k, v in s.items() if k in DEFAULT_KEYS})
        for idx, item in enumerate(items):
            vals = dict(params, **item, flow=name, step=n, index=idx)
            where = f"step '{n}'" + (f" item {item}" if item else "")
            folder = s.get("folder")
            if folder is None and item:
                folder = f"{n}/{_slug(item)}"
            cwd = os.path.normpath(os.path.join(base, _fill(folder, vals, where, errors))) if folder else base
            cmd = _fill(s["cmd"], vals, where, errors)
            env = {k: _fill(v, vals, where, errors) for k, v in dict(flow.get("env") or {}, **(opts.get("env") or {})).items()}
            spec = {
                "name": re.sub(r"[^A-Za-z0-9_.+-]", "_", f"{n}" + (f"_{_slug(item)}" if item else ""))[:60],
                "cmd": cmd, "cwd": cwd, "step": n, "item": item, "index": idx,
                "params": dict(item, **{k: v for k, v in (s.get("params") or {}).items()}),
                "depends_steps": list(s.get("depends") or []), "per_item": same and m in (s.get("depends") or []),
                "inputs": [_fill(x, vals, where, errors) for x in opts.get("inputs") or []],
                "outputs": [_fill(x, vals, where, errors) for x in opts.get("outputs") or []],
                "temp": [_fill(x, vals, where, errors) for x in opts.get("temp") or []],
                "require": [_fill(x, vals, where, errors) for x in list(flow.get("require") or []) + list(opts.get("require") or [])],
                "env": env,
            }
            for k in ("mem_gb", "cores", "gpus", "priority", "retries", "retry_on", "nice", "limit_memory", "container",
                      "host", "when_idle", "preempt", "tool", "incremental", "dep_mode"):
                if k in opts:
                    spec[k] = opts[k]
            if "timeout" in opts:
                spec["timeout_s"] = queue.parse_duration(opts["timeout"])
            if "container" in spec:
                spec["container"] = _fill(spec["container"], vals, where, errors)
            if "host" in spec:
                spec["host"] = _fill(spec["host"], vals, where, errors)
            spec["step_key"] = f"{name}:{n}:" + hashlib.sha1(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()[:10]
            jobs.append(spec)
            by_step.setdefault(n, []).append(spec)
    return jobs, errors


def check(path, overrides=None):
    try:
        f = load_file(path)
    except Exception as e:
        return None, [], [f"cannot read {path}: {type(e).__name__}: {e}"]
    jobs, errs = expand(f, overrides)
    return f, jobs, errs


def estimate(jobs):
    """Rough plan on an idle machine: [(spec, minutes, start_min)], total minutes, peak parallel."""
    th = probes.threads()
    total = probes.meminfo().get("MemTotal", 0)
    free_m = total - capacity.reserve_bytes(total)
    prof = capacity.profiles()
    done_at = {}
    running = []          # (end, cores, mem)
    t = 0.0
    plan = []
    peak = 0
    pending = list(jobs)
    guard = 0
    while pending and guard < 100000:
        guard += 1
        started = False
        for s in list(pending):
            deps = _dep_specs(s, jobs)
            if any(id(d) not in done_at or done_at[id(d)] > t for d in deps):
                continue
            tool = s.get("tool") or queue.guess_tool(s["cmd"])
            p = prof.get(tool or "", {})
            cores = float(s.get("cores") or p.get("cores") or 1.0)
            mem = float(s.get("mem_gb") or p.get("peak_gb") or 2.0) * queue.GiB
            used_c = sum(r[1] for r in running if r[0] > t)
            used_m = sum(r[2] for r in running if r[0] > t)
            if used_c + cores <= th and used_m + mem <= free_m or not [r for r in running if r[0] > t]:
                mins = float(p.get("minutes") or 30)
                running.append((t + mins, cores, mem))
                done_at[id(s)] = t + mins
                plan.append((s, mins, t, bool(p.get("minutes"))))
                pending.remove(s)
                started = True
                peak = max(peak, len([r for r in running if r[0] > t]))
        if not started:
            nxt = [r[0] for r in running if r[0] > t]
            if not nxt:
                break
            t = min(nxt)
    end = max([r[0] for r in running] or [0])
    return plan, end, peak


def _dep_specs(s, jobs):
    out = []
    for d in s.get("depends_steps") or []:
        cands = [j for j in jobs if j["step"] == d]
        if s.get("per_item"):
            cands = [j for j in cands if j["index"] == s["index"]]
        out += cands
    return out


def submit_flow(path, overrides=None, resume=False, dry_run=False, schedule_id=None):
    f, specs, errs = check(path, overrides)
    if errs:
        raise FlowError("\n".join(errs))
    if dry_run:
        return None, specs
    FLOWS.mkdir(parents=True, exist_ok=True)
    fid = f"{f['name']}-{time.strftime('%Y%m%d-%H%M%S')}"
    rec = {"id": fid, "name": f["name"], "file": f["_file"], "created": time.time(), "jobs": [],
           "on_success": f.get("on_success"), "on_failure": f.get("on_failure"), "cwd": os.path.expanduser(str(f["cwd"])),
           "notify": f.get("notify", True), "notified": False, "schedule": schedule_id, "overrides": overrides or {}}
    ids = {}
    for s in specs:
        deps = [ids[id(d)] for d in _dep_specs(s, specs)]
        opts = {k: v for k, v in s.items() if k not in ("cmd", "cwd", "name", "depends_steps", "per_item", "step", "item", "index")}
        opts["flow"] = {"id": fid, "name": f["name"], "step": s["step"], "item": s["item"]}
        job = queue.submit(s["cmd"], cwd=s["cwd"], name=s["name"], depends=deps, **opts)
        if resume:
            m = Path(job["cwd"]) / ".msq_done.json"
            try:
                e = json.loads(m.read_text()).get(job["step_key"])
            except (OSError, ValueError):
                e = None
            if e and e.get("key") == queue.job_key(job):
                queue.update(job["id"], state="cached", reason="already done (resume)", ended=time.time())
        ids[id(s)] = job["id"]
        rec["jobs"].append(job["id"])
    (FLOWS / f"{fid}.json").write_text(json.dumps(rec, indent=1))
    return rec, specs


def flows():
    out = []
    for p in sorted(FLOWS.glob("*.json")) if FLOWS.exists() else []:
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            pass
    return out


def flow_jobs(rec):
    return [j for j in (queue.load(i) for i in rec["jobs"]) if j]


def flow_state(rec):
    js = flow_jobs(rec)
    cnt = {}
    for j in js:
        cnt[j["state"]] = cnt.get(j["state"], 0) + 1
    done = all(j["state"] in queue.TERMINAL for j in js)
    ok = done and all(j["state"] in ("done", "cached") for j in js)
    return cnt, done, ok


def finish_flows(sched):
    for rec in flows():
        if rec.get("notified"):
            continue
        cnt, done, ok = flow_state(rec)
        if not done:
            continue
        js = flow_jobs(rec)
        t0 = min((j.get("started") or j["created"]) for j in js) if js else time.time()
        t1 = max((j.get("ended") or time.time()) for j in js) if js else time.time()
        summary = ", ".join(f"{v} {k}" for k, v in sorted(cnt.items()))
        if ok:
            for j in js:
                for pat in j.get("temp") or []:
                    for f in glob.glob(os.path.join(j["cwd"], pat)):
                        try:
                            shutil.rmtree(f) if os.path.isdir(f) and not os.path.islink(f) else os.remove(f)
                        except OSError:
                            pass
        hook = rec.get("on_success") if ok else rec.get("on_failure")
        hook_rc = None
        if hook:
            try:
                hook_rc = subprocess.run(hook, shell=True, cwd=rec["cwd"] if os.path.isdir(rec["cwd"]) else None,
                                         env=dict(os.environ, MSQ_FLOW_ID=rec["id"], MSQ_FLOW_STATUS="ok" if ok else "failed"),
                                         timeout=3600, capture_output=True).returncode
            except (OSError, subprocess.TimeoutExpired):
                hook_rc = -1
        rec.update(notified=True, finished=time.time(), status="ok" if ok else "failed", summary=summary, hook_rc=hook_rc)
        (FLOWS / f"{rec['id']}.json").write_text(json.dumps(rec, indent=1))
        if rec.get("notify", True):
            fail = [j for j in js if j["state"] not in ("done", "cached")]
            detail = f"{summary} · {queue_hms(t1 - t0)}" + (f" · first problem: #{fail[0]['id']} {fail[0]['name']}: {fail[0]['reason']}" if fail else "")
            sched.notify(f"Flow {'finished' if ok else 'FAILED'}: {rec['name']}", detail,
                         "default" if ok else "high", "white_check_mark" if ok else "warning")


def queue_hms(s):
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def retry_flow(fid):
    p = FLOWS / f"{fid}.json"
    rec = json.loads(p.read_text())
    n = 0
    for j in flow_jobs(rec):
        if j["state"] in ("failed", "timeout", "cancelled", "skipped"):
            queue.update(j["id"], state="queued", attempt=j["attempt"] + 1, started=None, ended=None, rc=None,
                         reason="flow retried", retried=False, oom=False)
            n += 1
    rec["notified"] = False
    p.write_text(json.dumps(rec, indent=1))
    return n


def cancel_flow(fid):
    rec = json.loads((FLOWS / f"{fid}.json").read_text())
    return sum(1 for j in flow_jobs(rec) if queue.cancel(j["id"]))


# ---------------------------------------------------------------- schedules
def cron_match(expr, t=None):
    """Standard 5-field cron (minute hour day month weekday); supports *, */n, a-b, a-b/n and lists."""
    t = time.localtime(t or time.time())
    fields = expr.split()
    if len(fields) != 5:
        raise FlowError(f"cron needs 5 fields: {expr!r}")
    vals = [t.tm_min, t.tm_hour, t.tm_mday, t.tm_mon, (t.tm_wday + 1) % 7]
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    for f, v, (lo, hi) in zip(fields, vals, ranges):
        ok = False
        for part in f.split(","):
            step = 1
            if "/" in part:
                part, st = part.split("/")
                step = int(st)
            if part == "*":
                a, b = lo, hi
            elif "-" in part:
                a, b = map(int, part.split("-"))
            else:
                a = b = int(part)
            if hi == 7 and v == 0 and b == 7:
                ok = ok or (7 - a) % step == 0
            if a <= v <= b and (v - a) % step == 0:
                ok = True
        if not ok:
            return False
    return True


def schedules():
    try:
        return json.loads(SCHED.read_text())
    except (OSError, ValueError):
        return []


def add_schedule(file, cron=None, idle=False, git=None, overrides=None):
    _, _, errs = check(file, overrides)
    if errs:
        raise FlowError("\n".join(errs))
    if cron:
        cron_match(cron)
    if not (cron or idle or git):
        raise FlowError("give --cron, --idle or --on-commit")
    sc = schedules()
    sid = max([s["id"] for s in sc] + [0]) + 1
    e = {"id": sid, "file": str(Path(file).expanduser().resolve()), "cron": cron, "idle": bool(idle), "git": None,
         "overrides": overrides or {}, "last": None, "created": time.time()}
    if git:
        repo, _, branch = git.partition(":")
        repo = os.path.expanduser(repo)
        e["git"] = {"repo": repo, "branch": branch or "HEAD", "head": _git_head(repo, branch or "HEAD")}
    sc.append(e)
    queue._ensure()
    SCHED.write_text(json.dumps(sc, indent=1))
    return e


def remove_schedule(sid):
    sc = [s for s in schedules() if s["id"] != int(sid)]
    SCHED.write_text(json.dumps(sc, indent=1))


def _git_head(repo, branch):
    try:
        return subprocess.run(["git", "-C", repo, "rev-parse", branch], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def run_schedules(sched):
    sc = schedules()
    if not sc:
        return
    now = time.time()
    changed = False
    active = {r.get("schedule") for r in flows() if not r.get("notified")}
    for e in sc:
        fire, why = False, ""
        if e.get("cron") and cron_match(e["cron"], now) and (now - (e.get("last") or 0)) > 60:
            fire, why = True, f"cron {e['cron']}"
        if e.get("idle") and now - (e.get("last") or 0) > 20 * 3600 and sched.idle():
            fire, why = True, "machine idle"
        if e.get("git"):
            h = _git_head(e["git"]["repo"], e["git"]["branch"])
            if h and h != e["git"].get("head"):
                e["git"]["head"] = h
                changed = True
                fire, why = True, f"new commit {h[:8]}"
        if fire:
            e["last"] = now
            changed = True
            if e["id"] in active:          # overlap policy: skip while the previous run is still going
                continue
            try:
                rec, _ = submit_flow(e["file"], e.get("overrides"), resume=True, schedule_id=e["id"])
                sched.notify(f"Flow started: {rec['name']}", f"scheduled ({why}) · {len(rec['jobs'])} job(s)", "min", "calendar")
            except Exception as ex:
                sched.notify("Scheduled flow could not start", f"{e['file']}: {ex}", "high", "warning")
    if changed:
        SCHED.write_text(json.dumps(sc, indent=1))


# ---------------------------------------------------------------- views
def dag_text(specs):
    lines = []
    steps = []
    for s in specs:
        if s["step"] not in steps:
            steps.append(s["step"])
    for st in steps:
        ss = [s for s in specs if s["step"] == st]
        dep = ss[0]["depends_steps"]
        rel = (" ← " + ", ".join(dep) + (" (per item)" if ss[0]["per_item"] else " (all)")) if dep else ""
        lines.append(f"{st}: {len(ss)} job(s){rel}")
    return "\n".join(lines)


def timeline_rows(job_list):
    """[(label, start, end, state)] for a Gantt chart."""
    out = []
    for j in job_list:
        if j.get("started"):
            out.append((f"#{j['id']} {j['name']}", j["started"], j.get("ended") or time.time(), j["state"]))
    return sorted(out, key=lambda r: r[1])


def timeline_html(job_list, title="Timeline"):
    rows = timeline_rows(job_list)
    if not rows:
        return "<p>No started jobs.</p>"
    t0 = min(r[1] for r in rows)
    t1 = max(r[2] for r in rows)
    span = max(1.0, t1 - t0)
    col = {"done": "#26a269", "cached": "#8f8f8f", "running": "#3584e4", "suspended": "#c88800", "failed": "#c01c28",
           "timeout": "#c64600", "cancelled": "#8f8f8f"}
    bars = []
    for i, (lab, a, b, st) in enumerate(rows):
        x = (a - t0) / span * 100
        w = max(0.4, (b - a) / span * 100)
        bars.append(f"<div class='r'><div class='l'>{lab}</div><div class='t'><div class='b' style='left:{x:.2f}%;"
                    f"width:{w:.2f}%;background:{col.get(st, '#999')}' title='{st} · {queue_hms(b - a)}'></div></div></div>")
    return (f"<!doctype html><meta charset='utf-8'><title>{title}</title><style>body{{font:13px system-ui;margin:20px}}"
            ".r{display:flex;align-items:center;height:20px}.l{width:280px;overflow:hidden;white-space:nowrap}"
            ".t{flex:1;position:relative;height:14px;background:#f1f1f1}.b{position:absolute;top:0;height:14px;border-radius:3px}</style>"
            f"<h2>{title}</h2><p>{time.strftime('%F %H:%M', time.localtime(t0))} → {time.strftime('%F %H:%M', time.localtime(t1))}"
            f" · {queue_hms(span)}</p>" + "".join(bars))
