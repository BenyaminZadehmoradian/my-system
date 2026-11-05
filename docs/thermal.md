# Keeping the CPU below a temperature

`ms thermal setup 80` (sudo) sets up the three layers below; `ms thermal` shows what is in force and the highest
temperature of the last 24 h; the daily check warns when the limits are gone (after a BIOS update, a failed boot
service) or the CPU got hotter than allowed. `ms thermal off` removes it all.

| Layer | What it does | Why it is needed |
|---|---|---|
| thermald trip point (`/etc/thermald/thermal-conf.xml`, LIMIT − 8 °C) | lowers the CPU's power (RAPL) when the package passes the trip point | holds the steady state, whatever program runs |
| `cpu-cool.service` → `/usr/local/sbin/cpu-cool.sh` | PL2 (short turbo bursts) 80 W, `max_perf_pct` 85 % — at every boot and after sleep | the first seconds of a heavy load jumped 10 °C before thermald could react; and a few cores at full turbo voltage run hotter than all cores at a lower clock |
| `MAX_TEMP_C` (my_system.conf) | queue jobs here wait from LIMIT − 5 and pause above LIMIT; they still go to servers | the queue reacts too, and says why a job waits |

Measured on an i7-10700 (Dell Precision 3440, fans at ~880 rpm), 16 busy threads:

| | without | thermald only | all three |
|---|---|---|---|
| first seconds | 83 °C | 83 °C | 77 °C |
| steady | — | 70–72 °C | 70–72 °C |
| 2 busy threads | up to 85 °C | up to 85 °C | 73 °C |
| right after the load ends | — | up to 89 °C | 66 °C |

Cost: about 10–15 % on single-thread work (85 % of the top clock); multi-thread work is limited by the
temperature anyway. A faster fan curve in the BIOS (Thermal Configuration) lowers temperatures without that cost.

thermald's own `intel_pstate` cooling device is left out on purpose: it would set `max_perf_pct` back to 100 after
each cooling step and undo the 85 %.
