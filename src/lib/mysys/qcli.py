"""Command line for the queue (`ms q …`), flows (`ms flow …`), semaphores (`ms sem`), lineage and secrets."""
import json
import os
import shlex
import subprocess
import sys
import time

from . import flow, queue

Q_HELP = """ms q — job queue that starts jobs only when CPU, RAM, licenses and other resources are free

  ms q add [options] -- CMD ...      queue a command (run in the current folder); prints the job id
    resources   --mem GB  --cores N  --res NAME=N (license seats, boards; config/resources.conf)
                --disk-gb N [--disk-path P]  --gpus N  --tool vivado
    order       --priority P  --after ID,..  --after-any ID,..  --after-fail ID,..  --top
                --begin 16:00|+2h  --deadline "2026-11-02 08:00"  --when-idle  --preempt  --collapse KEY
    failures    --retries N  --retry-on oom,timeout,fail,any  --retry-codes 1,137  --retry-delay 30s
                --retry-backoff 2  --timeout 2h|auto|300%  --idle-timeout 20m  --signal-before USR1@300
                --kill-seq TERM,30,KILL  --exit-code 1=warning  --warn-pattern REGEX
    files       --follow vivado.log  --scratch [ram|PATH]  --outputs GLOB  --inputs GLOB
    other       --name N  --nice N  --no-limit  --container IMG  --host SSH  --env K=V  --require "mounted /mnt/data"
                --approval "text" (wait for ms q approve)  --notify always|failure|never  --secret NAME  --wait
                --group G (limit per group in config/groups.conf)  --label L  --stdin (accept ms q send)
  ms q [list] [--all] [--state S] [--name RE] [--label L] [--group G] [--json]   jobs
  ms q show ID | log ID | stat ID    details | output | memory/CPU over time
  ms q cancel|hold|release|suspend|resume|retry|requeue|top|approve|reject ID ...
  ms q clone ID                      run a job again with the same settings
  ms q wait ID                       block until the job ends; exit code 0 = success
  ms q prio ID P                     change priority (higher runs first)
  ms q why ID                        why a job would run again (what changed)
  ms q explain ID                    why a waiting job has not started: every condition, pass/fail
  ms q edit ID [--cmd C] [--mem GB] [--cores N] [--priority P] [--name N] [--timeout T] [--group G] [--env K=V]
  ms q send ID TEXT                  type a line into a running job started with --stdin
  ms q groups                        groups, their limits and how many jobs run in each
  ms q flaky                         steps that failed and then passed on retry without changes
  ms q cancel|hold|release --group G every job of a group
  ms q warnings ID                   new / fixed / outstanding warnings vs. the previous run
  ms q history [--days 7] [--state failed] [--name REGEX]
  ms q eta                           estimated start time of waiting jobs
  ms q report [FLOW_ID]              efficiency: memory and CPU reserved vs. used, I/O
  ms q timeline [FLOW_ID] [--html FILE]
  ms q clear [DAYS]                  forget finished jobs
  ms q status                        is the scheduler running?
"""

FLOW_HELP = """ms flow — steps, parameter sweeps and dependencies described in one YAML file

  ms flow example | reference                    example file | every key explained
  ms flow check FILE [--set k=v] [--profile P]   validate and show the plan with a time estimate
  ms flow lint FILE                              advice (missing timeouts, outputs, licenses …)
  ms flow run FILE [--dry-run] [--resume] [--set k=v ...] [--profile P] [--sample 5%|N]
               [--until STEP] [--from STEP] [--only STEP] [--force STEP|all] [--stub]
  ms flow summary FILE                           every job: up to date or why it would run
  ms flow touch FILE [STEP]                      mark outputs as up to date without running
  ms flow untracked FILE                         files in the run folders no step declares
  ms flow list | show FLOW_ID | report FLOW_ID [--html FILE]
  ms flow retry FLOW_ID [--step S] [--downstream] | cancel FLOW_ID
  ms flow schedule FILE  --cron "17 2 * * *" | --idle | --on-commit REPO[:BRANCH] | --after-flow NAME
        [--remote] [--debounce 10m] [--paths 'rtl/**'] [--skip-if-successful] [--only-if-changed]
        [--catchup 24] [--profile P] [--set k=v]
  ms flow schedules | unschedule ID | pause ID | unpause ID
"""

EXAMPLE = '''name: s18_doe
cwd: /mnt/data/benyamin/efpga/S18            # every folder below is relative to this
defaults:
  tool: vivado                               # memory/CPU per job come from measured runs
  resources: {vivado_license: 1}             # define seats in config/resources.conf
  retries: 1
  retry_on: [oom]                            # an out-of-memory run is retried with more memory
  timeout: auto                              # 3x the usual duration of this tool
  idle_timeout: 30m                          # stop a run that prints nothing for 30 min
  follow: [vivado.log]                       # show Vivado's own log live in the job output
  quality_gate: {new_critical: 0}            # warn when new CRITICAL WARNINGs appear
require: ["mounted /mnt/data", "free_gb /mnt/data 50"]
params: {part: xc7z020clg400-1}
halt: {fail_pct: 30, mode: soon}             # stop starting new runs when 30% have failed
notify_mode: problem                         # notify on the first failure and when it is fixed
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
    outputs: ["routed.dcp"]
    temp: [".Xil", "*.jou"]                  # removed as soon as nothing needs them any more
on_success: "python3 collect_results.py"
'''

REFERENCE = """FLOW FILE REFERENCE (my-system)

top level   name cwd params params_schema{p:{type,enum,min,max,pattern,required,default}} profiles{name:{...}}
            defaults{step keys} env dotenv secrets[NAME] require[...] steps[...] halt{fail|fail_pct, mode now|soon}
            notify notify_mode(always|failing|problem|change) publish{dir,mode copy|symlink|move,pattern}
            on_success on_failure on_fixed on_regression on_change on_cancel on_exit
step        name cmd depends[steps] dep_mode(ok|any|notok) folder
  sweeps    matrix{k:[...]} | [{...}] | <earlier step>   matrix_mode(product|zip) matrix_exclude[{...}]
            matrix_filter "expr" matrix_csv file.csv  foreach "{{steps.X.outputs.list}}" (runtime fan-out, {{item}})
  logic     when "expr" (skip if false)  assert "expr" (fail if false)  approval "text"  flow: other.yaml with{...}
            wait{file,until HH:MM,duration,cmd,timeout,interval}  repeat{until_exit|until cmd, interval, limit}
  resources mem_gb cores gpus resources{name:n} disk_gb disk_path max_parallel tool nice limit_memory
            when_idle preempt priority begin deadline collapse_key respect_reservations group labels[..] stdin
  race      stop_when "expr on outputs/metrics" (e.g. "wns >= 0") stop_after N — once N variants of the step reach
            the goal, the others are stopped and do not count as failures
  failure   retries retry_on[oom,timeout,fail,any] retry_exit_codes[..] retry_delay retry_backoff retry_max_delay
            retry_delays[30s,2m,10m] retry_scale{mem,time} timeout(2h|auto|300%) idle_timeout signal_before(USR1@300)
            kill_seq(TERM,30,KILL) exit_codes{1: warning} warn_patterns[...] on_error(terminate|finish|ignore|retry)
  files     inputs outputs temp clean_on(success|failure|always) scratch(true|ram|PATH) protect ensure{non_empty,sha256}
            keep_incomplete require_outputs publish follow[...] junit[...] cache(shared) cache_mode(standard|lenient|deep)
            rerun_on[cmd,params,inputs,software]
  checks    quality_gate{new_critical:0,total_critical:N,new_warning:N,level:warning|failed} require[...] stub
  other     env secrets[NAME] notify(always|failure|never) benchmark N container host
placeholders {{param}} {{matrix_key}} {{flow}} {{step}} {{index}} {{item}} {{steps.S.outputs.K}} {{steps.S.metrics.K}}
outputs     a job writes 'key=value' lines to $MSQ_OUTPUTS; progress: echo 40 >&3
"""


def _ids(a):
    out = []
    for x in a:
        out += [int(y) for y in str(x).split(",") if y.strip()]
    return out


def filter_jobs(js, a):
    """--state S[,S2] --name REGEX --label L --group G (pueue/squeue style filters)."""
    import re as _re

    def arg(k):
        return a[a.index(k) + 1] if k in a and a.index(k) + 1 < len(a) else None
    st, nm, lb, gr = arg("--state"), arg("--name"), arg("--label"), arg("--group")
    if st:
        js = [j for j in js if j["state"] in st.split(",")]
    if nm:
        js = [j for j in js if _re.search(nm, j.get("name", ""))]
    if lb:
        js = [j for j in js if lb in (j.get("labels") or [])]
    if gr:
        js = [j for j in js if j.get("group") == gr]
    return js


def send_input(jid, text):
    j = queue.load(jid)
    if not j or j["state"] != "running":
        print(f"job {jid} is not running")
        return 1
    if not j.get("stdin"):
        print(f"job {jid} was not started with --stdin")
        return 1
    fifo = queue.LOGS / f"{jid}.in"
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        print("the job is not reading input yet — try again in a few seconds")
        return 1
    with os.fdopen(fd, "w") as f:
        f.write(text + "\n")
    return 0


def _fmt_t(t):
    return time.strftime("%m-%d %H:%M", time.localtime(t)) if t else "–"


def _row(j):
    e = queue.efficiency(j)
    fl = j.get("flow") or {}
    dur = queue.runtime(j)
    note = j.get("reason", "") or (f"{j['progress']:.0f}% " if j.get("progress") is not None and j["state"] == "running" else "") + \
        (j.get("last_line") or "" if j["state"] == "running" else "")
    return (f"{j['id']:>5} {j['state']:<9} {int(j.get('priority') or 0):>3} {j['name'][:26]:<26} "
            f"{(fl.get('name', '') + ':' + fl.get('step', ''))[:20] if fl else '':<20} "
            f"{e['mem_req_gb']:>5.1f}G {e['cores_req']:>4.1f}c {flow.queue_hms(dur) if dur else '':>7}  {note[:70]}")


ADD_SIMPLE = {"--name": "name", "--mem": "mem_gb", "--cores": "cores", "--priority": "priority", "--retries": "retries",
              "--timeout": "timeout_s", "--tool": "tool", "--gpus": "gpus", "--container": "container", "--host": "host",
              "--nice": "nice", "--idle-timeout": "idle_timeout_s", "--signal-before": "signal_before",
              "--kill-seq": "kill_seq", "--retry-delay": "retry_delay", "--retry-backoff": "retry_backoff",
              "--begin": "begin", "--deadline": "deadline", "--collapse": "collapse_key", "--disk-gb": "disk_gb",
              "--disk-path": "disk_path", "--approval": "approval", "--notify": "notify", "--benchmark": "benchmark",
              "--group": "group"}
ADD_LIST = {"--require": "require", "--follow": "follow", "--outputs": "outputs", "--inputs": "inputs",
            "--warn-pattern": "warn_patterns", "--secret": "secrets", "--junit": "junit", "--label": "labels"}
ADD_FLAG = {"--no-limit": ("limit_memory", False), "--when-idle": ("when_idle", True), "--preempt": ("preempt", True),
            "--protect": ("protect", True), "--stdin": ("stdin", True)}


def parse_add(a):
    opt, env, i = {}, {}, 0
    wait = top = False
    while i < len(a) and a[i] != "--":
        k = a[i]
        v = a[i + 1] if i + 1 < len(a) else None
        if k in ADD_SIMPLE:
            opt[ADD_SIMPLE[k]] = v
            i += 2
        elif k in ADD_LIST:
            opt.setdefault(ADD_LIST[k], []).append(v)
            i += 2
        elif k in ADD_FLAG:
            opt[ADD_FLAG[k][0]] = ADD_FLAG[k][1]
            i += 1
        elif k in ("--after", "--after-any", "--after-fail"):
            opt["depends"] = _ids([v])
            opt["dep_mode"] = {"--after": "ok", "--after-any": "any", "--after-fail": "notok"}[k]
            i += 2
        elif k == "--retry-on":
            opt["retry_on"] = v.split(",")
            i += 2
        elif k == "--retry-codes":
            opt["retry_exit_codes"] = [int(x) for x in v.split(",")]
            i += 2
        elif k == "--res":
            n, _, c = v.partition("=")
            opt.setdefault("resources", {})[n] = int(c or 1)
            i += 2
        elif k == "--exit-code":
            c, _, what = v.partition("=")
            opt.setdefault("exit_codes", {})[c] = what or "warning"
            i += 2
        elif k == "--env":
            kk, _, vv = v.partition("=")
            env[kk] = vv
            i += 2
        elif k == "--scratch":
            if v and not v.startswith("--"):
                opt["scratch"] = v
                i += 2
            else:
                opt["scratch"] = True
                i += 1
        elif k == "--wait":
            wait = True
            i += 1
        elif k == "--top":
            top = True
            i += 1
        else:
            raise ValueError(f"unknown option {k}")
    cmd = a[i + 1:] if i < len(a) else []
    for k in ("mem_gb", "cores", "disk_gb", "retry_backoff"):
        if k in opt:
            opt[k] = float(opt[k])
    for k in ("priority", "retries", "gpus", "nice", "benchmark"):
        if k in opt:
            opt[k] = int(opt[k])
    if env:
        opt["env"] = env
    return opt, cmd, wait, top


def q_cmd(a):
    if not a or a[0] in ("list", "ls") or a[0].startswith("--"):
        show_all = "--all" in a
        js = [j for j in queue.all_jobs() if show_all or j["state"] not in queue.TERMINAL
              or time.time() - (j.get("ended") or 0) < 3600]
        js = filter_jobs(js, a)
        if "--json" in a:
            print(json.dumps(js, indent=1, default=str))
            return 0
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
        try:
            opt, cmd, wait, top = parse_add(a)
        except ValueError as e:
            print(f"{e}\n\n{Q_HELP}")
            return 2
        if not cmd:
            print("usage: ms q add [options] -- CMD ...")
            return 2
        j = queue.submit(" ".join(shlex.quote(c) for c in cmd) if len(cmd) > 1 else cmd[0], **opt)
        if top:
            queue.top(j["id"])
        print(f"{j['id']}")
        if j.get("clamped"):
            print(f"note: memory request lowered to {j['mem_gb']} GB (what this machine can give)", file=sys.stderr)
        if queue.daemon_state()[0] != "running":
            print("note: the scheduler is not running (systemctl --user start my-system-queue)", file=sys.stderr)
        if wait:
            j = queue.wait(j["id"])
            return 0 if j and j["state"] in queue.SUCCESS else 1
        return 0
    if sub == "show":
        for jid in _ids(a):
            j = queue.load(jid)
            if not j:
                print(f"no job {jid}")
                continue
            e = queue.efficiency(j)
            for k in ("id", "name", "state", "reason", "cmd", "cwd", "tool", "priority", "attempt", "retries", "depends",
                      "timeout_s", "resources", "run_id", "outputs", "last_line", "start_note", "group", "labels"):
                if j.get(k) not in (None, "", [], {}):
                    print(f"{k:>10}: {j.get(k)}")
            print(f"{'times':>10}: created {_fmt_t(j['created'])} · started {_fmt_t(j.get('started'))} · ended {_fmt_t(j.get('ended'))}")
            print(f"{'memory':>10}: reserved {e['mem_req_gb']} GB · peak {e['mem_peak_gb']} GB" + (f" ({e['mem_eff']}%)" if e['mem_eff'] is not None else ""))
            print(f"{'cpu':>10}: reserved {e['cores_req']} · used {e['cores_used']}" + (f" ({e['cpu_eff']}%)" if e['cpu_eff'] is not None else ""))
            if j.get("io_read") is not None:
                print(f"{'disk I/O':>10}: read {j['io_read'] / 2**30:.2f} GB · written {j['io_write'] / 2**30:.2f} GB")
            if j.get("issues"):
                print(f"{'issues':>10}: {j['issues']}")
            if j.get("diagnosis"):
                print(f"{'cause':>10}: {j['diagnosis']['name']} — {j['diagnosis']['fix']}\n{'':>12}{j['diagnosis']['line']}")
            print(f"{'log':>10}: {queue.LOGS / (str(jid) + '.log')}")
        return 0
    if sub == "log":
        jid = _ids(a)[0]
        try:
            sys.stdout.write((queue.LOGS / f"{jid}.log").read_text(errors="replace")[-20000:])
        except OSError:
            print("(no output yet)")
        return 0
    if sub == "stat":
        jid = _ids(a)[0]
        f = queue.Q / "series" / f"{jid}.csv"
        try:
            rows = f.read_text().splitlines()[1:]
        except OSError:
            print("no samples yet")
            return 1
        print("time      RAM GB  cores  read MB  written MB")
        for r in rows[-40:]:
            t, rss, c, rd, wr = r.split(",")
            print(f"{time.strftime('%H:%M:%S', time.localtime(int(t)))}  {float(rss):6.2f}  {float(c):5.2f}  {rd:>7}  {wr:>10}")
        return 0
    actions = {"cancel": queue.cancel, "hold": queue.hold, "release": queue.release, "suspend": queue.suspend,
               "resume": queue.resume, "retry": queue.retry, "requeue": queue.requeue, "top": queue.top,
               "approve": lambda i: queue.approve(i, True), "reject": lambda i: queue.approve(i, False)}
    if sub in actions:
        if "--group" in a:
            g = a[a.index("--group") + 1]
            ids = [i for i in queue.in_group(g) if (queue.load(i) or {}).get("state") not in queue.TERMINAL]
            n = sum(1 for i in ids if actions[sub](i))
            print(f"{sub}: {n} job(s) of group {g}")
            return 0
        bad = [i for i in _ids(a) if not actions[sub](i)]
        if bad:
            print(f"not possible for: {', '.join(map(str, bad))} (check their state with ms q)")
            return 1
        return 0
    if sub == "clone":
        j = queue.clone(_ids(a)[0])
        print(j["id"] if j else "no such job")
        return 0 if j else 1
    if sub == "wait":
        j = queue.wait(_ids(a)[0])
        if not j:
            return 2
        print(f"#{j['id']} {j['state']} {j.get('reason', '')}")
        return 0 if j["state"] in queue.SUCCESS else 1
    if sub == "prio":
        return 0 if queue.set_priority(int(a[0]), int(a[1])) else 1
    if sub == "why":
        j = queue.load(_ids(a)[0])
        if not j:
            return 1
        r = queue.why_rerun(j)
        print("up to date — would be skipped" if not r else "would run because:\n  " + "\n  ".join(r))
        return 0
    if sub == "explain":
        rows = queue.explain(_ids(a)[0])
        for ok, check, detail in rows:
            mark = "  ok " if ok else "  NO "
            if check == "suggestion":
                mark = "  -> "
            print(f"{mark} {check:<34} {detail}")
        return 0
    if sub == "edit":
        if not a:
            print("usage: ms q edit ID [--cmd C] [--mem GB] [--cores N] [--priority P] [--name N] [--timeout T] [--group G] [--env K=V]")
            return 2
        jid, rest, kw = int(a[0]), a[1:], {}
        names = {"--cmd": "cmd", "--mem": "mem_gb", "--cores": "cores", "--priority": "priority", "--name": "name",
                 "--timeout": "timeout_s", "--group": "group", "--retries": "retries", "--nice": "nice", "--cwd": "cwd"}
        i = 0
        while i < len(rest):
            k = rest[i]
            v = rest[i + 1] if i + 1 < len(rest) else None
            if k in names and v is not None:
                kw[names[k]] = v
            elif k == "--env" and v:
                kk, _, vv = v.partition("=")
                kw.setdefault("env", {})[kk] = vv
            elif k == "--label" and v:
                kw.setdefault("labels", []).append(v)
            else:
                print(f"unknown option {k}")
                return 2
            i += 2
        for k in ("retries", "nice"):
            if k in kw:
                kw[k] = int(kw[k])
        ok, msg = queue.edit(jid, **kw)
        print(msg)
        return 0 if ok else 1
    if sub == "send":
        if len(a) < 2:
            print("usage: ms q send ID TEXT")
            return 2
        return send_input(int(a[0]), " ".join(a[1:]))
    if sub == "groups":
        lim = queue.groups()
        js = queue.all_jobs()
        names = sorted(set(lim) | {j["group"] for j in js if j.get("group")})
        if not names:
            print("no groups. Define limits in config/groups.conf ('vivado 3') and add jobs with --group vivado")
            return 0
        print(f"{'group':<20} {'limit':>5} {'running':>7} {'waiting':>7}")
        for g in names:
            run = sum(1 for j in js if j.get("group") == g and j["state"] in queue.ACTIVE)
            wait = sum(1 for j in js if j.get("group") == g and j["state"] in ("queued", "held", "approval"))
            print(f"{g:<20} {str(lim.get(g, '–')):>5} {run:>7} {wait:>7}")
        return 0
    if sub == "flaky":
        d = queue.flaky()
        if not d:
            print("no flaky jobs recorded")
            return 0
        for k, e in sorted(d.items(), key=lambda kv: -kv[1]["count"]):
            print(f"{e['count']:>4}x  {k:<50} last {_fmt_t(e.get('last'))}  jobs {', '.join(map(str, e['jobs'][-5:]))}")
        return 0
    if sub == "warnings":
        from . import issues, jobrun
        j = queue.load(_ids(a)[0])
        cur = issues.load(j["id"]) if j else None
        if cur is None:
            print("no issues recorded for this job")
            return 1
        ref = jobrun.reference_job(j)
        cmp = issues.compare(cur, issues.load(ref["id"]) if ref else [])
        print(f"compared with #{ref['id']} {ref['name']}" if ref else "no earlier run to compare with (all are new)")
        for k in ("new", "fixed", "outstanding"):
            print(f"\n{k.upper()} ({len(cmp[k])})")
            for i in sorted(cmp[k], key=lambda x: ("critical", "error", "warning").index(x["sev"]) if x["sev"] in ("critical", "error", "warning") else 9)[:60]:
                print(f"  {i['sev']:<8} [{i['id']}] {i['msg'][:150]}" + (f"  ×{i['count']}" if i["count"] > 1 else ""))
        return 0
    if sub == "history":
        days = float(a[a.index("--days") + 1]) if "--days" in a else 7
        st = a[a.index("--state") + 1] if "--state" in a else None
        nm = a[a.index("--name") + 1] if "--name" in a else None
        for j in filter_jobs(queue.history(days, st, nm), a):
            print(_row(j))
        return 0
    if sub == "eta":
        return _eta()
    if sub == "clear":
        print(f"{queue.clear(float(a[0]) if a else 0)} finished job(s) removed")
        return 0
    if sub == "status":
        st, d = queue.daemon_state()
        print(f"scheduler: {st}" + (f" (last error: {d['error']})" if d and d.get("error") else ""))
        return 0 if st == "running" else 1
    if sub == "report":
        js = _select(a)
        print(f"{'id':>5} {'name':<26} {'state':<8} {'time':>7} {'wait':>6} {'mem res':>7} {'peak':>6} {'mem%':>5} "
              f"{'cpu res':>7} {'used':>5} {'cpu%':>5} {'read GB':>8} {'write GB':>8}")
        tot_req = tot_peak = 0
        for j in js:
            e = queue.efficiency(j)
            if not j.get("started"):
                continue
            tot_req += e["mem_req_gb"]
            tot_peak += e["mem_peak_gb"]
            wait = flow.queue_hms(j["started"] - j["created"]) if j.get("created") else "–"
            print(f"{j['id']:>5} {j['name'][:26]:<26} {j['state']:<8} {flow.queue_hms(e['seconds']):>7} {wait:>6} {e['mem_req_gb']:>6.1f}G "
                  f"{e['mem_peak_gb']:>5.1f}G {str(e['mem_eff'] or '–'):>5} {e['cores_req']:>7.1f} {str(e['cores_used'] or '–'):>5} "
                  f"{str(e['cpu_eff'] or '–'):>5} {(j.get('io_read') or 0) / 2**30:>8.2f} {(j.get('io_write') or 0) / 2**30:>8.2f}")
        if tot_req:
            print(f"\nmemory efficiency overall: {tot_peak / tot_req * 100:.0f}%  "
                  "(well below 60% → lower mem_gb and run more jobs in parallel; near 100% → raise it)")
        return 0
    if sub == "timeline":
        html_out = None
        if "--html" in a:
            i = a.index("--html")
            html_out = a[i + 1]
            a = a[:i] + a[i + 2:]
        js = _select(a)
        if html_out:
            open(html_out, "w").write("<!doctype html><meta charset='utf-8'><body style='font:13px sans-serif'>"
                                      + flow.timeline_html(js, "Timeline"))
            print(f"written {html_out}")
            return 0
        rows = flow.timeline_rows(js)
        if not rows:
            print("no started jobs")
            return 0
        t0, t1 = min(r[1] for r in rows), max(r[2] for r in rows)
        width = 60
        for lab, s, e, st, _ in rows:
            x = int((s - t0) / max(1, t1 - t0) * width)
            w = max(1, int((e - s) / max(1, t1 - t0) * width))
            ch = {"done": "█", "running": "▶", "failed": "✗", "timeout": "⌛", "suspended": "‖", "warning": "▓"}.get(st, "▒")
            print(f"{lab[:30]:<30} |{' ' * x}{ch * w}{' ' * max(0, width - x - w)}| {st}")
        print(f"{'':<30}  {_fmt_t(t0)}{' ' * (width - 20)}{_fmt_t(t1)}")
        return 0
    if sub == "daemon":
        queue.Scheduler().loop()
        return 0
    if sub == "_exec":
        return queue.execute(int(a[0]))
    print(Q_HELP)
    return 2


def _eta():
    """Estimated start of waiting jobs: simulate the queue with measured durations (squeue --start)."""
    from . import capacity, probes
    jobs = queue.all_jobs()
    prof = capacity.profiles()
    th = probes.threads()
    total = probes.meminfo().get("MemTotal", 0)
    free_m = total - capacity.reserve_bytes(total)
    now = time.time()
    running = []
    for j in jobs:
        if j["state"] in queue.ACTIVE:
            m = (prof.get(j.get("tool") or "", {}).get("minutes") or 30) * 60
            running.append((max(now + 60, (j.get("started") or now) + m), float(j["cores"]), float(j["mem_gb"]) * 2**30))
    wait = sorted([j for j in jobs if j["state"] == "queued"], key=lambda j: (-int(j.get("order") or 0), -int(j.get("priority") or 0), j["id"]))
    load_ext = max(0.0, probes.loadavg()[0] - sum(r[1] for r in running))
    t = now
    est = {}
    for j in wait:
        need_c, need_m = float(j["cores"]), float(j["mem_gb"]) * 2**30
        while True:
            cur = [r for r in running if r[0] > t]
            if sum(r[1] for r in cur) + load_ext + need_c <= th and sum(r[2] for r in cur) + need_m <= free_m or not cur:
                break
            t = min(r[0] for r in cur)
        start = max(t, j.get("begin") or 0, j.get("not_before") or 0)
        m = (prof.get(j.get("tool") or "", {}).get("minutes") or 30) * 60
        running.append((start + m, need_c, need_m))
        est[j["id"]] = start
    print(f"{'id':>5} {'name':<30} {'starts about':<14} note")
    for j in wait:
        dep = " (after dependencies)" if j.get("depends") else ""
        print(f"{j['id']:>5} {j['name'][:30]:<30} {time.strftime('%m-%d %H:%M', time.localtime(est[j['id']])):<14} "
              f"{'now' if est[j['id']] - now < 120 else 'in ' + flow.queue_hms(est[j['id']] - now)}{dep}")
    print("\n(rough: assumes measured or 30-minute durations and today's background load)")
    return 0


def _select(a):
    if a:
        try:
            return flow.flow_jobs(flow.load_flow(a[0]))
        except (OSError, ValueError):
            pass
    return [j for j in queue.all_jobs() if time.time() - (j.get("started") or j["created"]) < 7 * 86400]


def _opts(a):
    """--set k=v (repeatable) and other --key [value] options; returns (sets, opts, positional)."""
    import yaml
    sets, opts, rest = {}, {}, []
    flags = {"--dry-run", "--resume", "--stub", "--remote", "--skip-if-successful", "--only-if-changed", "--idle", "--downstream"}
    i = 0
    while i < len(a):
        if a[i] == "--set" and i + 1 < len(a):
            k, _, v = a[i + 1].partition("=")
            try:
                v = yaml.safe_load(v)
            except Exception:
                pass
            sets[k] = v
            i += 2
        elif a[i] in flags:
            opts[a[i][2:]] = True
            i += 1
        elif a[i].startswith("--") and i + 1 < len(a):
            opts.setdefault(a[i][2:], []).append(a[i + 1])
            i += 2
        else:
            rest.append(a[i])
            i += 1
    return sets, opts, rest


def _one(opts, k):
    v = opts.get(k)
    return v[-1] if isinstance(v, list) else v


def flow_cmd(a):
    if not a or a[0] in ("-h", "--help", "help"):
        print(FLOW_HELP)
        return 0
    sub, a = a[0], a[1:]
    if sub == "example":
        print(EXAMPLE)
        return 0
    if sub == "reference":
        print(REFERENCE)
        return 0
    sets, o, rest = _opts(a)
    profile = _one(o, "profile")
    if sub in ("check", "run"):
        if not rest:
            print(f"usage: ms flow {sub} FILE")
            return 2
        f, specs, errs = flow.check(rest[0], sets, profile, _one(o, "sample"))
        if errs:
            print("Problems:")
            for e in errs:
                print(f"  - {e}")
            return 1
        specs = flow.select(specs, o.get("until"), o.get("from"), o.get("only"))
        if sub == "check" or o.get("dry-run"):
            plan, total, peak = flow.estimate(specs)
            print(f"Flow '{f['name']}': {len(specs)} job(s)\n{flow.dag_text(specs)}\n")
            for s, mins, start, measured in plan:
                print(f"  {s['step']:<10} {s['name'][:34]:<34} ~{mins:>4.0f} min{'' if measured else '?'}  starts ~{start:>5.0f} min  {s['cwd']}")
            print(f"\nEstimated on an idle machine: about {flow.queue_hms(total * 60)} with up to {peak} jobs in parallel"
                  " ('?' = no measured runs of that tool yet, 30 min assumed).")
            adv = flow.lint(rest[0], profile)
            if adv:
                print("\nAdvice (ms flow lint):\n  - " + "\n  - ".join(adv))
            return 0
        rec, specs = flow.submit_flow(rest[0], sets, resume=o.get("resume", False), profile=profile, sample=_one(o, "sample"),
                                      until=o.get("until"), from_=o.get("from"), only=o.get("only"), force=o.get("force"),
                                      stub=o.get("stub", False))
        cached = sum(1 for j in flow.flow_jobs(rec) if j["state"] == "cached")
        print(f"flow {rec['id']}: {len(rec['jobs'])} job(s) queued" + (f", {cached} already done (resume)" if cached else ""))
        if queue.daemon_state()[0] != "running":
            print("note: the scheduler is not running (systemctl --user start my-system-queue)", file=sys.stderr)
        return 0
    if sub == "lint":
        adv = flow.lint(rest[0], profile)
        print("no advice — looks good" if not adv else "  - " + "\n  - ".join(adv))
        return 0
    if sub == "summary":
        for st, nm, status, reasons in flow.summary(rest[0], sets, profile):
            print(f"{st:<12} {nm[:40]:<40} {status:<11} {'; '.join(reasons)[:120]}")
        return 0
    if sub == "touch":
        print(f"{flow.touch(rest[0], rest[1:] or None, sets, profile)} job(s) marked up to date")
        return 0
    if sub == "untracked":
        for p in flow.untracked(rest[0], sets, profile):
            print(p)
        return 0
    if sub == "list":
        for r in flow.flows()[-40:]:
            cnt, done, ok = flow.flow_state(r)
            st = "ok" if ok else "failed" if done else "running"
            print(f"{r['id']:<44} {st:<8} " + ", ".join(f"{v} {k}" for k, v in sorted(cnt.items())))
        return 0
    if sub == "show":
        rec = flow.load_flow(rest[0])
        print(f"{rec['id']}  {rec['file']}")
        for j in flow.flow_jobs(rec):
            print(_row(j))
        return 0
    if sub == "report":
        out = _one(o, "html") or str(queue.Q / f"report-{rest[0]}.html")
        open(out, "w").write(flow.report_html(rest[0]))
        print(f"written {out}")
        return 0
    if sub == "retry":
        print(f"{flow.retry_flow(rest[0], o.get('step'), o.get('downstream', False))} job(s) queued again")
        return 0
    if sub == "cancel":
        print(f"{flow.cancel_flow(rest[0])} job(s) cancelled")
        return 0
    if sub == "schedule":
        deb = _one(o, "debounce")
        e = flow.add_schedule(rest[0], cron=_one(o, "cron"), idle=o.get("idle", False), git=_one(o, "on-commit"),
                              overrides=sets, after_flow=_one(o, "after-flow"), catchup_h=float(_one(o, "catchup") or 24),
                              skip_if_successful=o.get("skip-if-successful", False), only_if_changed=o.get("only-if-changed", False),
                              debounce_s=queue.parse_duration(deb) if deb else 0, paths=o.get("paths"),
                              remote=o.get("remote", False), profile=profile)
        print(f"schedule {e['id']} added")
        return 0
    if sub == "schedules":
        for e in flow.schedules():
            how = e.get("cron") or ("when idle (once a day)" if e.get("idle") else "") or \
                (f"after flow {e['after_flow']}" if e.get("after_flow") else "") or f"on commit to {e['git']['repo']}"
            print(f"{e['id']:>3}  {how:<28} {'PAUSED ' if e.get('paused') else ''}{e['file']}  last: {_fmt_t(e.get('last'))}")
        return 0
    if sub == "unschedule":
        flow.remove_schedule(rest[0])
        return 0
    if sub in ("pause", "unpause"):
        flow.pause_schedule(rest[0], sub == "pause")
        return 0
    print(FLOW_HELP)
    return 2


def sem_cmd(a):
    """ms sem NAME[:COUNT] -- CMD   run CMD while holding one slot of a named semaphore (GNU Parallel sem)."""
    from . import resources
    if "--" not in a or a.index("--") == 0:
        print("usage: ms sem NAME[:COUNT] -- CMD ...   (e.g. ms sem diskio -- cp big.dcp /mnt/data/)")
        return 2
    name, _, cnt = a[0].partition(":")
    cmd = a[a.index("--") + 1:]
    h = resources.sem_acquire(name, int(cnt or 1))
    try:
        return subprocess.call(cmd)
    finally:
        h.close()


def lineage_cmd(a):
    from . import lineage
    if not a:
        print("usage: ms lineage FILE")
        return 2
    sha, prod, cons = lineage.query(a[0])
    print(f"{a[0]}  sha256 {sha[:16]}…")
    if not prod and not cons:
        print("unknown: no queued job recorded this exact content")
        return 1
    for r in prod:
        print(f"  produced by job #{r['job']} {r['name']} ({r.get('flow') or 'no flow'}) at {_fmt_t(r['t'])} as {r['path']}")
    for r in cons:
        print(f"  used by     job #{r['job']} {r['name']} ({r.get('flow') or 'no flow'}) at {_fmt_t(r['t'])}")
    return 0


def secret_cmd(a):
    from . import jobrun
    p = jobrun.SECRETS
    if not a or a[0] == "list":
        for k in jobrun.load_secrets():
            print(k)
        return 0
    if a[0] == "set" and len(a) >= 2:
        import getpass
        val = getpass.getpass(f"value for {a[1]}: ")
        cur = jobrun.load_secrets()
        cur[a[1]] = val
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("".join(f"{k}={v}\n" for k, v in cur.items()))
        print(f"saved; use it in a job with --secret {a[1]} (flow: secrets: [{a[1]}]); it is masked in logs")
        return 0
    if a[0] == "rm" and len(a) >= 2:
        cur = jobrun.load_secrets()
        cur.pop(a[1], None)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("".join(f"{k}={v}\n" for k, v in cur.items()))
        return 0
    print("usage: ms secret list | set NAME | rm NAME")
    return 2


def webhook_cmd(a):
    """ms q serve [--port 8765]: localhost-only webhook to start flows (POST /flow/<schedule-id>, header X-Token)."""
    import http.server
    import secrets as sec
    port = int(a[a.index("--port") + 1]) if "--port" in a else 8765
    tok_f = queue.Q / "webhook_token"
    if not tok_f.exists():
        tok_f.write_text(sec.token_hex(16))
        os.chmod(tok_f, 0o600)
    token = tok_f.read_text().strip()

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            if self.headers.get("X-Token") != token:
                self.send_response(403)
                self.end_headers()
                return
            m = self.path.strip("/").split("/")
            if len(m) == 2 and m[0] == "flow":
                e = next((x for x in flow.schedules() if str(x["id"]) == m[1]), None)
                if e:
                    rec, _ = flow.submit_flow(e["file"], e.get("overrides"), resume=True, schedule_id=e["id"], profile=e.get("profile"))
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(json.dumps({"flow": rec["id"], "jobs": len(rec["jobs"])}).encode())
                    return
            self.send_response(404)
            self.end_headers()

        def log_message(self, *x):
            pass
    print(f"listening on http://127.0.0.1:{port}  (token in {tok_f})")
    http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
    return 0
