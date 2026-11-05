# Live tests against the real server (Titan)

These send real Slurm jobs to the shared cluster, so `ms selftest` and `tests/run_tests.sh` never run them.
Start them yourself, from this machine, with the VPN on outside the university.

## 1. `titan_diagnose.sh` — why do Vivado/Vitis not work there?

Needs nothing from my-system (works before installing 1.18):

```bash
bash tests/live/titan_diagnose.sh titan
# other values: ACCOUNT=benyamin VER=2025.2.1 PART=xc7z020clg400-1 HEAD=bacchus SHARE=/mnt/perflab
```

One job (4 CPUs, at most 2 h) on a node checks: the share and its mount options, the link
`~/ACCOUNT/xilinx`, ssh/scp from the node to the head node, Vivado's and Vitis' `settings64.sh` (PATH, execute
bits, missing libraries), the locale, starts vivado / vitis-run / v++ / vitis_hls / xelab, and really synthesizes
a small Verilog design and a small HLS function. It ends with a SUMMARY and saves `titan-diagnose-TIME.txt` here.

## 2. `live_titan.py` — does my-system run Vivado/Vitis jobs there end to end?

After installing 1.18 (`./install.sh`):

```bash
python3 tests/live/live_titan.py titan
```

1. `ms remote check` + a plain test job, 2. `ms remote tools`, 3. a Vivado synthesis job through the queue whose
Tcl uses this machine's absolute paths (path translation) and whose report must come back, 4. an HLS job.
Report: `~/My_System/reports/live-titan-TIME.txt`.

Both are also tested against a simulated cluster in `tests/test_v18.py`.
