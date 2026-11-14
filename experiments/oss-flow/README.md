# Open-source synthesis flow for Dynamatic (Yosys + nextpnr, Lattice ECP5)

This fork adds an open-source back end to Dynamatic, so a kernel can go from C to a placed and routed design on a Lattice ECP5 without Vivado. I then used it to check how well the timing estimate of buffer placement matches the clock period measured after place and route.

I wrote this with Claude Code (an AI coding agent). I checked the numbers in the reports by hand.

## Branches

All branches start from upstream `main` at 464990bde (13 Nov 2026).

| branch | what it adds |
|---|---|
| `oss-synthesize` | `synthesize --tool yosys` (Yosys `synth_ecp5` + `nextpnr-ecp5`, LFE5UM5G-85F), `compile --timing-models <json>`, and `compile --back-annotation <json>` (extra per-channel delays for the MILP) |
| `ecp5-characterization` | a nextpnr back end for `tools/backend/synth-characterization`, and two ECP5 timing models in `data/` |
| `oss-critical-path` | `tools/backend/synth-critical-path/critical_path.py`, which maps the nextpnr critical path back to the dataflow units and channels it goes through |
| `oss-flow` | the three branches merged, plus this folder with the experiment scripts and the reports |

The docs are in `docs/UserGuide/CommandReference.md`, `docs/DeveloperGuide/Specs/TimingCharacterization.md` and `docs/DeveloperGuide/Specs/CriticalPathAnalysis.md`.

## Results so far

1. The open-source flow works on loop_multiply, fir, binary_search, matvec, insertion_sort, atax, iir and matrix. Simulation passes in all 30 runs (`qor/REPORT.md`).
2. With the ECP5 model the design meets the target on the ECP5 in 9 of 13 runs, against 7 of 13 with the default Kintex-7 model (insertion_sort left out, see below). For example fir at 8 ns gets 8.24 ns with the Kintex-7 model and 7.34 ns with the ECP5 one, at the same cycle count.
3. insertion_sort uses an LSQ, and the LSQ is not in the ECP5 model: the measured period is 17 to 22 ns for an 8 or 10 ns target. These rows are kept apart.
4. On binary_search the estimate is off by up to 4.6 ns. In all 17 binary_search runs the critical path is a valid/ready chain of 6 to 14 units. In the median, 54% of the path is on the nets between units and 18% inside buffers. The MILP counts zero for both, because the buffer placement code passes a null timing model for the buffers it inserts (`FPL22Buffers.cpp`).
5. I tried to feed the measured delays back to the MILP in four ways (`closed-loop/REPORT.md`). None of them improves the result reliably. The best case (binary_search at 8 ns, from 9.2 to 6.6 ns) is matched by simply asking the MILP for 6 ns, with fewer buffers. So I do not claim it as a result.

Next step, not done yet: characterize the buffers on the ECP5 and give their models to the MILP.

## Folder contents

- `qor/`: `run_matrix.py` runs every kernel with both models at 8 and 10 ns through `flow.py` (compile with fpl22 and Cbc, write-hdl, simulate, synthesize), `make_report.py` writes the tables of `REPORT.md` from `results.csv`.
- `closed-loop/`: `closed_loop.py` (the four feedback strategies), `exp/` (run scripts and table helpers), `seedcheck/seeds.sh` (place and route again with other seeds), `REPORT.md`.
- `reports/`: how the ECP5 latency keys were measured, and the second ECP5 model (fork, divsi, remsi, store and uitofp measured; it is better at 8 ns and worse at 10 ns on binary_search, so it is not the default).

## How to run

Build Dynamatic as usual. Put the [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build) on `PATH` (it has Yosys, nextpnr-ecp5 and GHDL). Then in the Dynamatic shell:

```
set-src integration-test/fir/fir.c
set-clock-period 8
compile --buffer-algorithm fpl22 --milp-solver cbc --timing-models data/components-ecp5.json
write-hdl --hdl vhdl
simulate
synthesize --tool yosys
```

The scripts here find the repository from their own path, or from `DYNAMATIC_DIR` if it is set. I ran everything on macOS (Apple silicon) with Cbc as the MILP solver. Building on macOS needed three small local patches that are not in these branches.

## Limits

- The fork model has no dimension for the number of outputs: its ready delay goes from 0.96 ns with 2 outputs to 2.15 ns with 8.
- divsi and remsi can only be characterized at 32 bits.
- Some MILPs stop at the 300 s Cbc time limit. The reports say which ones.
- The ECP5 characterization looked at the work in Endri Bezati's fork (github.com/endrix/dynamatic) first.
