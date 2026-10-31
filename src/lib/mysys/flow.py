"""Flows: one YAML file describing steps, parameter sweeps, dependencies and how to run them.

Minimal example (`ms flow example` prints a complete one):

    name: s18_doe
    cwd: /mnt/data/benyamin/efpga/S18
    defaults: {tool: vivado, retries: 1, retry_on: [oom], timeout: auto}
    steps:
      - name: synth
        matrix: {seed: [1, 2, 3], strategy: [Explore, Default]}
        folder: "doe_{{seed}}_{{strategy}}"
        cmd: "vivado -mode batch -source ../synth.tcl -tclargs {{seed}} {{strategy}}"
        inputs: ["../rtl/*.v"]
        outputs: ["post_synth.dcp"]
      - name: impl
        matrix: synth                 # same items: each impl waits only for its own synth
        depends: [synth]
        folder: "doe_{{seed}}_{{strategy}}"
        cmd: "vivado -mode batch -source ../impl.tcl"
        when: "{{steps.synth.outputs.lut}} < 50000"

Placeholders are {{name}} (double braces, so Tcl braces are safe). Values from earlier steps
({{steps.<step>.outputs.<key>}} / {{steps.<step>.metrics.<name>}}) are filled in when the job starts.
"""
import csv
import glob
import hashlib
import html
import itertools
import json
import os
import random
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import capacity, config, expr, probes, queue

FLOWS = queue.Q / "flows"
SCHED = queue.Q / "schedules.json"
TOP_KEYS = {"name", "cwd", "defaults", "params", "params_schema", "profiles", "require", "env", "dotenv", "steps",
            "on_success", "on_failure", "on_fixed", "on_regression", "on_change", "on_cancel", "on_exit", "notify",
            "notify_mode", "halt", "publish", "description", "schedule", "secrets"}
STEP_KEYS = {"name", "cmd", "matrix", "matrix_exclude", "matrix_filter", "matrix_mode", "matrix_csv", "folder", "depends",
             "dep_mode", "inputs", "outputs", "temp", "clean_on", "require", "mem_gb", "cores", "gpus", "priority",
             "retries", "retry_on", "retry_delay", "retry_backoff", "retry_max_delay", "retry_delays",
             "retry_exit_codes", "retry_scale", "timeout", "idle_timeout", "signal_before", "kill_seq", "nice",
             "limit_memory", "container", "host", "when_idle", "preempt", "env", "tool", "incremental", "params",
             "description", "when", "assert", "foreach", "flow", "with", "repeat", "wait", "approval", "stub", "scratch",
             "protect", "ensure", "keep_incomplete", "publish", "junit", "quality_gate", "follow", "exit_codes",
             "warn_patterns", "resources", "disk_gb", "disk_path", "benchmark", "cache_mode", "rerun_on", "cache",
             "on_error", "secrets", "notify", "begin", "deadline", "collapse_key", "max_parallel", "require_outputs",
             "respect_reservations", "stop_when", "stop_after", "group", "labels", "stdin", "remote"}
DEFAULT_KEYS = STEP_KEYS - {"name", "cmd", "matrix", "matrix_exclude", "matrix_filter", "matrix_mode", "matrix_csv",
                            "folder", "depends", "description", "foreach", "flow", "with", "wait", "approval"}
PASS_KEYS = ["mem_gb", "cores", "gpus", "priority", "retries", "retry_on", "retry_delay", "retry_backoff",
             "retry_max_delay", "retry_delays", "retry_exit_codes", "retry_scale", "nice", "limit_memory", "when_idle",
             "preempt", "tool", "incremental", "dep_mode", "signal_before", "kill_seq", "scratch", "protect", "ensure",
             "keep_incomplete", "quality_gate", "exit_codes", "warn_patterns", "resources", "disk_gb", "benchmark",
             "cache_mode", "rerun_on", "cache", "secrets", "notify", "collapse_key", "max_parallel", "require_outputs",
             "respect_reservations", "repeat", "approval", "stub", "begin", "deadline", "stop_when", "stop_after",
             "group", "labels", "stdin", "remote"]
PH = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")
LATE = re.compile(r"^steps\.[A-Za-z0-9_.+-]+\.(outputs|metrics)\.[A-Za-z0-9_]+$")


class FlowError(Exception):
    pass


# ---------------------------------------------------------------- loading
def _merge(a, b):
    out = dict(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _dotenv(path):
    out = {}
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip().removeprefix("export ").strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def load_file(path, profile=None):
    p = Path(path).expanduser()
    txt = p.read_text()
    if p.suffix == ".json":
        data = json.loads(txt)
    elif p.suffix == ".toml":
        import tomllib
        data = tomllib.loads(txt)
    else:
        import yaml
        data = yaml.safe_load(txt)
    if not isinstance(data, dict):
        raise FlowError("the file must contain a mapping (name:, steps:, …)")
    base = config.CONF / "flow_base.yaml"            # shared defaults for every flow (Dagu base.yaml)
    if base.exists():
        try:
            import yaml
            b = yaml.safe_load(base.read_text()) or {}
            data = _merge({k: v for k, v in b.items() if k in ("defaults", "env", "require", "notify_mode", "halt")}, data)
        except Exception:
            pass
    if profile:
        prof = (data.get("profiles") or {}).get(profile)
        if prof is None:
            raise FlowError(f"unknown profile '{profile}' (defined: {', '.join(data.get('profiles') or {}) or 'none'})")
        data = _merge(data, prof)
    data.setdefault("cwd", str(p.parent.resolve()))
    if data.get("dotenv"):
        envf = os.path.join(str(p.parent), os.path.expanduser(data["dotenv"]))
        data["env"] = dict(_dotenv(envf), **(data.get("env") or {}))
    data["_file"] = str(p.resolve())
    return data


def _fill(text, values, where, errors, late_ok=True):
    def rep(m):
        k = m.group(1)
        if k in values:
            return str(values[k])
        if late_ok and LATE.match(k):
            return m.group(0)                      # filled when the job starts
        errors.append(f"{where}: unknown placeholder {{{{{k}}}}}")
        return m.group(0)
    return PH.sub(rep, str(text))


def _check_params(schema, params, errors):
    for k, rule in (schema or {}).items():
        if k not in params:
            if rule.get("required"):
                errors.append(f"param '{k}' is required")
            elif "default" in rule:
                params[k] = rule["default"]
            continue
        v = params[k]
        t = rule.get("type")
        try:
            if t == "int":
                params[k] = v = int(v)
            elif t in ("float", "number"):
                params[k] = v = float(v)
            elif t == "bool":
                params[k] = v = str(v).lower() in ("1", "true", "yes", "on")
        except (TypeError, ValueError):
            errors.append(f"param '{k}': expected {t}, got {v!r}")
            continue
        if "enum" in rule and v not in rule["enum"]:
            errors.append(f"param '{k}': {v!r} not one of {rule['enum']}")
        if "min" in rule and isinstance(v, (int, float)) and v < rule["min"]:
            errors.append(f"param '{k}': {v} < min {rule['min']}")
        if "max" in rule and isinstance(v, (int, float)) and v > rule["max"]:
            errors.append(f"param '{k}': {v} > max {rule['max']}")
        if "pattern" in rule and not re.fullmatch(rule["pattern"], str(v)):
            errors.append(f"param '{k}': {v!r} does not match {rule['pattern']}")


def _matrix_items(s, base, params, where, errors):
    m = s.get("matrix")
    if s.get("matrix_csv"):
        f = os.path.join(base, os.path.expanduser(s["matrix_csv"]))
        try:
            with open(f, newline="") as fh:
                items = [dict(r) for r in csv.DictReader(fh, delimiter="\t" if f.endswith(".tsv") else ",")]
        except OSError as e:
            errors.append(f"{where}: cannot read matrix_csv: {e}")
            return [{}]
    elif m is None:
        return [{}]
    elif isinstance(m, list):
        if not all(isinstance(x, dict) for x in m):
            errors.append(f"{where}: matrix list must contain mappings")
            return [{}]
        items = [dict(x) for x in m]
    elif isinstance(m, dict):
        keys = list(m)
        vals = [v if isinstance(v, list) else [v] for v in m.values()]
        if s.get("matrix_mode") == "zip":
            if len({len(v) for v in vals}) > 1:
                errors.append(f"{where}: matrix_mode zip needs lists of equal length")
                return [{}]
            items = [dict(zip(keys, combo)) for combo in zip(*vals)]
        else:
            items = [dict(zip(keys, combo)) for combo in itertools.product(*vals)]
    else:
        errors.append(f"{where}: matrix must be a mapping of lists, a list of mappings, or the name of an earlier step")
        return [{}]
    excl = s.get("matrix_exclude") or []
    items = [it for it in items if not any(all(str(it.get(k)) == str(v) for k, v in e.items()) for e in excl)]
    if s.get("matrix_filter"):
        keep = []
        for it in items:
            try:
                if expr.evaluate(_fill(s["matrix_filter"], dict(params, **it), where, errors, late_ok=False)):
                    keep.append(it)
            except expr.ExprError as e:
                errors.append(f"{where}: matrix_filter: {e}")
                return items
        items = keep
    return items


def _slug(item):
    return "_".join(f"{v}" for v in item.values()) if item else ""


def expand(flow, overrides=None, sample=None, seed=None):
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
    _check_params(flow.get("params_schema"), params, errors)
    defaults = dict(flow.get("defaults") or {})
    for k in defaults:
        if k not in DEFAULT_KEYS:
            errors.append(f"defaults: unknown key '{k}'")
    steps = flow.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("steps: at least one step is required")
        return [], errors
    names = [s.get("name") for s in steps if isinstance(s, dict)]
    seen = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            errors.append(f"steps[{i}]: must be a mapping")
            continue
        n = s.get("name")
        if not n or not re.match(r"^[A-Za-z0-9_.+-]+$", str(n)):
            errors.append(f"steps[{i}]: name required (letters/digits/_ . + -)")
        elif n in seen:
            errors.append(f"step '{n}': name used twice")
        seen.append(n)
        for k in s:
            if k not in STEP_KEYS and not k.startswith("x_"):
                errors.append(f"step '{n}': unknown key '{k}'")
        if not (s.get("cmd") or s.get("flow") or s.get("wait")):
            errors.append(f"step '{n}': needs cmd (or flow: / wait:)")
        for d in s.get("depends") or []:
            if d not in names:
                errors.append(f"step '{n}': depends on unknown step '{d}'")
        for key in ("timeout", "idle_timeout", "retry_delay", "retry_max_delay"):
            v = s.get(key, defaults.get(key))
            if v not in (None, "auto") and not (isinstance(v, str) and v.strip().endswith("%")):
                try:
                    queue.parse_duration(v)
                except ValueError as e:
                    errors.append(f"step '{n}': {key}: {e}")
        for key in ("on_error",):
            v = s.get(key, defaults.get(key))
            if v and v not in ("terminate", "finish", "ignore", "retry"):
                errors.append(f"step '{n}': on_error must be terminate, finish, ignore or retry")
        for key in ("begin", "deadline"):
            if s.get(key):
                try:
                    queue.parse_when(s[key])
                except ValueError as e:
                    errors.append(f"step '{n}': {e}")
    halt = flow.get("halt") or {}
    if halt and not (set(halt) <= {"fail", "fail_pct", "mode"}):
        errors.append("halt: keys are fail, fail_pct, mode (now|soon)")
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

    rnd = random.Random(seed)
    items_of = {}
    jobs = []
    for s in steps:
        n = s["name"]
        m = s.get("matrix")
        same = isinstance(m, str)
        where = f"step '{n}'"
        if same:
            if m not in items_of:
                errors.append(f"{where}: matrix '{m}' must name an EARLIER step")
                continue
            items = items_of[m]
        else:
            items = _matrix_items(s, base, params, where, errors)
            if sample and len(items) > 1:
                k = max(1, int(len(items) * float(str(sample)[:-1]) / 100)) if str(sample).endswith("%") else min(len(items), int(sample))
                items = rnd.sample(items, k)
        items_of[n] = items
        opts = dict(defaults)
        opts.update({k: v for k, v in s.items() if k in DEFAULT_KEYS})
        for idx, item in enumerate(items):
            vals = dict(params, **item, flow=name, step=n, index=idx)
            iw = where + (f" item {item}" if item else "")
            fo = s.get("foreach")
            if fo is not None:
                vals.setdefault("item", "{{item}}")
            folder = s.get("folder")
            if folder is None and item:
                folder = f"{n}/{_slug(item)}"
            cwd = os.path.normpath(os.path.join(base, _fill(folder, vals, iw, errors, late_ok=False))) if folder and "{{item" not in str(folder) else base
            spec = {
                "name": re.sub(r"[^A-Za-z0-9_.+-]", "_", f"{n}" + (f"_{_slug(item)}" if item else ""))[:60],
                "cmd": _fill(s.get("cmd") or "", vals, iw, errors), "cwd": cwd, "step": n, "item": item, "index": idx,
                "params": dict(item, **{k: v for k, v in (s.get("params") or {}).items()}),
                "depends_steps": list(s.get("depends") or []), "per_item": same and m in (s.get("depends") or []),
                "inputs": [_fill(x, vals, iw, errors) for x in opts.get("inputs") or []],
                "outputs": [_fill(x, vals, iw, errors) for x in opts.get("outputs") or []],
                "temp": [_fill(x, vals, iw, errors) for x in opts.get("temp") or []],
                "require": [_fill(x, vals, iw, errors) for x in list(flow.get("require") or []) + list(opts.get("require") or [])],
                "env": {k: _fill(v, vals, iw, errors) for k, v in dict(flow.get("env") or {}, **(opts.get("env") or {})).items()},
                "secrets": list(flow.get("secrets") or []) + list(opts.get("secrets") or []),
            }
            for k in PASS_KEYS:
                if k in opts and k not in ("secrets",):
                    spec[k] = opts[k]
            for k in ("timeout", "idle_timeout"):
                if k in opts:
                    v = opts[k]
                    spec[k + "_s"] = v if v == "auto" or (isinstance(v, str) and v.strip().endswith("%")) else queue.parse_duration(v)
            for k in ("container", "host", "when", "assert", "approval", "stub", "disk_path"):
                if k in opts and opts[k] is not None:
                    spec[k] = _fill(opts[k], vals, iw, errors)
            if opts.get("follow"):
                spec["follow"] = [_fill(x, vals, iw, errors) for x in opts["follow"]]
            if opts.get("junit"):
                spec["junit"] = [_fill(x, vals, iw, errors) for x in opts["junit"]]
            pub = opts.get("publish") or flow.get("publish")
            if pub:
                spec["publish"] = {**pub, "dir": os.path.join(base, os.path.expanduser(_fill(pub.get("dir", "results"), vals, iw, errors)))}
            if s.get("wait"):
                spec["wait"] = {k: _fill(v, vals, iw, errors) if isinstance(v, str) else v for k, v in s["wait"].items()}
                spec["light"] = True
            if s.get("flow"):
                spec["subflow"] = {"file": os.path.join(base, _fill(s["flow"], vals, iw, errors)),
                                   "with": {k: _fill(v, vals, iw, errors) for k, v in (s.get("with") or {}).items()}}
                spec["light"] = True
            if fo is not None:
                spec["fanout"] = {"items": fo if isinstance(fo, list) else _fill(fo, vals, iw, errors),
                                  "folder": s.get("folder"), "cmd": s.get("cmd"), "values": {k: v for k, v in vals.items() if k != "item"}}
                spec["light"] = True
            if "on_error" in opts:
                spec["on_error"] = opts["on_error"]
            if "clean_on" in opts:
                spec["clean_on"] = opts["clean_on"]
            spec["step_key"] = f"{name}:{n}:" + hashlib.sha1(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()[:10]
            for k in ("when", "assert"):
                if spec.get(k):
                    probe = PH.sub("1", spec[k])
                    try:
                        expr.evaluate(probe)
                    except expr.ExprError as e:
                        errors.append(f"{iw}: {k}: {e}")
            jobs.append(spec)
    return jobs, errors


def select(specs, until=None, from_=None, only=None):
    """Snakemake --until / --omit-from style partial runs."""
    if not (until or from_ or only):
        return specs
    deps = {}
    for s in specs:
        deps.setdefault(s["step"], set()).update(s["depends_steps"])
    keep = set()
    if only:
        keep = set(only)
    if until:
        todo = list(until)
        while todo:
            x = todo.pop()
            if x not in keep:
                keep.add(x)
                todo += list(deps.get(x, []))
    if from_:
        down = set(from_)
        changed = True
        while changed:
            changed = False
            for st, ds in deps.items():
                if st not in down and ds & down:
                    down.add(st)
                    changed = True
        keep = (keep & down) if (until or only) else down
    out = []
    for s in specs:
        if s["step"] in keep:
            s = dict(s, depends_steps=[d for d in s["depends_steps"] if d in keep])
            out.append(s)
    return out


def check(path, overrides=None, profile=None, sample=None):
    try:
        f = load_file(path, profile)
    except Exception as e:
        return None, [], [f"cannot read {path}: {type(e).__name__}: {e}"]
    jobs, errs = expand(f, overrides, sample)
    return f, jobs, errs


def lint(path, profile=None):
    """Advice that is not an error (Snakemake --lint)."""
    f, specs, errs = check(path, profile=profile)
    if errs:
        return errs
    out = []
    steps = {}
    for s in specs:
        steps.setdefault(s["step"], []).append(s)
    for st, ss in steps.items():
        s = ss[0]
        if not s.get("outputs") and not s.get("subflow") and not s.get("wait"):
            out.append(f"step '{st}': no outputs declared — it can never be skipped as up to date or published")
        if not s.get("timeout_s") and not s.get("idle_timeout_s") and not s.get("light"):
            out.append(f"step '{st}': no timeout or idle_timeout — a hanging tool runs forever")
        if len(ss) > 200:
            out.append(f"step '{st}': {len(ss)} jobs — consider matrix_filter or --sample to preview first")
        if s.get("tool") == "vivado" and "-nojournal" not in s["cmd"] and not s.get("temp"):
            out.append(f"step '{st}': Vivado writes .jou/.Xil files; add -nojournal or temp: ['.Xil', '*.jou']")
        if "/tmp/" in s["cmd"]:
            out.append(f"step '{st}': uses /tmp — every job has its own $TMPDIR; prefer that")
        if s.get("tool") == "vivado" and not s.get("resources"):
            out.append(f"step '{st}': no license resource — if seats are limited, add resources: {{vivado_license: 1}}")
        if any(re.search(r"\b(sudo|apt|pip install)\b", s["cmd"]) for _ in [0]):
            out.append(f"step '{st}': changes the system (sudo/apt/pip) — not allowed in flows")
    return out


# ---------------------------------------------------------------- estimate and submit
def estimate(jobs):
    """Rough plan on an idle machine: [(spec, minutes, start_min, measured)], total minutes, peak parallel."""
    th = probes.threads()
    total = probes.meminfo().get("MemTotal", 0)
    free_m = total - capacity.reserve_bytes(total)
    prof = capacity.profiles()
    done_at = {}
    running = []
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
            tool = s.get("tool") or queue.guess_tool(s["cmd"] or "true")
            p = prof.get(tool or "", {})
            cores = 0 if s.get("light") else float(s.get("cores") or p.get("cores") or 1.0)
            mem = 0 if s.get("light") else float(s.get("mem_gb") or p.get("peak_gb") or 2.0) * queue.GiB
            used_c = sum(r[1] for r in running if r[0] > t)
            used_m = sum(r[2] for r in running if r[0] > t)
            if used_c + cores <= th and used_m + mem <= free_m or not [r for r in running if r[0] > t]:
                mins = float(p.get("minutes") or (1 if s.get("light") else 30))
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


def submit_flow(path, overrides=None, resume=False, dry_run=False, schedule_id=None, profile=None, sample=None,
                until=None, from_=None, only=None, force=None, stub=False, parent_job=None):
    f, specs, errs = check(path, overrides, profile, sample)
    if errs:
        raise FlowError("\n".join(errs))
    specs = select(specs, until, from_, only)
    if dry_run:
        return None, specs
    FLOWS.mkdir(parents=True, exist_ok=True)
    fid = f"{f['name']}-{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(2).hex()}"
    rec = {"id": fid, "name": f["name"], "file": f["_file"], "created": time.time(), "jobs": [],
           "hooks": {k: f.get(k) for k in ("on_success", "on_failure", "on_fixed", "on_regression", "on_change",
                                           "on_cancel", "on_exit") if f.get(k)},
           "cwd": os.path.expanduser(str(f["cwd"])), "notify": f.get("notify", True),
           "notify_mode": f.get("notify_mode", "always"), "halt": f.get("halt"), "notified": False,
           "schedule": schedule_id, "overrides": overrides or {}, "profile": profile, "parent_job": parent_job}
    ids = {}
    for s in specs:
        deps = [ids[id(d)] for d in _dep_specs(s, specs)]
        opts = {k: v for k, v in s.items() if k not in ("cmd", "cwd", "name", "depends_steps", "per_item", "step", "item", "index")}
        opts["flow"] = {"id": fid, "name": f["name"], "step": s["step"], "item": s["item"],
                        "on_error": s.get("on_error"), "ignore_failure": s.get("on_error") == "ignore"}
        if force and (s["step"] in force or "all" in force):
            opts["force"] = True
        if stub:
            opts["stub_mode"] = True
            opts["limit_memory"] = False
        cmd = s["cmd"] or "true"
        job = queue.submit(cmd, cwd=s["cwd"], name=s["name"], depends=deps, **opts)
        if resume and not opts.get("force"):
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


def load_flow(fid):
    return json.loads((FLOWS / f"{fid}.json").read_text())


def save_flow(rec):
    (FLOWS / f"{rec['id']}.json").write_text(json.dumps(rec, indent=1))


def flow_jobs(rec):
    return [j for j in (queue.load(i) for i in rec["jobs"]) if j]


def flow_state(rec):
    js = flow_jobs(rec)
    cnt = {}
    for j in js:
        cnt[j["state"]] = cnt.get(j["state"], 0) + 1
    done = all(j["state"] in queue.TERMINAL for j in js)
    ok = done and all(j["state"] in queue.SUCCESS or j.get("cond_skip") or (j.get("flow") or {}).get("ignore_failure") for j in js)
    return cnt, done, ok


# ---------------------------------------------------------------- runtime: before a job starts
def _late_values(j, byid):
    """Values of {{steps.X.outputs.K}} / {{steps.X.metrics.K}} from the jobs this one depends on."""
    from . import runlog
    vals = {}
    for d in j.get("depends") or []:
        dj = byid.get(d) or queue.load(d)
        if not dj:
            continue
        st = (dj.get("flow") or {}).get("step") or dj["name"]
        for k, v in (dj.get("outputs") or {}).items():
            vals.setdefault(f"steps.{st}.outputs.{k}", []).append(v)
        if dj.get("run_id"):
            r = runlog.read(os.path.join(dj["cwd"], runlog.FILENAME)).get(dj["run_id"])
            for k, v in (r or {}).get("metrics", {}).items():
                vals.setdefault(f"steps.{st}.metrics.{k}", []).append(v)
    return {k: v[0] if len(v) == 1 else " ".join(map(str, v)) for k, v in vals.items()}


def _late_fill(text, vals):
    missing = []

    def rep(m):
        k = m.group(1)
        if k in vals:
            return str(vals[k])
        if LATE.match(k):
            missing.append(k)
        return m.group(0)
    return PH.sub(rep, str(text)), missing


def before_start(sched, j, byid):
    """Called by the scheduler for a ready job. Returns 'go' to let it start normally."""
    now = time.time()
    fl = j.get("flow") or {}
    # per-step concurrency (Nextflow maxForks)
    if j.get("max_parallel") and fl.get("id"):
        n = sum(1 for x in queue.all_jobs() if (x.get("flow") or {}).get("id") == fl["id"]
                and (x.get("flow") or {}).get("step") == fl.get("step") and x["state"] in queue.ACTIVE)
        if n >= int(j["max_parallel"]):
            if j.get("reason") != f"waiting: max_parallel {j['max_parallel']} for this step":
                queue.update(j["id"], reason=f"waiting: max_parallel {j['max_parallel']} for this step")
            return "wait"
    # late placeholders
    fo = j.get("fanout")
    late_items = fo and isinstance(fo.get("items"), str) and "{{" in fo["items"]
    if late_items or any("{{steps." in str(j.get(k) or "") for k in ("cmd", "when", "assert")) or \
            any("{{steps." in str(v) for v in (j.get("env") or {}).values()):
        vals = _late_values(j, byid)
        upd, miss = {}, []
        for k in ("cmd", "when", "assert"):
            if j.get(k):
                v, m = _late_fill(j[k], vals)
                upd[k], miss = v, miss + m
        env = {}
        for k, v in (j.get("env") or {}).items():
            env[k], m = _late_fill(v, vals)
            miss += m
        upd["env"] = env
        if late_items:
            v, m = _late_fill(fo["items"], vals)
            upd["fanout"] = dict(fo, items=v)
            miss += m
        if miss:
            queue.update(j["id"], state="failed", ended=now, reason="no value for " + ", ".join(sorted(set(miss))))
            return "failed"
        j = queue.update(j["id"], **upd)
    if j.get("when"):
        try:
            if not expr.evaluate(j["when"]):
                queue.update(j["id"], state="skipped", ended=now, reason=f"condition false: {j['when']}", cond_skip=True)
                return "skipped"
        except expr.ExprError as e:
            queue.update(j["id"], state="failed", ended=now, reason=f"condition error: {e}")
            return "failed"
    if j.get("fanout"):
        return _start_fanout(sched, j)
    if j.get("subflow"):
        try:
            rec, _ = submit_flow(j["subflow"]["file"], j["subflow"].get("with"), resume=True, parent_job=j["id"])
            queue.update(j["id"], state="running", started=now, child_flow=rec["id"], reason=f"running flow {rec['id']}")
        except Exception as e:
            queue.update(j["id"], state="failed", ended=now, reason=f"sub-flow: {e}")
        return "handled"
    if j.get("cache") == "shared" and not j.get("force") and cache_restore(j):
        queue.update(j["id"], state="cached", ended=now, reason="restored from the shared cache")
        return "cached"
    return "go"


def _start_fanout(sched, j):
    fo = j["fanout"]
    items = fo["items"]
    if isinstance(items, str):
        s = items.strip()
        try:
            items = json.loads(s)
        except ValueError:
            items = [x for x in re.split(r"[\s,]+", s) if x]
    if not isinstance(items, list):
        items = [items]
    kids = []
    rec = None
    fid = (j.get("flow") or {}).get("id")
    for i, it in enumerate(items):
        vals = dict(fo["values"], item=it, index=i)
        errs = []
        cmd = _fill(fo["cmd"] or "true", vals, "foreach", errs)
        folder = _fill(fo["folder"], vals, "foreach", errs) if fo.get("folder") else None
        cwd = os.path.normpath(os.path.join(j["cwd"], folder)) if folder else j["cwd"]
        opts = {k: v for k, v in j.items() if k in PASS_KEYS or k in ("inputs", "outputs", "temp", "require", "env",
                                                                       "secrets", "publish", "follow", "junit", "timeout_s",
                                                                       "idle_timeout_s", "assert")}
        opts["params"] = dict(j.get("params") or {}, item=it)
        opts["flow"] = dict(j.get("flow") or {}, item=dict((j.get("flow") or {}).get("item") or {}, item=it))
        opts["step_key"] = f"{j.get('step_key', j['name'])}:{hashlib.sha1(str(it).encode()).hexdigest()[:8]}"
        k = queue.submit(cmd, cwd=cwd, name=f"{j['name']}_{re.sub(r'[^A-Za-z0-9_.+-]', '_', str(it))}"[:60], **opts)
        kids.append(k["id"])
    if fid:
        try:
            rec = load_flow(fid)
            rec["jobs"] += kids
            save_flow(rec)
        except (OSError, ValueError):
            pass
    queue.update(j["id"], state="running", started=time.time(), children=kids, reason=f"{len(kids)} job(s) from foreach")
    if not kids:
        queue.update(j["id"], state="done", ended=time.time(), reason="foreach list was empty")
    return "handled"


# ---------------------------------------------------------------- runtime: reactions every tick
def react(sched, jobs):
    now = time.time()
    byid = {j["id"]: j for j in jobs}
    # fan-out and sub-flow parents finish when their children do
    for j in jobs:
        if j["state"] != "running":
            continue
        if j.get("children") is not None:
            kids = [byid.get(k) or queue.load(k) for k in j["children"]]
            if all(k and k["state"] in queue.TERMINAL for k in kids):
                bad = [k for k in kids if k["state"] not in queue.SUCCESS]
                queue.update(j["id"], state="failed" if bad else "done", ended=now,
                             reason=f"{len(bad)} of {len(kids)} foreach job(s) failed" if bad else f"{len(kids)} foreach job(s) done")
        elif j.get("child_flow"):
            try:
                cnt, done, ok = flow_state(load_flow(j["child_flow"]))
            except (OSError, ValueError):
                continue
            if done:
                queue.update(j["id"], state="done" if ok else "failed", ended=now,
                             reason=f"sub-flow {j['child_flow']}: " + ", ".join(f"{v} {k}" for k, v in cnt.items()))
    # on_error strategies of finally failed jobs
    for j in jobs:
        fl = j.get("flow") or {}
        if j["state"] in ("failed", "timeout") and j.get("retried") and not j.get("reacted") and fl.get("id"):
            queue.update(j["id"], reacted=True)
            how = fl.get("on_error")
            if how in ("terminate", "finish"):
                for x in jobs:
                    if (x.get("flow") or {}).get("id") != fl["id"] or x["id"] == j["id"]:
                        continue
                    if x["state"] in ("queued", "held", "approval"):
                        queue.update(x["id"], state="skipped", ended=now, reason=f"flow stopped after #{j['id']} failed")
                    elif how == "terminate" and x["state"] in queue.ACTIVE:
                        queue.cancel(x["id"])
    # race (stop_when): once enough variants of a step reach the goal, the others are not needed any more
    from . import expr
    won = {}
    for j in jobs:
        fl = j.get("flow") or {}
        if not j.get("stop_when") or not fl.get("id") or j["state"] not in queue.SUCCESS:
            continue
        key = (fl["id"], fl.get("step"))
        if j.get("race_ok") is None:
            names = {}
            for k, v in (j.get("outputs") or {}).items():
                try:
                    names[k] = float(v)
                except (TypeError, ValueError):
                    names[k] = v
            try:
                from . import runlog
                r = runlog.read(os.path.join(j["cwd"], runlog.FILENAME)).get(j.get("run_id") or "")
                names.update((r or {}).get("metrics", {}))
                ok = bool(expr.evaluate(j["stop_when"], names))
            except Exception:
                ok = False
            queue.update(j["id"], race_ok=ok)
            j["race_ok"] = ok
        if j["race_ok"]:
            won.setdefault(key, []).append(j)
    for (fid, step), winners in won.items():
        need = int(winners[0].get("stop_after") or 1)
        if len(winners) < need:
            continue
        wid = winners[0]["id"]
        for x in jobs:
            fx = x.get("flow") or {}
            if fx.get("id") != fid or fx.get("step") != step or x["state"] in queue.TERMINAL:
                continue
            why = f"not needed: #{wid} already reached the goal ({x.get('stop_when')})"
            fx = dict(fx, ignore_failure=True)
            if x["state"] in queue.ACTIVE:
                queue.update(x["id"], flow=fx, cond_skip=True, race_lost=True)
                queue.cancel(x["id"], why)
            else:
                queue.update(x["id"], state="skipped", ended=now, reason=why, flow=fx, cond_skip=True, race_lost=True)
        if not any(r.get("race_told") for r in winners):
            queue.update(wid, race_told=True)
            sched.notify("Goal reached", f"#{wid} {winners[0]['name']} met {winners[0]['stop_when']} — "
                                         f"remaining variants of step {step} stopped", "default", "trophy")
    # halt rules (GNU Parallel --halt)
    for rec in flows():
        h = rec.get("halt")
        if not h or rec.get("halted") or rec.get("notified"):
            continue
        js = [byid.get(i) for i in rec["jobs"] if byid.get(i)]
        fin = [x for x in js if x["state"] in queue.TERMINAL]
        bad = [x for x in fin if x["state"] in ("failed", "timeout") and x.get("retried")]
        trip = (h.get("fail") is not None and len(bad) >= int(h["fail"])) or \
               (h.get("fail_pct") is not None and fin and len(bad) / len(js) * 100 >= float(h["fail_pct"]) and len(bad) >= 1)
        if trip:
            rec["halted"] = True
            save_flow(rec)
            for x in js:
                if x["state"] in ("queued", "held", "approval"):
                    queue.update(x["id"], state="skipped", ended=now, reason="flow halted (too many failures)")
                elif h.get("mode", "soon") == "now" and x["state"] in queue.ACTIVE:
                    queue.cancel(x["id"])
            sched.notify(f"Flow halted: {rec['name']}", f"{len(bad)} failed of {len(js)} — remaining jobs stopped", "high", "stop_sign")
    # temp files: delete as soon as every job that needed them is finished (Snakemake temp())
    for j in jobs:
        if not j.get("temp") or j.get("temp_cleaned"):
            continue
        when = j.get("clean_on", "success")
        st_ok = j["state"] in queue.SUCCESS
        if not ((when == "success" and st_ok) or (when == "failure" and j["state"] in queue.TERMINAL - queue.SUCCESS)
                or (when == "always" and j["state"] in queue.TERMINAL)):
            continue
        users = [x for x in jobs if j["id"] in (x.get("depends") or [])]
        if all(x["state"] in queue.TERMINAL for x in users):
            for pat in j["temp"]:
                for f in glob.glob(os.path.join(j["cwd"], pat)):
                    try:
                        shutil.rmtree(f) if os.path.isdir(f) and not os.path.islink(f) else os.remove(f)
                    except OSError:
                        pass
            queue.update(j["id"], temp_cleaned=True)


def _prev_status(rec):
    prev = [r for r in flows() if r["name"] == rec["name"] and r.get("finished") and r["id"] != rec["id"]
            and r["created"] < rec["created"]]
    return sorted(prev, key=lambda r: r["created"])[-1].get("status") if prev else None


def _hook(cmd, rec, status):
    try:
        return subprocess.run(cmd, shell=True, cwd=rec["cwd"] if os.path.isdir(rec["cwd"]) else None,
                              env=dict(os.environ, MSQ_FLOW_ID=rec["id"], MSQ_FLOW_STATUS=status),
                              timeout=3600, capture_output=True).returncode
    except (OSError, subprocess.TimeoutExpired):
        return -1


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
        cancelled = cnt.get("cancelled", 0) > 0
        status = "ok" if ok else ("cancelled" if cancelled and not any(j["state"] in ("failed", "timeout") for j in js) else "failed")
        prev = _prev_status(rec)
        hooks = rec.get("hooks") or {"on_success": rec.get("on_success"), "on_failure": rec.get("on_failure")}
        ran = {}
        todo = [("on_success" if ok else "on_failure")]
        if prev is not None and prev != status:
            todo.append("on_change")
        if ok and prev in ("failed", "cancelled"):
            todo.append("on_fixed")
        if not ok and prev == "ok":
            todo.append("on_regression")
        if cancelled:
            todo.append("on_cancel")
        todo.append("on_exit")
        for h in todo:
            if hooks.get(h):
                ran[h] = _hook(hooks[h], rec, status)
        rec.update(notified=True, finished=time.time(), status=status, summary=summary, hooks_run=ran, prev_status=prev)
        save_flow(rec)
        mode = rec.get("notify_mode", "always")
        tell = rec.get("notify", True) and (
            mode == "always" or (mode == "failing" and status != "ok") or (mode == "change" and prev is not None and prev != status)
            or (mode == "problem" and ((status != "ok" and prev in (None, "ok")) or (ok and prev in ("failed", "cancelled")))))
        if tell:
            fail = [j for j in js if j["state"] not in queue.SUCCESS and not j.get("cond_skip")]
            word = "back to OK" if ok and prev in ("failed", "cancelled") else ("finished" if ok else status.upper())
            detail = f"{summary} · {queue_hms(t1 - t0)}" + (f" · first problem: #{fail[0]['id']} {fail[0]['name']}: {fail[0]['reason']}" if fail else "")
            sched.notify(f"Flow {word}: {rec['name']}", detail, "default" if ok else "high", "white_check_mark" if ok else "warning")


def queue_hms(s):
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def retry_flow(fid, steps=None, downstream=False):
    rec = load_flow(fid)
    js = flow_jobs(rec)
    pick = set()
    if steps:
        pick = {j["id"] for j in js if (j.get("flow") or {}).get("step") in steps}
        if downstream:
            changed = True
            while changed:
                changed = False
                for j in js:
                    if j["id"] not in pick and set(j.get("depends") or []) & pick:
                        pick.add(j["id"])
                        changed = True
    n = 0
    for j in js:
        if (steps and j["id"] in pick) or (not steps and j["state"] in ("failed", "timeout", "cancelled", "skipped")):
            queue.update(j["id"], state="queued", attempt=j["attempt"] + 1, started=None, ended=None, rc=None,
                         reason="flow retried", retried=False, oom=False, reacted=False, force=bool(steps))
            n += 1
    rec.update(notified=False, halted=False)
    save_flow(rec)
    return n


def cancel_flow(fid):
    rec = load_flow(fid)
    return sum(1 for j in flow_jobs(rec) if queue.cancel(j["id"]))


# ---------------------------------------------------------------- shared cache between flows
def cache_dir():
    d = config.get("SHARED_CACHE_DIR") or (os.path.join(os.path.expanduser(config.get("BIG_FOLDER_DEST")), ".ms_cache")
                                          if config.get("BIG_FOLDER_DEST") else str(config.STATE / "cache"))
    return Path(os.path.expanduser(d))


def cache_restore(j):
    d = cache_dir() / queue.job_key(j)
    if not (d / ".complete").exists():
        return False
    for f in d.rglob("*"):
        if f.name == ".complete" or f.is_dir():
            continue
        dest = Path(j["cwd"]) / f.relative_to(d)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
    queue.mark_done(j)
    return True


def cache_store(j):
    if j.get("cache") != "shared" or not j.get("outputs"):
        return
    d = cache_dir() / queue.job_key(j)
    if (d / ".complete").exists():
        return
    try:
        for pat in j["outputs"]:
            for f in glob.glob(os.path.join(j["cwd"], pat), recursive=True):
                rel = os.path.relpath(f, j["cwd"])
                dest = d / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                if os.path.isdir(f):
                    shutil.copytree(f, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(f, dest)
        (d / ".complete").write_text(json.dumps({"job": j["id"], "t": time.time(), "name": j["name"]}))
    except OSError:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- inspection: summary, why, touch, untracked
def _as_job(s):
    return {"id": 0, "name": s["name"], "cmd": s["cmd"], "cwd": s["cwd"], "params": s["params"], "inputs": s["inputs"],
            "outputs": s["outputs"], "step_key": s["step_key"], "rerun_on": s.get("rerun_on"), "cache_mode": s.get("cache_mode")}


def summary(path, overrides=None, profile=None):
    """[(step, name, status, reasons)] — Snakemake --summary/--list-changes."""
    f, specs, errs = check(path, overrides, profile)
    if errs:
        raise FlowError("\n".join(errs))
    out = []
    for s in specs:
        j = _as_job(s)
        reasons = queue.why_rerun(j)
        out.append((s["step"], s["name"], "up to date" if not reasons else "would run", reasons))
    return out


def touch(path, steps=None, overrides=None, profile=None):
    f, specs, errs = check(path, overrides, profile)
    if errs:
        raise FlowError("\n".join(errs))
    n = 0
    for s in specs:
        if steps and s["step"] not in steps:
            continue
        j = _as_job(s)
        for pat in j["outputs"]:
            for fn in glob.glob(os.path.join(j["cwd"], pat)):
                os.utime(fn)
        queue.mark_done(j)
        n += 1
    return n


def untracked(path, overrides=None, profile=None):
    f, specs, errs = check(path, overrides, profile)
    if errs:
        raise FlowError("\n".join(errs))
    known = set()
    folders = set()
    for s in specs:
        folders.add(s["cwd"])
        for pat in s["inputs"] + s["outputs"] + s["temp"]:
            known |= {os.path.abspath(x) for x in glob.glob(os.path.join(s["cwd"], pat))}
    out = []
    for d in sorted(folders):
        for e in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            p = os.path.abspath(os.path.join(d, e))
            if e in ("msrun.jsonl", ".msq_done.json", "msrun.out") or p in known or any(k.startswith(p + "/") for k in known):
                continue
            if os.path.isdir(p) and any(os.path.abspath(x["cwd"]) == p for x in specs):
                continue
            out.append(p)
    return out


# ---------------------------------------------------------------- schedules
def cron_match(expr_, t=None):
    """Standard 5-field cron (minute hour day month weekday); supports *, */n, a-b, a-b/n, lists and @daily-style aliases."""
    aliases = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0",
               "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *"}
    expr_ = aliases.get(expr_.strip(), expr_)
    t = time.localtime(t or time.time())
    fields = expr_.split()
    if len(fields) != 5:
        raise FlowError(f"cron needs 5 fields: {expr_!r}")
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
            if hi == 7 and v == 0 and b == 7 and (7 - a) % step == 0:
                ok = True
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


def add_schedule(file, cron=None, idle=False, git=None, overrides=None, after_flow=None, catchup_h=24,
                 skip_if_successful=False, only_if_changed=False, debounce_s=0, paths=None, remote=False, profile=None):
    _, _, errs = check(file, overrides, profile)
    if errs:
        raise FlowError("\n".join(errs))
    if cron:
        cron_match(cron)
    if not (cron or idle or git or after_flow):
        raise FlowError("give --cron, --idle, --on-commit or --after-flow")
    sc = schedules()
    sid = max([s["id"] for s in sc] + [0]) + 1
    e = {"id": sid, "file": str(Path(file).expanduser().resolve()), "cron": cron, "idle": bool(idle), "git": None,
         "after_flow": after_flow, "overrides": overrides or {}, "profile": profile, "last": None, "last_check": time.time(),
         "catchup_h": catchup_h, "skip_if_successful": skip_if_successful, "only_if_changed": only_if_changed,
         "paused": False, "created": time.time()}
    if git:
        repo, _, branch = git.partition(":")
        repo = os.path.expanduser(repo)
        ref = f"origin/{branch or 'main'}" if remote else (branch or "HEAD")
        e["git"] = {"repo": repo, "ref": ref, "remote": remote, "head": _git_head(repo, ref, remote), "debounce_s": debounce_s,
                    "paths": paths or [], "pending": None}
    sc.append(e)
    queue._ensure()
    SCHED.write_text(json.dumps(sc, indent=1))
    return e


def remove_schedule(sid):
    sc = [s for s in schedules() if s["id"] != int(sid)]
    SCHED.write_text(json.dumps(sc, indent=1))


def pause_schedule(sid, paused=True):
    sc = schedules()
    for s in sc:
        if s["id"] == int(sid):
            s["paused"] = paused
    SCHED.write_text(json.dumps(sc, indent=1))


def _git_head(repo, ref, remote=False):
    try:
        if remote:
            subprocess.run(["git", "-C", repo, "fetch", "--quiet"], capture_output=True, timeout=120)
        return subprocess.run(["git", "-C", repo, "rev-parse", ref], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _changed_paths(repo, a, b):
    try:
        out = subprocess.run(["git", "-C", repo, "diff", "--name-only", a, b], capture_output=True, text=True, timeout=30).stdout
        return [x for x in out.splitlines() if x]
    except (OSError, subprocess.TimeoutExpired):
        return []


def _succeeded_today(e):
    today = time.strftime("%Y-%m-%d")
    return any(r.get("schedule") == e["id"] and r.get("status") == "ok" and
               time.strftime("%Y-%m-%d", time.localtime(r.get("finished", 0))) == today for r in flows())


def run_schedules(sched):
    sc = schedules()
    if not sc:
        return
    now = time.time()
    changed = False
    active = {r.get("schedule") for r in flows() if not r.get("notified")}
    finished_recent = [r for r in flows() if r.get("finished") and now - r["finished"] < 120 and r.get("status") == "ok"]
    for e in sc:
        if e.get("paused"):
            continue
        fire, why = False, ""
        if e.get("cron"):
            start = max(e.get("last_check") or now - 60, now - float(e.get("catchup_h", 24)) * 3600)
            t = (int(start) // 60 + 1) * 60
            while t <= now:
                if cron_match(e["cron"], t) and t > (e.get("last") or 0):
                    fire, why = True, f"cron {e['cron']}" + (" (missed while off)" if now - t > 120 else "")
                t += 60
            e["last_check"] = now
            changed = True
        if e.get("idle") and now - (e.get("last") or 0) > 20 * 3600 and sched.idle():
            fire, why = True, "machine idle"
        if e.get("after_flow"):
            for r in finished_recent:
                if r["name"] == e["after_flow"] and r["id"] not in (e.get("seen") or []):
                    e.setdefault("seen", []).append(r["id"])
                    e["seen"] = e["seen"][-50:]
                    fire, why = True, f"after flow {r['name']} succeeded"
                    changed = True
        g = e.get("git")
        if g:
            if now - (g.get("last_poll") or 0) >= (300 if g.get("remote") else 60):
                g["last_poll"] = now
                h = _git_head(g["repo"], g["ref"], g.get("remote"))
                changed = True
                if h and h != g.get("head") and h != (g.get("pending") or {}).get("head"):
                    g["pending"] = {"head": h, "since": now}
            p = g.get("pending")
            if p and now - p["since"] >= float(g.get("debounce_s") or 0):
                files = _changed_paths(g["repo"], g.get("head") or p["head"], p["head"]) if g.get("head") else []
                important = not g.get("paths") or any(any(re.fullmatch(_glob_re(pt), f) for pt in g["paths"]) for f in files)
                g["head"], g["pending"] = p["head"], None
                changed = True
                if important:
                    fire, why = True, f"new commit {p['head'][:8]}"
        if not fire:
            continue
        e["last"] = now
        changed = True
        if e["id"] in active:              # overlap policy: skip while the previous run is still going
            continue
        if e.get("skip_if_successful") and _succeeded_today(e):
            continue
        try:
            if e.get("only_if_changed"):
                rows = summary(e["file"], e.get("overrides"), e.get("profile"))
                if all(r[2] == "up to date" for r in rows):
                    continue
            rec, _ = submit_flow(e["file"], e.get("overrides"), resume=True, schedule_id=e["id"], profile=e.get("profile"))
            sched.notify(f"Flow started: {rec['name']}", f"scheduled ({why}) · {len(rec['jobs'])} job(s)", "min", "calendar")
        except Exception as ex:
            sched.notify("Scheduled flow could not start", f"{e['file']}: {ex}", "high", "warning")
    if changed:
        SCHED.write_text(json.dumps(sc, indent=1))


def _glob_re(pat):
    out = ""
    i = 0
    while i < len(pat):
        c = pat[i]
        if pat[i:i + 2] == "**":
            out += ".*"
            i += 2
            continue
        out += "[^/]*" if c == "*" else "." if c == "?" else re.escape(c)
        i += 1
    return out


# ---------------------------------------------------------------- views: DAG, timeline, report
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
        kind = " [foreach]" if ss[0].get("fanout") else " [flow]" if ss[0].get("subflow") else " [wait]" if ss[0].get("wait") else ""
        lines.append(f"{st}{kind}: {len(ss)} job(s){rel}")
    return "\n".join(lines)


def dag_svg(specs, states=None):
    """Layered SVG of the step graph; boxes show job counts and (optionally) states."""
    steps, deps = [], {}
    for s in specs:
        if s["step"] not in steps:
            steps.append(s["step"])
        deps.setdefault(s["step"], set()).update(s["depends_steps"])
    level = {}

    def lv(x):
        if x not in level:
            level[x] = 0 if not deps.get(x) else 1 + max(lv(d) for d in deps[x])
        return level[x]
    for st in steps:
        lv(st)
    cols = {}
    for st in steps:
        cols.setdefault(level[st], []).append(st)
    W, H, gx, gy = 170, 46, 60, 24
    pos = {}
    for L, sts in cols.items():
        for i, st in enumerate(sts):
            pos[st] = (20 + L * (W + gx), 20 + i * (H + gy))
    width = 40 + (max(cols) + 1) * (W + gx)
    height = 40 + max(len(v) for v in cols.values()) * (H + gy)
    out = [f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' font-family='sans-serif' font-size='12'>",
           "<defs><marker id='a' markerWidth='8' markerHeight='8' refX='8' refY='4' orient='auto'><path d='M0,0 L8,4 L0,8 z' fill='#888'/></marker></defs>"]
    for st in steps:
        for d in deps.get(st, []):
            x1, y1 = pos[d][0] + W, pos[d][1] + H / 2
            x2, y2 = pos[st][0], pos[st][1] + H / 2
            out.append(f"<path d='M{x1},{y1} C{x1 + 30},{y1} {x2 - 30},{y2} {x2},{y2}' stroke='#888' fill='none' marker-end='url(#a)'/>")
    for st in steps:
        x, y = pos[st]
        n = sum(1 for s in specs if s["step"] == st)
        cnt = (states or {}).get(st, {})
        bad = cnt.get("failed", 0) + cnt.get("timeout", 0)
        col = "#c01c28" if bad else "#26a269" if cnt and all(k in ("done", "cached", "warning") for k in cnt) else "#3584e4"
        sub = ", ".join(f"{v} {k}" for k, v in cnt.items()) if cnt else f"{n} job(s)"
        out.append(f"<rect x='{x}' y='{y}' width='{W}' height='{H}' rx='8' fill='white' stroke='{col}' stroke-width='2'/>")
        out.append(f"<text x='{x + 10}' y='{y + 19}' font-weight='bold'>{html.escape(st)}</text>")
        out.append(f"<text x='{x + 10}' y='{y + 36}' fill='#666'>{html.escape(sub[:28])}</text>")
    out.append("</svg>")
    return "".join(out)


def timeline_rows(job_list):
    """[(label, start, end, state, created)] for a Gantt chart (created → start = waiting in the queue)."""
    out = []
    for j in job_list:
        if j.get("started"):
            out.append((f"#{j['id']} {j['name']}", j["started"], j.get("ended") or time.time(), j["state"], j.get("created")))
    return sorted(out, key=lambda r: r[1])


def timeline_html(job_list, title="Timeline"):
    rows = timeline_rows(job_list)
    if not rows:
        return "<p>No started jobs.</p>"
    t0 = min(min(r[1], r[4] or r[1]) for r in rows)
    t1 = max(r[2] for r in rows)
    span = max(1.0, t1 - t0)
    col = {"done": "#26a269", "warning": "#c88800", "cached": "#8f8f8f", "running": "#3584e4", "suspended": "#c88800",
           "failed": "#c01c28", "timeout": "#c64600", "cancelled": "#8f8f8f"}
    bars = []
    for lab, a, b, st, cr in rows:
        x = (a - t0) / span * 100
        w = max(0.4, (b - a) / span * 100)
        wait = ""
        if cr and cr < a:
            wx = (cr - t0) / span * 100
            wait = f"<div class='b' style='left:{wx:.2f}%;width:{max(0.2, x - wx):.2f}%;background:#ddd' title='waiting {queue_hms(a - cr)}'></div>"
        bars.append(f"<div class='r'><div class='l'>{html.escape(lab)}</div><div class='t'>{wait}<div class='b' style='left:{x:.2f}%;"
                    f"width:{w:.2f}%;background:{col.get(st, '#999')}' title='{st} · {queue_hms(b - a)}'></div></div></div>")
    return (f"<div class='tl'><style>.r{{display:flex;align-items:center;height:20px}}.l{{width:280px;overflow:hidden;white-space:nowrap}}"
            ".t{flex:1;position:relative;height:14px;background:#f4f4f4}.b{position:absolute;top:0;height:14px;border-radius:3px}</style>"
            f"<p>{time.strftime('%F %H:%M', time.localtime(t0))} → {time.strftime('%F %H:%M', time.localtime(t1))}"
            f" · {queue_hms(span)} · grey = waiting in the queue</p>" + "".join(bars) + "</div>")


def report_html(fid):
    """Self-contained HTML report of one flow run (Snakemake --report / Nextflow -with-report)."""
    from . import runlog
    import statistics as stt
    rec = load_flow(fid)
    js = flow_jobs(rec)
    cnt, done, ok = flow_state(rec)
    try:
        _, specs, _ = check(rec["file"], rec.get("overrides"), rec.get("profile"))
    except Exception:
        specs = []
    per_step = {}
    for j in js:
        per_step.setdefault((j.get("flow") or {}).get("step", "?"), []).append(j)
    states = {st: {} for st in per_step}
    for st, jl in per_step.items():
        for j in jl:
            states[st][j["state"]] = states[st].get(j["state"], 0) + 1
    rows = []
    for st, jl in per_step.items():
        ts = [queue.runtime(j) / 60 for j in jl if j.get("started") and j.get("ended")]
        pk = [(j.get("peak_rss") or 0) / queue.GiB for j in jl if j.get("peak_rss")]
        eff = [queue.efficiency(j)["mem_eff"] for j in jl if queue.efficiency(j)["mem_eff"]]
        rows.append(f"<tr><td>{html.escape(st)}</td><td>{len(jl)}</td><td>{', '.join(f'{v} {k}' for k, v in states[st].items())}</td>"
                    f"<td>{min(ts):.1f} / {stt.median(ts):.1f} / {max(ts):.1f}</td>" if ts else
                    f"<tr><td>{html.escape(st)}</td><td>{len(jl)}</td><td>{', '.join(f'{v} {k}' for k, v in states[st].items())}</td><td>–</td>")
        rows[-1] += (f"<td>{stt.median(pk):.1f} / {max(pk):.1f}</td>" if pk else "<td>–</td>") + \
                    (f"<td>{stt.median(eff):.0f}%</td>" if eff else "<td>–</td>") + "</tr>"
    metric_names, mrows = [], []
    for j in js:
        r = runlog.read(os.path.join(j["cwd"], runlog.FILENAME)).get(j.get("run_id")) if j.get("run_id") else None
        m = {k: v for k, v in ((r or {}).get("metrics") or {}).items() if k not in ("runtime_s", "cpu_s", "queue_wait_s")}
        for k in m:
            if k not in metric_names:
                metric_names.append(k)
        mrows.append((j, m))
    metric_names = metric_names[:12]
    mtab = "".join(f"<tr><td>#{j['id']}</td><td>{html.escape(j['name'])}</td><td>{j['state']}</td>" +
                   "".join(f"<td>{html.escape(str(m.get(k, '')))}</td>" for k in metric_names) + "</tr>" for j, m in mrows)
    iss = {"critical": 0, "warning": 0, "error": 0}
    for j in js:
        for k, v in (j.get("issues") or {}).items():
            iss[k] = iss.get(k, 0) + v
    fails = "".join(f"<li>#{j['id']} {html.escape(j['name'])}: {html.escape(j.get('reason', ''))}</li>"
                    for j in js if j["state"] not in queue.SUCCESS)
    css = ("body{font:14px system-ui,sans-serif;margin:24px;color:#1d1d1f}table{border-collapse:collapse;margin:8px 0}"
           "td,th{border-bottom:1px solid #ddd;padding:4px 10px;text-align:left}h2{margin-top:28px}.ok{color:#26a269}.bad{color:#c01c28}")
    t0 = min((j.get("started") or j["created"]) for j in js) if js else rec["created"]
    t1 = max((j.get("ended") or time.time()) for j in js) if js else time.time()
    return (f"<!doctype html><meta charset='utf-8'><title>Flow {html.escape(rec['name'])}</title><style>{css}</style>"
            f"<h1>Flow {html.escape(rec['name'])}</h1><p>{html.escape(rec['id'])} · {html.escape(rec['file'])}<br>"
            f"<b class='{'ok' if ok else 'bad'}'>{'OK' if ok else 'running' if not done else 'FAILED'}</b> · "
            f"{', '.join(f'{v} {k}' for k, v in cnt.items())} · {queue_hms(t1 - t0)} · "
            f"issues: {iss['critical']} critical, {iss['warning']} warnings</p>"
            + (f"<h2>Problems</h2><ul>{fails}</ul>" if fails else "")
            + "<h2>Steps</h2>" + (dag_svg(specs, states) if specs else "")
            + "<table><tr><th>Step</th><th>Jobs</th><th>States</th><th>Minutes min/median/max</th><th>Peak GB median/max</th>"
              "<th>Memory use vs reserved</th></tr>" + "".join(rows) + "</table>"
            + "<h2>Results</h2><table><tr><th>Job</th><th>Name</th><th>State</th>" + "".join(f"<th>{html.escape(k)}</th>" for k in metric_names)
            + "</tr>" + mtab + "</table><h2>Timeline</h2>" + timeline_html(js))
