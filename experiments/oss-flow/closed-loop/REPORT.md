# Closed-loop buffer placement: results (14 Nov 2026)

Condensed from the agent's final report (it could not write this file), then extended by hand (last section).
Driver: `closed_loop.py` in this folder. Experiments in `exp/` (one folder per case, with `iter_N/out`,
`history.json`, `summary.json`, `annotation_iter_N.json`). Flow: fpl22 + Cbc (300 s limit), write-hdl,
simulate, synthesize with Yosys + nextpnr-ecp5. cp = critical path after place and route, in ns; three
values = the flow's own nextpnr run / seed 2 / seed 3. Simulation passes on every run.

## Strategies
- `path`: annotate the channels on the measured critical path with the measured net delay.
- `path-est`: same, but only the part the estimate does not already cover.
- `scale`: tighten the target period given to the MILP (T' = T * est / measured, repeated).
- `wire`: the same delay d on every channel, `d_next = max(0, d + (median - est) / n)`.
Front-end option used: `compile --back-annotation <json>` (patch `step3-compile-back-annotation.patch`).

## binary_search, ECP5 model, target 8 ns
| case | setting | est | cp (3 seeds) | median | cycles | buffers | LUT4/FF | MILPs stopped |
|---|---|---|---|---|---|---|---|---|
| open loop | - | 7.649 | 9.275 / 9.224 / 8.902 | 9.224 | 238 | 23 (16 DV, 7 R) | 1271/720 | 0 |
| wire it1 | d = 0.143 | 7.933 | 9.575 / 9.218 / 9.488 | 9.488 | 238 | 29 (1 FIFO, 15 DV, 13 R) | 1406/856 | - |
| wire it2 | d = 0.217 | 7.587 | 8.966 / 9.188 / 9.417 | 9.188 | 238 | 32 (2 FIFO, 13 DV, 17 R) | 1304/739 | 1 |
| wire it3 | d = 0.340 | 7.841 | 6.474 / 6.713 / 6.641 | 6.641 | 238 | 48 (28 DV, 20 R) | 1329/802 | 0 |
| fixed d | d = 0.28 | 7.709 | 8.863 / 8.850 / 9.243 | 8.863 | 238 | 32 (15 DV, 17 R) | 1189/739 | - |
| scale it1 | T' = 6.938 | 6.791 | 9.691 / 9.929 / 9.952 | 9.929 | 238 | 32 (17 DV, 15 R) | - | 1 |
| scale it2 | T' = 5.590 | 5.582 | 11.370 / 10.813 / 11.377 | 11.370 | 317 | 31 | - | 1 |
| path-est it1 | - | 7.903 | 10.938 / 11.035 / 11.049 | 11.035 | 238 | 25 (2 FIFO, 13 DV, 10 R) | - | - |
| path it1 | 0.843 ns on 11 channels | 7.995 | 11.152 (one seed) | - | 239 | 32 | - | - |
| path it2 | 0.606 ns on 17 channels | 7.978 | 11.082 (one seed) | - | 238 | 34 | - | - |

Five-seed check of wire it3: 6.502 / 6.713 / 6.640 / 7.200 / 6.524. Open loop, five seeds:
9.160 / 9.224 / 8.902 / 8.961 / 8.844.

## Controls
| control | est | cp (3 seeds) | median | cycles | buffers | LUT4/FF |
|---|---|---|---|---|---|---|
| open loop, T = 6 | 5.970 | 6.677 / 6.476 / 7.145 | 6.677 | 238 | 32 (2 FIFO, 15 DV, 15 R) | 1411/771 |
| open loop, T = 5 | 4.908 | 10.375 / 9.949 / 10.355 | 10.355 | 316 | 36 | 1416/898 |
| wire at T = 10 | - | 9.780 / 9.833 / 10.221 | 9.833 | 238 | 20 | 1195/595 |

`wire` at T = 10 stops at iteration 0 (median already under 10).

Fixed d = 0.34 at 8 ns on the other kernels (open loop -> d = 0.34):
| kernel | est | median cp | cycles | buffers |
|---|---|---|---|---|
| fir | 7.941 -> 7.609 | 7.314 -> 7.182 | 1006 -> 2005 | 10 -> 15 |
| loop_multiply | 7.335 -> 7.856 | 7.315 -> 7.302 | 26 -> 29 | 8 -> 10 |
| matvec | 7.732 -> 7.669 | 7.349 -> 7.382 | 10107 -> 20306 | 35 -> 54 |

## Reading
- The gain of `wire` (9.2 -> 6.6 ns at the same 238 cycles) is matched by an open-loop run that simply asks
  for 6 ns, with fewer buffers. It is not evidence for the feedback.
- The response to both knobs (T', d) is not gradual: d = 0.14, 0.22, 0.28 give nothing, 0.34 gives 2.5 ns;
  T = 6 gives 6.7 ns, T = 5 gives 10.4 ns and 33% more cycles. One run per point cannot separate a step
  from noise in the MILP's choice.
- d = 0.34 is not a device constant: on fir and matvec, which already meet 8 ns, it doubles the cycle count
  for no gain in clock period.
- `path`, `path-est` and `scale` make binary_search worse.
- Across the binary_search placements the MILP estimate (4.9 to 8.0) does not predict the measured period
  (6.5 to 11.4).
- On flattened netlists `critical_path.py` cannot tell which part of a path the model already covers, so
  `path` annotates the whole routing delay instead of the excess.

## Where the time goes on the measured critical paths (17 binary_search runs, checked by hand)
`critical_path.py` on every binary_search run of this folder, of `../ecp5-v2` and of `../qor/runs`.
Every critical path is a control chain (valid/ready) of 6 to 14 units: it starts at a register of a buffer
(`control.outputvalid`, `control.fullreg`) or of a fork (`regblock.transmitvalue`), or in two cases at
`mem_controller1`, and ends at the enable of a data register (`datareg (CE)`) or in `select0`.

| part of the path | median (ns) | min | max | median share |
|---|---|---|---|---|
| nets between units | 4.96 | 3.55 | 7.83 | 54% |
| inside buffers | 2.08 | 0.14 | 5.27 | 18% |
| inside forks | 0.57 | 0 | 2.85 | 5% |
| inside other units | 1.50 | 0.14 | 5.05 | 14% |

Correlation with the critical path over the 17 runs: number of cells 0.85, nets between units 0.71, number
of units 0.59, number of forks 0.44, number of buffers -0.37, time inside buffers -0.05.

The MILP counts zero for the nets and zero for the buffers it inserts: `FPL22Buffers.cpp` had
`const TimingModel *bufModel = nullptr;` (same in `FPGA20Buffers.cpp`, `CostAwareBuffers.cpp`,
`MAPBUFBuffers.cpp`), with a comment saying that no models exist for the buffers and that this hurts
accuracy. Follow-up in `../buf-model/`.
