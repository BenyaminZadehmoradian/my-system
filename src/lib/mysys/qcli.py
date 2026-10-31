"""Command line for the queue (`ms q …`) and flows (`ms flow …`)."""
import json
import sys
import time

from . import flow, queue

Q_HELP = """ms q — job queue that starts jobs only when CPU and RAM are free (format of run logs: ms log help)

  ms q add [options] -- CMD ...      queue a command (run in the current folder)
      --name N  --mem GB  --cores N  --priority P  --after ID,ID  --after-any ID,ID
      --retries N  --retry-on oom,timeout,fail,any  --timeout 2h|auto  --tool vivado
      --no-limit (no hard memory limit)  --gpus N  --container IMAGE  --host SSH_HOST
      --when-idle  --preempt  --nice N  --require "mounted /mnt/data"  --env K=V
  ms q [list] [--all]                jobs (finished ones only with --all)
  ms q show ID | log ID              details | output of a job
  ms q cancel|hold|release|suspend|resume|retry ID ...
  ms q prio ID P                     change priority (higher runs first)
  ms q report [FLOW_ID]              efficiency: memory and CPU reserved vs. used
  ms q timeline [FLOW_ID] [--html FILE]
  ms q clear [DAYS]                  forget finished jobs
  ms q status                        is the scheduler running?
"""

FLOW_HELP = """ms flow — steps, parameter sweeps and dependencies described in one YAML file

  ms flow check FILE [--set k=v ...]             validate and show the plan
  ms flow run FILE [--dry-run] [--resume] [--set k=v ...]
  ms flow list | show FLOW_ID | retry FLOW_ID | cancel FLOW_ID
  ms flow schedule FILE --cron "17 2 * * *" | --idle | --on-commit REPO[:BRANCH]
  ms flow schedules | unschedule ID
  ms flow example                                print an example flow file
"""

EXAMPLE = '''name: s18_doe
cwd: /mnt/data/benyamin/efpga/S18            # every folder below is relative to this
defaults:
  tool: vivado                               # memory/CPU per job come from measured runs
  retries: 1
  retry_on: [oom]                            # an out-of-memory run is retried with 1.5x memory
  timeout: auto                              # 3x the usual duration of this tool
require: ["mounted /mnt/data", "free_gb /mnt/data 50"]
params: {part: xc7z020clg400-1}
steps:
  - name: synth
    matrix: {seed: [1, 2, 3], strategy: [Performance_Explore, Default]}
    folder: "doe_{{seed}}_{{strategy}}"
    cmd: "vivado -mode batch -nojournal -source ../synth.tcl -tclargs {{part}} {{seed}} {{strategy}}"
    inputs: ["../rtl/*.v", "../synth.tcl"]   # rerun only when these change
    outputs: ["post_synth.dcp"]
  - name: impl
    matrix: synth                            # same items: each impl waits for its own synth
    depends: [synth]
    folder: "doe_{{seed}}_{{strategy}}"
    cmd: "vivado -mode batch -nojournal -source ../impl.tcl"
    temp: [".Xil", "*.jou"]                  # removed after the whole flow succeeded
on_success: "python3 collect_results.py"
'''


def _ids(a):
    out = []
    for x in a:
        out += [int(y) for y in str(x).split(",") if y.strip()]
    return out


def _fmt_t(t):
    return time.strftime("%m-%d %H:%M", time.localtime(t)) if t else "–"


def _row(j):
    e = queue.efficiency(j)
    fl = j.get("flow") or {}
    dur = queue.runtime(j)
    return (f"{j['id']:>5} {j['state']:<9} {int(j.get('priority') or 0):>3} {j['name'][:26]:<26} "
            f"{(fl.get('name', '') + ':' + fl.get('step', ''))[:20] if fl else '':<20} "
            f"{e['mem_req_gb']:>5.1f}G {e['cores_req']:>4.1f}c {flow.queue_hms(dur) if dur else '':>7}  {j.get('reason', '')[:60]}")


def q_cmd(a):
    if not a or a[0] in ("list", "ls") or a[0].startswith("--all"):
        show_all = "--all" in a
        js = [j for j in queue.all_jobs() if show_all or j["state"] not in queue.TERMINAL
              or time.time() - (j.get("ended") or 0) < 3600]
        st, _ = queue.daemon_state()
        print(f"scheduler: {st}   ({len(js)} job(s) shown; --all for history)")
        print(f"{'id':>5} {'state':<9} {'pri':>3} {'name':<26} {'flow:step':<20} {'mem':>6} {'cpu':>5} {'time':>7}  note")
        for j in js:
            print(_row(j))
        return 0
    sub, a = a[0], a[1:]
    if sub in ("-h", "--help", "help"):
        print(Q_HELP)
        return 0
    if sub == "add":
        opt, i = {}, 0
        env = {}
        req = []
        while i < len(a) and a[i] != "--":
            k = a[i]
            v = a[i + 1] if i + 1 < len(a) else None
            simple = {"--name": "name", "--mem": "mem_gb", "--cores": "cores", "--priority": "priority",
                      "--retries": "retries", "--timeout": "timeout_s", "--tool": "tool", "--gpus": "gpus",
                      "--container": "container", "--host": "host", "--nice": "nice"}
            if k in simple:
                opt[simple[k]] = v
                i += 2
            elif k in ("--after", "--after-any"):
                opt["depends"] = _ids([v])
                opt["dep_mode"] = "any" if k == "--after-any" else "ok"
                i += 2
            elif k == "--retry-on":
                opt["retry_on"] = v.split(",")
                i += 2
            elif k == "--require":
                req.append(v)
                i += 2
            elif k == "--env":
                kk, _, vv = v.partition("=")
                env[kk] = vv
                i += 2
            elif k in ("--no-limit", "--when-idle", "--preempt"):
                opt[{"--no-limit": "limit_memory", "--when-idle": "when_idle", "--preempt": "preempt"}[k]] = k != "--no-limit"
                i += 1
            else:
                print(f"unknown option {k}\n\n{Q_HELP}")
                return 2
        cmd = a[i + 1:] if i < len(a) else []
        if not cmd:
            print("usage: ms q add [options] -- CMD ...")
            return 2
        for k in ("mem_gb", "cores"):
            if k in opt:
                opt[k] = float(opt[k])
        for k in ("priority", "retries", "gpus", "nice"):
            if k in opt:
                opt[k] = int(opt[k])
        if req:
            opt["require"] = req
        if env:
            opt["env"] = env
        import shlex
        j = queue.submit(" ".join(shlex.quote(c) for c in cmd) if len(cmd) > 1 else cmd[0], **opt)
        st, _ = queue.daemon_state()
        print(f"{j['id']}")
        if st != "running":
            print("note: the scheduler is not running (systemctl --user start my-system-queue)", file=sys.stderr)
        return 0
    if sub == "show":
        for jid in _ids(a):
            j = queue.load(jid)
            if not j:
                print(f"no job {jid}")
                continue
            e = queue.efficiency(j)
            for k in ("id", "name", "state", "reason", "cmd", "cwd", "tool", "priority", "attempt", "retries", "depends",
                      "timeout_s", "run_id"):
                print(f"{k:>10}: {j.get(k)}")
            print(f"{'times':>10}: created {_fmt_t(j['created'])} · started {_fmt_t(j.get('started'))} · ended {_fmt_t(j.get('ended'))}")
            print(f"{'memory':>10}: reserved {e['mem_req_gb']} GB · peak {e['mem_peak_gb']} GB" + (f" ({e['mem_eff']}%)" if e['mem_eff'] is not None else ""))
            print(f"{'cpu':>10}: reserved {e['cores_req']} · used {e['cores_used']}" + (f" ({e['cpu_eff']}%)" if e['cpu_eff'] is not None else ""))
            print(f"{'log':>10}: {queue.LOGS / (str(jid) + '.log')}")
        return 0
    if sub == "log":
        jid = _ids(a)[0]
        try:
            sys.stdout.write((queue.LOGS / f"{jid}.log").read_text(errors="replace")[-20000:])
        except OSError:
            print("(no output yet)")
        return 0
    actions = {"cancel": queue.cancel, "hold": queue.hold, "release": queue.release, "suspend": queue.suspend,
               "resume": queue.resume, "retry": queue.retry}
    if sub in actions:
        bad = [i for i in _ids(a) if not actions[sub](i)]
        if bad:
            print(f"not possible for: {', '.join(map(str, bad))} (check their state with ms q)")
            return 1
        return 0
    if sub == "prio":
        return 0 if queue.set_priority(int(a[0]), int(a[1])) else 1
    if sub == "clear":
        print(f"{queue.clear(float(a[0]) if a else 0)} finished job(s) removed")
        return 0
    if sub == "status":
        st, d = queue.daemon_state()
        print(f"scheduler: {st}" + (f" (last error: {d['error']})" if d and d.get("error") else ""))
        return 0 if st == "running" else 1
    if sub == "report":
        js = _select(a)
        print(f"{'id':>5} {'name':<28} {'state':<9} {'time':>7} {'mem res':>7} {'peak':>6} {'mem%':>5} {'cpu res':>7} {'used':>5} {'cpu%':>5}")
        tot_req = tot_peak = 0
        for j in js:
            e = queue.efficiency(j)
            if not j.get("started"):
                continue
            tot_req += e["mem_req_gb"]
            tot_peak += e["mem_peak_gb"]
            print(f"{j['id']:>5} {j['name'][:28]:<28} {j['state']:<9} {flow.queue_hms(e['seconds']):>7} {e['mem_req_gb']:>6.1f}G "
                  f"{e['mem_peak_gb']:>5.1f}G {str(e['mem_eff'] or '–'):>5} {e['cores_req']:>7.1f} {str(e['cores_used'] or '–'):>5} {str(e['cpu_eff'] or '–'):>5}")
        if tot_req:
            print(f"\nmemory efficiency overall: {tot_peak / tot_req * 100:.0f}%  "
                  "(well below 60% → lower mem_gb and run more jobs in parallel; near 100% → raise it)")
        return 0
    if sub == "timeline":
        html = None
        if "--html" in a:
            i = a.index("--html")
            html = a[i + 1]
            a = a[:i] + a[i + 2:]
        js = _select(a)
        if html:
            open(html, "w").write(flow.timeline_html(js, "Timeline"))
            print(f"written {html}")
            return 0
        rows = flow.timeline_rows(js)
        if not rows:
            print("no started jobs")
            return 0
        t0, t1 = min(r[1] for r in rows), max(r[2] for r in rows)
        width = 60
        for lab, s, e, st in rows:
            x = int((s - t0) / max(1, t1 - t0) * width)
            w = max(1, int((e - s) / max(1, t1 - t0) * width))
            ch = {"done": "█", "running": "▶", "failed": "✗", "timeout": "⌛", "suspended": "‖"}.get(st, "▒")
            print(f"{lab[:30]:<30} |{' ' * x}{ch * w}{' ' * (width - x - w)}| {st}")
        print(f"{'':<30}  {_fmt_t(t0)}{' ' * (width - 20)}{_fmt_t(t1)}")
        return 0
    if sub == "daemon":
        queue.Scheduler().loop()
        return 0
    if sub == "_exec":
        return queue.execute(int(a[0]))
    print(Q_HELP)
    return 2


def _select(a):
    if a:
        try:
            rec = json.loads((flow.FLOWS / f"{a[0]}.json").read_text())
            return flow.flow_jobs(rec)
        except (OSError, ValueError):
            pass
    return [j for j in queue.all_jobs() if time.time() - (j.get("started") or j["created"]) < 7 * 86400]


def _sets(a):
    ov, rest = {}, []
    i = 0
    while i < len(a):
        if a[i] == "--set" and i + 1 < len(a):
            k, _, v = a[i + 1].partition("=")
            try:
                import yaml
                v = yaml.safe_load(v)
            except Exception:
                pass
            ov[k] = v
            i += 2
        else:
            rest.append(a[i])
            i += 1
    return ov, rest


def flow_cmd(a):
    if not a or a[0] in ("-h", "--help", "help"):
        print(FLOW_HELP)
        return 0
    sub, a = a[0], a[1:]
    if sub == "example":
        print(EXAMPLE)
        return 0
    if sub in ("check", "run"):
        ov, rest = _sets(a)
        if not rest:
            print(f"usage: ms flow {sub} FILE")
            return 2
        f, specs, errs = flow.check(rest[0], ov)
        if errs:
            print("Problems:")
            for e in errs:
                print(f"  - {e}")
            return 1
        dry = sub == "check" or "--dry-run" in rest
        if dry:
            plan, total, peak = flow.estimate(specs)
            print(f"Flow '{f['name']}': {len(specs)} job(s)\n{flow.dag_text(specs)}\n")
            for s, mins, start, measured in plan:
                print(f"  {s['step']:<10} {s['name'][:34]:<34} ~{mins:>4.0f} min{'' if measured else '?'}  starts ~{start:>5.0f} min  {s['cwd']}")
            print(f"\nEstimated on an idle machine: about {flow.queue_hms(total * 60)} with up to {peak} jobs in parallel"
                  " ('?' = no measured runs of that tool yet, 30 min assumed).")
            return 0
        rec, specs = flow.submit_flow(rest[0], ov, resume="--resume" in rest)
        cached = sum(1 for j in flow.flow_jobs(rec) if j["state"] == "cached")
        print(f"flow {rec['id']}: {len(rec['jobs'])} job(s) queued" + (f", {cached} already done (resume)" if cached else ""))
        if queue.daemon_state()[0] != "running":
            print("note: the scheduler is not running (systemctl --user start my-system-queue)", file=sys.stderr)
        return 0
    if sub == "list":
        for r in flow.flows()[-40:]:
            cnt, done, ok = flow.flow_state(r)
            st = "ok" if ok else "failed" if done else "running"
            print(f"{r['id']:<40} {st:<8} " + ", ".join(f"{v} {k}" for k, v in sorted(cnt.items())))
        return 0
    if sub == "show":
        rec = json.loads((flow.FLOWS / f"{a[0]}.json").read_text())
        print(f"{rec['id']}  {rec['file']}")
        for j in flow.flow_jobs(rec):
            print(_row(j))
        return 0
    if sub == "retry":
        print(f"{flow.retry_flow(a[0])} job(s) queued again")
        return 0
    if sub == "cancel":
        print(f"{flow.cancel_flow(a[0])} job(s) cancelled")
        return 0
    if sub == "schedule":
        ov, rest = _sets(a)
        cron = idle = git = None
        if "--cron" in rest:
            cron = rest[rest.index("--cron") + 1]
        if "--idle" in rest:
            idle = True
        if "--on-commit" in rest:
            git = rest[rest.index("--on-commit") + 1]
        e = flow.add_schedule(rest[0], cron=cron, idle=idle, git=git, overrides=ov)
        print(f"schedule {e['id']} added")
        return 0
    if sub == "schedules":
        for e in flow.schedules():
            how = e.get("cron") or ("when idle (once a day)" if e.get("idle") else "") or f"on commit to {e['git']['repo']}"
            print(f"{e['id']:>3}  {how:<28} {e['file']}  last: {_fmt_t(e.get('last'))}")
        return 0
    if sub == "unschedule":
        flow.remove_schedule(a[0])
        return 0
    print(FLOW_HELP)
    return 2
