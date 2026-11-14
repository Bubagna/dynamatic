#!/bin/bash
# usage: seeds.sh <label> <dynamatic out dir> [seeds...]
# Place and route the netlist of a finished run again with other nextpnr seeds
# and print the critical path of each (ns).
label=$1; out=$2; shift 2
seeds=${@:-1 2 3}
# Yosys and nextpnr-ecp5 (e.g. from OSS CAD Suite) must be on PATH
json=$(ls "$out"/synth/*.json | grep -v nextpnr_report | head -1)
sdc=$(ls "$out"/synth/*.sdc | head -1)
here=$(cd "$(dirname "$0")" && pwd)
for s in $seeds; do
  log="$here/${label}_s$s.log"
  nice -n 10 nextpnr-ecp5 --um5g-85k --package CABGA381 --json "$json" --out-of-context \
    --sdc "$sdc" --timing-allow-fail --seed "$s" > "$log" 2>&1
  f=$(grep -o "Max frequency for clock.*" "$log" | tail -1 | sed -E 's/.*: ([0-9.]+) MHz.*/\1/')
  echo "$label seed$s fmax=$f cp=$(echo "scale=3; 1000/$f" | bc)"
done
