# ECP5 timing model v2: fork, divsi, store, remsi, uitofp measured (14 Nov 2026)

Condensed from the agent's final report, then checked by hand (see "Checked afterwards").
Model: `data/components-ecp5-v2.json` (untracked). The model in
use, `data/components-ecp5.json`, was not touched (md5 3dd3cd4398c227b870852f72e0929560). Scratch: this
folder. [run] = verified by running, [read] = read from code or logs.

## Files changed in the worktree (nothing committed)
- `tools/backend/synth-characterization/utils.py`: `handshake.store`, `handshake.divsi`, `handshake.fork`
  removed from `skipping_units`; the `add_2d_ports` regex accepts the space in `data_array (SIZE ...)`.
- `tools/backend/synth-characterization/unit_characterization.py`: `extract_generics_ports` also accepts an
  entity named like the unit with "." replaced by "_" (`handshake_fork`, the only such entity in `data/vhdl`).
- `data/rtl-config-vhdl-vivado.json` (untracked): entries for `handshake.remsi` (shaped like divsi) and
  `handshake.uitofp` (shaped like sitofp), +16 lines. Copy from before: `rtl-config-vhdl-vivado.before.json`.
- Vivado path, dry run with `--synth-tool true`: all 5 units parse and get their TCL. [run]

## Measured values (ns, median of 3 seeds; run took 2 min 38 s) [run]
| unit | field | ECP5 v2 | Kintex-7 copy |
|---|---|---|---|
| fork (SIZE 2) | data, width 1..64 | 0.493/0.492/0.501/0.508/0.506/0.660/0.665 | data{64: 0.0} |
| fork | valid / ready | 0.85 / 0.96 | 0.1 / 0.1 |
| divsi (32 only) | ready / VR | 0.907 / 0.74 | 1.121 / 1.125 |
| divsi | latency key (35 cycles) | 4.853 | 2.488 |
| remsi (32 only) | ready / VR | 1.143 / 0.727 | 1.121 / 1.125 |
| remsi | latency key (35 cycles) | 4.170 | 2.488 |
| store | data / valid / ready / VR | 0.66 / 0.491 / 0.493 / 0.0 | 0.672 / 1.397 / 1.4 / 1.409 |
| uitofp | ready | 0.907 | 1.121 |
| uitofp | latency keys (5 cycles), width 1..64 | 1.903/1.801/1.992/1.876/2.012/2.363/2.154 | {64: 1.190} |
| uitofp | inport data, width 1..64 | 0.653/1.326/2.921/3.722/5.331/11.752/11.298 | 0 |

divsi and remsi fail at every width but 32 (GHDL: "mismatching vector length; got 1, expect 32"): the
divider inside is fixed at 32 bits. Full field diff in `model-diff.txt`.

### Fork by number of outputs (scratch runs `size4/`, `size8/`)
| SIZE | valid | ready |
|---|---|---|
| 2 | 0.85 | 0.96 |
| 4 | 0.902 | 1.87 |
| 8 | 1.053 | 2.145 |

The model has no SIZE dimension; v2 holds the SIZE 2 values.

## binary_search with v2 against the current model [run]
fpl22, Cbc, full flow. Place and route seeds 1/2/3 on the same netlist.

| T | model | est | seeds 1/2/3 | median | flow's own run | cycles | buffers | MILPs stopped |
|---|---|---|---|---|---|---|---|---|
| 8 | current | 7.649 | 9.160 / 9.224 / 8.902 | 9.160 | 9.275 | 238 | 23 (16 DV, 7 R) | 0 |
| 8 | v2 | 7.999 | 8.401 / 8.343 / 8.471 | 8.401 | 9.106 | 238 | 34 (16 DV, 17 R, 1 FIFO) | 1 |
| 10 | current | 9.828 | 10.001 / 9.832 / 10.220 | 10.001 | - | 238 | 20 (12 DV, 8 R) | 1 |
| 10 | v2 | 9.990 | 11.299 / 12.016 / 11.119 | 11.299 | 11.864 | 238 | 30 (16 DV, 13 R, 1 FIFO) | 1 |

Reading: better at 8 ns (0.76 ns), worse at 10 ns (1.3 ns). Measuring the fork alone does not make the
estimate track the measurement. In both v2 runs one MILP was stopped by the time limit.

## Not done or not checked
- Which fork sizes binary_search has was not looked at.
- How Dynamatic picks a value for divsi/remsi above 32 bits when the model only has a width-32 entry.
- divui, remui, control_merge, floating point units. No autopep8.
- The SIZE 4 and 8 runs used no `--reference-json`: scratch runs for the delays only.

## Checked afterwards by hand
- md5 of `data/components-ecp5.json` unchanged; `git status` of the worktree shows the 7 modified tracked
  files and 4 untracked ones listed above; the `utils.py` diff is the 3 skip-list lines and the regex. [run]
- At 8 ns the flow's own nextpnr run (default seed) gives 9.106 while seeds 1-3 give 8.34-8.47: the default
  seed is an outlier here, larger than the spread seen elsewhere (about 0.25 ns). Not explained.
