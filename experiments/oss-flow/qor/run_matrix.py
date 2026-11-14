#!/usr/bin/env python3
"""Run flow.py over kernels x timing model x target period, one row per run
appended to results.csv as soon as the run finishes. Resumable: rows already
in the CSV (same kernel, model, period) are skipped. Runs are sequential.

Usage: python run_matrix.py [--periods 8 10] [--kernels k1 k2 ...]
                            [--target-ok 10]
"""

import argparse
import csv
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from flow import DYN, METRIC_KEYS, run_flow  # noqa: E402

MODELS = {
    "kintex7": None,  # default data/components.json
    "ecp5": os.path.join(DYN, "data/components-ecp5.json"),
}
# The six requested first, then small integer kernels from integration-test/
KERNELS = [
    "loop_multiply", "fir", "binary_search", "matvec", "insertion_sort", "atax",
    "iir", "matrix", "vector_rescale", "sumi3_mem", "threshold", "triangular",
    "polyn_mult", "stencil_2d", "image_resize", "video_filter", "bicg",
    "jacobi_1d_imper", "matrix_power", "kernel_2mm",
]
CSV_PATH = os.path.join(HERE, "results.csv")
COLUMNS = ["model"] + list(METRIC_KEYS)


def kernel_src(k):
    for p in (f"{DYN}/integration-test/{k}/{k}.c",
              f"{DYN}/tutorials/Introduction/Ch1/{k}.c"):
        if os.path.exists(p):
            return p
    raise FileNotFoundError(k)


def read_rows():
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, newline="") as f:
        return list(csv.DictReader(f))


def append_row(row):
    new = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new:
            w.writeheader()
        out = {}
        for k in COLUMNS:
            v = row.get(k)
            if isinstance(v, (dict, list)):
                v = json.dumps(v, sort_keys=True)
            out[k] = "" if v is None else v
        w.writerow(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--periods", nargs="+", type=float, default=[8.0, 10.0])
    ap.add_argument("--kernels", nargs="+", default=KERNELS)
    ap.add_argument("--target-ok", type=int, default=10,
                    help="stop after this many kernels with all runs ok")
    a = ap.parse_args()

    def done_keys():
        return {(r["kernel"], r["model"], float(r["target_period_ns"]))
                for r in read_rows()}

    def kernel_all_ok(k):
        rows = [r for r in read_rows() if r["kernel"] == k]
        need = {(m, p) for m in MODELS for p in a.periods}
        got = {(r["model"], float(r["target_period_ns"])) for r in rows
               if r["status"] == "ok"}
        return need <= got

    for k in a.kernels:
        if sum(kernel_all_ok(x) for x in a.kernels) >= a.target_ok:
            break
        src = kernel_src(k)
        for p in a.periods:
            for model, tm in MODELS.items():
                if (k, model, p) in done_keys():
                    continue
                out = os.path.join(HERE, "runs", k, f"{model}_{p:g}ns")
                t0 = time.time()
                print(f"[{time.strftime('%H:%M:%S')}] {k} {model} {p:g} ns",
                      flush=True)
                try:
                    m = run_flow(src, out, p, timing_models=tm,
                                 buffer_algorithm="fpl22", milp_solver="cbc",
                                 hdl="vhdl", calculate_path_delays=True)
                except Exception as e:  # record, do not stop the matrix
                    traceback.print_exc()
                    m = {key: None for key in METRIC_KEYS}
                    m.update(kernel=k, target_period_ns=p, status="flow.py",
                             error=f"{type(e).__name__}: {e}",
                             buffer_algorithm="fpl22", out_dir=out)
                m["model"] = model
                append_row(m)
                print(f"    -> {m['status']} est={m['est_cp_ns']} "
                      f"cp={m['cp_ns']} cyc={m['cycles']} "
                      f"({time.time() - t0:.0f} s) {m['error'] or ''}",
                      flush=True)


if __name__ == "__main__":
    main()
