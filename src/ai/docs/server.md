# Jobs on the server (Slurm, e.g. Titan) — details

The servers, their limits and what is missing there are in the system map (*Remote servers*). With
`offload = prefer` every allowed queue job goes to the server first and runs on this machine only when the server
is full, unreachable or measurably slower.

- **Where a job runs** is decided when it starts. `ms q explain ID` says where and why; the job log shows the node
  (`[remote] titan02 ...`). `--on here` keeps a job on this machine, `--on NAME` forces a server — only when the
  user asks.
- **What is copied:** the job folder (minus `.git`, `.Xil`, journals), at most `max_upload_gb`; with
  `--inputs GLOB` only those files. Afterwards every file the job wrote or changed comes back (`--outputs GLOB`
  limits it; files over 100 MB stay on the server when a job wrote more than `max_download_gb`).
- **Paths:** in the command and in the text files of the job folder, the folder's own absolute path and every
  `path_map` entry are rewritten on the node, and back in the files the job writes. Your files here are untouched.
  Absolute paths to anything else on this machine do not exist there: the queue keeps such a job here.
- `cd DIR && CMD` (also inside `bash -c '...'`) runs in DIR. `.git` is not copied: do `git pull` before queuing and
  write the commit to a file if the job needs it.
- **Programs** listed as missing there, and devices without a license there, always run here.
- **Limits:** `max_jobs` and `max_cores` (reserved cores of your running jobs together). Smaller reservations =
  more jobs at once. `ms remote usage` lists what jobs of each name really used and what to ask for.
- **Failures:** a job that could not run there (upload, missing program or file) runs here once. A job whose own
  command failed there is *failed*: fix it; it is not run again automatically.
- **Restart of this computer:** jobs on the server continue and are followed again afterwards.
- **Time limit:** `--timeout` becomes Slurm's limit, counted from the start on the node.
- **Secrets:** jobs with `--secret` never go to a server.
