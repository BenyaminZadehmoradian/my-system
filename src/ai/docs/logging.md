# Run logs on this machine — details

Format: `~/My_System/runlog/RUNLOG_SPEC.md` (`ms log template spec`). One file `msrun.jsonl` per run folder;
my-system reads these files for the app (Run logs, Results), comparisons and notifications.

## Queue jobs (the normal case)
The queue writes the log. The job only prints `::ms ...` lines (see the rules, section 3). On the server the
lines travel back with the job's output, so the same script works in both places.

| Line | Meaning |
|---|---|
| `::ms stage NAME` | begins stage NAME and ends the one before |
| `::ms metric NAME VALUE [UNIT]` | a result; VALUE is a number, `true`/`false`, or text. `NAME=VALUE` works too |
| `::ms progress PCT [TEXT]` | 0–100. (`echo PCT >&3` also works, here and on the server) |
| `::ms artifact PATH [KIND]` | KIND: report, bitstream, checkpoint, netlist, plot, data, log, other (guessed from the file name if left out) |
| `::ms info TEXT` / `::ms warn TEXT` / `::ms error TEXT` | a message with its level |

Also: `NAME=VALUE` lines written to the file `$MSQ_OUTPUTS` become metrics and are passed to later flow steps.
A line my-system cannot understand becomes a warning in the log; it never stops the job.

Metric names: snake_case with the unit as suffix — `wns_ns`, `tns_ns`, `fmax_mhz`, `lut`, `ff`, `bram`, `dsp`,
`power_w`, `energy_uj`, `latency_cycles`, `runtime_s`, `accuracy_pct`, `passed` (true/false). Use the same name
for the same thing in every project, so runs can be compared.

Required results: `ms q add --metrics wns_ns,lut -- ...` or in a flow step `metrics: [wns_ns, lut]`.

## A whole script as an example
```bash
#!/bin/bash
set -euo pipefail                      # a failed step ends the job with a non-zero exit code
echo "::ms stage hls"
vitis-run --mode hls --tcl hls.tcl
echo "::ms metric latency_cycles $(grep -oP 'latency=\K\d+' hls.rpt)"
echo "::ms stage impl"
vivado -mode batch -source impl.tcl -nojournal
echo "::ms metric wns_ns $(awk '/WNS/{print $2}' timing.txt) ns"
echo "::ms artifact timing.txt report"
```

## Programs that manage their own runs (not queue jobs)
- Python: `ms log template python > msrun.py`, then `with Run("name", params={...}) as r: r.stage("synth"); r.metric("lut", 41233)`
- Tcl: `ms log template tcl > msrun.tcl`, then `msrun::start name {seed 3}` … `msrun::metric lut 41233` … `msrun::end ok`
- Any command: `ms log run --name NAME -- CMD`
Do not use these *inside* a queue job: the job already has its log, and a second one shows up as a second run.

## Reading
`ms log list` · `ms log show FOLDER` · `ms log check FOLDER` · `ms log compare FOLDER --sort wns_ns --desc` ·
`ms log diff RUN_A RUN_B` · MCP tools `runs_list`, `run_detail`, `runs_compare`.
