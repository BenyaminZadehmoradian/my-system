# Changelog

## 1.4.0 — 2026-10-31

Ideas taken from GNU Parallel, task-spooler, Slurm, Snakemake, Nextflow, Dagu, evQueue, Jenkins and Buildbot.

- **Job queue** (`ms q`, user service `my-system-queue`): starts jobs only when the threads and RAM they need are free
  (live load + RAM still to be taken by just-started jobs), priorities with backfill, optional preemption,
  pause/resume of the newest low-priority job when RAM runs low, retries (out-of-memory → 1.5× memory),
  timeouts and stuck-job alerts, hard memory limits via systemd scopes, dependencies (after ok / after any),
  preconditions, idle-only jobs, GPUs (when nvidia-smi works), containers and SSH hosts.
- Every queued job writes the standard run log with provenance (tool path and version, input fingerprints, environment).
- **Flows** (`ms flow`): YAML steps with parameter matrices, per-item dependencies, `{{placeholders}}`, validation,
  dry-run time estimate, resume, incremental re-runs, temp-file cleanup, on_success/on_failure hooks,
  schedules (cron, when idle, on new commit) and one summary notification per flow.
- Reports: `ms q report` (memory/CPU reserved vs used), `ms q timeline [--html]`, `reports/metrics.prom` for Prometheus.
- App: new **Queue** page (control buttons, Gantt timeline, flow plan dialog), **Run logs** gets a metric trend chart
  and an artifact viewer; sidebar without icons.
- **MCP server** `ms mcp` (registered for Claude Code): capacity, queue, flows and run logs for AI assistants.
- AI rules and capacity guide now point to the queue instead of tsp.

## 1.3.0 — 2026-10-31

- **Standard run log** (`msrun.jsonl`, format v1, `runlog/RUNLOG_SPEC.md`): one JSON event per line —
  start, stages, log (DEBUG/INFO/WARN/ERROR), metrics, artifacts, progress, end.
- Writers: `msrun.py` (Python, stdlib only), `msrun.tcl` (Vivado/Vitis/Tcl), `ms log run -- CMD` (wraps any command,
  turns ERROR / CRITICAL WARNING lines into events), and `ms log start/stage/metric/msg/end` for shell scripts.
- `ms log check` validates files against the spec (exit 1 on any violation); `ms log show`, `ms log list`, `ms log index`.
- New **Run logs** page in the app: all runs with status, duration, errors, metrics; timeline of the selected run.
- Watcher alerts once for every failed run; the daily job searches project folders for run logs.
- AI rules: every program an assistant writes must produce this log and pass `ms log check`.

## 1.2.2 — 2026-10-28

- Capacity: runs first seen mid-way (e.g. already running at install) are marked *partial* and never lower the
  estimate; CPU need per job uses the job's busiest minute (not its share on a crowded machine), at least one thread.
  Fixes impossible advice such as 17 parallel Vivado runs on 16 threads.

## 1.2.1 — 2026-10-28

- Capacity guide: consistent thread advice (idle default = threads − 2), disk sizes in the same units as the rest of the app, wording fix.

## 1.2.0 — 2026-10-28

- **Capacity guide for AI assistants**: the system map now has a section *This machine and how to run heavy work on it* —
  CPU, vector ISA, caches, RAM and reserve, swap/zram, GPU/CUDA availability, disk roles, measured cost of each tool,
  and ten concrete rules (check first, queue with tsp, cap threads, Vivado maxThreads, nice/ionice, outputs on the data disk).
- `ms capacity [TOOL] [--json]`: live headroom and how many more jobs of each kind fit right now; BUSY when the machine is saturated.
- The watcher now measures every finished job (peak RAM, CPU cores, duration) per tool in `state/job_profiles.json`.
- AI rules: run `ms capacity` before heavy work. System report: new *Capacity for heavy work* section.

## 1.1.2 — 2026-10-28

- New **System report** page in the app: every report is shown inside the app (light/dark), with *Create report now* and *Hardware check now*; no browser needed.

## 1.1.1 — 2026-10-28

- Memory makers shown by name when the BIOS only reports JEDEC codes (e.g. 0198… → Kingston, 802C… → Micron).
- Git recommendation broken down by problem (not in backup, uncommitted, unpushed, no remote).
- zram size shown in GiB.

## 1.1.0 — 2026-10-28

- **Full system report**: `ms report [--open]`, monthly on the 1st (timer), and a button on the Reports page.
  HTML (light/dark) + Markdown + JSON snapshot; includes changes since the previous report and prioritised recommendations.
- **Hardware collector** (root, read-only, daily): SMART health of every disk, memory modules, firmware updates.
  Daily health check warns on failing disks, bad sectors, wear, and notes new unsafe shutdowns.
- New probes without root: fans, temperature sensors, swap/zram, Secure Boot, apt sources, kernels, GPUs, network links, power profile.
- `ms hw`, doctor checks for the report timer and hardware data.

## 1.0.1 — 2026-10-28

- Backup status now says *running (since HH:MM)* or *first backup not finished yet* instead of *not set up* while the first backup runs.
- `ms doctor` checks the backup and restore-test timers and the backup state.
- No "backup is behind" alert while a backup is still running.

## 1.0.0 — 2026-10-28
First general release: auto-detection, config-driven everything, plugins (Vivado, Vitis HLS,
yosys/nextpnr/VPR, simulators, make), results extraction, CPU overload advice, reboot hint,
watchdog, git project status, weekly digest, AI guard hook + managed policy, daily AI system map,
encrypted backup with monthly restore test, desktop app with 11 pages, phone notifications.
Upgrades the earlier single-machine layout (scripts/) in place.
