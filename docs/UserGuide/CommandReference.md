# Command Reference
The Dynamatic shell is an interactive command line-based interface (you can launch it from Dynamatic's top level directory with `./bin/dynamatic` after building Dynamatic) that allows users to interact with Dynamatic and use the different commands available to generate dataflow circuits from C code.  

This document provides an overview of the different commands available in the Dynamatic frontend and their respective flags and options.

## Dynamatic Shell Commands
- `help`: Display list of commands.
- `set-dynamatic-path <path>`: Set the path of the root (top-level) directory of Dynamatic, so that it can locate various scripts it needs to function. This is not necessary if you run Dynamatic from said directory.
- `set-vivado-path <path>`: Set the path to the installation directory of Vivado.
- `set-polygeist-path <path>`: Sets the path to the Polygeist installation directory.
- `set-fp-units-generator <flopoco|vivado>`: Choose which floating point unit generator to use. See [this section](OptimizationsAndDirectives.md#floating-point-ips) for more information.
- `set-clock-period <clk>`: Sets the target clock period in nanoseconds.
- `set-src <source-path>`: Sets the path of the `.c` file of the kernel that you want to compile. 
- `compile [...]`: Compiles the source kernel (chosen by `set-src`) into a dataflow circuit. For more options, run `compile --help`.
> [!NOTE]  
> The `compile` command does not require Gurobi by default, but it is needed for [smart buffer placement options](OptimizationsAndDirectives.md#optimization-algorithms-in-dynamatic).  

The `--buffer-algorithm` flag allows users to use smart buffer placement algorithms notably `fpga20` and `fpl22` for throughput and timing optimizations.

The `--fast-token-delivery` flag enables the *Fast Token Delivery (FTD)* algorithm during the CF → Handshake lowering stage. Note that this option is currently incompatible with smart buffer placement algorithms.

The `--timing-models <path>` option sets the JSON file with the timing models of the units, used by buffer placement and the other timing-driven passes (default: `data/components.json`). It allows, for example, to compile with timing models characterized for another FPGA.

- `write-hdl [--hdl <vhdl|verilog|smv>]`: Convert results from `compile` to a VHDL, Verilog or SMV file.
- `simulate [--simulator <vsim|xsim|ghdl|verilator]`: Simulates the HDL produced by `write-hdl`. 
> [!NOTE]  
> Requires a ModelSim/Questa (`vsim`), Vivado (`xsim`), GHDL (`ghdl`) or Verilator (`verilator`) installation.

- `synthesize [--tool <vivado|yosys>]`: Synthesizes, places and routes the HDL result from `write-hdl`, out of context, and reports resource utilization and timing against the clock period set by `set-clock-period`. The default tool, `vivado`, targets a Xilinx Kintex-7 (`xc7k160tfbg484-2`). `yosys` uses the open-source flow Yosys (`synth_ecp5`) and nextpnr (`nextpnr-ecp5`) to target a Lattice ECP5 (`LFE5UM5G-85F`), so that no Vivado license is needed.
> [!NOTE]  
> `vivado` requires a Vivado installation! `yosys` requires `yosys` and `nextpnr-ecp5` in your `PATH`: the [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build) from YosysHQ ships both as prebuilt binaries for Linux and macOS: download and extract a release, then `source <suite>/environment`, or add `<suite>/bin` to your `PATH` and, for VHDL, set `GHDL_PREFIX=<suite>/lib/ghdl`. Verilog is read natively. VHDL (from `write-hdl --hdl vhdl`, but also the LSQs and the FloPoCo floating-point units of a Verilog design) needs the GHDL plugin of Yosys, which the OSS CAD Suite includes. The plugin cannot read VHDL that instantiates Verilog modules, as the FloPoCo units of a Verilog design do: for floating-point kernels, use `write-hdl --hdl vhdl`.

- `verify-invariants`: Verify that automatically-generated invariants during rigidification are provably correct.
> [!NOTE]  
> Requires nuXmv installation!

- `visualize`: Visualizes the execution of the simulated circuit. 
> [!NOTE]  
> Requires Godot Engine and [the visualizer component must be built!](AdvancedBuild.md#4-interactive-dataflow-circuit-visualizer)

- `exit`: Exits the interactive Dynamatic shell.

For more information and examples on the typical usage of the commands, checkout the [using Dynamatic](../GettingStarted/Tutorials/Introduction/UsingDynamatic.md) and [example](../GettingStarted/Tutorials/Introduction/Examples.md) pages.
