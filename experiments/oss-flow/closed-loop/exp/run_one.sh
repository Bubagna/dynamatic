#!/bin/bash
# usage: run_one.sh <kernel> <model: ecp5|kintex7> <T> <strategy> <max-iter>
k=$1; model=$2; T=$3; s=$4; n=$5
D=${DYNAMATIC_DIR:-$(cd "$(dirname "$0")/../../../.." && pwd)}
P=$D/build/python3-venv/bin/python
if [[ $model == ecp5 ]]; then TM=$D/data/components-ecp5.json; else TM=$D/data/components.json; fi
SRC=$D/integration-test/$k/$k.c
[[ -f $SRC ]] || SRC=$D/tutorials/Introduction/Ch1/$k.c
I0=$D/experiments/oss-flow/qor/runs/$k/${model}_${T%.*}ns
ARGS=()
[[ -f $I0/metrics.json ]] && ARGS=(--iter0 $I0)
OUT=$D/experiments/oss-flow/closed-loop/exp/${k}_${model}_${T}_${s}
$P $D/experiments/oss-flow/closed-loop/closed_loop.py --src $SRC --out $OUT --period $T --timing-models $TM --strategy $s --max-iter $n "${ARGS[@]}" > $OUT.log 2>&1
echo "$(date +%T) done $k $model $T $s exit=$?"
