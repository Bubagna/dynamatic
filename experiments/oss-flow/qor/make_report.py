#!/usr/bin/env python3
"""Regenerate the tables of REPORT.md from results.csv.

Rewrites only the text between the BEGIN/END TABLES markers in REPORT.md
(creates the file with just the tables if it does not exist).
"""

import csv
import glob
import math
import os
import statistics

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "results.csv")
REPORT = os.path.join(HERE, "REPORT.md")
BEGIN, END = "<!-- BEGIN TABLES -->", "<!-- END TABLES -->"
MODELS = ("kintex7", "ecp5")


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fmt(v, nd=2):
    if v is None or v == "":
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def gmean(xs):
    return math.exp(sum(math.log(x) for x in xs) / len(xs)) if xs else None


def load():
    with open(CSV_PATH, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ("target_period_ns", "est_cp_ns", "cp_ns", "exec_time_ns",
                  "fmax_mhz"):
            r[k] = num(r[k])
        for k in ("buffers", "buffer_slots", "cycles", "lut4", "ff", "dsp",
                  "bram"):
            r[k] = int(float(r[k])) if r[k] not in ("", None) else None
        r["met"] = (r["cp_ns"] <= r["target_period_ns"]
                    if r["cp_ns"] is not None else None)
    return rows


def tables(rows):
    out = []
    periods = sorted({r["target_period_ns"] for r in rows})
    kernels = []
    for r in rows:
        if r["kernel"] not in kernels:
            kernels.append(r["kernel"])

    out.append("### Per-run results\n")
    out.append("T = target period (ns). est = est_cp_ns (MILP), cp = nextpnr "
               "critical path (ns), met = cp <= T, exec = cycles x cp (ns). "
               "LUT4 = TRELLIS_COMB, FF = TRELLIS_FF, DSP = MULT18X18D.\n")
    out.append("| kernel | T | model | status | buffers | slots | cycles | "
               "est | cp | met | exec | LUT4 | FF | DSP | sim |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    idx = {(r["kernel"], r["target_period_ns"], r["model"]): r for r in rows}
    for k in kernels:
        for p in periods:
            for mdl in MODELS:
                r = idx.get((k, p, mdl))
                if r is None:
                    continue
                met = {True: "yes", False: "no", None: "-"}[r["met"]]
                sim = {"True": "pass", "False": "FAIL"}.get(r["sim_pass"], "-")
                out.append(
                    f"| {k} | {p:g} | {mdl} | {r['status']} | "
                    f"{fmt(r['buffers'])} | {fmt(r['buffer_slots'])} | "
                    f"{fmt(r['cycles'])} | {fmt(r['est_cp_ns'])} | "
                    f"{fmt(r['cp_ns'])} | {met} | {fmt(r['exec_time_ns'], 0)} | "
                    f"{fmt(r['lut4'])} | {fmt(r['ff'])} | {fmt(r['dsp'])} | "
                    f"{sim} |")
    out.append("")

    ok = [r for r in rows if r["status"] == "ok"]
    out.append("### Timing target met (runs that reached synthesis)\n")
    out.append("| model | T | ok runs | met | mean cp/T | max cp |")
    out.append("|---|---|---|---|---|---|")
    for mdl in MODELS:
        for p in periods + [None]:
            rs = [r for r in ok if r["model"] == mdl
                  and (p is None or r["target_period_ns"] == p)
                  and r["cp_ns"] is not None]
            if not rs:
                continue
            met = sum(r["met"] for r in rs)
            ratio = statistics.mean(r["cp_ns"] / r["target_period_ns"]
                                    for r in rs)
            label = f"{p:g}" if p is not None else "all"
            out.append(f"| {mdl} | {label} | {len(rs)} | {met}/{len(rs)} "
                       f"({100 * met / len(rs):.0f}%) | {ratio:.2f} | "
                       f"{max(r['cp_ns'] for r in rs):.2f} |")
    out.append("")

    out.append("### Estimation error: est_cp_ns / cp_ns\n")
    out.append("Below 1 = the MILP believes the circuit is faster than "
               "nextpnr measures it.\n")
    out.append("| model | T | n | mean | median | min | max |")
    out.append("|---|---|---|---|---|---|---|")
    for mdl in MODELS:
        for p in periods + [None]:
            xs = [r["est_cp_ns"] / r["cp_ns"] for r in ok if r["model"] == mdl
                  and (p is None or r["target_period_ns"] == p)
                  and r["est_cp_ns"] and r["cp_ns"]]
            if not xs:
                continue
            label = f"{p:g}" if p is not None else "all"
            out.append(f"| {mdl} | {label} | {len(xs)} | "
                       f"{statistics.mean(xs):.3f} | "
                       f"{statistics.median(xs):.3f} | {min(xs):.3f} | "
                       f"{max(xs):.3f} |")
    out.append("")

    out.append("### ECP5 model relative to Kintex-7 model (geometric mean of "
               "ecp5/kintex7 over kernels where both runs are ok)\n")
    out.append("| T | pairs | exec_time | cycles | cp | LUT4 | FF | "
               "LUT4+FF | buffers |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for p in periods + [None]:
        pairs = []
        for k in kernels:
            for pp in periods:
                if p is not None and pp != p:
                    continue
                a = idx.get((k, pp, "kintex7"))
                b = idx.get((k, pp, "ecp5"))
                if a and b and a["status"] == "ok" and b["status"] == "ok":
                    pairs.append((a, b))
        if not pairs:
            continue

        def g(f):
            xs = [f(b) / f(a) for a, b in pairs
                  if f(a) is not None and f(b) is not None and f(a) > 0
                  and f(b) > 0]
            return f"{gmean(xs):.3f}" if xs else "-"
        label = f"{p:g}" if p is not None else "all"
        out.append(
            f"| {label} | {len(pairs)} | {g(lambda r: r['exec_time_ns'])} | "
            f"{g(lambda r: r['cycles'])} | {g(lambda r: r['cp_ns'])} | "
            f"{g(lambda r: r['lut4'])} | {g(lambda r: r['ff'])} | "
            f"{g(lambda r: (r['lut4'] or 0) + (r['ff'] or 0))} | "
            f"{g(lambda r: r['buffers'])} |")
    out.append("")

    out.append("### Buffer placement MILPs stopped by the Cbc time limit\n")
    out.append("Such a run uses the best solution found, not the optimum.\n")
    stopped = []
    for r in rows:
        sols = sorted(glob.glob(os.path.join(r["comp_dir"] or "",
                                             "cbc_solution_*.sol")))
        hit = [s for s in sols
               if open(s).readline().startswith("Stopped on time")]
        if hit:
            stopped.append(f"| {r['kernel']} | {r['target_period_ns']:g} | "
                           f"{r['model']} | {len(hit)} of {len(sols)} |")
    if not stopped:
        out.append("None.\n")
    else:
        out.append("| kernel | T | model | solves stopped |")
        out.append("|---|---|---|---|")
        out.extend(stopped)
        out.append("")

    fails = [r for r in rows if r["status"] != "ok"]
    out.append("### Failures\n")
    if not fails:
        out.append("None.\n")
    else:
        out.append("| kernel | T | model | step | error |")
        out.append("|---|---|---|---|---|")
        for r in fails:
            err = (r["error"] or "").replace("|", "\\|")
            out.append(f"| {r['kernel']} | {r['target_period_ns']:g} | "
                       f"{r['model']} | {r['status']} | `{err}` |")
        out.append("")
    return "\n".join(out)


def main():
    body = tables(load())
    block = f"{BEGIN}\n{body}\n{END}"
    if os.path.exists(REPORT):
        text = open(REPORT).read()
        if BEGIN in text and END in text:
            pre = text.split(BEGIN)[0]
            post = text.split(END, 1)[1]
            text = pre + block + post
        else:
            text = text.rstrip() + "\n\n" + block + "\n"
    else:
        text = "# QoR: Kintex-7 vs ECP5 timing model\n\n" + block + "\n"
    with open(REPORT, "w") as f:
        f.write(text)
    print(body)


if __name__ == "__main__":
    main()
