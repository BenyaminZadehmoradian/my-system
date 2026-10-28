# Changelog

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
