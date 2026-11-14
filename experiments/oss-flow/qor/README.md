# QoR flow runner

`flow.py` runs one kernel through compile -> write-hdl -> simulate (GHDL) ->
synthesize (Yosys + nextpnr-ecp5, ECP5 um5g-85k) and writes `<out>/metrics.json`.
Standard library only. It sets up the environment itself (`experiments/oss-flow/env.sh` +
`experiments/oss-flow/oss-synth/bin` in PATH) and runs every step under `nice -n 10`.

## Command line

```
build/python3-venv/bin/python flow.py \
  --src <abs path to kernel .c> --out <abs dir> --period <ns> \
  [--timing-models <json>] [--buffer-algorithm fpl22] [--milp-solver cbc] \
  [--back-annotation <json>] [--hdl vhdl] [--no-sim] [--no-synth] \
  [--compile-timeout <s, default 1200>] [--calculate-path-delays]
```

Exit code 0 when `status == "ok"`. The metrics are printed as JSON.

## Module

```python
import sys; sys.path.insert(0, "experiments/oss-flow/qor")
from flow import run_flow
m = run_flow(src, out, period, timing_models=None, buffer_algorithm="fpl22",
             milp_solver="cbc", back_annotation=None, hdl="vhdl", sim=True,
             synth=True, compile_timeout=1200, calculate_path_delays=False,
             write_hdl_timeout=600, sim_timeout=1800, synth_timeout=1800)
```

Other helpers usable on an existing run: `parse_buffers(export_mlir)`,
`parse_est_cp(comp_dir)`, `parse_sim(sim_dir)`, `parse_nextpnr(report_json)`.

## Layout of `<out>`

- `<kernel>.c/.h`: copy of the sources (the flow never runs in `integration-test/`).
- `out/`: Dynamatic output dir (`out/comp`, `out/hdl`, `out/sim`, `out/synth`).
- `logs/<step>.dyn`, `logs/<step>.log`: one frontend script and log per step.
- `metrics.json`, `est_cp.json` (where `est_cp_ns` comes from, and the raw log value).

Each step is a separate `bin/dynamatic --exit-on-failure --run` process in its own
process group; on timeout the whole group is killed. A step that fails within 5 s
(e.g. binary being rebuilt) is retried once after 90 s.
`--back-annotation` is emitted as `compile --back-annotation <json>` only when given.

## metrics.json

`kernel`, `target_period_ns`, `timing_models` (absolute path; the default
`data/components.json` when not given), `buffer_algorithm`, `back_annotation`,
`status` (`ok` or the failed step: `compile`, `write-hdl`, `simulate`,
`synthesize`), `error` (one line), `buffers`, `buffer_slots`, `buffers_by_type`,
`est_cp_ns`, `cp_ns`, `fmax_mhz`, `cycles`, `sim_pass`, `exec_time_ns`, `lut4`
(TRELLIS_COMB), `ff` (TRELLIS_FF), `dsp` (MULT18X18D), `bram` (DP16KD), `wall_s`
(per step), `out_dir`, `comp_dir`, `nextpnr_report`. Missing values are `null`.

- `buffers`, `buffer_slots`, `buffers_by_type`: `buffer` ops in
  `comp/handshake_export.mlir` (`bufferType`, `numSlots`).
- `est_cp_ns` (needs `--calculate-path-delays`, FPGA20/FPL22 only): largest
  `{data,valid,ready}Path{In,Out}_<channel>` arrival time of the MILP re-solve
  with buffers pinned. With CBC the `*_accurate_delays.log` files are wrong:
  `CbcSoluParser` does not clear its map between solves and CBC writes only
  non-zero variables, so zeros keep their value from the first solve. The flow
  therefore reads the re-solve solutions (`comp/cbc_solution_*.sol` whose model
  objective has only path variables); with no CBC files it falls back to the
  logs. By construction `est_cp_ns <= target_period_ns`.
- `cycles`: from the testbench line `Simulation done! Latency = N cycles` in
  `out/sim/report.txt`: clock cycles from reset release to the cycle where the
  joined end/memory-done handshake fires.
- `sim_pass`: `C and VHDL outputs match` in the hls-verifier report.
- `cp_ns` = 1000 / `fmax_mhz`, `fmax_mhz` = `fmax.clk.achieved` of
  `out/synth/nextpnr_report.json`; `exec_time_ns` = `cycles * cp_ns`.
