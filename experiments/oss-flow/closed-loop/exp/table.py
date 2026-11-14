#!/usr/bin/env python3
"""Prints one Markdown row per experiment: open loop (iteration 0) and the best
iteration of each strategy, from exp/<kernel>_<model>_<T>_<strategy>/."""

import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def cells(m):
    met = "yes" if m.get("cp_ns") and m["cp_ns"] <= m["T"] else "no"
    return [met, m.get("cp_ns"), m.get("cycles"), m.get("exec_time_ns"), m.get("buffers"),
            "%s/%s" % (m.get("lut4"), m.get("ff"))]


def main():
    rows = []
    for d in sorted(glob.glob(os.path.join(HERE, "*_*_*_*"))):
        if not os.path.isdir(d) or not os.path.isfile(os.path.join(d, "history.json")):
            continue
        name = os.path.basename(d)
        kernel, model, T, strategy = name.rsplit("_", 3)
        T = float(T)
        hist = json.load(open(os.path.join(d, "history.json")))
        summ = json.load(open(os.path.join(d, "summary.json"))) if os.path.isfile(
            os.path.join(d, "summary.json")) else {}
        best = summ.get("best_iter")
        for label, h in (("open loop", hist[0]), (strategy, hist[best] if best is not None else None)):
            if label == "open loop" and strategy == "scale" and any(r[0] == (kernel, model, T) and r[1] == "open loop" for r in rows):
                continue
            if h is None:
                rows.append(((kernel, model, T), label, ["-"] * 6, "-", "-", summ.get("stop")))
                continue
            m = dict(h["metrics"], T=T)
            extra = "T'=%.3f" % h["target_period_ns"] if strategy == "scale" and label != "open loop" else ""
            rows.append(((kernel, model, T), label, cells(m),
                         "%d (best %d)" % (len(hist), best) if label != "open loop" else "1",
                         summ.get("wall_s") if label != "open loop" else h.get("wall_s"),
                         (summ.get("stop") or "") + (" " + extra if extra else "") if label != "open loop" else ""))
    w = sys.stdout.write
    w("| kernel | model | T | run | met | cp_ns | cycles | exec_time_ns | buffers | LUT4/FF | iterations | wall s | stop |\n")
    w("|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
    for (k, model, T), label, c, it, wall, stop in rows:
        w("| %s | %s | %g | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |\n"
          % (k, model, T, label, *c, it, wall, stop))


if __name__ == "__main__":
    main()
