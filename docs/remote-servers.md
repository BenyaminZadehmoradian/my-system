# Remote servers (Titan) and installing AMD tools there

What we learned connecting this machine to **Titan** (UGent CSL PerfLab, Slurm) and installing Vivado/Vitis
2025.2.1 on it, written down so the next server or the next tool version takes an hour instead of a day.
Everything here was observed on Titan in November 2026.

## 1. How Titan is built (and why it matters)

| Fact | Consequence |
|---|---|
| You log in to the head node `bacchus.ugent.be` as the **shared** account `slurmslave`. Many people use it. | Never write outside your own folders; never `rm` something you did not create (check creation times with `ls -la --time-style=full-iso`). |
| Your Slurm account (`sacctmgr show assoc user=slurmslave`) is your own login, e.g. `benyamin`. | `remotes.conf: account = benyamin`. `scancel` only with `--account=benyamin`. |
| 16 nodes × 72 CPUs; nodes report no RAM to Slurm. | `--mem` is refused: `slurm_mem = auto` leaves it out. |
| **Each node has its own local `/home/slurmslave`** (titan02: `/dev/sda6`, 885 GB), different from bacchus' home (11 TB LVM). | A program installed in bacchus' home is invisible to jobs. |
| The only folder all nodes and bacchus share is **`/mnt/perflab`** = the UGent SMB share `//files.ugent.be/.../ea06csl/perflab`. On this machine the same share is `/mnt/ea06csl/perflab`. | Shared tools go there, in your own folder: `/mnt/perflab/benyamin/`. |
| The SMB share is **case-insensitive** (`CaseTest` and `casetest` are one file) but supports symlinks and long paths. | AMD installers fail on it (see 3). Install elsewhere, then copy. |
| bacchus has internet (`curl -sI https://www.xilinx.com` answers). | The 350 MB web installer can download only what is needed. |
| Nodes run Ubuntu 24.04 (glibc 2.39). | Vivado/Vitis 2025.2 support it. `installLibs.sh` needs root: ask the admin if a library is missing. |

## 2. Connecting (one time)

1. Key: `ssh-keygen -t ed25519 -f ~/.ssh/titan_ed25519 -C "benyamin titan"`; send the **.pub** file to the admin.
2. `~/.ssh/config`:
   ```
   Host titan
     HostName bacchus.ugent.be
     User slurmslave
     IdentityFile ~/.ssh/titan_ed25519
     IdentitiesOnly yes
   ```
3. First connection must accept the host key once: `ssh -o StrictHostKeyChecking=accept-new titan hostname`
   (with `BatchMode=yes` alone it fails with "Host key verification failed").
4. `ms unlock`, edit `~/My_System/config/remotes.conf`, `ms lock`, then `ms remote check titan` and
   `ms remote test titan`.

## 3. Installing Vivado/Vitis on the server — the recipe that works

**Do not install directly on the SMB share.** We tried three times; every time the installer stopped at ~23 %
on `sdk_gnu_0019_*.xz` ("the disk is full, you've exceeded disk quota, or the destination directory is too long").
The real cause: the GNU cross toolchain contains files whose names differ only in upper/lower case, and the share is
case-insensitive. `xz -t` on the payload files proves nothing (AMD's `.xz` files are not plain xz).

Also learned:
- The installer is Java and ignores `HOME=`: it writes `~/.Xilinx` (token, install_config) into the shared
  account's home. Use `_JAVA_OPTIONS=-Duser.home=/mnt/perflab/benyamin/installers/home` on **every** `xsetup` call.
  Even then it creates `~/.Xilinx/xinstall` logs in the real home — clean up afterwards.
- In batch mode a download with a bad checksum ends the whole install ("user did not want to retry"). Downloaded
  files are checksum-verified, so re-running continues; the script below retries.
- The success line is **"Tool installation completed."** (not "Installation completed successfully").
- Product **2 (Vivado)** has no HLS (`vitis-run`). HLS needs product **1 (Vitis)**.
- Typing the AMD password goes only into the `xsetup -b AuthTokenGen` prompt — never at a shell prompt (if it
  happens: change the password, `sed -i '/^THE-LINE/d' ~/.bash_history; history -c; history -r`).
- Timing on Titan: download ~28 GB in ~10 min, install 2 h 11 min (local disk), copy to the share ~3–4 h.

### Steps (run from this machine; nothing outside your own folders is touched)

```
# 0. download the Linux web installer (AMD account) to ~/Downloads, then:
mkdir -p /mnt/ea06csl/perflab/benyamin/installers
cp ~/Downloads/FPGAs_AdaptiveSoCs_Unified_SDI_2025.2.1_0320_0604_Lin64.bin /mnt/ea06csl/perflab/benyamin/installers/

# 1. unpack on bacchus
ssh titan "cd /mnt/perflab/benyamin/installers && bash FPGAs_*_Lin64.bin --keep --noexec --target unified_2025.2.1"

# 2. login token (type email and password at the prompt; the token is valid 7 days)
ssh -t titan "mkdir -p /mnt/perflab/benyamin/installers/home && cd /mnt/perflab/benyamin/installers/unified_2025.2.1 && \
  _JAVA_OPTIONS=-Duser.home=/mnt/perflab/benyamin/installers/home ./xsetup -b AuthTokenGen"

# 3. config: choose 1 (Vitis); then edit it
ssh -t titan "cd /mnt/perflab/benyamin/installers/unified_2025.2.1 && \
  _JAVA_OPTIONS=-Duser.home=/mnt/perflab/benyamin/installers/home ./xsetup -b ConfigGen"
#    In .../installers/home/.Xilinx/install_config.txt set:
#      Destination=/home/slurmslave/benyamin/xilinx          (bacchus' local disk, your own folder)
#      only the devices you need :1 (Zynq-7000 All Programmable SoC:1), DocNav:0
#      CreateProgramGroupShortcuts=0 CreateDesktopShortcuts=0 CreateFileAssociation=0

# 4. install with retries (in the background; survives a closed ssh)
#    script: docs/install_retry.sh (copy it to .../installers/ first)
ssh titan "nohup bash /mnt/perflab/benyamin/installers/install_retry.sh > /dev/null 2>&1 < /dev/null & echo started"
ssh titan "cat /mnt/perflab/benyamin/installers/attempts.log; tail -c 300 /mnt/perflab/benyamin/installers/install.log"

# 5. check on bacchus
ssh titan "source /home/slurmslave/benyamin/xilinx/2025.2.1/Vivado/settings64.sh && vivado -version | head -2"

# 6. copy to the share (a few dozen case-only duplicates in gnu/ fail; HLS/Vivado do not use them).
#    -p keeps the execute bits: without it (rsync -rlt) the programs may not be executable on the share
ssh titan "nohup rsync -rltp /home/slurmslave/benyamin/xilinx/2025.2.1 /mnt/perflab/benyamin/xilinx/ \
  > /mnt/perflab/benyamin/installers/rsync.log 2>&1 < /dev/null & echo started"

# 7. clean up: the login token, the stray ~/.Xilinx (check it is yours and from today first)
ssh titan "rm -f /mnt/perflab/benyamin/installers/home/.Xilinx/wi_authentication_key"
ssh titan "ls -la --time-style=full-iso /home/slurmslave/.Xilinx"   # then remove it only if all of it is from today
```

Mind `A && B && C &` in a shell: the `&` puts the **whole chain** in the background (a slow `rm -rf` on the
share then delays the copy behind it, and `rsync.log` appears only later).

### Making the nodes find it (remotes.conf)

```
links = ~/benyamin/xilinx -> /mnt/perflab/benyamin/xilinx
path_map = /mnt/data/Apps/2025.2.1 => ~/benyamin/xilinx/2025.2.1
setup = source ~/benyamin/xilinx/2025.2.1/Vitis/settings64.sh
```
- `setup`: source **Vitis**' settings64.sh, not Vivado's — Vitis' sets up Vivado too, while Vivado's alone leaves
  `vitis-run` / `v++` (HLS) off the PATH.
- `links`: before each job, on the node, `~/benyamin/xilinx` becomes a link to the share (only inside your own
  folder; nothing existing is replaced). The tools then live at exactly the path they were installed to, so no
  path inside the installation needs changing. On bacchus the real installation is at that path anyway.
- `path_map`: scripts that use this machine's Vivado path keep working there (rewritten on the node only).
- Remove `vivado vitis vitis_hls ...` from `missing_tools` only after `ms remote tools titan` says they work on a
  node (it prints the new `missing_tools` line).

### When the tools do not work there

First run `bash tests/live/titan_diagnose.sh titan` (works without my-system 1.18; really synthesizes with Vivado and
HLS on a node), then, with 1.18 installed, `python3 tests/live/live_titan.py titan`. See tests/live/README.md.

#### `ms remote tools titan`

It sends a job that starts Vivado/Vitis on a node exactly like a real job (setup, links, path_map) and reports:

| It says | Cause and fix |
|---|---|
| `XILINX_VIVADO and XILINX_VITIS are empty` | `setup` is empty or sources a file that does not exist on the node; check the `link` line it prints. |
| `link …: a real folder/file is there` | Something already sits at `~/benyamin/xilinx` on that node, so the link to the share was not made. Remove it there if it is yours. |
| `link … TARGET NOT FOUND` | `/mnt/perflab` is not mounted on that node, or the copy to the share is incomplete. |
| `… is executable — NO` | The copy lost the execute bits: copy again with `rsync -rltp`. |
| `missing: libXYZ.so` | A library is missing on the nodes: ask the admins, or put the `.so` in `~/benyamin/lib` (on the share) and add `export LD_LIBRARY_PATH=…` to `setup`. |
| `HLS (vitis-run / v++) is on PATH — NO` | Only Vivado's settings were sourced: use `Vitis/settings64.sh`. |
| `no 'vitis_hls' command there` | Newer Vitis has `vitis-run --mode hls` / `v++ -c --mode hls` instead; change those jobs or keep `vitis_hls` in `missing_tools`. |
| `no libc6-dev` | HLS C simulation cannot link on the nodes; csynth works. |
| `device families installed there` | Only these parts can be used there (the install chose Zynq-7000 only); other parts fail with "part not found". |

A job that stops before writing its log (exit 90/91/92) is now reported with the reason; exit 92 almost always
means the node cannot `scp` from bacchus (each node has its own `~/.ssh`). The locale sent along by ssh is replaced
by `C.UTF-8` on the node when it does not exist there (Vivado aborts on an unknown locale).

## 4. How a job runs there (my-system)

1. The job folder (minus `exclude`) is packed and copied to `~/jobs/msq/benyamin/<id>-<attempt>` on bacchus;
   `sbatch` starts the job script on a node.
2. On the node: links are made, files are fetched (scp from bacchus; nodes do not share bacchus' home), paths
   are rewritten (job folder, `path_map`; time stamps kept), `setup` runs, then the command.
3. The log is copied back every minute (`ms q log ID`); at the end only files the job wrote come back, with
   server paths translated back to local ones. The job folder on bacchus is removed after success.
4. A job sent automatically that fails there runs once more here. Jobs that use files that exist only here
   (absolute paths outside the job folder and not in `path_map`) are not sent: `ms q explain ID` says which.

## 5. Safety rules that stay in force
- Only your own folders: `jobs/msq/benyamin/...`, `/mnt/perflab/benyamin/...`, `~/benyamin/...` on nodes.
- Only your own jobs: `scancel --account=benyamin ID`, `ms remote clean` removes only my-system's job folders.
- AI assistants cannot ssh/scp/sbatch to the server (guard); they submit through the queue.
- Nothing secret travels: `--secret` jobs are never sent.
