# How to work on this machine (rules for AI assistants)

Installed by **my-system**; read-only. The user set these rules. The *System map* below (regenerated daily)
lists the hardware, tools, projects, servers and limits. Live state: `ms ai status`. These rules: `ms ai rules`.

## 1. Run code like this

| You want to | Do exactly this |
|---|---|
| run anything that takes over a minute, or any Vivado / Vitis / ISE / simulation / sweep | `cd RUN_FOLDER` then `ms q add --name NAME --cores N --mem GB [--metrics a,b] -- COMMAND` |
| run many variants or steps | a flow file: `ms flow example` → `ms flow check FILE` → `ms flow run FILE` |
| run something short (seconds: git, ls, a quick script, a unit test) | just run it |
| see what happens | `ms q` · `ms q show ID` · `ms q log ID` · `ms q explain ID` (why it waits / where it runs) |
| wait for a job | `ms q wait ID` (blocks; exit code 0 = success). Do not poll in a loop with sleep. |
| change a waiting job | `ms q edit ID --cores N --mem GB --after OTHER` (not cancel and add again) |
| stop a job | only jobs you added in this session: `ms q cancel ID`. Never anyone else's, never with kill. |

- **Never start Vivado, Vitis, ISE or a script/Makefile that starts them directly** (blocked). No `&`, `nohup`,
  `tmux`, `screen` for long work: only the queue. The queue starts each job when CPU and RAM are free and decides
  **where** it runs (this machine or the server); you do not check capacity first and you do not choose.
- **Job names** are `<study>_<what>_<variant>` with a stable first part (`k70v_aes_c0`, `k70v_aes_c1`): results
  and measured needs are grouped by that first part.
- **Ask for what the job really uses**, not more: `--cores` = number of tool processes that run *at the same time*
  × about 2 (one Vivado or HLS run uses 1–2 cores); a script that runs designs one after another needs
  `--cores 1`. `--mem` = measured peak + 20 %. The system map and `ms remote usage` show what earlier jobs of the
  same name used; `ms q add` prints a hint when you ask for too much. Reserved-but-unused cores keep your other
  jobs waiting and take the shared server from other people.
- **Size of a job:** at least about 30 minutes of work per job (each job has 2–5 minutes of fixed cost on the
  server), and split anything over a few hours so it can run in parallel.
- **A job that needs files made by another job waits for it:** `--after ID` (flow: `needs:`). Before queuing a
  sweep, check that every input file it reads exists or is produced by a job it depends on — do not queue 13 jobs
  that all stop in the first seconds on a missing input.
- Use thread counts from `$MSQ_THREADS` in scripts (`set_param general.maxThreads $::env(MSQ_THREADS)`,
  `make -j$MSQ_THREADS`); never "all cores".

## 2. Every job folder is self-contained (it must work anywhere)

The queue may run a job on the server. Only **the job's own folder** (where you ran `ms q add`) is copied there and
back, and paths are translated for you (the folder itself, and the tool paths listed in the system map).

- One folder per run. Everything the job reads is inside it; everything it writes goes into it. Relative paths.
- No absolute paths to other places on this machine (another project, `~/Downloads`, `/opt/...`): such a job
  silently stays on this machine and is slow. `ms q explain ID` names the path. Big folders: `--inputs GLOB`.
- Big temporary files: delete them at the end of the job or pass `--outputs GLOB` (only those come back).
- Never use `ssh`, `scp`, `rsync`, `sbatch`, `srun`, `scancel` yourself (blocked). Do not add `--on` unless the
  user asks. A job on the server survives a restart of this computer: do not submit it again.
- More: `~/My_System/ai/docs/server.md`.

## 3. Log like this (the run log)

Every queue job gets a **standard run log automatically** (`msrun.jsonl` in its folder: start, command, git commit,
time, CPU, RAM, exit code, errors). **You add the content by printing lines** — from any language, on this machine
and on the server, no library needed:

```
echo "::ms stage synth"                      # a new stage begins (short stable names: hls, cosim, synth, place, route, power)
echo "::ms metric wns_ns -0.084 ns"          # every result that matters: NAME VALUE [UNIT], snake_case, unit in the name
echo "::ms metric lut 41233"
echo "::ms progress 40 placing"              # percent done
echo "::ms artifact reports/timing.rpt"      # files worth keeping (path inside the job folder)
echo "::ms warn clock uncertainty high"      # also: ::ms error TEXT, ::ms info TEXT
```
Tcl: `puts "::ms metric wns_ns $wns ns"` · Python: `print("::ms metric lut", lut, flush=True)`

Required of every job you write:
1. **A stage line before each phase** that takes more than a minute.
2. **A metric line for every number the user will ask about** (timing, resources, power, latency, accuracy,
   cycles, pass/fail as `true`/`false`). Results only inside a report file do not count.
3. Name the metrics in `ms q add --metrics wns_ns,lut ...` (flow key `metrics:`): a job that ends without them
   is marked *warning* and you must fix the script, not ignore it.
4. On a problem: `::ms error what went wrong` and exit non-zero. Never end with exit 0 after a failed step.
5. Keep the tool's own log files too.

Outside the queue (a short script): `ms log run --name NAME -- COMMAND` gives the same log and understands the
same lines. Check with `ms log check FOLDER`; read with `ms log show FOLDER`; compare runs with
`ms log compare FOLDER --sort METRIC` / `ms log diff A B` instead of parsing logs yourself.
Details and the Python/Tcl helpers: `~/My_System/ai/docs/logging.md`.

## 4. Never (blocked automatically, the user is told)

1. Change the system: no `sudo`, `apt`/`snap`/`flatpak`, no `pip install` outside a project virtual environment,
   no edits of shell start-up files, system files, tool installs or the protected paths in the system map.
2. Stop, kill, restart or renice anything you did not start. Long runs of other sessions are usually active.
3. Delete outside the project folder; inside it, ask before deleting what you did not create in this session.
4. Write outside the project folder you were started in — except its run folders on the data disk named in the
   system map (large outputs go there, never on the root disk).
5. Search the whole disk. What exists is in the system map and the inventory it points to; if something is
   missing, ask — do not install it.
6. Use a Vivado/Vitis/ISE version the project is not allowed (system map, *tool versions per project*), even if an
   old script names it. Propose updating the script instead.
7. Change my-system itself: `ms pause/resume/lock/unlock`, `ms tools allow/deny`, files in `~/My_System`.
8. Update or recreate an environment or library without `ms software uses NAME` first and telling the user who
   else uses it.
9. Put passwords, keys or tokens into a job, a script or a log.

If a command is blocked: do not look for another way to do the same thing. Say what you wanted and why, and let
the user decide.

## 5. Before you say "done"

- `ms q show ID` of every job you added: state `done` (not `warning`/`failed`), and the metrics are there.
- `ms log check FOLDER` passes for the run folders you created.
- Tell the user: the job ids, the run folders, the key metrics, anything that failed or was skipped and why.
  Do not report numbers you did not read from a finished run.

Every tool call is logged and visible to the user (my-system app, *AI guard*).
