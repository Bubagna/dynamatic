# Measured latency keys for the ECP5 timing model (14 Nov 2026)

Condensed from the agent's final report, then checked by hand (see "Checked afterwards").
Branch `ecp5-characterization`.
[run] = verified by running, [read] = read from code.

## What changed
A latency map is `path -> bitwidth -> {delay key: cycles}`. Dynamatic picks the cycles of the largest key
that does not exceed the target period and warns when every key exceeds it (`selectLatency`,
`include/dynamatic/Support/TimingModels.h`). The ECP5 model used to copy the keys of the Kintex-7 model
(`muli`: `{"64": {"1.0": 4}}`). Now the key is the longest register-to-register delay measured inside the
unit on the ECP5, per bitwidth. The number of cycles still comes from the reference model.

- Units: every characterized unit with reference latency > 0: muli (4 cycles), load (1), sitofp (5),
  fptosi (5).
- Measurement: the `posedge clk -> posedge clk` critical path of the unit's own clock domain in nextpnr's
  report, all segments included (both flip-flops belong to the unit). On all 84 reports it equals
  `1000 / fmax.clk.achieved` exactly. [run]
- Seeds: worst of the 3 seeds (the key is a lower bound on the period; a unit alone in an empty device is
  the optimistic case). The other delays keep the median.
- Bitwidths: one entry per characterized width, each with the latency of the reference entry Dynamatic
  would pick for that width; reference entries wider than the widest measured width are kept. The
  selected latency for every width 1..64 is unchanged in all 4 units. [run]
- Vivado back end unchanged: the step runs only for `ecp5` with `--reference-json`. [read + dry run]

## Files changed in the worktree
- `tools/backend/synth-characterization/nextpnr_backend.py`: `internal_rpt_filename`, `internal_delay()`
  reads the clk->clk path, `extract_job` writes `rpt_internal_*.txt` and per-seed values in `delays.json`,
  `write_rpt` gets an optional `reduction` label, new `keyed_latency()` and `set_latency_keys()`.
- `tools/backend/synth-characterization/main.py`: calls `set_latency_keys` after `merge_reference_model`;
  help texts of `--seeds` and `--reference-json`.
- `docs/DeveloperGuide/Specs/TimingCharacterization.md`: paragraph on the latency key.
- `data/components-ecp5.json`: latency maps of muli, load, sitofp, fptosi.
- autopep8 not run (not installed); lines under 200 characters, `py_compile` passes.

## Measured register-to-register delays (ns, seeds 1/2/3, key = worst) [run]
| unit | width | seed 1 / 2 / 3 | key |
|---|---|---|---|
| muli | 1 | 1.617 / 1.344 / 1.107 | 1.617 |
| muli | 2 | 4.598 / 4.490 / 4.527 | 4.598 |
| muli | 4 | 4.200 / 4.547 / 4.519 | 4.547 |
| muli | 8 | 4.489 / 4.601 / 4.756 | 4.756 |
| muli | 16 | 4.734 / 4.532 / 4.540 | 4.734 |
| muli | 32 | 7.214 / 7.160 / 7.142 | 7.214 |
| muli | 64 | 10.702 / 11.016 / 10.722 | 11.016 |
| load | 1, 2, 4, 8, 16, 32, 64 | 1.7 to 2.3 | 2.203, 2.032, 2.110, 2.345, 2.088, 2.231, 2.296 |
| sitofp | 32 | 2.069 / 2.222 / 1.877 | 2.222 |
| fptosi | 32 | 1.920 / 1.794 / 1.670 | 1.920 |

muli 32: input flip-flop, MULT18X18D, CCU2C carry chain, `q0` (matches the hand measurement, 6.94-7.15 ns).
load: addr_tehb `fullreg`, one LUT, enable of the 64 address registers. sitofp/fptosi: the key is small
because their long conversion logic is an input-to-register path, which the MILP already checks through
`inport`. The run took 177 s (4 units only, `make_config.py`).

## Verification on fir (fpl22, Cbc, compile + write-hdl only) [run]
- 8 ns: no warning with either model, `handshake_export.mlir` byte-identical.
- 6 ns: new model prints 3 times `CRITICAL WARNING: an operator has no known implementation capable of
  running at the requested operating frequency. Closest match selected. ...`; buffers and
  `handshake_export.mlir` unchanged. The message goes to stderr, does not name the operator, and compile
  still ends with "Compilation succeeded".
- Outputs in `fir/{old,new}_{6,8}ns`. Old model saved as `components-ecp5.old.json`;
  `check_model_diff.py` reports that only the `latency` field of the 4 units differs.

## The 24 entries still copied from Kintex-7
There is no `handshake.buffer` entry in either model, and every buffer algorithm sets `bufModel = nullptr`
[read]: characterizing buffers would change nothing without C++ changes.

| entries | cause | what it would take |
|---|---|---|
| fork (1) | in `skipping_units`; two parser bugs once removed: entity `handshake_fork` not accepted by `extract_generics_ports` (AttributeError), and the `add_2d_ports` regex does not allow the space in `data_array (SIZE ...)` | fixed in a scratch copy (`task2/`): data 0.49-0.67, valid 0.85, ready 0.96 ns (Kintex-7 file: 0.1/0.1 by hand). About 1 h. Open: SIZE fixed to 2, the model has no SIZE dimension |
| divsi (1) | in the skip list | works at 32 bits [run]: key 4.853 (Kintex-7 2.488), 35 cycles, ready 0.907. About 15 min |
| store (1) | in the skip list, RTL is only wires | about 15 min, not run |
| divui (1) | VHDL uses a component with no entity in `data/vhdl` | needs the generator, half a day to a day |
| remsi, remui (2) | no entry in the RTL config | remsi about 30 min; remui as divui |
| uitofp (1) | no config entry | copy sitofp's entry, about 15 min |
| control_merge (1) | legacy VHDL has `assert false` | generator entry, about half a day |
| end, return (2) | no RTL exists | cannot be characterized |
| addf, subf, mulf, divf, cmpf, maximumf, minimumf x {vivado, flopoco} (14) | vivado variants need the Xilinx floating point library (impossible with Yosys). For these units the delay key is not only a threshold: it selects the FloPoCo core (`internal_delay`) | FloPoCo: bind each architecture in the wrapper and keep the keys, 1-2 days |

## Checked afterwards by hand
- Recomputed the diff between `components-ecp5.old.json` and the new model: same 42 units, only the
  `latency` field of muli, load, sitofp and fptosi differs. [run]
- The delay key selects RTL only for ops with `FPUImplInterface` (addf, subf, mulf, divf, cmpf); muli,
  load, sitofp and fptosi do not have it, so the new keys cannot change which RTL is generated. [read:
  `HandshakeSetUnitImplAttributes.cpp:59-71`, `HandshakeArithOps.td`]
- Not done: fir was not simulated or synthesized with the new model; store, remsi, uitofp not run; the
  fork and divsi fixes live only in `task2/`, not in the worktree.
