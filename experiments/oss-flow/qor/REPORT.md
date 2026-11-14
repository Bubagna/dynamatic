# QoR: Kintex-7 vs ECP5 timing model (FPL22, CBC, ECP5 um5g-85k)

Status (14 Nov 2026): **partial**, 30 rows (simulation passes in all of them): loop_multiply, fir, binary_search, matvec, insertion_sort, atax and iir with both models at 8 and 10 ns, plus matrix at 8 ns. The matrix of runs was still going when this copy was made. insertion_sort uses an LSQ, which the ECP5 model does not cover, so its rows are not comparable with the others. The ECP5 model changed at 14:28 (measured latency keys, same delays): rows from insertion_sort on use the new one. To regenerate the tables, run `python make_report.py`; it rewrites only the block between the markers.

Flow: `flow.py --buffer-algorithm fpl22 --milp-solver cbc --calculate-path-delays --hdl vhdl`, then GHDL simulation, then `synthesize --tool yosys`. Kintex-7 model = `data/components.json`. ECP5 model = `data/components-ecp5.json`.

## How the metrics are obtained
- `est_cp_ns`: the largest `{data,valid,ready}Path{In,Out}_<channel>` value of the MILP re-solve with the buffers pinned (`calculatePathDelays`).
- With CBC the `*_accurate_delays.log` files are wrong: `CbcSoluParser` never clears its result map, and CBC writes only the non-zero variables, so the zeros keep the values of the first solve. Example: loop_multiply, 8 ns: the log max is 8.0 and that value is stale; the real value is 7.335.
- The flow therefore reads the re-solve `cbc_solution_*.sol` files. `est_cp.json` in each run keeps both values.
- By construction est <= T.
- `cycles`: from the line `Simulation done! Latency = N cycles` in `out/sim/report.txt`, i.e. the number of clock cycles from reset release to the end/memory-done handshake.
- `cp_ns` = 1000 / `fmax.clk.achieved` from `nextpnr_report.json`. "met" means cp <= T.
- Checked by hand:
- binary_search, Kintex-7 model, 10 ns: 68.470 MHz gives 14.605 ns; × 238 cycles = 3476.0 ns.
- loop_multiply, ECP5 model, 8 ns: 26 × 7.373 = 191.7 ns.

## Largest disagreements between estimate and measurement
| run | est | cp | real critical path (critical_path.py; flattened netlist, so the names are hints) |
|---|---|---|---|
| binary_search, Kintex-7, 10 ns | 9.96 | 14.61 | buffer7 data → fork0 → carry chain (comparison) → fork3 → control_merge0 → mux1 → load0 → fork2 → control_merge2 → fork5 → fork4 → clock enable of load0's register. 78% routing, 1 of 12 hops between adjacent units. A data → compare → handshake chain that the model does not see as one path. |
| binary_search, ECP5, 8 ns | 7.65 | 9.28 | Loop among the valid/ready logic of buffer11, buffer13, buffer17, buffer12, mux8, fork12. 77% routing, 0 of 8 hops between adjacent units. |
| binary_search, Kintex-7, 8 ns | 7.60 | 9.06 | Not analysed. |
| loop_multiply, ECP5, 8 ns | 7.335 | 7.373 | All inside muli0, register to register (4.1 ns logic + 3.3 ns routing). The model puts 1.0 ns on this path; the estimate comes close only because another channel happens to reach 7.3 ns. |

At 10 ns, fir and loop_multiply are overestimated: est 9.0–9.5 against cp 7.1–7.4. There the measured cp is set by muli0.

Outputs of critical_path.py are in `critpath/` (`binary_search_kintex7_10ns.*`, `binary_search_ecp5_8ns.*`, `test_lm.*`), as .txt, .json and annotated .dot/.png/.svg.

<!-- BEGIN TABLES -->
### Per-run results

T = target period (ns). est = est_cp_ns (MILP), cp = nextpnr critical path (ns), met = cp <= T, exec = cycles x cp (ns). LUT4 = TRELLIS_COMB, FF = TRELLIS_FF, DSP = MULT18X18D.

| kernel | T | model | status | buffers | slots | cycles | est | cp | met | exec | LUT4 | FF | DSP | sim |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| loop_multiply | 8 | kintex7 | ok | 8 | 8 | 29 | 7.33 | 7.47 | yes | 217 | 518 | 381 | 3 | pass |
| loop_multiply | 8 | ecp5 | ok | 8 | 8 | 26 | 7.33 | 7.37 | yes | 192 | 474 | 379 | 3 | pass |
| loop_multiply | 10 | kintex7 | ok | 7 | 7 | 29 | 9.47 | 7.20 | yes | 209 | 450 | 347 | 3 | pass |
| loop_multiply | 10 | ecp5 | ok | 7 | 7 | 26 | 9.48 | 7.36 | yes | 191 | 499 | 346 | 3 | pass |
| fir | 8 | kintex7 | ok | 12 | 18 | 1005 | 7.90 | 8.24 | no | 8277 | 554 | 517 | 3 | pass |
| fir | 8 | ecp5 | ok | 10 | 17 | 1006 | 7.94 | 7.34 | yes | 7384 | 782 | 506 | 3 | pass |
| fir | 10 | kintex7 | ok | 11 | 17 | 1005 | 9.37 | 7.14 | yes | 7179 | 580 | 514 | 3 | pass |
| fir | 10 | ecp5 | ok | 11 | 17 | 1005 | 9.06 | 7.18 | yes | 7221 | 579 | 505 | 3 | pass |
| binary_search | 8 | kintex7 | ok | 27 | 27 | 238 | 7.60 | 9.06 | no | 2156 | 1308 | 802 | 0 | pass |
| binary_search | 8 | ecp5 | ok | 23 | 23 | 238 | 7.65 | 9.28 | no | 2208 | 1271 | 720 | 0 | pass |
| binary_search | 10 | kintex7 | ok | 19 | 19 | 238 | 9.96 | 14.61 | no | 3476 | 1654 | 626 | 0 | pass |
| binary_search | 10 | ecp5 | ok | 20 | 20 | 238 | 9.83 | 9.78 | yes | 2328 | 1195 | 595 | 0 | pass |
| matvec | 8 | kintex7 | ok | 34 | 47 | 10108 | 7.23 | 7.70 | yes | 77862 | 1631 | 909 | 3 | pass |
| matvec | 8 | ecp5 | ok | 35 | 47 | 10107 | 7.73 | 7.51 | yes | 75924 | 1406 | 811 | 3 | pass |
| matvec | 10 | kintex7 | ok | 33 | 56 | 10207 | 9.56 | 10.00 | yes | 102070 | 1496 | 945 | 3 | pass |
| matvec | 10 | ecp5 | ok | 30 | 40 | 10106 | 9.92 | 8.38 | yes | 84658 | 1136 | 716 | 3 | pass |
| insertion_sort | 8 | kintex7 | ok | 49 | 49 | 1076 | 7.65 | 19.91 | no | 21420 | 15382 | 3085 | 0 | pass |
| insertion_sort | 8 | ecp5 | ok | 48 | 48 | 1131 | 7.99 | 17.35 | no | 19620 | 14761 | 3076 | 0 | pass |
| insertion_sort | 10 | kintex7 | ok | 38 | 38 | 1076 | 9.89 | 20.13 | no | 21656 | 14787 | 2841 | 0 | pass |
| insertion_sort | 10 | ecp5 | ok | 42 | 42 | 1076 | 9.86 | 21.72 | no | 23368 | 14642 | 2970 | 0 | pass |
| atax | 8 | kintex7 | ok | 67 | 79 | 891 | 7.93 | 12.19 | no | 10860 | 13530 | 3017 | 6 | pass |
| atax | 8 | ecp5 | ok | 70 | 82 | 1068 | 7.94 | 12.00 | no | 12812 | 13820 | 3155 | 6 | pass |
| atax | 10 | kintex7 | ok | 73 | 91 | 1029 | 9.95 | 12.84 | no | 13216 | 13646 | 3113 | 6 | pass |
| atax | 10 | ecp5 | ok | 73 | 84 | 910 | 10.00 | 12.48 | no | 11355 | 13441 | 3042 | 6 | pass |
| iir | 8 | kintex7 | ok | 15 | 15 | 4998 | 7.52 | 7.28 | yes | 36400 | 1186 | 935 | 6 | pass |
| iir | 8 | ecp5 | ok | 14 | 14 | 4998 | 7.89 | 7.80 | yes | 38979 | 990 | 902 | 6 | pass |
| iir | 10 | kintex7 | ok | 13 | 13 | 4998 | 9.12 | 8.32 | yes | 41588 | 1212 | 890 | 6 | pass |
| iir | 10 | ecp5 | ok | 14 | 14 | 4998 | 9.78 | 7.41 | yes | 37055 | 1072 | 901 | 6 | pass |
| matrix | 8 | kintex7 | ok | 93 | 111 | 28837 | 7.96 | 9.79 | no | 282401 | 2125 | 1271 | 3 | pass |
| matrix | 8 | ecp5 | ok | 93 | 181 | 28957 | 7.88 | 9.73 | no | 281896 | 2508 | 1428 | 3 | pass |

### Timing target met (runs that reached synthesis)

| model | T | ok runs | met | mean cp/T | max cp |
|---|---|---|---|---|---|
| kintex7 | 8 | 8 | 3/8 (38%) | 1.28 | 19.91 |
| kintex7 | 10 | 7 | 4/7 (57%) | 1.15 | 20.13 |
| kintex7 | all | 15 | 7/15 (47%) | 1.22 | 20.13 |
| ecp5 | 8 | 8 | 4/8 (50%) | 1.22 | 17.35 |
| ecp5 | 10 | 7 | 5/7 (71%) | 1.06 | 21.72 |
| ecp5 | all | 15 | 9/15 (60%) | 1.15 | 21.72 |

### Estimation error: est_cp_ns / cp_ns

Below 1 = the MILP believes the circuit is faster than nextpnr measures it.

| model | T | n | mean | median | min | max |
|---|---|---|---|---|---|---|
| kintex7 | 8 | 8 | 0.825 | 0.889 | 0.384 | 1.033 |
| kintex7 | 10 | 7 | 0.947 | 0.956 | 0.492 | 1.315 |
| kintex7 | all | 15 | 0.882 | 0.938 | 0.384 | 1.315 |
| ecp5 | 8 | 8 | 0.859 | 0.910 | 0.460 | 1.082 |
| ecp5 | 10 | 7 | 1.045 | 1.184 | 0.454 | 1.319 |
| ecp5 | all | 15 | 0.946 | 1.005 | 0.454 | 1.319 |

### ECP5 model relative to Kintex-7 model (geometric mean of ecp5/kintex7 over kernels where both runs are ok)

| T | pairs | exec_time | cycles | cp | LUT4 | FF | LUT4+FF | buffers |
|---|---|---|---|---|---|---|---|---|
| 8 | 8 | 0.988 | 1.016 | 0.973 | 1.005 | 0.984 | 0.995 | 0.956 |
| 10 | 7 | 0.884 | 0.966 | 0.915 | 0.912 | 0.956 | 0.924 | 1.019 |
| all | 15 | 0.938 | 0.992 | 0.946 | 0.961 | 0.971 | 0.962 | 0.985 |

### Buffer placement MILPs stopped by the Cbc time limit

Such a run uses the best solution found, not the optimum.

None.

### Failures

None.

<!-- END TABLES -->

## Not done
- The matrix is incomplete: matvec with the ECP5 model, insertion_sort, atax and the extra kernels are still running or queued. The 6 ns column was not started.
- The summary numbers rest on only 3 kernel pairs.
- critical_path.py was run on 3 runs, only 2 of them with the ECP5 model.
