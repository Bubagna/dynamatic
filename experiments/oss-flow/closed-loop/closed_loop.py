#!/usr/bin/env python3
"""
Closed-loop buffer placement: place and route the circuit, map the real
critical path (nextpnr) onto the channels of the circuit before buffer
placement, tell the MILP where its timing model was wrong (back-annotated
channel delays), place buffers again.

  closed_loop.py --src <kernel.c> --out <dir> --period T --timing-models <json>
                 --strategy {path,scale} [--max-iter 8]
  closed_loop.py --show <dynamatic out dir> [--timing-models <json>]

Standard library only. The critical path is parsed by critical_path.py
(imported from the oss-critical-path worktree) and every run goes through
experiments/oss-flow/qor/flow.py.
"""

import argparse
import json
import os
import glob
import re
import subprocess
import sys
import time
from collections import deque

DYN = os.environ.get("DYNAMATIC_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))
CRITPATH_DIR = os.path.join(DYN, "tools/backend/synth-critical-path")
QOR_DIR = os.path.join(DYN, "experiments", "oss-flow", "qor")
ECP5_MODEL = os.path.join(DYN, "data/components-ecp5.json")

sys.path.insert(0, CRITPATH_DIR)
import critical_path as cpa  # noqa: E402

# Longest chain of channels used to join two units of the path that are not
# neighbours in the circuit (their logic was merged by flattening)
MAX_GAP = 6

NAME_RE = re.compile(r'handshake\.name\s*=\s*"([^"]+)"')
VALUE_RE = re.compile(r'%[\w$.\-]+(?:#\d+)?')
CTRL_RE = re.compile(r'%[\w$.\-]+(?:#\d+)?\s*:')
BUFTYPE_RE = re.compile(r'bufferType\s*=\s*(\w+)')


# ============================================================================ #
# Circuit before buffer placement (handshake_transformed.mlir)
# ============================================================================ #

class PreBufferIR:
    """Ops of handshake_transformed.mlir with their operands, in operand order.

    The handshake custom assembly prints the operands of every op in operand
    order (checked against the to_idx of the DOT edges, see check_edges)."""

    def __init__(self, path):
        self.ops = {}       # name -> {"op": mnemonic, "operands": [value]}
        self.producer = {}  # value -> (op name, result index) or ("arg", name)
        self.end = None
        self.memrefs = set()  # function arguments that are memories, not channels
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("handshake.func"):
                    sig = line[line.index("(") + 1:line.index(")")]
                    for arg in re.findall(r'(%[\w$.\-]+)\s*:', sig):
                        self.producer[arg] = ("arg", arg[1:])
                    self.memrefs.update(re.findall(r'(%[\w$.\-]+)\s*:\s*memref', sig))
                    continue
                m = NAME_RE.search(line)
                if not m:
                    continue
                name = m.group(1)
                lhs, rhs = ("", line)
                if re.match(r'^%\S.*?\s=\s', line):
                    lhs, rhs = line.split(" = ", 1)
                mnemonic = rhs.split()[0].split("[")[0].replace("handshake.", "")
                # Attributes hold no SSA values, so every value right of '='
                # is an operand
                operands = self.ordered_operands(rhs)
                self.ops[name] = {"op": mnemonic, "operands": operands}
                idx = 0
                for res in [r.strip() for r in lhs.split(",") if r.strip()]:
                    base, _, count = res.partition(":")
                    if count:
                        for i in range(int(count)):
                            self.producer["%s#%d" % (base, i)] = (name, idx)
                            idx += 1
                    else:
                        self.producer[base] = (name, idx)
                        idx += 1
                if mnemonic == "end":
                    self.end = name

    @staticmethod
    def ordered_operands(rhs):
        """SSA values of an op in operand order. The LSQ prints its groups as
        'groups(%ctrl0: [load(%a)], %ctrl1: [...])' but its operands are all
        the group controls first, then all the accesses."""
        m = re.search(r'\bgroups\((.*?)\)\s*(?:end\(|\{)', rhs)
        if not m or not CTRL_RE.search(m.group(1)):
            return VALUE_RE.findall(rhs)
        groups = m.group(1)
        ctrls = [c.rstrip(": ") for c in CTRL_RE.findall(groups)]
        accesses = VALUE_RE.findall(CTRL_RE.sub(":", groups))
        return (VALUE_RE.findall(rhs[:m.start(1)]) + ctrls + accesses
                + VALUE_RE.findall(rhs[m.end(1):]))

    def channels(self):
        """Every (consumer, operand index) that is a channel."""
        return [(name, i) for name, op in self.ops.items()
                for i, v in enumerate(op["operands"]) if v not in self.memrefs]

    def is_identity(self, name):
        """Ops that canonicalization removes after buffer placement: their
        output is their only input."""
        op = self.ops.get(name)
        if op is None:
            return False
        return op["op"] == "br" or (op["op"] in ("merge", "control_merge")
                                    and len(op["operands"]) == 1)

    def source_of(self, consumer, idx):
        """(producer, result index) of an operand, looking through the ops
        that are removed after buffer placement."""
        value = self.ops[consumer]["operands"][idx]
        prod = self.producer.get(value)
        while prod and prod[0] != "arg" and self.is_identity(prod[0]):
            prod = self.producer.get(self.ops[prod[0]]["operands"][0])
        return prod


# ============================================================================ #
# From the exported circuit (DOT) to channels before buffer placement
# ============================================================================ #

def buffer_types(export_mlir):
    types = {}
    with open(export_mlir) as f:
        for line in f:
            m, t = NAME_RE.search(line), BUFTYPE_RE.search(line)
            if m and t:
                types[m.group(1)] = t.group(1)
    return types


class ChannelMap:
    """Maps the channels (DOT edges) of the exported circuit onto the channels
    of the circuit before buffer placement, named as the back-annotation pass
    names them: (consumer op, operand index)."""

    def __init__(self, circuit, ir):
        self.c, self.ir = circuit, ir
        self.out_edges, self.in_edges = {}, {}
        for i, e in enumerate(circuit.edges):
            self.out_edges.setdefault(e["src"], []).append(i)
            self.in_edges.setdefault(e["dst"], []).append(i)

    def inserted(self, unit):
        """Units of the exported circuit that buffer placement inserted."""
        return unit in self.c.units and not self.c.is_arg(unit) and unit not in self.ir.ops

    def original(self, idx):
        """{consumer, operand, producer, units} of the channel that edge idx is
        part of; units are the inserted units (buffers) on it."""
        e = self.c.edges[idx]
        units = []
        while self.inserted(e["dst"]) and len(self.out_edges.get(e["dst"], [])) == 1:
            units.append(e["dst"])
            e = self.c.edges[self.out_edges[e["dst"]][0]]
        last = e
        e = self.c.edges[idx]
        while self.inserted(e["src"]) and len(self.in_edges.get(e["src"], [])) == 1:
            units.insert(0, e["src"])
            e = self.c.edges[self.in_edges[e["src"]][0]]
        first = e
        consumer = last["dst"]
        # Function results are drawn as nodes; the edge goes to the end op
        if self.c.is_arg(consumer):
            consumer = self.ir.end
        if consumer not in self.ir.ops or last["to_idx"] is None:
            return None
        return {"consumer": consumer, "operand": int(last["to_idx"]), "producer": first["src"],
                "producer_result": first["from_idx"], "units": units}

    def check(self, ch):
        """True if the producer in the IR is the producer in the DOT."""
        prod = self.ir.source_of(ch["consumer"], ch["operand"])
        if prod is None:
            return False
        if prod[0] == "arg":
            return prod[1] == ch["producer"]
        return prod[0] == ch["producer"] and str(prod[1]) == str(ch["producer_result"])

    def check_edges(self):
        """(consistent, total) over all channels of the exported circuit."""
        ok = total = 0
        for i in range(len(self.c.edges)):
            ch = self.original(i)
            if ch is None:
                continue
            total += 1
            ok += self.check(ch)
        return ok, total

    def route(self, a, b):
        """Shortest chain of channels joining two units of the path, as a list
        of (edge index, 'forward' | 'backward'). The path crosses a channel
        forward on data or valid and backward on ready, and can turn inside a
        unit (valid -> ready), so edges are followed both ways. None if the
        units are more than MAX_GAP channels apart."""
        seen, queue = {a: None}, deque([(a, 0)])
        while queue:
            u, depth = queue.popleft()
            if u == b:
                steps = []
                while seen[u] is not None:
                    idx, direction, prev = seen[u]
                    steps.insert(0, (idx, direction))
                    u = prev
                return steps
            if depth >= MAX_GAP:
                continue
            nexts = [(i, "forward", self.c.edges[i]["dst"]) for i in self.out_edges.get(u, [])]
            nexts += [(i, "backward", self.c.edges[i]["src"]) for i in self.in_edges.get(u, [])]
            for i, direction, w in nexts:
                if w not in seen and (w == b or not self.c.is_arg(w)):
                    seen[w] = (i, direction, u)
                    queue.append((w, depth + 1))
        return None


def load_run(comp_dir, report_path, model_path):
    """Parses a finished run; returns (circuit, analysis results, ChannelMap)."""
    dots = [p for p in os.listdir(comp_dir) if p.endswith(".dot") and not p.endswith("_CFG.dot")]
    if len(dots) != 1:
        raise RuntimeError("expected one circuit DOT in %s, found %s" % (comp_dir, dots))
    kernel = dots[0][:-4]
    out_dir = os.path.dirname(comp_dir)
    hdl = None
    for ext in (".vhd", ".v"):
        p = os.path.join(out_dir, "hdl", kernel + ext)
        if os.path.isfile(p):
            hdl = p
    netlist = os.path.join(os.path.dirname(report_path), kernel + ".json")
    flattened = cpa.is_flattened(netlist if os.path.isfile(netlist) else None)
    circuit = cpa.Circuit(os.path.join(comp_dir, dots[0]), hdl, exact_names=flattened is False)
    with open(report_path) as f:
        report = json.load(f)
    model = None
    if model_path:
        with open(model_path) as f:
            model = json.load(f)
    results = cpa.analyse(report, circuit, model)
    ir = PreBufferIR(os.path.join(comp_dir, "handshake_transformed.mlir"))
    return circuit, results, ChannelMap(circuit, ir), buffer_types(os.path.join(comp_dir, "handshake_export.mlir"))


def critical_channels(comp_dir, report_path, model_path=ECP5_MODEL, clock="clk"):
    """The channels the real critical path crosses, before buffer placement.

    Returns a dict with R (path delay, ns: clk-to-q + logic + routing + setup),
    the unit sequence, the channels (consumer, operand, signal, how it was
    found, whether the path's own start or end buffer already cuts it), the
    model delay M of the units on the path, and 'unit_limited'."""
    circuit, results, cmap, buftypes = load_run(comp_dir, report_path, model_path)
    paths = [r for r in results if r["clock"] == clock] or results
    if not paths:
        raise RuntimeError("no critical path in " + report_path)
    r = max(paths, key=lambda p: p["delay"])
    items = r["items"]
    hops = [it for it in items if it["kind"] == "unit"]
    start_unit = hops[0]["unit"] if hops and hops[0]["start"] else None
    end_unit = hops[-1]["unit"] if hops and hops[-1]["end"] else None

    # Units of the path that are units of the circuit, in order; cells at the
    # top level or named after nothing in the DOT are skipped
    seq = []
    for i, it in enumerate(items):
        if it["kind"] == "unit" and it["unit"] in circuit.units and not circuit.is_arg(it["unit"]):
            if not seq or seq[-1][0] != it["unit"]:
                seq.append((it["unit"], i))

    channels, order, gaps = {}, [], []

    def add(edge_idx, signal, how):
        ch = cmap.original(edge_idx)
        if ch is None:
            gaps.append("edge %s -> %s: consumer not in the IR before buffer placement"
                        % (circuit.edges[edge_idx]["src"], circuit.edges[edge_idx]["dst"]))
            return
        key = (ch["consumer"], ch["operand"])
        if key not in channels:
            ch.update(signals=[], how=how, ir_check=cmap.check(ch),
                      cut=bool({start_unit, end_unit} & set(ch["units"]) - {None}))
            channels[key] = ch
            order.append(key)
        if signal not in channels[key]["signals"]:
            channels[key]["signals"].append(signal)
        # A channel found from a net named after it is the most reliable
        if how == "net":
            channels[key]["how"] = "net"

    for (a, ia), (b, ib) in zip(seq, seq[1:]):
        named = [it["channel"] for it in items[ia + 1:ib] if it["kind"] == "net" and it["channel"]
                 and {it["channel"]["driver"][0], it["channel"]["receiver"][0]} & {a, b}]
        if named:
            for ch in named:
                for e in ch["edges"]:
                    add(e, ch["signal"], "net")
            continue
        found = cmap.route(a, b)
        if found is None:
            gaps.append("%s -> %s: no chain of at most %d channels joins them" % (a, b, MAX_GAP))
            continue
        # Adjacent: one channel, possibly through the buffers placed on it
        inner = [circuit.edges[e]["dst"] if d == "forward" else circuit.edges[e]["src"]
                 for e, d in found[:-1]]
        how = "adjacent" if all(cmap.inserted(u) for u in inner) else "inferred"
        for e, direction in found:
            add(e, "data/valid" if direction == "forward" else "ready", how)

    chans = [channels[k] for k in order]
    free = [c for c in chans if not c["cut"]]
    model_units = sum(it["model"] for it in hops if it.get("model") is not None)
    unknown = sum(1 for it in hops if it.get("model") is None and it["unit"] in circuit.units)
    if len(seq) <= 1 and not chans:
        limited = "the path starts and ends inside %s" % (seq[0][0] if seq else "?")
    elif not free:
        limited = "every channel on the path is already cut by its start or end buffer"
    else:
        limited = None
    return {
        "R": r["delay"], "clock": r["clock"], "period": r["period"], "fmax": r["fmax"],
        "start": "%s in %s" % (hops[0]["start"], hops[0]["unit"]) if start_unit else None,
        "end": "%s in %s" % (hops[-1]["end"], hops[-1]["unit"]) if end_unit else None,
        "units": [{"unit": it["unit"], "op": it["op"], "delay": round(it["logic"] + it["routing"], 3),
                   "model": it.get("model"), "in": it.get("in"), "out": it.get("out")} for it in hops],
        "channels": [{"consumer": c["consumer"], "operand": c["operand"], "producer": c["producer"],
                      "signals": c["signals"], "how": c["how"], "cut": c["cut"],
                      "buffers": ["%s (%s)" % (u, buftypes.get(u, "?")) for u in c["units"]],
                      "ir_check": c["ir_check"]} for c in chans],
        "gaps": gaps, "model_units": model_units, "units_without_model": unknown,
        "unit_limited": limited, "edge_check": cmap.check_edges(),
    }


def show(cp, w=sys.stdout.write):
    w("R = %.3f ns (clock %s, achieved %s MHz)\n" % (cp["R"], cp["clock"], cp["fmax"]))
    if cp["start"]:
        w("  starts at %s\n" % cp["start"])
    if cp["end"]:
        w("  ends at   %s\n" % cp["end"])
    w("  units: %s\n" % " -> ".join("%s(%.2f%s)" % (u["unit"], u["delay"],
                                                     ", model %.2f" % u["model"] if u["model"] else "")
                                    for u in cp["units"]))
    w("  model delay of the units on the path M = %.3f ns (%d units without a number)\n"
      % (cp["model_units"], cp["units_without_model"]))
    for c in cp["channels"]:
        w("  channel -> %s operand %d (from %s)  %-10s %-8s %s%s%s\n"
          % (c["consumer"], c["operand"], c["producer"], "/".join(c["signals"]), c["how"],
             "CUT " if c["cut"] else "", "via " + ", ".join(c["buffers"]) + " " if c["buffers"] else "",
             "" if c["ir_check"] else "[producer mismatch with IR]"))
    for g in cp["gaps"]:
        w("  gap: %s\n" % g)
    ok, total = cp["edge_check"]
    w("  DOT/IR consistency: %d of %d exported channels map to an operand with the same producer\n"
      % (ok, total))
    w("  unit-limited: %s\n" % (cp["unit_limited"] or "no"))


# ============================================================================ #
# The loop
# ============================================================================ #

def write_annotation(path, ann):
    data = {"operations": [], "operands": [
        {"operation-name": op, "operand-idx": idx, "attribute-type": "buffering-properties",
         "attribute-data": {"unbuf-delay": round(d, 4)}}
        for (op, idx), d in sorted(ann.items())]}
    with open(path, "w") as f:
        json.dump(data, f, indent=1)


def best_iteration(history, T):
    """Among the iterations that meet T (median cp over seeds), the fastest
    (cycles x median cp); otherwise the one with the lowest median cp."""
    ok = [h for h in history if h.get("cp_median")]
    met = [h for h in ok if h["cp_median"] <= T and h.get("exec_time_median_ns")]
    if met:
        return min(met, key=lambda h: h["exec_time_median_ns"])["iter"]
    if ok:
        return min(ok, key=lambda h: h["cp_median"])["iter"]
    return None


NEXTPNR_SEEDS = (2, 3)


def seed_cps(dyn_out, log_dir, seeds=NEXTPNR_SEEDS):
    """Places and routes the netlist of a finished run again with other
    nextpnr seeds (same options as seedcheck/seeds.sh); cp (ns) per seed."""
    synth = os.path.join(dyn_out, "synth")
    jsons = sorted(f for f in os.listdir(synth) if f.endswith(".json") and "nextpnr_report" not in f)
    sdcs = sorted(f for f in os.listdir(synth) if f.endswith(".sdc"))
    os.makedirs(log_dir, exist_ok=True)
    env = dict(os.environ, PATH=os.environ.get("OSS_SYNTH_BIN", "") + ":" + os.environ["PATH"])
    cps = []
    for seed in seeds:
        log = os.path.join(log_dir, "nextpnr_seed%d.log" % seed)
        with open(log, "w") as f:
            subprocess.run(["nice", "-n", "10", "nextpnr-ecp5", "--um5g-85k", "--package", "CABGA381",
                            "--json", os.path.join(synth, jsons[0]), "--out-of-context",
                            "--sdc", os.path.join(synth, sdcs[0]), "--timing-allow-fail",
                            "--seed", str(seed)], stdout=f, stderr=subprocess.STDOUT, env=env)
        with open(log) as f:
            fmax = re.findall(r"Max frequency for clock.*?: ([0-9.]+) MHz", f.read())
        cps.append(round(1000.0 / float(fmax[-1]), 3) if fmax else None)
    return cps


def milp_stopped(comp_dir):
    """Number of Cbc solutions that hit the time limit."""
    n = 0
    for f in glob.glob(os.path.join(comp_dir, "cbc_solution_*.sol")):
        with open(f) as fh:
            n += fh.readline().startswith("Stopped on time")
    return n


def all_channels(comp_dir):
    return PreBufferIR(os.path.join(comp_dir, "handshake_transformed.mlir")).channels()


def run_loop(src, out, T, timing_models, strategy, max_iter, iter0=None, wire_d0=0.0, wire_ir=None):
    sys.path.insert(0, QOR_DIR)
    from flow import run_flow
    os.makedirs(out, exist_ok=True)
    hist_path = os.path.join(out, "history.json")
    history, ann, last_sets, target, stop = [], {}, [], T, None
    wire_d = wire_d0
    if strategy == "wire" and wire_d0 > 0:
        # The channels must be known before the first compile: take them from
        # the pre-buffer IR of an earlier compile of the same kernel
        assert wire_ir and not iter0, "--wire-d0 needs --wire-ir and no --iter0"
        ann = {key: wire_d for key in PreBufferIR(wire_ir).channels()}
    t_start = time.time()
    for k in range(max_iter):
        it_dir = os.path.join(out, "iter_%d" % k)
        ba = None
        if strategy == "wire" and wire_d > 0:
            ba = os.path.join(out, "annotation_iter_%d.json" % k)
            write_annotation(ba, ann)
        elif strategy not in ("scale", "wire") and ann:
            ba = os.path.join(out, "annotation_iter_%d.json" % k)
            write_annotation(ba, ann)
        t0 = time.time()
        if k == 0 and iter0:
            # A finished open-loop run of flow.py with the same options
            with open(os.path.join(iter0, "metrics.json")) as f:
                m = json.load(f)
            assert m["target_period_ns"] == T and m["timing_models"] == timing_models, \
                "iteration 0 run does not match the options"
            wall = sum(v for v in (m.get("wall_s") or {}).values() if v)
        else:
            m = run_flow(src, it_dir, target, timing_models=timing_models, buffer_algorithm="fpl22",
                         milp_solver="cbc", back_annotation=ba, hdl="vhdl",
                         calculate_path_delays=True)
            wall = time.time() - t0
        rec = {"iter": k, "target_period_ns": target, "back_annotation": ba,
               "annotations": {"%s:%d" % key: round(d, 4) for key, d in sorted(ann.items())},
               "metrics": m, "wall_s": round(wall, 1), "reused": bool(k == 0 and iter0)}
        if strategy == "wire" and k == 0 and wire_d0 > 0 and m.get("comp_dir"):
            with open(wire_ir) as f1, open(os.path.join(m["comp_dir"], "handshake_transformed.mlir")) as f2:
                rec["wire_ir_match"] = f1.read() == f2.read()
        if strategy == "wire":
            rec["wire_d"] = wire_d
            rec["annotations"] = {"all %d channels" % len(ann): round(wire_d, 4)} if ann else {}
        history.append(rec)
        status = m.get("status")
        cp_ns = m.get("cp_ns")
        if status == "ok" and cp_ns:
            # nextpnr results move with the seed: two more seeds, and the
            # median is what the stop tests and the updates use
            t1 = time.time()
            seeds_dir = os.path.join(out, "iter_0_seeds") if rec["reused"] else os.path.join(it_dir, "seeds")
            rec["cp_seeds"] = [cp_ns] + seed_cps(m["out_dir"], seeds_dir)
            valid = sorted(c for c in rec["cp_seeds"] if c)
            rec["cp_median"] = valid[len(valid) // 2]
            if m.get("cycles"):
                rec["exec_time_median_ns"] = round(m["cycles"] * rec["cp_median"], 1)
            rec["seeds_wall_s"] = round(time.time() - t1, 1)
            rec["milp_stopped"] = milp_stopped(m["comp_dir"])
            try:
                cp = critical_channels(m["comp_dir"], m["nextpnr_report"], timing_models)
                rec["critical_path"] = cp
            except Exception as exc:  # report it, do not lose the run
                cp = None
                rec["critical_path_error"] = repr(exc)
        print("[iter %d] target %.3f%s: %s cp=%s median=%s cycles=%s buffers=%s milp_stopped=%s (%.0f s)"
              % (k, target, " d=%.4f" % wire_d if strategy == "wire" else "", status,
                 rec.get("cp_seeds", cp_ns), rec.get("cp_median"), m.get("cycles"), m.get("buffers"),
                 rec.get("milp_stopped"), rec["wall_s"] + rec.get("seeds_wall_s", 0)), flush=True)
        cp_med = rec.get("cp_median")

        if status != "ok" or not cp_ns:
            stop = "run failed at step '%s': %s" % (status, m.get("error"))
        elif strategy == "wire":
            if cp_med <= T:
                stop = "met"
            else:
                free = [c for c in cp["channels"] if not c["cut"]] if cp and not cp["unit_limited"] else []
                n = len(free) or 10
                est = m.get("est_cp_ns")
                if est is None:
                    stop = "no est_cp_ns"
                else:
                    new_d = max(0.0, wire_d + (cp_med - est) / n)
                    rec["update"] = {"d": wire_d, "R": cp_med, "est": est, "n": n,
                                     "n_default": not free, "d_next": new_d}
                    if abs(new_d - wire_d) < 0.01:
                        stop = "d changes by less than 0.01 ns"
                    else:
                        wire_d = new_d
                        ann = {key: wire_d for key in all_channels(m["comp_dir"])}
        elif strategy in ("path", "path-est"):
            if cp_med <= T:
                stop = "met"
            elif cp is None:
                stop = "critical path could not be mapped"
            elif cp["unit_limited"]:
                stop = "unit-limited path (%s): R = %.3f ns is the floor" % (cp["unit_limited"], cp["R"])
            else:
                free = [c for c in cp["channels"] if not c["cut"]]
                keys = [(c["consumer"], c["operand"]) for c in free]
                last_sets.append(frozenset(keys))
                if len(last_sets) >= 3 and len(set(last_sets[-3:])) == 1:
                    stop = "same channels critical three times in a row"
                else:
                    R, n = cp["R"], len(keys)
                    M = cp["model_units"] + sum(ann.get(key, 0.0) for key in keys)
                    # Variant: the units on a flattened path usually have no
                    # model number (M = 0), so take the MILP's own estimate of
                    # the critical path as the delay the model believed
                    if strategy == "path-est" and m.get("est_cp_ns"):
                        M = max(M, m["est_cp_ns"])
                    d = max(R - M, R - T, 0.1) / n
                    for key in keys:
                        ann[key] = ann.get(key, 0.0) + d
                    rec["update"] = {"R": R, "M": M, "n": n, "d_per_channel": d,
                                     "channels": ["%s:%d" % key for key in keys]}
        else:
            if cp_med <= T and (T - cp_med) / T < 0.02:
                stop = "converged (within 2% below the target)"
            else:
                new = min(max(target * T / cp_med, 0.5 * T), 1.5 * T)
                rec["next_target"] = new
                if abs(new - target) / target < 0.01:
                    stop = "target changes by less than 1%"
                target = new
        rec["stop"] = stop
        with open(hist_path, "w") as f:
            json.dump(history, f, indent=1)
        if stop:
            break
    if stop is None:
        stop = "max-iter reached"
        history[-1]["stop"] = stop
    summary = {"src": src, "period": T, "strategy": strategy, "timing_models": timing_models,
               "iterations": len(history), "stop": stop, "best_iter": best_iteration(history, T),
               "wall_s": round(sum(h["wall_s"] for h in history), 1),
               "elapsed_s": round(time.time() - t_start, 1)}
    with open(hist_path, "w") as f:
        json.dump(history, f, indent=1)
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1)
    print(json.dumps(summary, indent=1))
    return summary, history


def main():
    p = argparse.ArgumentParser(description="Closed-loop buffer placement with nextpnr in the loop.")
    p.add_argument("--src", help="kernel C source")
    p.add_argument("--out", help="output directory")
    p.add_argument("--period", type=float, help="target clock period T (ns)")
    p.add_argument("--timing-models", default=ECP5_MODEL)
    p.add_argument("--strategy", choices=("path", "path-est", "scale", "wire"), default="path",
                   help="path: annotate the critical channels with (R - M) / n, M from the "
                        "units' timing model; path-est: same with M = the MILP's estimated "
                        "critical path; scale: scale the target period; wire: one delay d on "
                        "every channel, d += (R - est) / n")
    p.add_argument("--max-iter", type=int, default=8)
    p.add_argument("--iter0", metavar="RUN_DIR",
                   help="use this finished flow.py run (same kernel, period, model) as iteration 0")
    p.add_argument("--wire-d0", type=float, default=0.0,
                   help="wire: delay on every channel at iteration 0 (default 0); with "
                        "--max-iter 1, one run at that delay")
    p.add_argument("--wire-ir", metavar="MLIR",
                   help="wire with --wire-d0: handshake_transformed.mlir of an earlier compile "
                        "of the same kernel, which gives the channels")
    p.add_argument("--show", metavar="OUT_DIR",
                   help="only print the critical channels of a finished run (Dynamatic out dir)")
    args = p.parse_args()
    if args.show:
        comp = os.path.join(args.show, "comp")
        show(critical_channels(comp, os.path.join(args.show, "synth", "nextpnr_report.json"),
                               args.timing_models))
        return
    if not (args.src and args.out and args.period):
        p.error("--src, --out and --period are required")
    run_loop(os.path.abspath(args.src), os.path.abspath(args.out), args.period,
             os.path.abspath(args.timing_models), args.strategy, args.max_iter,
             os.path.abspath(args.iter0) if args.iter0 else None, args.wire_d0,
             os.path.abspath(args.wire_ir) if args.wire_ir else None)


if __name__ == "__main__":
    main()
