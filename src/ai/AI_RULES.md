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
   The queue decides when each job may start; do not try to work around it.
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

## Transparency
Every tool call is logged and visible to the user in the my-system app (AI guard tab).
If a command is blocked, explain what you wanted to do and why, and let the user decide.
