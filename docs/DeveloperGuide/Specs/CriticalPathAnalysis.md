# Critical Path Analysis on the Dataflow Circuit

This document describes a [Python script](https://github.com/EPFL-LAP/dynamatic/tree/main/tools/backend/synth-critical-path/critical_path.py) that takes the critical path found by nextpnr after placing and routing a Dynamatic kernel on a Lattice ECP5 (Yosys `synth_ecp5` followed by `nextpnr-ecp5`) and maps it back onto the dataflow circuit: which units the path goes through, how much delay is spent inside each unit and on the channels between them, and how this compares with the [timing model](../CompilerIntrinsics/TimingInformation.md) that buffer placement uses.

The script only needs the Python standard library.

## Usage

The simplest call takes the output directory of a Dynamatic run:

`python3 tools/backend/synth-critical-path/critical_path.py out --timing-model`

From `out`, the script finds:

- the DOT of the circuit after buffer placement, `out/comp/<kernel>.dot` (one node per unit, with its `mlir_op`, one edge per channel, with its ports);
- the top-level HDL, `out/hdl/<kernel>.v` or `.vhd`, which gives the bitwidth of every channel;
- the nextpnr JSON report (`nextpnr-ecp5 --report`), in `out/synth/nextpnr_report.json`;
- the Yosys JSON netlist next to the report, `<kernel>.json`, only used to tell whether the design was flattened.

Each of them can also be passed explicitly with `--dot`, `--hdl`, `--report` and `--netlist`. `--timing-model` with no argument compares with `data/components.json`; another timing model file can be given instead. `--json FILE` writes the same analysis as JSON, and `--dot-out FILE` sets where the annotated DOT goes (by default next to the report).

## Output

For each critical path in the report (one per pair of clock domains) the script prints the total delay, the achieved frequency against the constraint, the registers where the path starts and ends, and the path cut into dataflow units:

```
  #   unit                     op                         logic   route   total  in -> out              model  ratio
  9   fork15                   handshake.fork             0.141   0.000   0.141  valid -> valid         0.100   1.41
      model: valid
      fork15_outs_0_valid                                        0.459  channel fork15.outs_0 -> mux3.index, valid
  10  mux3                     handshake.mux              0.151   0.000   0.151  valid -> ready         1.117   0.14
      model: VR
      cond_br8_falseOut_ready                                    0.448  channel cond_br8.falseOut -> mux3.ins_0, ready (through extui0)
```

Each unit row gives the delay spent inside the unit, split into cell delay (`logic`, which includes clock-to-output and setup) and routing between cells of the same unit. The lines between two units are the nets that join them, with the channel and the signal (data, valid or ready) when the net is named after a channel. A summary follows: logic against routing, delay inside units against delay on the nets between them, and the unit with the largest share of the path.

The script also writes a copy of the DOT where the units and channels of the critical path are drawn in red with their measured delay, and renders it to PNG and SVG if Graphviz `dot` is on the `PATH`.

### Comparison with the timing model

The signals through which the path enters and leaves a unit give the kind of crossing, and so the entry of the timing model to compare with: `data` (at the unit's bitwidth, chosen as in `getOpDatawidth`), `valid`, `ready`, `VR`, `CV`, `CR`, `VC` or `VD`. A data signal on a `condition` or `index` port counts as a condition. When the path both starts and ends at registers of the same pipelined unit, it is compared with the internal delay of the implementation the model would pick for the target period.

No number is printed when it cannot be told: when the path starts or ends inside the unit and leaves or enters it by a port (the model only has port-to-port numbers, and the `inport` and `outport` delays are unused), when the crossing or the bitwidth is unknown, and for units that have no entry in the model (buffers, constants, extensions, memory controllers...). Note that the delays in `data/components.json` were measured with Vivado on a Xilinx FPGA, and that the `fpga20` buffer placement algorithm only uses the data delays.

### Per-endpoint timing

When nextpnr is run with `--detailed-timing-report`, its JSON report also contains, for every net that drives a timing endpoint (a flip-flop data, clock enable or reset input), two arrival times at that endpoint; the larger one is the worst arrival (at the critical endpoint it equals the delay of the critical path). The script then lists the endpoints with the latest arrival, and the worst endpoint of each unit, with the slack against the clock period. The report does not give the setup time of these endpoints, so the slack is the period minus the arrival. It contains no path to these endpoints either, only the arrival time, so it cannot be used to map more paths onto the circuit.

## How cells and nets are mapped to units

Yosys keeps the hierarchy in the names. With the default `synth_ecp5`, which flattens the design, the first dotted component of a cell name is the unit the cell was named after (`muli0.mul_4_stage_inst.b_reg_TRELLIS_FF_Q_18` is in `muli0`), cells without a dot are top-level logic, and nets can keep the name of a channel wire, either the top-level wire (`fork2_outs_1_valid`, `start_valid`) or a unit port (`muli0.lhs[5]`, `fork0.outs_valid[2]` for port `outs_2`).

In a flattened netlist these names are only hints:

- ABC maps the logic of several units into the same LUTs, and Yosys then names each LUT after a nearby named net, so a LUT named after a unit may contain logic of its neighbours. Channel wires usually disappear inside the LUTs, so most nets between units are not named after a channel and the crossing kind stays unknown. The script reports how many consecutive units on the path are adjacent in the circuit, which shows how far the names can be trusted.
- A flip-flop is named after the net on its output, which is often the input port of the next unit (`buffer1.ins_TRELLIS_FF_Q` is the output register of `buffer0`). A cell that drives a channel net is therefore attributed to the unit that drives the channel, looking through units that only wire signals through (`extui`, `extsi`, and `trunci` for data and valid); such units are marked with `*`.

For exact attribution, the design can be synthesized without flattening (`synth_ecp5 -noflatten`). nextpnr then names every cell with its instance path and every net between units with the name of its channel wire. The circuit is slower, since no logic is optimized across units, so its critical path is not the same as the one of the flattened design.
