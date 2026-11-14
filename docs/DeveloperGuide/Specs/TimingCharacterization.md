# Dataflow Unit Characterization Script Documentation

This document describes how Dynamatic obtains the timing characteristics of the dataflow units. Please check out [this doc](../CompilerIntrinsics/TimingInformation.md) if you are unfamiliar with Dynamatic's timing model. 

Dynamatic uses a [Python script](https://github.com/EPFL-LAP/dynamatic/tree/main/tools/backend/synth-characterization/main.py) to obtain the timing characterization.

**NOTE**: The script and the following documentation are tailored for the specific version of Dynamatic and the current status of the structure of the [timing information file](https://github.com/EPFL-LAP/dynamatic/blob/main/data/components.json). When generating new dataflow units, try to follow the same structure as other dataflow units (in the timing information file and in the VHDL definition). This would make it possible to extend the characterization to new dataflow units.

## What is Unit Characterization?

Unit characterization refers to the systematic process of evaluating hardware units (e.g., VHDL modules) for various configurations. The script supports:

- **Parameter Sweeping**: Automatically varying generic parameters (e.g., bitwidth, depth) and generating the corresponding testbenches and synthesis scripts.
- **Dependency Resolution**: Ensuring all required VHDL files and dependencies are included for synthesis.
- **Parallel Synthesis**: Running multiple synthesis jobs concurrently to speed up characterization.
- **Automated Reporting**: Collecting and organizing timing and resource reports for each configuration.

## How to Use the Script

1. **Prepare VHDL and Dependency Files**
Ensure all required VHDL files and dependency metadata are available.
2. **Configure Parameters**
Update `parameters_ranges` for the units you wish to characterize.
3. **Run Characterization**
Call `run_unit_characterization` for each unit, specifying the required directories and tool.
4. **Analyze Results**
Timing and synthesis reports are generated for each parameter combination and stored in the designated report directory.

### How to `Run Characterization`  

An example on how to call the script is the following one:

`python main.py --json-output out.json --dynamatic-dir /home/dynamatic/ --synth-tool "vivado-2019 vivado"`

which would save the output JSON file in `out.json` which contains timing information, it would specify the dynamatic home directory as `/home/dynamatic/` and it would call vivado using the command `vivado-2019 vivado`. An alternative call is the following one:

`python main.py --json-output out.json --dynamatic-dir /home/dynamatic/ --synth-tool "vivado-2019 vivado" --json-input struct.json`

where the only key difference is the specification of the input JSON (`struct.json`) which contains information related to RTL characteristics of each component. If unspecified, the script will look for the following file `DYNAMATIC_DIR/data/rtl-config-vhdl-vivado.json`, which describes the generic VHDL units of `data/vhdl` (the file `rtl-config-vhdl.json` describes generated units, whose VHDL has no generics to sweep).

To characterize the units on a Lattice ECP5 with Yosys and nextpnr instead of Vivado, see [ECP5 Backend](#ecp5-backend-yosys--nextpnr).

## Overview

The script automates the extraction of VHDL entity information, testbench generation, synthesis script creation, dependency management, and parallel synthesis execution. Its primary goal is to characterize hardware units by sweeping parameter values and collecting synthesis/timing results.

## Where Characterization Data is Stored

All generated files and results are organized in a user-specified directory structure:

- **HDL Output Directory**: Contains all generated/copy VHDL files for each unit and configuration.
- **TCL Directory**: Stores synthesis scripts for each configuration.
- **Report Directory**: Contains timing and resource reports produced by the synthesis tool.
- **Log Directory**: Stores log files for each synthesis run.

Each configuration (i.e., a unique set of parameter values) is associated with its own set of files, named to reflect the parameter values used.

## Scripts Structure

The scripts are organized according to the following structure:
<pre lang="markdown">. 
├── hdl_manager.py # Moves HDL files from the folder containing all the HDL files to the working directory 
├── report_parser.py # Extracts delay information from synthesis reports 
├── main.py # Main script: orchestrates filtering, generation, synthesis, parsing 
├── run_synthesis.py # Runs synthesis (e.g., with Vivado), supports parallel execution 
├── unit_characterization.py # Coordinates unit-level processing: port handling, VHDL generation, exploration across all parameters 
└── utils.py # Shared helpers: common class definitions and constants </pre>

## Core Data Structures and Functions

The scripts uses several key functions and data structures to orchestrate characterization:

### Parameter Management

- **parameters_ranges**: (File `utils.py`)

    A dictionary mapping parameter names to lists of values to sweep. Enables exhaustive exploration of the design space. 


### Entity Extraction

- **extract_generics_ports(vhdl_code, entity_name)**: (File `unit_characterization.py`)
    
    Parses VHDL code to extract the list of generics (parameters) and ports for the specified entity.
    - Removes comments for robust parsing.
    - Handles multiple entity definitions in a single file.
    - Returns: `(entity_name, VhdlInterfaceInfo)`.

- **VhdlInterfaceInfo**: (File `utils.py`)

    A class that contains information related to generics and ports of a VHDL module

### Testbench Generation

- **generate_wrapper_top(entity_name, VhdlInterfaceInfo, param_names)**: (File `unit_characterization.py`)

    Produces a VHDL testbench wrapper for the entity, with generics mapped to parameter placeholders.
    - Ensures all generics are parameterized.
    - Handles port mapping for instantiation.


### Synthesis Script Generation

- **UnitCharacterization**: (File `utils.py`)

    A class that contains information related to parameters used for a characerization and the corresponding timing reports.

- **write_tcl(top_file, top_entity_name, hdl_files, tcl_file, sdc_file, rpt_timing, VhdlInterfaceInfo)**: (File `utils.py`)

    Generates a TCL script for the synthesis tool (e.g., Vivado), including:
    - Reading HDL and constraint files.
    - Synthesizing and implementing the design.
    - Generating timing reports for relevant port pairs.
- **write_sdc_constraints(sdc_file, period_ns)**: (File `run_synthesis.py`)

    Creates an SDC constraints file specifying the clock period.


### Dependency Handling

- **get_hdl_files(unit_name, generic, generator, dependencies, hdl_out_dir, dynamatic_dir, dependency_list)**: (File `hdl_manager.py`)

    Ensures all required VHDL files (including dependencies) are present in the output directory for synthesis.


### Synthesis Execution

- **run_synthesis(tcl_files, synth_tool, log_file)**: (File `run_synthesis.py`)

    Runs synthesis jobs in parallel using the specified number of CPU cores.
    - Each job is executed with its own TCL script and log file.
- **_synth_worker(args)**: (File `run_synthesis.py`)

    Worker function for executing a single synthesis job.

### Report Parsing

- **extract_rpt_data(map_unit_to_list_unit_chars, json_output)**: (File `report_parser.py`)

    Extract data from the different reports and it saves it into the `json_output` file. 
    The data `map_unit_to_list_unit_chars` contains a mapping between unit and a list of UnitCharacterization objects.
    Please look at the end of this doc to find an example of the structure of the expected report.

### High-Level Flow

- **run_unit_characterization(unit_name, list_params, hdl_out_dir, synth_tool, top_def_file, tcl_dir, rpt_dir, log_dir)**: (File `unit_characterization.py`)

    Orchestrates the full characterization process for a single unit:
    - Gathers all HDL files and dependencies.
    - Extracts entity information and generates testbench templates.
    - Sweeps all parameter combinations, generating top files and TCL scripts for each.
    - Runs synthesis and collects reports.
    - Returns a mapping from report filenames to parameter values.

## Using a New Synthesis Tool

For now the code has some specific information related to Vivado tool. However, adding support for a new backend should not take too long. Here it is a list of places to change to use a different backend:
- `_synth_worker` -> This function runs the synthesis tool. It assumes the tool can be called as follows: `SYNTHESIS_TOOL -mode batch -source TCL_SCRIPT`. 
- `write_tcl` -> This function writes the tcl script with tcl commands specific of Vivado. 
- `write_sdc_constraints` -> This function writes the sdc file and it is tailored for Vivado. It might also require some changes.
- `PATTERN_DELAY_INFO` -> This is a constant string used to identify the line where the report specifies the delay value. This is tailored for Vivado.
- `extract_delay` -> This function extracts the total delay of a path from the reports. This is tailored for Vivado.

These files might require some changes if the synthesis tool has different features from Vivado.

## ECP5 Backend (Yosys + nextpnr)

The script can also characterize the units on a Lattice ECP5 FPGA with open-source tools only: Yosys (with its GHDL plugin, which reads the VHDL units) for synthesis and nextpnr-ecp5 for placement and routing. The backend is in `nextpnr_backend.py` and is selected with `--synth-tool ecp5`:

`python main.py --json-output components-ecp5.json --dynamatic-dir /home/dynamatic/ --synth-tool ecp5 --reference-json /home/dynamatic/data/components.json`

`yosys`, `yosys-config` and `nextpnr-ecp5` must be on the `PATH` (the [OSS CAD Suite](https://github.com/YosysHQ/oss-cad-suite-build) has all of them). The GHDL plugin needs `GHDL_PREFIX`; if it is not set, the backend takes the `lib/ghdl` directory next to Yosys' data directory, which is where the OSS CAD Suite puts it. The target device is the LFE5UM5G-85F in the CABGA381 package, speed grade 8 (the only grade nextpnr supports for the ECP5-5G parts); `DEVICE`, `PACKAGE` and `SPEED_GRADE` in `nextpnr_backend.py` change it.

Options that matter for this backend:
- `--seeds N` (default 3): every parameter set is placed and routed with N placement seeds, and each delay is the median over them (the register-to-register delays described below take the worst seed). One-LUT paths move by a few tenths of a nanosecond from one seed to the next.
- `--jobs N` (default `NUM_CORES`): number of parameter sets run in parallel. Each nextpnr run uses one thread.
- `--reference-json FILE`: the script measures delays only. Each characterized unit takes its latency (the number of cycles) from FILE, and the units this run does not characterize (skipped units, units whose RTL needs vendor IP, failed runs) are copied whole from it. Without it, the output has the `delay` (and `inport`/`outport`) entries only.

### How the delays are measured

nextpnr has no equivalent of Vivado's `report_timing -from <port> -to <port>`: its JSON timing report (`--report`) gives one critical path per pair of clock domains. The backend therefore wraps the unit in a top entity where every input port class (data, valid, ready, condition) is driven by flip-flops on a clock of its own (`clk_in_data`, `clk_in_valid`, ...) and every output port class is captured by flip-flops on a clock of its own (`clk_out_data`, ...). The unit's `clk` and `rst` stay top-level ports. The critical path from `clk_in_X` to `clk_out_Y` is then the longest path through the unit from a port of class X to a port of class Y, so one placement and routing gives all the delay classes at once:

| Delay | Path |
|---|---|
| `data` | `clk_in_data` -> `clk_out_data` |
| `valid`, `ready` | `clk_in_valid` -> `clk_out_valid`, `clk_in_ready` -> `clk_out_ready` |
| `VR`, `VD`, `VC` | `clk_in_valid` -> `clk_out_ready`, `clk_out_data`, `clk_out_condition` |
| `CV`, `CR` | `clk_in_condition` -> `clk_out_valid`, `clk_out_ready` |
| `inport` data | `clk_in_data` -> `clk` (data inputs to the unit's registers) |
| `outport` data | `clk` -> `clk_out_data` (the unit's registers to its data outputs) |

The delay of a path is the sum of its `logic` and `routing` segments; the `clk-to-q` of the launch flip-flop and the `setup` of the capture flip-flop belong to the wrapper and are left out. The routing from the launch flip-flop into the unit and from the unit to the capture flip-flop is inside the number: on a unit whose outputs are wires from its inputs this is about 0.47 ns (0.66 ns at 64 bits), which is not subtracted, while Vivado's port-to-port paths start and end at unplaced ports. The unit keeps its own hierarchy in the netlist (`keep_hierarchy`), so that synthesis cannot merge its logic with the wrapper's flip-flops. Buffer placement reads the `inport`/`outport` delays only for units with a latency, and only on the data signal, so the valid and ready port delays are written as zero.

The unit's own `clk` is a clock domain of its own as well, so the critical path from `clk` to `clk` is the longest path between two of the unit's registers. For a unit with a latency, this delay replaces the delay key of its `latency` entry: `"latency": {"0": {"32": {"7.214": 4.0}}}` says that the 32-bit unit has a latency of 4 cycles and needs a clock period of at least 7.214 ns. When every key of a unit is above the target clock period, Dynamatic prints `CRITICAL WARNING: an operator has no known implementation capable of running at the requested operating frequency` and takes the entry with the smallest key; for the units this backend characterizes the key is used for nothing else (only the floating-point units, which are skipped here, also read it as their internal delay). Here the delay includes the `clk-to-q` and `setup` of the two flip-flops, since both belong to the unit, and it is the worst over the seeds, since the key is a lower bound on the clock period and a unit placed in a full circuit is unlikely to get a better placement than alone in an empty device. Each characterized width gets its own entry, with the latency that the reference gives for that width; reference entries wider than the widest characterized width are kept unchanged. This register-to-register delay does not include the paths from the unit's inputs or to its outputs: those are the `inport` and `outport` delays above.

For each parameter set the backend writes a shell script (in the TCL directory) that runs Yosys, then nextpnr once per seed, then `nextpnr_backend.py` itself on the nextpnr reports. That last step writes, for each delay class, the report file that `report_parser.py` reads, in the same `Data Path Delay:` format as Vivado's, so the JSON assembly is shared with the Vivado flow. The per-seed delays of every clock pair are kept in `delays.json` next to the nextpnr reports.

### Limits

- Each unit is placed alone in an empty device: routing is shorter than in a full circuit.
- The units in `skipping_units` are not characterized, among them the floating-point units, which instantiate Vivado or FloPoCo cores; with `--reference-json` they are carried over from the reference. The latencies (number of cycles) come from the reference as well; only their delay keys are measured, and only for the characterized units.
- GHDL stops elaboration at a failing assertion of a VHDL unit: the legacy `control_merge` with data stops on purpose (it asks for the newer backend) and is carried over from the reference, and `sitofp` and `fptosi` are only characterized at 32 bits.
- Nothing retimes the units: `sitofp` and `fptosi` do their whole conversion before their first register, and their `inport` delay (about 12 and 9 ns) is longer than any usual clock period.
- Delays are not forced to grow with the bitwidth: a placement can make a narrower unit slower than a wider one by a few tenths of a nanosecond, while `BitwidthDepMetric` assumes a monotonic metric.

## Example: Parameter Sweep and Synthesis

Suppose you want to characterize a FIFO unit with varying depths and widths. You would set up `parameters_ranges` as follows:

```python
parameters_ranges = {
    "DEPTH": [8, 16, 32],
    "WIDTH": [8, 16, 32]
}
```

The script will automatically:

- Generate all combinations (e.g., DEPTH=8, WIDTH=8; DEPTH=8, WIDTH=16; ...).
- For each combination, generate a top-level testbench, TCL script, and SDC constraints.
- Run synthesis for each configuration in parallel.
- Collect and store timing/resource reports for later analysis.


## Example: Expected Report Structure

The synthesis report is expected to contain this line `Data Path Delay:        DELAY_VALUEns` which is used to extract its delay.

Please refer to `Using a New Synthesis Tool` section if the lines containing ports and delays information are different in your report.

## Notes

- The script is designed for batch automation in hardware design flows, specifically targeting VHDL and Xilinx Vivado.
- It assumes a certain structure for VHDL entities and their dependencies.
- Parallelization is controlled by the `NUM_CORES` variable.
- The script can be extended to support additional synthesis tools or more complex dependency structures.
