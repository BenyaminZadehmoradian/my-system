# Changelog

## 1.19.3 — 2026-11-07
- `ms remote test / tools / licenses` removed their temporary job folder (state/remote/ms-remote-test-*) never;
  ten were left on the workstation. They are now removed when the test ends, and the daily housekeeping removes
  any older than a day.

## 1.19.2 — 2026-11-05
- The GUI test failed on GitHub's runners: PyQt6 installed with `pip --user` was not found once the test changed
  HOME for the app it starts. Its folder is now passed on (PYTHONPATH). Only the test changed.

## 1.19.1 — 2026-11-05
- `ms remote licenses`: a device without a license was reported as "failed (not a license problem)". Inside
  `catch`, Vivado prints only `WARNING: [Common 17-348] Failed to get the license …`, not the final 17-345 error
  (seen on Titan for xc7vx330t); both count now, and the caught message is printed too. The output shows only the
  result lines instead of every synthesis line.

## 1.19.0 — 2026-11-05
- **The app no longer freezes.** A run left "running" in a run log made the Run logs page read every run log again
  on each 15-s refresh (1.8 s with 40 logs; the window opened in 2.2 s). Run logs are now parsed once and again only
  when the file changes (whether a run still lives is checked fresh); the table is rebuilt only when something
  changed (running runs once a minute); the sidebar statuses are computed in a background thread; a page whose
  refresh takes over 0.7 s is refreshed once a minute. Window in 0.5 s, the Run logs page in 0.2 s, refreshes ~0 s.
  `ms gui --profile` writes the time of every page to state/gui_profile.txt.
- `ms q add` checks the command first: the script / Tcl file it names (bash X, ./X, source X, -source, -f, --tcl,
  -tclbatch, make → Makefile) must exist, and it must not run in ~, ~/My_System or a my-system checkout.
  `--anyway` adds it all the same.
- `AMD_SETUP` (my_system.conf), e.g. `source /mnt/data/Apps/2025.2.1/Vivado/settings64.sh`: put in front of every
  job that uses AMD tools — in its command line or in the script or Makefile it runs — unless it sources a
  settings64.sh itself. `ms q show` gives the command as typed (cmd_typed). On a server it is translated by path_map.
- A job you paused (ms q suspend) keeps its RAM: a message after `SUSPEND_REMIND_H` hours (24), then once a day.
- `ms thermal` (status, highest temperature in 24 h), `ms thermal check` (in the daily check: limits lost after a
  reboot, hotter than MAX_TEMP_C), `ms thermal setup 80` / `ms thermal off` (thermald trip point + cpu-cool.service,
  as measured in docs/thermal.md).
- `ms remote licenses NAME [PART ...]`: a tiny synthesis per part on a node; parts without a license there are
  remembered (jobs for them stay here), licensed ones are cleared. Default: the parts your recent jobs use.
- path_map accepts `~/x` on the left (the literal text, for scripts that call `~/.local/bin/vitis_hls`);
  docs/vitis_hls_wrapper.sh turns `vitis_hls -f` into `vitis-run --mode hls --tcl` on Vitis 2025.
- Failure diagnosis: "Device needs a license" (Common 17-345 for a device) instead of "all seats in use".
- GitHub Actions: lint (pyflakes, shellcheck) and every test on each push (.github/workflows/tests.yml).
- docs: thermal.md; remote-servers.md (adding devices with docs/add_retry.sh, licenses, vitis_hls on the server).
- tests/test_v19.py: 16 tests, including the app started offscreen with stuck runs (skipped without PyQt6).

## 1.18.5 — 2026-11-05
- A server job that fails with "A valid license was not found for … device 'X'" makes the queue keep later jobs
  for device X here (30 days, or until the server's `setup` changes, e.g. to add XILINXD_LICENSE_FILE).
  `ms q explain` says "no license there for: …", `ms remote tools` lists such devices. Seen on Titan: Virtex-7
  xc7vx330t needs a paid license that the free edition lacks.

## 1.18.4 — 2026-11-05
- `MAX_TEMP_C` (my_system.conf, off by default), e.g. 80: from MAX_TEMP_C − QUEUE_TEMP_MARGIN (5) no new queue job
  starts on this machine (jobs for servers still go); above MAX_TEMP_C for 20 s the lowest-priority job running here
  is paused (SIGSTOP, like for low RAM) and continues below MAX_TEMP_C − QUEUE_TEMP_RESUME (10). One action per 30 s,
  a phone/desktop message at most every 30 min. Only queue jobs are controlled: for a hard limit on everything,
  use the CPU's own limits (thermald / RAPL), see README.

## 1.18.3 — 2026-11-05
- `ms remote tools` asks Vivado itself which devices are installed (`get_parts`); the old list of folder names
  showed every family (virtex7 too) even when only Zynq-7000 was installed. The list is saved per server.
- The queue does not send a job to a server whose installation lacks a part the job uses (found in its command
  and the Tcl/project/config files of its folder); `ms q explain` names the part. Seen on Titan: a Virtex-7 job
  failed with "Part 'xc7vx330tffg1157-1' is not installed".
- tests/live/titan_diagnose.sh: the same device query, and `PART=…` is checked directly.

## 1.18.2 — 2026-11-05
- HLS with Vitis 2025+ (`vitis-run`, `v++`) gets a real starting estimate (4 GB / 1.5 cores, v++ 8 GB / 2 cores)
  instead of the 2 GB / 1 core of an unknown program, here and in the cores asked from Slurm.
- A job without --name is named after its tool or first real program: `source settings64.sh && vivado ...` is
  "vivado" (was "source"); `cd`, `export`, `VAR=value` are skipped too.
- 1.18.1 (same day): hermetic server tests, `ms q explain` keeps saying why a job was not sent after it ran here.

## 1.18.0 — 2026-11-05
Server (Titan) fixes and a tool check:
- **A server job that died before writing its log (e.g. exit 92: the node cannot scp from the head node) was
  followed forever** and looked "running" here; while a job was pending its state was never shown. The poll no
  longer fails when the log does not exist yet, and such jobs end with the reason (exit code, Slurm's own output).
- A busy Slurm controller (squeue answering nothing) no longer ends a running job: sacct must say it ended.
- A cancel reaches `scancel` at once (the runner waited up to a whole poll interval, and the SIGKILL after 30 s
  could leave the job running on the shared account). Cancelling during the upload no longer starts the job there.
- Tool logs that are not UTF-8 (or a character cut between two polls) no longer crash the runner.
- Results: links the job made come back; unsafe entries (absolute links, links out of the folder) are skipped and
  listed instead of stopping the copy halfway.
- The node falls back to `C.UTF-8` when the locale sent along by ssh does not exist there (Vivado aborts with
  "locale::facet::_S_create_c_locale name not valid").
- Link check on the node says when a real folder or another link sits where the link should be.
- `sbatch` refused `--mem` (Titan) after sinfo could not answer: retried once without it, and remembered.
- Free CPUs are counted in the configured partition only.
- New `ms remote tools NAME`: starts Vivado, vitis-run, v++, vitis_hls and xelab on a node exactly like a job and
  explains what is missing (settings, execute bits, a library named in a failed start, locale, HLS on PATH, libc6-dev for csim, installed
  device families) and which `missing_tools` can be removed.
- All command helpers decode output with `errors="replace"`: a non-UTF-8 file name (e.g. on the SMB share) no
  longer crashes the daily check, system report or software page (UnicodeDecodeError is not an OSError).
- Tests: `tests/run_tests.sh` runs every test file without pytest; `tests/test_v18.py` (11 tests) can run as a
  script too, so `ms selftest` really runs it. Live tests for the real server in `tests/live/`:
  `titan_diagnose.sh` (standalone: one Slurm job that checks the node, share, link, settings, libraries, locale and
  really synthesizes with Vivado and HLS) and `live_titan.py` (check, tools, a Vivado and an HLS job through the
  queue with path translation). Neither is run automatically: they use the shared cluster.
- docs/remote-servers.md: verified Titan configuration (Vivado and HLS jobs pass on titan02; in 2025.2.1 Vivado's
  settings64.sh also sets up Vitis; no vitis_hls command); copy with `rsync -rltp`; a troubleshooting table.
- `ms q explain` says why a job was not sent to a server also after it ran here (it only did while queued), and
  says "the server first" when offload = prefer.
- Tests that use the fake cluster no longer see this machine's own AMD tools (PATH, XILINX_*).

## 1.17.3 — 2026-11-03
- The job log of a server job keeps the server's real paths (it was translated back like result files, which
  made "No such file" errors point at local paths) and starts with what was used there: each link and whether
  its target exists, and each path translation.

## 1.17.2 — 2026-11-03
- `ms remote check` also checks `links` and `path_map` targets on the server and shows `setup` and
  `missing_tools`.

## 1.17.1 — 2026-11-03
Second review round:
- Path translation ends a path at any character that cannot be part of a name, so `cd /path;ls`,
  `PATH=/path:x` and Tcl `{/path}` are translated too.
- AI guard also catches `bash -lc` / `sh -xc`, `ms log run -- vivado`, `ms sem NAME -- vivado` (they run here, now),
  `exec -a`, `find -exec`, `script -c`, `parallel`, `systemd-run`, and `V=vivado; $V ...`. A variable is blocked
  in ssh/rsync only in the host position (`rsync -a $SRC/ backup/` is fine).
- Config comments for AI_HEAVY_VIA_QUEUE / AI_PROCESS_NAMES fixed.

## 1.17.0 — 2026-11-03
Fixes from an independent review of 1.12-1.16:
- Server path translation is exact: a path is replaced only when followed by `/`, a quote, a space or the line
  end (`/home/u/proj` no longer touches `/home/u/proj_tmp`; before, `rm -rf /home/u/proj_tmp/*` could become
  `rm -rf /*` on the node). The command is written to a file in the job folder and translated there, so quoted
  paths work and the real node path is used (no shell variable in the command).
- Only real text files are translated (no NUL byte anywhere, at most 8 MB, not links); binary results are never
  changed. With a container the job folder maps to /mnt/run (MSQ_WORKDIR passed in); `~/` path_map entries are
  refused there.
- `links` must start with your own folder (`~/ACCOUNT/...`).
- The folder scan before sending a job skips build/result trees and stops after 30 000 entries; the cheap checks
  (max_jobs, server status) run first.
- AI guard: a quote-aware command parser (new mysys/cmdparse.py) follows `bash -c`, `eval`, `$(...)`, wrappers
  (timeout -s, flock, xargs, unbuffer, env, nice ...), docker/podman, `python -c`, `cd dir && ./script`, `make -C`.
  No more false blocks for `ms q add -- "cd hw && vivado ..."`, quoted text, or scripts that only call `ms q add`.
  ssh checks also see `-oProxyJump=`, `-J` and hosts in variables.
- Only the user changes settings: `"ms" tools ...` and `python3 -m mysys.versions|pause` are blocked, and
  saving tool access or pausing refuses when an AI assistant started the process (AI_PROCESS_NAMES).
- Tool versions: a saved empty default now means "nothing allowed" (was "all"); `ms tools deny` works on a
  project that allowed everything; the GUI keeps rule entries for versions that are not installed.
- Version checks judge what runs (programs, sourced settings files, PATH/XILINX values, the script a shell runs),
  not every file name: `ls .../2023.2` or `python3 cmp.py vivado_2023.2_ref/` are no longer refused or stopped.
- A job refused for its tool version no longer sends an "approval needed" message.

## 1.16.0 — 2026-11-03
- Paths on the server: `path_map = LOCAL => SERVER` and `map_job_dir = yes` in remotes.conf. On the node the
  command and the job folder's text files are rewritten (the job folder's absolute path -> its folder there,
  path_map entries -> their server paths) with time stamps kept, so nothing comes back changed; files the job
  writes are translated back before they are copied home. Your files here are never modified.
- Jobs that use absolute paths that exist only here (scripts/configs pointing to other folders under /home, /mnt,
  /data, /tools, /opt ...) are not sent automatically; `ms q explain` names the path and the fix.
- `ms q explain` says exactly why a job is not sent to a server (secret, missing program, tools, paths, ...).
- AI_RULES rule 12 and the system map explain the translation and how to write jobs that run anywhere.
- docs/remote-servers.md: everything learned connecting Titan and installing Vivado/Vitis there (SMB share is
  case-insensitive, per-node homes, Java ignores HOME, checksum retries, success line, timing, cleanup);
  docs/install_retry.sh: the batch installer with retries.

## 1.15.1 — 2026-11-03
- remotes.conf `links = ~/ACCOUNT/NAME -> /target`: a link made on each node before a job (only inside your
  home and with your account name in the path; an existing file or link is never replaced). Used for Titan, where
  each node has its own home: ~/benyamin/xilinx -> /mnt/perflab/benyamin/xilinx.
- remotes.conf `setup =`: shell lines run before every job command on the server (e.g. source settings64.sh).

## 1.15.0 — 2026-11-03
- AI assistants must start Vivado, Vitis (vitis-run, v++), ISE (xtclsh, xst, par, ...) and Quartus through the
  queue: the guard blocks running them directly, and also shell/Tcl/Python scripts and Makefiles that start them.
  `ms q add` / `ms flow run` / queue_submit stay allowed, and so do quick `-version` / `-help` calls.
  AI_HEAVY_VIA_QUEUE=0 turns this off.
- The watcher tells you (once per project, at most every 6 h) when an AI assistant runs such tools outside the
  queue. Nothing is stopped.
- AI_RULES rule 8: sweeps that start several tool processes must declare their cores/memory or be a flow.

## 1.14.0 — 2026-11-03
- Tool versions per project: state/tool_access.json holds a default list and per-project lists of allowed
  AMD/Xilinx versions (e.g. default 2025.2, ~/Desktop/MCGEMMA 2025.2 + ISE 14.7). A project's rule covers its
  subfolders. Before the file exists, XILINX_ALLOWED is the default.
- New GUI page Protection > Tool access: one row per project, one tick box per installed version, "Follow default",
  "Undo changes", "Save"; projects with their own rule are listed first.
- `ms tools` shows the rules; `ms tools allow|deny|default|reset` change them (AI assistants are blocked from it).
- Queue jobs, the job runner and the AI guard judge versions by the job's / the AI's folder; the system map lists
  the rules per project.

## 1.13.2 — 2026-11-03
- A job whose command names a tool version that is not allowed is refused at once by `ms q add` / `queue_submit`
  (it no longer waits in the queue for CPU/RAM first). Jobs that reach the version only through a script are
  still stopped within seconds of starting.

## 1.13.1 — 2026-11-03
- `ms q show|log|cancel|... NAME` accepts a job name (the newest job with that name) as well as an id, with a clear
  message instead of a traceback when nothing matches.

## 1.13.0 — 2026-11-03
- One AMD/Xilinx tool version: `XILINX_ALLOWED="2025.2"` in my_system.conf allows only that version and its updates
  (2025.2.1). Any other installed Vivado, Vitis, Vitis HLS, ISE or SDK version is not allowed:
  - a queue job whose command names it does not start; a job that runs it anyway (e.g. from a script) is stopped
    within seconds, with the reason in `ms q show`;
  - the AI guard blocks shell commands that name it;
  - AI_RULES rule 13 and the system map tell AI assistants which version to use.

## 1.12.2 — 2026-11-03
- Live job logs: output is written to the log at least every second (and every few seconds for quiet jobs), so
  `ms q log ID` and the GUI show it while the job runs. Before, short outputs stayed in a buffer until the job ended.

## 1.12.1 — 2026-11-02
- AI assistants learn the server rules: AI_RULES rule 12 rewritten (the queue decides where a job runs; never
  ssh/sbatch yourself; self-contained job folders; `--on here` / `--on NAME`), and the system map gets a
  "Remote servers" section built from remotes.conf (mode, allowed jobs, missing programs, limits).
- The AI guard blocks Slurm commands (sbatch, srun, salloc, scancel, scontrol) and ssh/scp/rsync/sftp to the
  configured servers (aliases and their real host names from ~/.ssh/config).
- `--on here` (and `on: "here"` in queue_submit) keeps a job on this machine even with offload = prefer.
- `ms lock` regenerates the AI system map, so changed settings reach AI assistants at once.

## 1.12.0 — 2026-11-02
- Server first: `offload = prefer` in remotes.conf sends every allowed queue job to the server before this machine
  is tried. The job runs here instead when the server is full (no idle CPUs), unreachable, already runs `max_jobs`
  of your jobs, or has measurably been slower for this kind of job (median of earlier runs there vs here, upload and
  Slurm wait included).
- `tools = *` allows every job; `missing_tools = vivado vitis ...` keeps jobs that use programs the server lacks here.
- Safety net: a job sent to a server automatically that fails there runs once more here before it counts as failed.
- `ms q explain` shows whether a waiting job will go to the server first.

## 1.11.3 — 2026-11-02

Shared server safety (one Unix account for everyone on Titan)
- my-system only touches its own work folder (must be relative, contain 'msq' and your account — anything else is
  refused), its own temporary folder on the node, and its own Slurm jobs (`scancel --account=you JOBID`).
- It deletes only job folders it created (`<workdir>/<id>-<attempt>`), never anything else; the node script refuses
  to run if its temporary folder could not be created.
- `ms remote clean NAME [--days 7]`: remove your own old job folders (kept after failed jobs).
- Tested: other people's jobs, results and folders on a simulated shared server stay untouched.

## 1.11.2 — 2026-11-02

- Remote Slurm servers whose nodes report no RAM (Titan): jobs are sent without --mem (it is refused there).
  Detected automatically (`slurm_mem = auto|yes|no`); `ms remote check` explains it.

## 1.11.1 — 2026-11-02

- `ms remote check`: clear fixes for an unknown server key (accept it once), a changed server key (stop), a key the
  server does not accept yet, and an unreachable server (UGent VPN).

## 1.11.0 — 2026-11-01

- Pause parts of my-system for a while or until you turn them on again: `ms pause FEATURE|all [--for 2h | --until 08:00]`,
  `ms resume FEATURE|all`, `ms paused`; in the app: Settings → "Pause parts of my-system".
  Features: notifications (still logged), phone, status-report, watcher, daily, backup (the nightly one; "Back up now"
  and `ms backup` still run), queue (running jobs continue, nothing new starts), remote (jobs wait here).
- What is paused shows in the Overview's attention list, the phone status report, and the sidebar.
- Only you can pause: the AI guard blocks `ms pause`, `ms resume`, `ms lock` and `ms unlock` for assistants.
  The AI guard itself cannot be paused.

## 1.10.1 — 2026-10-31

- Phone: only what needs you (failures, warnings, disk/RAM/crash alerts) plus the status report (08:00 13:00 19:00
  and on request) and the weekly summary. Successful batches stay on the desktop, in the app and in the status report.
  PHONE_MIN_PRIO=low brings them back to the phone.
- One summary per sweep: tool folders inside a design (V1009/prj/sol/sim/verilog …) count as the design, so a sweep of
  V1009…V1012 gives one "Finished: 8 runs (vitis_hls, xelab) in extra_int32_guided/" instead of four "… in sim/".
- Remote jobs: two jobs in the same folder could mix up their exit codes and logs (control files and the node log are
  now per job: msq-<id>.log).

## 1.10.0 — 2026-10-31

Remote servers (Slurm, e.g. Titan at UGent CSL PerfLab)
- `config/remotes.conf` + `ms remote setup|list|check|test|status NAME`. `check` goes step by step (ssh config, key login,
  Slurm commands, account, work folder, nodes) and says how to fix each step; `test` also sends a 1-minute job that
  reports what the nodes offer (CPUs, RAM, Vivado/Vitis/xsim, docker images, /mnt/perflab).
- `ms q add --on titan -- CMD` runs a job there; `--on auto` runs it here, or there when this machine is busy
  (offload = busy, after offload_after_min) or too small for it. Flows: `remote: titan | auto`. MCP: `on`.
- How it runs: the job folder (or only its declared inputs) is packed and copied to the head node, `sbatch` starts
  it with your account, cores, memory and time limit; on the node it fetches the files (or uses a shared disk), runs
  (optionally in a docker image), sends its log back every minute and its changed files at the end. Here it is a
  normal queue job: live log, run log, $MSQ_OUTPUTS, notifications, phone report and app ("on titan (node07)");
  cancelling it cancels the Slurm job; results land in the job's folder.
- Safety: jobs with secrets are never sent; no password is stored or used (only your ssh key); folders above
  max_upload_gb must declare inputs; at most max_jobs at once.
- App: "Remote servers" card in Settings (check, test, status).
- Tested end-to-end against a simulated Slurm cluster (shared disk and scp-only nodes, cancel, limits, offload).

Fix
- Cancelling a running job no longer kills its runner first: the runner finishes the log and cleans up.

## 1.9.0 — 2026-10-31

App
- New Overview: one honest sentence about the machine (All good / Needs a look / Problem) with what needs attention;
  tiles for CPU, memory, temperature, disks, network share and backup with levels that match reality (128% CPU or a
  root disk heading for full are no longer green); one activity table with what runs (time, RAM, time left), what
  waits and why, and the last finished runs; and the specification of this computer (processor, memory and modules,
  graphics, disks, system, swap, uptime).
- Helper tools of one design (vitis-run, vitis_hls, xelab in its subfolders) are one row, named after the design.
- Sidebar grouped (Work, This computer, Protection, Messages & settings), compact, scrolls when the window is small.
- Footer without the folder path.

Phone report
- Finished runs are named after the design folder ("extra_int32_guided/V1011 (vitis_hls)", not "sim/wrapc_pc");
  helper tools of one design finishing together are one entry.

## 1.8.0 — 2026-10-31

From the real machine's data and an independent code review of 1.5.3–1.7.1.

Safety
- AI guard: the 1.5.3 heredoc change let `cat <<EOF | bash`, `timeout 60 bash <<EOF`, and "write a script, then run
  it" through unchecked. A heredoc body is now skipped only when it is plainly written to a file that nothing in the
  command runs.
- Watcher could hang on a dead network share (path resolution touched the mount): plain path strings only.

Queue
- Room kept for a long-waiting job blocked the whole queue when the missing cores were used by programs outside the
  queue (your own Vivado runs). It now only applies when queue jobs hold what is missing.
- CPU patience (QUEUE_CPU_PATIENCE_MIN=60): a job waiting for cores while its RAM is free starts once half its cores
  are free (it runs niced). Before, a 12-core request could wait all day on a busy machine.
- One combined message for jobs that waited a long time (QUEUE_STUCK_NOTIFY_H=2) with what would let them start
  (`ms q edit ID --cores N`); jobs that wait on purpose (dependencies, start time, groups, licenses) are left out.
- Tool durations that vary widely (2-minute Vivado builds and 6-hour simulations) are no longer used for
  `timeout: auto`, "slower than usual", "may be stuck", deadlines and reserved windows. Before, `timeout: auto` would
  have killed long simulations after 30 minutes.
- Pressure: only sustained stalls count (10 s and 1 min averages), 0 disables, cached/no-resource jobs still pass, and
  after QUEUE_IO_PATIENCE_MIN of disk pressure jobs start again one every 5 minutes. A job paused for thrashing resumes
  only when the thrashing is over (no pause/resume flip-flop); pause notices at most every 30 min per job.
- Out of memory is recognised from the kernel's OOM counter (job scope or its parent), so a manual `kill -9` is no
  longer retried as out of memory. CPU pinning never locks a job onto fewer threads than it needs.

Notifications
- Batch summaries wait while the program driving the batch still runs (e.g. a launcher in the project folder):
  one summary instead of "Finished: 2 runs" every 25 minutes; at the latest a progress summary every 6 h.
- Runs directly in a project folder are named after the project, not after your home folder ("benyamin/").
- Repeated identical failures in one batch: one alert per kind of error; the summary lists them ("[Place 30-58] … ×4").
- Phone report: "usually X" comes from earlier runs in the same batch folder.

Other
- `ms log compare` no longer crashes on metrics that mix numbers and text.
- Restore test: "unchanged since the backup" uses ctime (mv, cp -p, rsync -t keep old mtimes).
- Vulnerability audit never sends the names of local/editable packages or of packages named like your projects.
- Crash detection: a service restart earlier in the day no longer counts as a clean shutdown.
- Faster: duration index and batch keys are cached (the phone report took ~1 s per tracked run).

## 1.7.1 — 2026-10-31

- No emoji in phone messages: the status report uses plain headings and dashes, and notifications no longer send
  ntfy tags (the ntfy app showed each tag as an emoji).

## 1.7.0 — 2026-10-31

- Status report for the phone (`ms brief`, `ms brief --send`): how the machine is (CPU busy %, temperature, free RAM,
  disk, backup), what needs attention, what is running (queue jobs and Vivado runs started outside the queue, with
  run time, progress, time left and RAM), what is waiting and why, and the last 3 finished runs with result, duration
  and WNS. Plain text with short lines, readable in the ntfy app.
- Sent at PHONE_REPORT_TIMES (default 08:00 13:00 19:00; quiet "low" priority unless something needs attention),
  and on request: type `status` (or `?`) as a message in your ntfy topic and the report arrives within a minute.
- Notifications page: "Preview" and "Send status report now".

## 1.6.0 — 2026-10-31

Third comparison with similar projects (pueue, below/atop, earlyoom/systemd-oomd, healthchecks.io, borgmatic,
osv-scanner/pip-audit, needrestart, DVC, HTCondor, Slurm backfill, Jenkins/Buildbot flaky tests).

Queue and flows
- Pressure-aware starts: no new jobs while the kernel reports memory or disk stalls (PSI); thrashing pauses a job.
- No starvation: a job that waited longer than QUEUE_STARVE_MIN gets room kept free; only jobs that usually finish
  within QUEUE_BACKFILL_MIN may still use the gap (Slurm backfill).
- Groups with their own limit: `--group NAME`, `config/groups.conf`, `ms q groups`, `ms q hold|release|cancel --group`.
- `ms q explain ID`: every start condition with pass/fail and a suggestion (HTCondor -better-analyze). Also in the app
  ("Why waiting?") and for AI assistants (MCP `queue_explain`).
- `ms q edit ID …` changes a waiting job (command, memory, cores, priority, name, timeout, group, env).
- `ms q send ID TEXT` types a line into a running job started with `--stdin`.
- Labels (`--label`) and filters: `ms q list --state S --name RE --label L --group G --json`.
- Queue jobs get oom_score_adj 300: if RAM runs out, the kernel kills them before your desktop/interactive tools.
- Optional CPU pinning (QUEUE_PIN_CPUS=1): each job gets its own physical cores.
- Flaky jobs (failed, then passed on retry unchanged) are marked and listed (`ms q flaky`).
- "Slower than usual" notification when a job runs QUEUE_SLOW_FACTOR × its usual time.
- Flow race: `stop_when: "wns >= 0"` (+ `stop_after: N`) stops the remaining variants once enough reached the goal;
  stopped variants do not count as failures and downstream steps still run.

Monitoring
- Minute-by-minute history (load, RAM, swap, temperature, PSI, biggest programs, jobs): `ms replay TIME`, History page.
- Report after an unexpected stop (power loss, freeze, out-of-memory hang) with the likely cause and what was running.
- Warnings: RAM almost exhausted (with the biggest programs), thrashing, data disk / network share not mounted or
  not answering (checked without ever blocking the watcher), and when it is back.
- Dead-man's switch: PING_WATCH / PING_DAILY / PING_BACKUP / PING_VERIFY (healthchecks.io-style start/ok/fail pings).

Software, backup, run logs
- `ms software audit`: Python packages vs. the OSV vulnerability database, with fixed version and affected projects
  (weekly from the daily job; shown in the update advice, the daily check and the app).
- Programs whose executable file was replaced by an update are listed for restart (like needrestart).
- Restore test: spot check where unchanged files must match exactly, file-count drop check, full repository check
  every BACKUP_REPO_CHECK_DAYS.
- `ms log compare` / `ms log diff` (DVC exp show / exp diff) and "Compare runs" in the app.
- `ms housekeeping` (daily): rotates notifications.log, trims AI audit logs, metrics and leftovers of cleared jobs.

Fixes
- Overview page: the status line ("All good" / "N things need attention") failed to update.
- A cancelled running job now always gets an end time; cancel keeps its reason.

## 1.5.3 — 2026-10-31

- Queue: a job killed by the kernel at its memory limit is now reported as "out of memory" (and retried with more RAM
  when retries are on). Before, the cgroup was already removed when the runner looked, so it showed only "exit code -9".
- Watcher: the "CPU overloaded" advice now counts how many threads each job really wants (load ÷ jobs) and never
  suggests more parallel jobs than are running; it also suggests fewer threads per job as an alternative.
- AI guard: text inside a heredoc that only writes a file (`cat > x.py <<'E' … E`) no longer trips the kill/sudo/reboot
  rules. Heredocs fed to a shell or interpreter (`bash <<EOF`, `python3 - <<EOF`) are still checked.

## 1.5.2 — 2026-10-31

- `ms selftest` and the unit tests work from the locked (read-only) installation: temporary copies are made writable and removed afterwards.

## 1.5.1 — 2026-10-31

- `ms selftest`: read the MCP registration from ~/.claude.json instead of `claude mcp list` (which can hang); a failing installation check no longer stops the test.

## 1.5.0 — 2026-10-31

Second, documentation-based comparison with GNU Parallel, task-spooler, Slurm, Snakemake, Nextflow, Dagu, evQueue,
Jenkins and Buildbot — everything that makes sense on one workstation is now in.

**Resources and scheduling** — named resources and license seats (`config/resources.conf`, optional lmstat check),
exclusive locks for boards/cables, per-tool caps (`tool:vivado N`), `ms sem` semaphores, `$MSQ_SLOT`, disk
reservations, reservation shrinking after a job settles, requests clamped to the machine, threads kept for the
desktop, reserved time windows (`config/reservations.conf`), start delay with automatic backoff, no starts while
swapping or while the disk is saturated, begin/deadline times, collapse of duplicate requests, move to front, ETA.

**Failures** — retry delays/backoff/schedules, retry only on chosen exit codes, retry with more memory (from the
measured peak) or more time, inactivity timeout, timeout as % of the usual duration, signal before the time limit,
configurable kill sequence, time-limit alerts, *warning* state (exit-code mapping, warn patterns, failed tests),
after-failure dependencies, approvals, requeue, wait, clone/run again, preemption exempt time.

**Flows** — outputs passed between steps (`$MSQ_OUTPUTS`, `{{steps.X.outputs.K}}`, metrics), `when` conditions,
`assert`, runtime fan-out (`foreach`), sub-flows, wait and repeat-until steps, matrix exclude/filter/zip/CSV,
`--sample`, `--until/--from/--only/--force`, `touch`, `summary`/`why` (what changed), rerun triggers and cache modes,
shared cache between flows, on_error terminate/finish/ignore, halt on failure count/percentage, max_parallel per
step, hooks on_fixed/on_regression/on_change/on_cancel/on_exit, notify modes, typed params, profiles, base file,
dotenv, secrets, stub runs, lint.

**Files** — scratch/shadow folders (only declared outputs come back), private `$TMPDIR`, protected outputs,
output checks (non-empty, sha256), incomplete outputs removed, temp files deleted when no longer needed, publish
by parameters, lineage (`ms lineage FILE`).

**Reports** — warning tracking new/fixed/outstanding vs. the previous run with quality gates and an ignore list,
failure knowledge base (built-in Vivado/HLS/system causes + your own), JUnit results, I/O and per-job time series,
benchmarks, queue wait times, flow HTML report with step graph, history search, retention.

**Schedules** — catch-up of missed runs, skip if already successful today, only if something changed, after another
flow, commit triggers with debounce, important paths and remote polling, pause; localhost webhook (`ms q serve`).

**Software management** — `ms software`: apt/snap/flatpak/pip updates, each with who uses it (projects, Python
environments, packages linked against system libraries, processes using it now) and advice: safe now / test after /
after runs / needs reboot; `ms software uses NAME`, `ms software project NAME`, `ms software plan`; Python EOL and
broken environments; programs to restart after updates; new **Software** page; daily scan; MCP tools.

**Self-test** — `ms selftest` runs end-to-end checks in a separate temporary queue (never touches real jobs) plus
all unit tests; results in reports/selftest-*.txt.

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
