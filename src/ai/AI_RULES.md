# Rules for AI assistants on this machine

These rules are installed by **my-system** and are read-only. The machine-specific part
(hardware, toolchains, environments, projects, protected paths) follows below in the
*System map*, regenerated every day.

## Hard rules
1. **Do not change the system.** No `sudo`, no package managers (`apt`, `snap`, `flatpak`),
   no `pip install` outside a project virtual environment, no edits to shell start-up files,
   system files, toolchain installs or the protected paths listed in the system map.
   Such commands are blocked automatically and the user is notified.
2. **Do not search the whole disk.** Everything you may need is listed in the system map and
   in the inventory folder it points to. If something is missing, ask the user — do not install it.
3. **Stay inside the project folder** you were started in. Writing elsewhere needs the user's
   explicit permission in the chat.
4. **Never stop, kill or restart** running jobs or services. Long simulations and builds are
   often in progress; check the running jobs before starting heavy work.
5. **Never delete** outside the project folder. Inside it, ask before deleting anything you
   did not create in this session.
6. **Large outputs go to the data disk** named in the system map, not the root disk.
7. **Before any heavy work, run `ms capacity`** and start no more jobs than it allows. It knows the
   live load, free RAM and the measured cost of each tool on this machine. Cap thread counts
   explicitly (`make -j N`, `OMP_NUM_THREADS`, `set_param general.maxThreads`), never "all cores".
8. **Long or heavy jobs go through the my-system queue**, never straight into the shell:
   `ms q add --name NAME [--mem GB] [--cores N] -- <command>` (or the `queue_submit` MCP tool).
   Sweeps and multi-step work are a flow file: `ms flow example` shows the format,
   `ms flow check FILE` must pass, then `ms flow run FILE`. Watch progress with `ms q`.
   The queue decides when each job may start; do not try to work around it. If a job waits,
   `ms q explain ID` says exactly which condition blocks it (RAM, CPU, licenses, group, pressure).
   For "try several variants until one meets the goal" (seeds, strategies) use `stop_when` in the
   flow step so the remaining variants are stopped automatically.
9. **Every program, script or flow you write that runs a job or produces results must write a
   standard run log** (`msrun.jsonl`, format in `~/My_System/runlog/RUNLOG_SPEC.md`) in its run
   folder — start, stages, errors, metrics, artifacts, end. Use the helpers, never hand-written JSON:
   Python `ms log template python > msrun.py`, Tcl/Vivado `ms log template tcl > msrun.tcl`,
   any other command `ms log run --name NAME -- CMD`. Keep the tool's own log as well.
   Before you say you are done, run `ms log check <run folder>` and fix every problem it reports.

10. **Before changing, updating or recreating any environment, package or library, ask who uses it:**
    `ms software uses NAME` (or the `software_uses` MCP tool) and tell the user which projects are affected.
    Never update packages yourself; `ms software plan` gives the user the commands.
11. **Make jobs observable:** print progress with `echo PERCENT >&3`, pass results to later steps by
    writing `key=value` lines to `$MSQ_OUTPUTS`, put temporary files in `$TMPDIR`, and declare
    `inputs`/`outputs` in flow steps so unchanged work is skipped.
12. **Remote servers (Slurm, e.g. Titan) — the queue decides where a job runs.** The servers, and whether jobs go
    there automatically, are listed under *Remote servers* in the system map. With `offload = prefer` every allowed
    queue job is sent to the server first and runs on this machine only when the server is full, unreachable or slower,
    and a job that fails there is run once more here. So:
    - Just submit with `ms q add` / a flow / `queue_submit`. Do **not** add `--on` unless the user asks:
      `--on NAME` forces a server, `--on here` keeps a job on this machine, `--on auto` = server only when busy here.
    - **Never use ssh, scp, rsync, sbatch, srun, salloc or scancel yourself** to reach a server (blocked). Many people
      share it; my-system only touches its own folder there and only your jobs. Status: `ms remote list`, `ms remote status`.
    - **Make jobs self-contained:** start each job from its own run folder; everything it reads must be inside that
      folder (relative paths, no `$HOME/...` or `/mnt/...` paths); results are written into that folder. Only the
      job folder is copied there and back. Big folders: declare what is needed with `--inputs GLOB` (flows: `inputs:`).
    - Programs listed as missing on a server (e.g. Vivado, Vitis on Titan) always run here; that is expected.
    - Never put passwords, keys, tokens or private data into a job (`--secret` jobs are never sent to a server).
    - `ms q explain ID` shows where a job will run and why; the job log shows the node (`[remote] titan02 ...`).
13. **Only the allowed AMD/Xilinx tool version.** If the system map has a section *AMD/Xilinx tool version*, use only
    that version (Vivado, Vitis, vitis-run). Do not run, source (`settings64.sh`) or write scripts for any other installed
    version, even if a project's old scripts mention one; update such scripts to the allowed version instead (ask first).
    Commands that name another version are blocked, and queue jobs that run one are stopped.
14. **To compare runs** use `ms log compare FOLDER --sort METRIC --desc` and `ms log diff RUN_A RUN_B`
    (or the `runs_compare` MCP tool) instead of parsing logs yourself. To find out what the machine was
    doing at a given time (e.g. why a run was slow or the machine froze) use `ms replay TIME`.

## Transparency
Every tool call is logged and visible to the user in the my-system app (AI guard tab).
If a command is blocked, explain what you wanted to do and why, and let the user decide.
