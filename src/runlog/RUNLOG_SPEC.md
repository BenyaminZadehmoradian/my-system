# my-system run log — format v1 (`msrun.jsonl`)

Every program, script or tool flow that runs on this machine and produces results writes **one
structured run log** next to its outputs, in addition to whatever human-readable log it prints.
my-system reads these files to show runs, results, failures and metrics in one place.

## File

- Name: **`msrun.jsonl`**, in the run's working folder (one folder per run is best).
  Several runs in the same folder append to the same file; they are told apart by `run`.
- Encoding: UTF-8, **one JSON object per line** (JSON Lines), append-only, flushed after every line.
- Never rewrite or truncate an existing file.

## Every line has

| key  | type   | meaning |
|------|--------|---------|
| `v`  | int    | format version, `1` |
| `ts` | string | ISO-8601 time with UTC offset, e.g. `2026-10-28T14:35:02+01:00` |
| `run`| string | run id, unique: `<name>-<YYYYmmddTHHMMSS>-<4 hex>` |
| `ev` | string | event type: `start`, `stage`, `log`, `metric`, `artifact`, `progress`, `end` |

Extra keys are allowed only when they start with `x_` (e.g. `x_seed`).

## Events

| `ev` | required keys | optional keys |
|---|---|---|
| `start` | `name` (string) | `project`, `cmd`, `cwd`, `host`, `pid`, `params` (object), `git` (`{commit, branch, dirty}`) |
| `stage` | `stage` (string), `state` (`begin` \| `end`) | `status` on end: `ok` \| `failed` \| `skipped` |
| `log` | `level` (`DEBUG` \| `INFO` \| `WARN` \| `ERROR`), `msg` (string) | `stage`, `src` (file:line or tool) |
| `metric` | `name` (snake_case, unit as suffix: `wns_ns`, `lut`, `power_w`, `runtime_s`), `value` (number, string or bool) | `unit`, `stage` |
| `artifact` | `path` (relative to the run folder), `kind` (`report` \| `bitstream` \| `checkpoint` \| `netlist` \| `plot` \| `data` \| `log` \| `other`) | `stage` |
| `progress` | `pct` (0–100) | `msg`, `stage` |
| `end` | `status` (`ok` \| `failed` \| `cancelled`), `seconds` (number) | `rc` (int), `error` (string, required when failed) |

## Rules

1. The first event of a run is `start`; the last is `end`. Exactly one of each per run.
   A run without `end` is shown as *running* (or *died* when its process is gone).
2. `ts` never goes backwards within a run.
3. Every error the program detects is also a `log` event with level `ERROR`.
4. Results that matter (timing, resources, power, accuracy, runtime…) are `metric` events,
   not only text in a report.
5. Stage names are short and stable across runs (`synth`, `place`, `route`, `bitgen`, `sim`, `train`).
6. Paths in `artifact` are relative to the run folder.

## Example

```json
{"v":1,"ts":"2026-10-28T14:35:02+01:00","run":"s18_doe3-20261028T143502-a1f3","ev":"start","name":"s18_doe3","project":"FPGA-bitstream-size","cmd":"vivado -mode batch -source run.tcl","params":{"strategy":"Performance_Explore","seed":3}}
{"v":1,"ts":"2026-10-28T14:35:10+01:00","run":"s18_doe3-20261028T143502-a1f3","ev":"stage","stage":"synth","state":"begin"}
{"v":1,"ts":"2026-10-28T14:52:40+01:00","run":"s18_doe3-20261028T143502-a1f3","ev":"metric","name":"lut","value":41233,"stage":"synth"}
{"v":1,"ts":"2026-10-28T14:52:41+01:00","run":"s18_doe3-20261028T143502-a1f3","ev":"stage","stage":"synth","state":"end","status":"ok"}
{"v":1,"ts":"2026-10-28T15:40:02+01:00","run":"s18_doe3-20261028T143502-a1f3","ev":"metric","name":"wns_ns","value":-0.084,"unit":"ns","stage":"route"}
{"v":1,"ts":"2026-10-28T15:40:03+01:00","run":"s18_doe3-20261028T143502-a1f3","ev":"artifact","path":"reports/timing.rpt","kind":"report"}
{"v":1,"ts":"2026-10-28T15:40:05+01:00","run":"s18_doe3-20261028T143502-a1f3","ev":"end","status":"ok","rc":0,"seconds":3903}
```

## Writing it

### From a queue job: print lines (the normal way)

Every job started with `ms q add` or a flow already has its run log: my-system writes `start`, the measurements
(time, CPU, RAM, I/O), warnings and errors found in the output, and `end`. The job adds its own content by
**printing lines that start with `::ms`** to standard output — in any language, without a library, on this machine
and on a remote server alike:

| Printed line | Becomes |
|---|---|
| `::ms stage NAME` | `stage` begin (and `end` of the stage before) |
| `::ms metric NAME VALUE [UNIT]` · `::ms metric NAME=VALUE` | `metric` (number, `true`/`false` or text) |
| `::ms progress PCT [TEXT]` | `progress` |
| `::ms artifact PATH [KIND]` | `artifact` (kind guessed from the file name when left out) |
| `::ms info TEXT` · `::ms warn TEXT` · `::ms error TEXT` | `log` with that level |

A `::ms` line that cannot be understood becomes a `WARN` in the log and never stops the job. The lines stay in
the job's output. Results a job must report are named when it is queued (`ms q add --metrics wns_ns,lut`, flow key
`metrics:`); a job that ends without them is marked *warning*. `ms log run -- CMD` understands the same lines.

### From a program that is not a queue job: the helpers (do not hand-roll JSON)

- **Python** (stdlib only, copy into the project or import from my-system):
  `ms log template python > msrun.py`
  ```python
  from msrun import Run
  with Run("s18_doe3", params={"seed": 3}) as r:      # writes start; end ok/failed automatically
      r.stage("synth")                                 # begin; ends the previous stage
      r.metric("lut", 41233)
      r.log("WARN", "clock uncertainty high")
      r.artifact("reports/timing.rpt", "report")
  ```
- **Tcl / Vivado / Vitis**: `ms log template tcl > msrun.tcl`, then in the flow:
  ```tcl
  source msrun.tcl
  msrun::start s18_doe3 {seed 3 strategy Performance_Explore}
  msrun::stage synth
  synth_design ...
  msrun::metric lut [get_lut_count]
  msrun::end ok
  ```
- **Any command, any language** (wraps it, records start/end/rc/duration, keeps its output in `msrun.out`):
  `ms log run --name s18_doe3 -- vivado -mode batch -source run.tcl`
- **Shell scripts**: `ms log start NAME [key=value ...]`, `ms log stage NAME`, `ms log msg LEVEL text`,
  `ms log metric NAME VALUE [UNIT]`, `ms log artifact PATH KIND`, `ms log end ok|failed [rc]`
  (the run id is kept in `.msrun_current` in the folder).

## Checking it

`ms log check <folder or file>` validates every line and run against this spec and exits non-zero
on any violation. It also prints *notes* for valid logs that say too little (a long run without stages or
without any result of its own, metric names that are not snake_case, a run that ended ok after errors);
`--strict` makes notes fail too. `ms log show <folder>` prints a readable timeline. The *Run logs* page of the
my-system app lists every run found.
