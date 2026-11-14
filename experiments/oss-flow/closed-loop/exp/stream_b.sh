#!/bin/bash
# Stream B: fair baselines and fixed wire delays (see REPORT). Sequential.
D=${DYNAMATIC_DIR:-$(cd "$(dirname "$0")/../../../.." && pwd)}
P=$D/build/python3-venv/bin/python
CL=$D/experiments/oss-flow/closed-loop/closed_loop.py
E=$D/experiments/oss-flow/closed-loop/exp
TM=$D/data/components-ecp5.json
Q=$D/experiments/oss-flow/qor/runs
src() { local s=$D/integration-test/$1/$1.c; [[ -f $s ]] || s=$D/tutorials/Introduction/Ch1/$1.c; echo $s; }
run() {  # <name> <closed_loop args...>
  local name=$1; shift
  echo "$(date +%T) start $name"
  $P $CL --timing-models $TM --out $E/$name "$@" > $E/$name.log 2>&1
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "$(date +%T) $name failed (exit $rc), retrying once"
    $P $CL --timing-models $TM --out $E/${name}-retry "$@" > $E/${name}-retry.log 2>&1
    rc=$?
  fi
  echo "$(date +%T) done $name exit=$rc"
}
# B1: open loop at tighter targets (T=7 is the scale iteration at T'=6.938)
run binary_search_ecp5_6_open --src $(src binary_search) --period 6 --strategy wire --max-iter 1
run binary_search_ecp5_5_open --src $(src binary_search) --period 5 --strategy wire --max-iter 1
# B2: fixed d = 0.34 ns on every channel, T = 8, plus seeds 2 and 3 of the open-loop runs
for k in fir loop_multiply matvec; do
  run ${k}_ecp5_8_wire-d0.34 --src $(src $k) --period 8 --strategy wire --max-iter 1 \
    --wire-d0 0.34 --wire-ir $Q/$k/ecp5_8ns/out/comp/handshake_transformed.mlir
  nice -n 15 $D/experiments/oss-flow/closed-loop/seedcheck/seeds.sh ${k}_ecp5_8_open $Q/$k/ecp5_8ns/out 2 3
done
# B3: fixed d = 0.28 ns on binary_search, T = 8
run binary_search_ecp5_8_wire-d0.28 --src $(src binary_search) --period 8 --strategy wire --max-iter 1 \
  --wire-d0 0.28 --wire-ir $Q/binary_search/ecp5_8ns/out/comp/handshake_transformed.mlir
# B4: wire loop at T = 10
run binary_search_ecp5_10_wire --src $(src binary_search) --period 10 --strategy wire --max-iter 4 \
  --iter0 $Q/binary_search/ecp5_10ns
echo "$(date +%T) stream B finished"
