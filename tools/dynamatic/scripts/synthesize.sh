#!/bin/bash

source "$1"/tools/dynamatic/scripts/utils.sh

# ============================================================================ #
# Variable definitions
# ============================================================================ #

# Script arguments
DYNAMATIC_DIR=$1
OUTPUT_DIR=$2
KERNEL_NAME=$3
FULL_CLOCK=$4
HALF_CLOCK=$5
SYNTH_TOOL=${6:-vivado}

# Generated directories/files
SYNTH_DIR="$OUTPUT_DIR/synth"
SYNTH_HDL_DIR="$SYNTH_DIR/hdl"
F_REPORT="$SYNTH_DIR/report.txt"
F_SCRIPT="$SYNTH_DIR/synthesize.tcl"
F_PERIOD="$SYNTH_DIR/period_${FULL_CLOCK}.xdc"
F_UTILIZATION_SYN="$SYNTH_DIR/utilization_post_syn.rpt"
F_TIMING_SYN="$SYNTH_DIR/timing_post_syn.rpt"
F_UTILIZATION_PR="$SYNTH_DIR/utilization_post_pr.rpt"
F_TIMING_PR="$SYNTH_DIR/timing_post_pr.rpt"

# Shortcuts
HDL_DIR="$OUTPUT_DIR/hdl"

# Resources directory
RESOURCE_DIR="$DYNAMATIC_DIR/tools/backend/synth-resources"

# ============================================================================ #
# Open-source synthesis flow (Yosys and nextpnr, Lattice ECP5)
# ============================================================================ #

# Target FPGA, passed to nextpnr-ecp5 as --$ECP5_DEVICE --package $ECP5_PACKAGE
ECP5_DEVICE="um5g-85k"
ECP5_PACKAGE="CABGA381"

F_YOSYS_SCRIPT="$SYNTH_DIR/synthesize.ys"
F_YOSYS_LOG="$SYNTH_DIR/yosys.log"
F_NETLIST="$SYNTH_DIR/$KERNEL_NAME.json"
F_SDC="$SYNTH_DIR/period_${FULL_CLOCK}.sdc"
F_NEXTPNR_LOG="$SYNTH_DIR/nextpnr.log"
F_NEXTPNR_REPORT="$SYNTH_DIR/nextpnr_report.json"

# Prints how many cells of a type the placed design uses.
#   $1: nextpnr cell type (e.g., TRELLIS_FF)
used_cells() {
  awk -v cell="$1:" 'BEGIN { n = 0 } $1 == cell { split($2, a, "/"); n = a[1] }
    END { print n }' "$F_UTILIZATION_PR"
}

synthesize_yosys() {
  local tool
  for tool in yosys nextpnr-ecp5; do
    if ! command -v "$tool" > /dev/null; then
      echo_fatal "$tool not found in PATH, install the OSS CAD Suite \
(https://github.com/YosysHQ/oss-cad-suite-build) and add its bin/ to PATH"
      exit 1
    fi
  done

  shopt -s nullglob
  local verilog_files=("$HDL_DIR"/*.v) vhdl_files=("$HDL_DIR"/*.vhd)
  shopt -u nullglob
  if [[ ${#verilog_files[@]} -eq 0 && ${#vhdl_files[@]} -eq 0 ]]; then
    echo_fatal "No HDL found in $HDL_DIR, run write-hdl first"
    exit 1
  fi

  # Yosys only reads VHDL through the GHDL plugin, which is not always there
  local read_hdl="" synth_hint=""
  if [[ ${#vhdl_files[@]} -gt 0 ]]; then
    # The plugin needs the GHDL libraries. The OSS CAD Suite only points to
    # them (GHDL_PREFIX) in its environment script: without it, look for them
    # next to Yosys
    local ghdl_lib
    ghdl_lib="$(yosys-config --datdir 2> /dev/null)/../../lib/ghdl"
    if [[ -z "$GHDL_PREFIX" && -d "$ghdl_lib" ]]; then
      export GHDL_PREFIX="$ghdl_lib"
    fi

    if ! yosys -p "plugin -i ghdl" > /dev/null 2>&1; then
      echo_fatal "VHDL sources need the GHDL plugin of Yosys, which could not \
be loaded; without it, only designs written with 'write-hdl --hdl verilog' \
that have no LSQ and no floating-point unit can be synthesized"
      exit 1
    fi

    # GHDL elaborates a design from its top entity. When the top is in Verilog,
    # elaborate instead the VHDL entities that the Verilog instantiates (LSQs)
    local entity vhdl_tops="$KERNEL_NAME"
    synth_hint="; if the GHDL plugin cannot read the VHDL, generate Verilog \
with 'write-hdl --hdl verilog' instead"
    if [[ ! -f "$HDL_DIR/$KERNEL_NAME.vhd" ]]; then
      synth_hint="; the GHDL plugin cannot read VHDL that instantiates Verilog \
modules (e.g., FloPoCo units), generate VHDL with 'write-hdl --hdl vhdl' instead"
      vhdl_tops=$(grep -hioE \
        "^[[:space:]]*entity[[:space:]]+[[:alnum:]_]+[[:space:]]+is" \
        "${vhdl_files[@]}" | awk '{ print $2 }' \
        | grep -ohiwF -f - "${verilog_files[@]}" | sort -u)
    fi
    # -fsynopsys: the FloPoCo units use std_logic_arith, which Vivado accepts
    read_hdl="plugin -i ghdl"
    for entity in $vhdl_tops; do
      read_hdl="$read_hdl
ghdl --std=08 -fsynopsys ${vhdl_files[*]} -e $entity"
    done
  fi
  if [[ ${#verilog_files[@]} -gt 0 ]]; then
    read_hdl="$read_hdl
read_verilog -defer ${verilog_files[*]}"
  fi

  # GHDL turns VHDL assertions (e.g., in LSQs) into formal cells, which nextpnr
  # cannot place; like Vivado, ignore them
  echo \
"$read_hdl
hierarchy -check -top $KERNEL_NAME
chformal -remove
synth_ecp5 -top $KERNEL_NAME -noiopad -json $F_NETLIST
tee -o $F_UTILIZATION_SYN stat" > "$F_YOSYS_SCRIPT"

  echo "create_clock -name clk -period $FULL_CLOCK [get_ports clk]" > "$F_SDC"

  echo_info "Created synthesis scripts"
  echo_info "Launching Yosys synthesis"
  yosys -s "$F_YOSYS_SCRIPT" > "$F_YOSYS_LOG" 2>&1
  exit_on_fail "Logic synthesis failed, see $F_YOSYS_LOG$synth_hint" \
    "Logic synthesis succeeded"

  # Out of context, so that the kernel ports need no I/O pins. Like Vivado,
  # report missed timing instead of failing
  echo_info "Launching nextpnr place and route on ECP5 $ECP5_DEVICE"
  nextpnr-ecp5 --"$ECP5_DEVICE" --package "$ECP5_PACKAGE" \
    --json "$F_NETLIST" --out-of-context --sdc "$F_SDC" --timing-allow-fail \
    --report "$F_NEXTPNR_REPORT" > "$F_NEXTPNR_LOG" 2>&1
  exit_on_fail "Place and route failed, see $F_NEXTPNR_LOG" \
    "Place and route succeeded"

  # Same report names as the Vivado flow, extracted from the nextpnr log
  echo "Device utilisation of $KERNEL_NAME on ECP5 $ECP5_DEVICE \
($ECP5_PACKAGE), from nextpnr" > "$F_UTILIZATION_PR"
  awk '/Device utilisation:/ { found = 1; next }
    found && !/^Info:[[:space:]]+[[:alnum:]_]+:/ { exit }
    found { sub(/^Info:[ \t]*\t/, ""); print }' "$F_NEXTPNR_LOG" \
    >> "$F_UTILIZATION_PR"
  # Only the analysis after routing gives the final timing
  awk '/Routing complete/ { routed = 1 }
    routed && /Critical path report|Max frequency for clock|Max delay/ { found = 1 }
    routed && /Slack histogram/ { exit }
    found { print }' "$F_NEXTPNR_LOG" > "$F_TIMING_PR"

  echo_info "Utilization: $(used_cells TRELLIS_COMB) LUT4s (TRELLIS_COMB), \
$(used_cells TRELLIS_FF) FFs, $(used_cells MULT18X18D) DSPs (MULT18X18D), \
$(used_cells DP16KD) BRAMs (DP16KD)"
  grep -o "Max frequency for clock.*" "$F_TIMING_PR" | while read -r line; do
    echo_info "$line"
  done
}

# ============================================================================ #
# Synthesis flow
# ============================================================================ #

# Reset simulation directory
rm -rf "$SYNTH_DIR" && mkdir -p "$SYNTH_DIR"

if [[ "$SYNTH_TOOL" == "yosys" ]]; then
  synthesize_yosys
  exit 0
fi

# Copy all synthesizable components to specific folder for Vivado
mkdir -p "$SYNTH_HDL_DIR"
cp "$HDL_DIR/"*.vhd "$SYNTH_HDL_DIR" 2> /dev/null
cp "$HDL_DIR/"*.v "$SYNTH_HDL_DIR" 2> /dev/null

# See if we should include any VHDL in the synthesis script
READ_VHDL=""
if ls "$HDL_DIR"/*.vhd 1> /dev/null 2>&1; then
  cp "$HDL_DIR/"*.vhd "$SYNTH_HDL_DIR"
  READ_VHDL="read_vhdl -vhdl2008 [glob $SYNTH_DIR/hdl/*.vhd]"
fi

# See if we should include any Verilog in the synthesis script
READ_VERILOG=""
if ls "$HDL_DIR"/*.v 1> /dev/null 2>&1; then
  cp "$HDL_DIR/"*.v "$SYNTH_HDL_DIR"
  READ_VERILOG="read_verilog [glob $SYNTH_DIR/hdl/*.v]"
fi

# Source tcl resources
READ_TCL=""
if ls "$RESOURCE_DIR"/*.tcl 1> /dev/null 2>&1; then
  for f in "$RESOURCE_DIR"/*.tcl; do
    READ_TCL="$READ_TCL\nsource $f"
  done
fi

# Set vivado commands for vivado IPs for floating point operations
VIVADO_CMDS="set vivado_ver [version -short]
set fpo_ver 7.1
if {[regexp -nocase {2015\.1.*} $vivado_ver match]} {
    set fpo_ver 7.0
}
"

# Generate synthesis script
echo -e \
"set_param general.maxThreads 8
$VIVADO_CMDS
$READ_VHDL
$READ_VERILOG
$READ_TCL
read_xdc "$F_PERIOD"
synth_design -top $KERNEL_NAME -part xc7k160tfbg484-2 -no_iobuf -mode out_of_context
report_utilization > $F_UTILIZATION_SYN
report_timing > $F_TIMING_SYN
opt_design
place_design
phys_opt_design
route_design
phys_opt_design
report_utilization > $F_UTILIZATION_PR
report_timing > $F_TIMING_PR
exit" > "$F_SCRIPT"

echo -e \
"create_clock -name clk -period $FULL_CLOCK -waveform {0.000 $HALF_CLOCK} [get_ports clk]
set_property HD.CLK_SRC BUFGCTRL_X0Y0 [get_ports clk]

#set_input_delay 0 -clock CLK  [all_inputs]
#set_output_delay 0 -clock CLK [all_outputs]" > "$F_PERIOD"

echo_info "Created synthesis scripts"
echo_info "Launching Vivado synthesis"
cd "$SYNTH_DIR"
vivado -mode tcl -source "$F_SCRIPT" > "$F_REPORT"
exit_on_fail "Logic synthesis failed" "Logic synthesis succeeded"
