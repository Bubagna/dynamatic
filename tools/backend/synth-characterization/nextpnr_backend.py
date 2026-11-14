# Characterization backend for the Lattice ECP5, with Yosys (and its GHDL
# plugin, to read the VHDL units) for synthesis and nextpnr-ecp5 for placement
# and routing.
#
# nextpnr has no equivalent of Vivado's `report_timing -from <port> -to <port>`:
# its timing report gives one critical path per pair of clock domains. The
# wrapper generated here turns that into a per-port-class report: every input
# port class of the unit (data, valid, ready, condition) is launched by
# flip-flops on a clock of its own, and every output port class is captured by
# flip-flops on a clock of its own. The critical path from `clk_in_X` to
# `clk_out_Y` is then the longest combinational path through the unit from a
# port of class X to a port of class Y, and a single place and route measures
# all the delay classes at once. The unit's own `clk` is a clock of its own as
# well, so the critical path from `clk` to `clk` is the longest path between
# two of the unit's registers.
#
# Each parameter set gets a shell script that runs Yosys once, nextpnr once per
# placement seed, and then this file as a script, which reduces the seeds and
# writes, for every delay class, the report file `report_parser.py` reads.
import json
import os
import re
import shutil
import statistics
import subprocess
import sys

# Name given to --synth-tool to select this backend
SYNTH_TOOL = "ecp5"

# Target device. nextpnr only supports speed grade 8 for the ECP5-5G parts,
# which is also what it picks when no grade is given for them.
DEVICE = "--um5g-85k"
PACKAGE = "CABGA381"
SPEED_GRADE = 8

YOSYS = "yosys"
NEXTPNR = "nextpnr-ecp5"

DEFAULT_NUM_SEEDS = 3

# Port classes, each with its own launch and capture clock in the wrapper
PORT_CLASSES = ["data", "valid", "ready", "condition"]

# Classes of the paths that end or start at the unit's own registers, written
# to report files of their own (see `port_rpt_filename`). Only data: buffer
# placement reads a unit's port delays when the unit has a latency, and
# Dynamatic models latency on the data signal only.
PORT_DELAY_CLASSES = ["data"]


def is_ecp5(synth_tool):
    return synth_tool == SYNTH_TOOL


def parse_port_declaration(declaration):
    """
    Split a VHDL port declaration into its names, direction and type.

    A declaration can name several ports (`clk, rst : in std_logic`), and the
    direction cannot be guessed from the text with a substring test since port
    names such as `ins_ready` or `index` contain "in".

    Returns:
        List[Tuple[str, str, str]]: (name, direction, type) for each port.
    """
    names, _, rest = declaration.partition(":")
    match = re.match(r"\s*(in|out|inout|buffer)\s+(.*)$", rest, re.DOTALL | re.IGNORECASE)
    assert match, f"Could not parse port declaration `{declaration}`."
    direction = match.group(1).lower()
    vhdl_type = match.group(2).strip()
    return [(name.strip(), direction, vhdl_type) for name in names.split(",") if name.strip()]


def generate_wrapper_top(entity_name, vhdl_interface_info, param_names):
    """
    Generate the multi-clock wrapper around the unit.

    The unit's `clk` and `rst` stay top-level ports. Every other input is driven
    by a flip-flop on `clk_in_<class>`, every other output is captured by a
    flip-flop on `clk_out_<class>`. Parameters appear as `<NAME>_const_value`
    placeholders, as in the Vivado wrapper.

    Returns:
        Tuple[str, Dict[str, List[str]]]: the VHDL of the wrapper, and the input
        and output port classes the unit has.
    """
    generics = [g.split(":")[0].strip() for g in vhdl_interface_info.get_list_generics()]
    for generic in generics:
        assert generic in param_names, f"Generic `{generic}` not found in parameter names."

    ports = []
    for declaration in vhdl_interface_info.get_list_ports():
        for name, direction, vhdl_type in parse_port_declaration(declaration):
            assert direction in ("in", "out"), f"Port `{name}` of {entity_name} is `{direction}`."
            for param in param_names:
                vhdl_type = re.sub(rf"\b{param}\b", f"{param}_const_value", vhdl_type)
            port_class = vhdl_interface_info.categorize_ports([name])[name]
            ports.append((name, direction, vhdl_type, port_class))

    classes = {"in": [], "out": []}
    for _, direction, _, port_class in ports:
        if port_class in PORT_CLASSES and port_class not in classes[direction]:
            classes[direction].append(port_class)
    classes = {d: [c for c in PORT_CLASSES if c in classes[d]] for d in classes}

    top_ports = []
    signals = []
    registers = []
    port_map = []
    for name, direction, vhdl_type, port_class in ports:
        if port_class not in PORT_CLASSES:
            # clk and rst
            top_ports.append(f"{name} : in {vhdl_type}")
            port_map.append(f"{name} => {name}")
            continue
        signals.append(f"signal {name} : {vhdl_type};")
        port_map.append(f"{name} => {name}")
        if direction == "in":
            top_ports.append(f"{name}_launch : in {vhdl_type}")
            registers.append(
                f"launch_{name} : process (clk_in_{port_class})\nbegin\n"
                f"  if rising_edge(clk_in_{port_class}) then\n    {name} <= {name}_launch;\n  end if;\nend process;")
        else:
            top_ports.append(f"{name}_capture : out {vhdl_type}")
            registers.append(
                f"capture_{name} : process (clk_out_{port_class})\nbegin\n"
                f"  if rising_edge(clk_out_{port_class}) then\n    {name}_capture <= {name};\n  end if;\nend process;")
    clocks = [f"clk_in_{c} : in std_logic" for c in classes["in"]] + \
        [f"clk_out_{c} : in std_logic" for c in classes["out"]]

    generic_map = [f"{param} => {param}_const_value" for param in param_names if param in generics]
    generic_map_str = "generic map (\n" + ",\n".join(generic_map) + "\n)\n" if generic_map else ""

    newline = "\n"
    wrapper_top = f"""library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use ieee.math_real.all;
use work.types.all;
entity tb is
port (
{(';' + newline).join(clocks + top_ports)}
);
end entity;
architecture tb_arch of tb is
{newline.join(signals)}
begin
{newline.join(registers)}
dut: entity work.{entity_name}
{generic_map_str}port map (
{(',' + newline).join(port_map)}
);
end architecture;
"""
    return wrapper_top, classes


def order_vhdl_files(top_file, hdl_files):
    """
    List the VHDL files the top file needs, each after the files it uses.

    GHDL analyses files in the order it is given them and an entity must be
    analysed before a design unit that instantiates it, while Vivado sorts the
    files itself. Files nothing reaches from the top are left out, so that a
    support file the unit does not use cannot stop the run.
    """
    defined_by = {}
    used = {}
    for hdl_file in [top_file] + hdl_files:
        with open(hdl_file, "r") as f:
            code = re.sub(r"--.*", "", f.read()).lower()
        for unit in re.findall(r"^\s*(?:entity|package)\s+(\w+)\s+is\b", code, re.MULTILINE):
            defined_by.setdefault(unit, hdl_file)
        used[hdl_file] = set(re.findall(r"\bwork\.(\w+)", code)) | \
            set(re.findall(r"^\s*component\s+(\w+)", code, re.MULTILINE))

    ordered = []

    def visit(hdl_file, visiting):
        if hdl_file in ordered or hdl_file in visiting:
            return
        visiting.add(hdl_file)
        for unit in sorted(used[hdl_file]):
            if unit in defined_by and defined_by[unit] != hdl_file:
                visit(defined_by[unit], visiting)
        ordered.append(hdl_file)

    visit(top_file, set())
    return ordered


def find_ghdl_prefix():
    """
    Find the GHDL library directory for Yosys' GHDL plugin.

    The OSS CAD Suite sets GHDL_PREFIX in its `environment` script; when the
    tools are called without it, the plugin cannot find the `std` and `ieee`
    libraries. They sit next to Yosys' data directory in the suite.
    """
    if os.environ.get("GHDL_PREFIX"):
        return os.environ["GHDL_PREFIX"]
    if not shutil.which("yosys-config"):
        return None
    datdir = subprocess.run(["yosys-config", "--datdir"], capture_output=True, text=True).stdout.strip()
    candidate = os.path.normpath(os.path.join(datdir, "..", "..", "lib", "ghdl"))
    return candidate if os.path.isdir(candidate) else None


def rpt_filename(rpt_dir, top_entity_name, unique_id, delay_type):
    return f"{rpt_dir}/rpt_timing_{top_entity_name}_top_{unique_id}_{delay_type[0]}_{delay_type[1]}.txt"


def port_rpt_filename(rpt_dir, top_entity_name, unique_id, side, port_class):
    """
    Report of the paths between the ports of one class and the unit's own
    registers: `in` for the paths from the input ports to the registers, `out`
    for the paths from the registers to the output ports.
    """
    return f"{rpt_dir}/rpt_port_{top_entity_name}_top_{unique_id}_{side}_{port_class}.txt"


def internal_rpt_filename(rpt_dir, top_entity_name, unique_id):
    """
    Report of the longest path between two of the unit's own registers.
    """
    return f"{rpt_dir}/rpt_internal_{top_entity_name}_top_{unique_id}.txt"


def write_run_script(unit_char, top_file, tcl_dir, rpt_dir, num_seeds):
    """
    Write the shell script that synthesizes, places and routes one parameter
    set and writes its per-class report files.

    Args:
        unit_char (UnitCharacterization): The parameter set; its map from delay
            class to report file is filled here.
        top_file (str): The wrapper for this parameter set.
        tcl_dir (str): Directory for the scripts.
        rpt_dir (str): Directory for the reports.
        num_seeds (int): Number of placement seeds to reduce over.

    Returns:
        str: Path to the script.
    """
    name = f"{unit_char.top_entity_name}_top_{unit_char.unique_id}"
    work_dir = f"{rpt_dir}/{name}"
    os.makedirs(work_dir, exist_ok=True)
    hdl_files = [f for f in unit_char.hdl_files if f != top_file and f.endswith(".vhd")]
    vhdl_files = order_vhdl_files(top_file, hdl_files)

    delay_rpts = {}
    for delay_type in unit_char.list_delay_types:
        rpt = rpt_filename(rpt_dir, unit_char.top_entity_name, unit_char.unique_id, delay_type)
        unit_char.map_signals_type_to_delay_rpt[delay_type] = rpt
        delay_rpts[rpt] = list(delay_type)
    port_rpts = {}
    for side in ("in", "out"):
        for port_class in PORT_DELAY_CLASSES:
            rpt = port_rpt_filename(rpt_dir, unit_char.top_entity_name, unit_char.unique_id, side, port_class)
            port_rpts[rpt] = [side, port_class]
    internal_rpt = internal_rpt_filename(rpt_dir, unit_char.top_entity_name, unit_char.unique_id)

    seeds = list(range(1, num_seeds + 1))
    pnr_reports = [f"{work_dir}/pnr_seed{seed}.json" for seed in seeds]
    job_file = f"{work_dir}/job.json"
    with open(job_file, "w") as f:
        json.dump({"pnr_reports": pnr_reports, "delay_rpts": delay_rpts, "port_rpts": port_rpts,
                   "internal_rpt": internal_rpt, "summary": f"{work_dir}/delays.json"}, f, indent=2)

    # The DUT keeps its hierarchy, so that synthesis cannot optimize across the
    # boundary between the unit and the wrapper's flip-flops (for instance by
    # moving output logic into their reset or enable pins): the measured paths
    # are then the unit's alone.
    yosys_script = f"{work_dir}/synth.ys"
    with open(yosys_script, "w") as f:
        f.write(f"ghdl --std=08 {' '.join(vhdl_files)} -e tb\n")
        f.write("setattr -set keep_hierarchy 1 tb/dut\n")
        f.write(f"synth_ecp5 -top tb -json {work_dir}/tb.json\n")

    script = f"{tcl_dir}/run_{name}.sh"
    ghdl_prefix = find_ghdl_prefix()
    with open(script, "w") as f:
        f.write("#!/usr/bin/env bash\n")
        if ghdl_prefix:
            f.write(f"export GHDL_PREFIX=\"{ghdl_prefix}\"\n")
        # Reports left by an earlier run must not pass for this run's
        f.write(f"rm -f {work_dir}/tb.json {' '.join(pnr_reports)} {' '.join(delay_rpts)} {' '.join(port_rpts)} {internal_rpt}\n")
        f.write(f"{YOSYS} -m ghdl -q -l {work_dir}/synth.log -s {yosys_script} || exit 1\n")
        for seed, pnr_report in zip(seeds, pnr_reports):
            f.write(f"{NEXTPNR} {DEVICE} --package {PACKAGE} --speed {SPEED_GRADE} --json {work_dir}/tb.json "
                    f"--out-of-context --threads 1 --timing-allow-fail --seed {seed} "
                    f"--report {pnr_report} -q -l {work_dir}/pnr_seed{seed}.log\n")
        f.write(f"\"{sys.executable}\" \"{os.path.abspath(__file__)}\" {job_file}\n")
    return script


def path_delays(pnr_report):
    """
    Read nextpnr's report and return, for each pair of clock domains, the delay
    of its critical path without the launch flip-flop's clock-to-output and the
    capture flip-flop's setup time, which belong to the wrapper.

    Returns:
        Dict[Tuple[str, str], float]: {(from clock, to clock): delay in ns}.
    """
    with open(pnr_report, "r") as f:
        report = json.load(f)
    delays = {}
    for path in report.get("critical_paths", []):
        start = path["from"].replace("posedge ", "")
        end = path["to"].replace("posedge ", "")
        delays[(start, end)] = sum(seg["delay"] for seg in path["path"] if seg["type"] in ("logic", "routing"))
    return delays


def internal_delay(pnr_report):
    """
    Read nextpnr's report and return the delay of the critical path from the
    unit's `clk` to its `clk`, that is between two of the unit's own registers,
    or None if the unit has no such path. Unlike in `path_delays`, the
    clock-to-output and setup times are counted: both flip-flops belong to the
    unit, and the delay is the shortest clock period the unit can run at.
    """
    with open(pnr_report, "r") as f:
        report = json.load(f)
    for path in report.get("critical_paths", []):
        if path["from"] == "posedge clk" and path["to"] == "posedge clk":
            return sum(seg["delay"] for seg in path["path"])
    return None


def clock_pair(delay_type):
    return f"clk_in_{delay_type[0]}", f"clk_out_{delay_type[1]}"


def port_clock_pair(side, port_class):
    return (f"clk_in_{port_class}", "clk") if side == "in" else ("clk", f"clk_out_{port_class}")


def write_rpt(rpt_file, delay, what, reduction="median over seeds"):
    # The line follows Vivado's, so that report_parser.py reads it unchanged.
    with open(rpt_file, "w") as f:
        if delay is None:
            f.write(f"No path {what} (nextpnr-ecp5)\n")
        else:
            f.write(f"Data Path Delay:        {delay:.3f}ns  ({what}, {reduction}, nextpnr-ecp5)\n")


def extract_job(job_file):
    """
    Reduce the seeds of one parameter set (median per clock pair, over the
    seeds whose report has that path; worst seed for the register-to-register
    delay) and write its report files.
    """
    with open(job_file, "r") as f:
        job = json.load(f)
    per_seed = []
    internal = []
    for pnr_report in job["pnr_reports"]:
        if os.path.exists(pnr_report):
            per_seed.append(path_delays(pnr_report))
            delay = internal_delay(pnr_report)
            if delay is not None:
                internal.append(delay)
        else:
            print(f"[ERROR] Missing nextpnr report {pnr_report}")
    if not per_seed:
        # No report file at all: report_parser.py then warns about this
        # parameter set instead of reading zeros.
        sys.exit(1)

    def reduce(pair):
        values = [delays[pair] for delays in per_seed if pair in delays]
        return statistics.median(values) if values else None

    summary = {"seeds": len(per_seed), "paths": {}, "clk -> clk with clk-to-q and setup": [round(d, 4) for d in internal]}
    for delays in per_seed:
        for pair in delays:
            summary["paths"].setdefault(f"{pair[0]} -> {pair[1]}", []).append(round(delays[pair], 4))
    for rpt_file, delay_type in job["delay_rpts"].items():
        pair = clock_pair(delay_type)
        write_rpt(rpt_file, reduce(pair), f"{pair[0]} -> {pair[1]}")
    for rpt_file, (side, port_class) in job["port_rpts"].items():
        pair = port_clock_pair(side, port_class)
        write_rpt(rpt_file, reduce(pair), f"{pair[0]} -> {pair[1]}")
    # The worst seed rather than the median: this delay is read as the shortest
    # period the unit runs at, and a unit placed among the rest of a circuit
    # does not get a better placement than alone in an empty device.
    write_rpt(job["internal_rpt"], max(internal) if internal else None, "clk -> clk", "worst over seeds")
    with open(job["summary"], "w") as f:
        json.dump(summary, f, indent=2)


def add_port_delays(map_unit_to_list_unit_chars, rpt_dir, json_output):
    """
    Add to the characterized model the `inport` and `outport` data delays: from
    the unit's data inputs to its registers, and from its registers to its data
    outputs. These are what buffer placement uses for a unit with a latency in
    place of the input-to-output delay. The valid and ready port delays are
    left at zero.

    Args:
        map_unit_to_list_unit_chars (dict): Unit name to its UnitCharacterization objects.
        rpt_dir (str): Directory of the reports.
        json_output (str): Path to the characterized model, rewritten in place.
    """
    # Imported here: report_parser is only needed by the main script
    from report_parser import extract_single_rpt
    with open(json_output, "r") as f:
        output_data = json.load(f)
    for unit_name, list_unit_chars in map_unit_to_list_unit_chars.items():
        if unit_name not in output_data:
            continue
        for side, key in (("in", "inport"), ("out", "outport")):
            data = {}
            for unit_char in list_unit_chars:
                rpt = port_rpt_filename(rpt_dir, unit_char.top_entity_name, unit_char.unique_id, side, "data")
                if os.path.exists(rpt):
                    data[str(unit_char.get_parameter_value("DATA_TYPE"))] = extract_single_rpt(rpt)
            if not data:
                continue
            output_data[unit_name][key] = {"delay": {"data": data, "valid": {"1": 0.0}, "ready": {"1": 0.0},
                                                     "VR": 0.0, "CV": 0.0, "CR": 0.0, "VC": 0.0, "VD": 0.0}}
    with open(json_output, "w") as f:
        json.dump(output_data, f, indent=2)


def keyed_latency(latency_by_width, delays):
    """
    Rebuild the bitwidth level of one retiming path of a latency map, with one
    entry per measured width, keyed by the delay measured at that width.

    Dynamatic takes, for a unit of width W, the entry of the smallest width
    that is >= W (`BitwidthDepMetric::select`), so each measured width takes
    the latency of that entry. Entries wider than the widest measured width
    are kept as they are, so that the widths only they cover stay covered.
    The latency Dynamatic selects for any width is then the same as before.

    Args:
        latency_by_width (dict): {width: {delay key: latency}}, from the model.
        delays (dict): {width (int): register-to-register delay in ns}.

    Returns:
        dict: The new {width: {delay key: latency}}, or None if a measured
        width has no entry to take its latency from, or an entry with several
        delay keys (those are several implementations, and a single RTL was
        characterized), or if an entry narrower than the widest measured width
        was not measured (dropping it could change the selected latency).
    """
    widths = sorted(int(width) for width in latency_by_width)
    if any(width < max(delays) and width not in delays for width in widths):
        return None
    keyed = {}
    for width, delay in sorted(delays.items()):
        ceil = next((w for w in widths if w >= width), None)
        if ceil is None or len(latency_by_width[str(ceil)]) != 1:
            return None
        latency = next(iter(latency_by_width[str(ceil)].values()))
        keyed[str(width)] = {f"{delay:.3f}": latency}
    for width in widths:
        if width > max(delays):
            keyed[str(width)] = latency_by_width[str(width)]
    return keyed


def set_latency_keys(map_unit_to_list_unit_chars, rpt_dir, json_output):
    """
    Key the latencies of the characterized units with a latency by the longest
    register-to-register delay measured inside the unit at each width, in place
    of the keys taken from the reference model. Dynamatic picks the latency of
    the largest key that does not exceed the target clock period, and warns
    that the unit cannot run at that period when every key exceeds it. The
    number of cycles still comes from the reference model. Must run after
    `merge_reference_model`, which gives the units their latency.

    Args:
        map_unit_to_list_unit_chars (dict): Unit name to its UnitCharacterization objects.
        rpt_dir (str): Directory of the reports.
        json_output (str): Path to the characterized model, rewritten in place.
    """
    # Imported here: report_parser is only needed by the main script
    from report_parser import extract_single_rpt
    with open(json_output, "r") as f:
        output_data = json.load(f)
    for unit_name, list_unit_chars in map_unit_to_list_unit_chars.items():
        latency = output_data.get(unit_name, {}).get("latency", {})
        delays = {}
        for unit_char in list_unit_chars:
            rpt = internal_rpt_filename(rpt_dir, unit_char.top_entity_name, unit_char.unique_id)
            if not os.path.exists(rpt):
                continue
            delay = extract_single_rpt(rpt)
            # A report without a path reads as 0.0, which as a key would claim
            # that the unit runs at any clock period
            if delay > 0.0:
                delays[unit_char.get_parameter_value("DATA_TYPE")] = delay
        if not delays:
            continue
        for path_id, latency_by_width in latency.items():
            if all(lat == 0.0 for by_delay in latency_by_width.values() for lat in by_delay.values()):
                continue
            keyed = keyed_latency(latency_by_width, delays)
            if keyed is None:
                print("\033[93m" + f"[WARNING] Latency of unit {unit_name} (path {path_id}) kept as in the reference: "
                      "its widths and the measured ones do not match (see keyed_latency)." + "\033[0m")
                continue
            latency[path_id] = keyed
            print(f"[INFO] Latency of unit {unit_name} (path {path_id}) keyed by its register-to-register delay: {keyed}")
    with open(json_output, "w") as f:
        json.dump(output_data, f, indent=2)


if __name__ == "__main__":
    extract_job(sys.argv[1])
