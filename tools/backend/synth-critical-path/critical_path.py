#!/usr/bin/env python3
"""
Maps the critical path(s) of a nextpnr timing report back onto the dataflow
circuit that Dynamatic generated.

nextpnr reports a path as a list of delays through low-level cells (LUT4,
TRELLIS_FF, CCU2C, MULT18X18D...) and the nets between them. Yosys and nextpnr
keep the hierarchy in the names: the first dotted component of a cell name is
the dataflow unit the cell is named after, and a net that carries a channel
can keep the name of the channel's wire. This script uses these names to cut
the path into dataflow units and channels, compares the delay spent in each
unit with Dynamatic's timing model, and highlights the path on the circuit's
DOT.

Only the Python standard library is used. See
docs/DeveloperGuide/Specs/CriticalPathAnalysis.md for usage and limitations.
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys

# Dynamatic's default timing model, used when --timing-model has no argument
DEFAULT_MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "..", "..", "data", "components.json")

QUOTED = r'"((?:[^"\\]|\\.)*)"'
EDGE_RE = re.compile(r'^\s*' + QUOTED + r'\s*->\s*' + QUOTED + r'\s*\[(.*)\]\s*;?\s*$')
NODE_RE = re.compile(r'^(\s*)' + QUOTED + r'\s*\[(.*)\]\s*;?\s*$')
ATTR_RE = re.compile(QUOTED + r'\s*=\s*' + QUOTED)
BIT_RE = re.compile(r'^(.*?)(?:\[(\d+)\])?$')
# Yosys (autoname) names a flip-flop after the net on its Q output
FF_NAME_RE = re.compile(r'^(.*)_TRELLIS_FF_Q(?:_\d+)?$')

# Units whose HDL only wires these signals through (data/verilog/arith), so
# one net carries the channels on both sides of them
WIRE_THROUGH = {
    "handshake.extui": ("data", "valid", "ready"),
    "handshake.extsi": ("data", "valid", "ready"),
    "handshake.trunci": ("data", "valid"),
}

# Ports whose data the timing model calls a condition (C)
CONDITION_PORTS = ("condition", "index")

# Timing model entry for each (entering signal, leaving signal) of a unit.
# Conditions are data for the "data" delay, as in buffer placement.
CROSSING_KEYS = {
    ("valid", "valid"): "valid",
    ("ready", "ready"): "ready",
    ("valid", "ready"): "VR",
    ("condition", "valid"): "CV",
    ("condition", "ready"): "CR",
    ("valid", "condition"): "VC",
    ("valid", "data"): "VD",
}


def parse_attrs(text):
    return {k: v for k, v in ATTR_RE.findall(text)}


def parse_hdl_widths(path):
    """Width of every wire, port or signal declared in the top-level HDL file."""
    widths = {}
    if not path or not os.path.isfile(path):
        return widths
    with open(path) as f:
        text = f.read()
    if path.endswith(".v") or path.endswith(".sv"):
        decl = re.compile(r'\b(?:wire|input|output|inout)\s+(?:reg\s+)?'
                          r'(?:\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*)?(\w+)')
        for hi, lo, name in decl.findall(text):
            widths[name] = abs(int(hi) - int(lo)) + 1 if hi else 1
    else:
        vec = re.compile(r'(\w+)\s*:\s*(?:in\s+|out\s+)?std_logic_vector\s*'
                         r'\(\s*(\d+)\s+downto\s+(\d+)\s*\)', re.I)
        for name, hi, lo in vec.findall(text):
            widths[name] = int(hi) - int(lo) + 1
        for name in re.findall(r'(\w+)\s*:\s*(?:in\s+|out\s+)?std_logic\b(?!_)', text, re.I):
            widths.setdefault(name, 1)
    return widths


class Circuit:
    """The dataflow circuit written by export-dot, with channel widths from the HDL."""

    def __init__(self, dot_path, hdl_path, exact_names):
        # In a hierarchical netlist, the name of a cell is its instance path
        self.exact_names = exact_names
        self.units = {}
        self.edges = []
        with open(dot_path) as f:
            for line in f:
                m = EDGE_RE.match(line)
                if m:
                    attrs = parse_attrs(m.group(3))
                    self.edges.append({"src": m.group(1), "dst": m.group(2),
                                       "from": attrs.get("from", ""), "to": attrs.get("to", ""),
                                       "from_idx": attrs.get("from_idx"), "to_idx": attrs.get("to_idx")})
                    continue
                m = NODE_RE.match(line)
                if m:
                    self.units[m.group(2)] = parse_attrs(m.group(3))
        self.widths = parse_hdl_widths(hdl_path)

        # Every name a channel's wires can carry in the netlist: the top-level
        # wire (<producer>_<port>, or the argument name), the result name, and
        # the port names on both units (<unit>.<port>), which Yosys may keep instead
        self.names = {}
        for i, e in enumerate(self.edges):
            self.names[self.top_name(e)] = i
            if self.is_arg(e["dst"]):
                self.names[e["dst"]] = i
            else:
                self.names[e["dst"] + "." + e["to"]] = i
            if not self.is_arg(e["src"]):
                self.names[e["src"] + "." + e["from"]] = i

    def is_arg(self, node):
        return self.units.get(node, {}).get("mlir_op") == "handshake.func"

    def top_name(self, e):
        return e["src"] if self.is_arg(e["src"]) else e["src"] + "_" + e["from"]

    def op(self, unit):
        return self.units.get(unit, {}).get("mlir_op", "?")

    def describe(self, unit):
        if unit is None:
            return "(top level)"
        return unit if unit in self.units else unit + " (not in DOT)"

    def wires_through(self, unit, kind):
        return kind in WIRE_THROUGH.get(self.op(unit), ())

    def channel_of(self, net):
        """The channel signal a net carries, or None.

        Returns the chain of DOT edges the net spans (more than one when it
        goes through wire-only units), the signal kind, the data bit, and the
        (unit, port) that drives and that receives the signal."""
        base, bit = BIT_RE.match(net.lstrip("\\")).groups()
        for suffix, kind in (("", "data"), ("_valid", "valid"), ("_ready", "ready")):
            if suffix and not base.endswith(suffix):
                continue
            stem = base[:len(base) - len(suffix)]
            idx = self.names.get(stem)
            # Arrays of handshake signals, e.g. fork0.outs_valid[1] for port outs_1
            if idx is None and suffix and bit is not None:
                idx = self.names.get(stem + "_" + bit)
            if idx is not None:
                return self.follow(idx, kind, bit if kind == "data" else None)
        return None

    def follow(self, idx, kind, bit):
        chain = [idx]
        while self.wires_through(self.edges[chain[0]]["src"], kind):
            ins = [i for i, e in enumerate(self.edges) if e["dst"] == self.edges[chain[0]]["src"]]
            if len(ins) != 1:
                break
            chain.insert(0, ins[0])
        while self.wires_through(self.edges[chain[-1]]["dst"], kind):
            outs = [i for i, e in enumerate(self.edges) if e["src"] == self.edges[chain[-1]]["dst"]]
            if len(outs) != 1:
                break
            chain.append(outs[0])
        first, last = self.edges[chain[0]], self.edges[chain[-1]]
        up, down = (first["src"], first["from"]), (last["dst"], last["to"])
        # Data and valid flow from producer to consumer, ready the other way
        driver, receiver = (down, up) if kind == "ready" else (up, down)
        return {"edges": chain, "signal": kind, "bit": bit, "driver": driver, "receiver": receiver}

    def describe_channel(self, ch):
        first, last = self.edges[ch["edges"][0]], self.edges[ch["edges"][-1]]
        text = "channel %s.%s -> %s.%s, %s" % (first["src"], first["from"], last["dst"], last["to"], ch["signal"])
        if ch["bit"] is not None:
            text += " bit " + ch["bit"]
        if len(ch["edges"]) > 1:
            text += " (through %s)" % ", ".join(self.edges[i]["src"] for i in ch["edges"][1:])
        return text

    def name_unit(self, cell):
        """Unit a cell is named after: the first dotted component of its name."""
        name = cell.lstrip("\\")
        return name.split(".", 1)[0] if "." in name else None

    def unit_of(self, cell, driven_net=None):
        """Unit of a cell.

        In a flattened netlist, a cell that drives a channel net is put in the
        unit that drives the channel, whatever unit Yosys named it after: a
        register is often named after the consumer's input port
        (buffer1.ins_TRELLIS_FF_Q is buffer0's output register)."""
        if not self.exact_names:
            m = FF_NAME_RE.match(cell.lstrip("\\"))
            for net in (driven_net, m.group(1) if m else None):
                ch = self.channel_of(net) if net else None
                if ch and not self.is_arg(ch["driver"][0]):
                    return ch["driver"][0]
        return self.name_unit(cell)

    def neighbours(self, u):
        return {e["dst"] for e in self.edges if e["src"] == u} | {e["src"] for e in self.edges if e["dst"] == u}

    def adjacent(self, a, b):
        """True if a channel joins the two units, possibly through wire-only units."""
        if a is None or b is None:
            return False
        near = self.neighbours(a)
        return b in near or any(self.op(w) in WIRE_THROUGH and b in self.neighbours(w) for w in near)

    def channel_width(self, e):
        """Data width of a channel from its HDL wire: 0 if dataless, None if unknown."""
        name = self.top_name(e)
        if name in self.widths:
            return self.widths[name]
        return 0 if name + "_valid" in self.widths else None

    def op_width(self, unit):
        """Bitwidth the timing model is looked up with (mirrors getOpDatawidth)."""
        op = self.op(unit).replace("handshake.", "")
        if op in ("constant", "source"):
            outs = [e for e in self.edges if e["src"] == unit and e["from_idx"] == "0"]
            return self.channel_width(outs[0]) if outs else None
        operands = {e["to_idx"]: e for e in self.edges if e["dst"] == unit}
        if op in ("load", "store"):
            ws = [self.channel_width(operands[i]) for i in ("0", "1") if i in operands]
            return None if len(ws) < 2 or None in ws else max(ws)
        if op.startswith("mem_controller") or op == "lsq":
            return None
        idx = "1" if op in ("select", "mux", "cond_br") else "0"
        return self.channel_width(operands[idx]) if idx in operands else None


def model_entry(model, op):
    """(timing model of an op, None) or (None, reason)."""
    if op in model:
        return model[op], None
    for prefix in ("handshake.cmpi", "handshake.cmpf"):
        # export-dot appends the label to comparisons (handshake.cmpicmpi0)
        if op.startswith(prefix) and prefix in model:
            return model[prefix], None
    if any(k.startswith(op + ".") for k in model):
        return None, "FPU implementation unknown"
    return None, "no model entry for " + op


def model_crossing(model, circuit, unit, sig_in, sig_out):
    """(predicted delay, note) for a path that enters and leaves a unit by ports."""
    entry, reason = model_entry(model, circuit.op(unit))
    if entry is None:
        return None, reason
    delays = entry.get("delay", {})
    if sig_in in ("data", "condition") and sig_out in ("data", "condition"):
        width = circuit.op_width(unit)
        if width is None:
            return None, "bitwidth unknown"
        points = sorted((int(w), d) for w, d in delays.get("data", {}).items() if int(w) >= width)
        if not points:
            return None, "bitwidth %d above the model's points" % width
        return points[0][1], "data at bitwidth %d (model point %d)" % (width, points[0][0])
    key = CROSSING_KEYS.get((sig_in, sig_out))
    if key is None or key not in delays:
        return None, "no model entry for %s -> %s" % (sig_in, sig_out)
    value = delays[key]
    if isinstance(value, dict):
        value = next(iter(value.values()))
    return value, key


def model_reg2reg(model, circuit, unit, period):
    """(predicted reg2reg delay, note) of a pipelined unit, with the
    implementation chosen as in TimingModels.h: the slowest within the period."""
    entry, reason = model_entry(model, circuit.op(unit))
    if entry is None:
        return None, reason
    width = circuit.op_width(unit)
    if width is None:
        return None, "bitwidth unknown"
    points = sorted((int(w), impls) for w, impls in entry.get("latency", {}).get("0", {}).items()
                    if int(w) >= width)
    if not points or not points[0][1]:
        return None, "no latency entry at bitwidth %d" % width
    impls = {float(d): lat for d, lat in points[0][1].items()}
    fitting = [d for d in impls if period is None or d <= period]
    delay = max(fitting) if fitting else min(impls)
    if impls[delay] == 0:
        return None, "the model has no pipelined implementation"
    return delay, "reg2reg, latency %g (model point %d)" % (impls[delay], points[0][0])


def split_path(path, circuit):
    """Cuts a nextpnr path into unit hops and the nets between them."""
    driven = {s["from"]["cell"]: s["net"] for s in path if s["type"] == "routing" and s.get("net")}

    def unit(cell):
        return circuit.unit_of(cell, driven.get(cell))

    items = []
    for seg in path:
        uf, ut = unit(seg["from"]["cell"]), unit(seg["to"]["cell"])
        if seg["type"] == "routing" and uf != ut:
            net = seg.get("net") or "?"
            items.append({"kind": "net", "net": net, "delay": seg["delay"], "from_unit": uf, "to_unit": ut,
                          "channel": circuit.channel_of(net), "adjacent": circuit.adjacent(uf, ut)})
            continue
        if not items or items[-1]["kind"] != "unit" or items[-1]["unit"] != uf:
            items.append({"kind": "unit", "unit": uf, "op": circuit.op(uf), "logic": 0.0, "routing": 0.0,
                          "cells": [], "start": None, "end": None, "renamed": False})
        hop = items[-1]
        # clk-to-q and setup are cell delays, like logic
        hop["routing" if seg["type"] == "routing" else "logic"] += seg["delay"]
        for cell in (seg["from"]["cell"], seg["to"]["cell"]):
            if cell not in hop["cells"]:
                hop["cells"].append(cell)
                hop["renamed"] |= unit(cell) != circuit.name_unit(cell)
        if seg["type"] == "clk-to-q":
            hop["start"] = seg["from"]["cell"]
        if seg["type"] == "setup":
            hop["end"] = "%s (%s)" % (seg["to"]["cell"], seg["to"]["port"])
    return items


def signal_at(net, unit, entering):
    """Signal kind with which the path enters or leaves a unit through a net, or '?'."""
    ch = net["channel"] if net else None
    if ch is None:
        return "?"
    side, port = ch["receiver"] if entering else ch["driver"]
    if side != unit:
        return "?"
    return "condition" if ch["signal"] == "data" and port in CONDITION_PORTS else ch["signal"]


def analyse(report, circuit, model):
    results = []
    for cp in report.get("critical_paths", []):
        path = cp.get("path", [])
        if not path:
            continue
        clock = cp["to"].split()[-1] if cp.get("to") else None
        fmax = report.get("fmax", {}).get(clock, {})
        period = 1000.0 / fmax["constraint"] if fmax.get("constraint") else None
        items = split_path(path, circuit)
        for i, it in enumerate(items):
            if it["kind"] != "unit":
                continue
            prev = items[i - 1] if i > 0 else None
            nxt = items[i + 1] if i + 1 < len(items) else None
            it["in"] = "reg" if it["start"] else signal_at(prev, it["unit"], True)
            it["out"] = "reg" if it["end"] else signal_at(nxt, it["unit"], False)
            it["model"], it["model_note"] = None, None
            if model is None:
                continue
            if it["unit"] not in circuit.units:
                it["model_note"] = "not a dataflow unit"
            elif it["start"] and it["end"]:
                it["model"], it["model_note"] = model_reg2reg(model, circuit, it["unit"], period)
            elif it["start"] or it["end"]:
                it["model_note"] = "path starts or ends inside the unit: no port-to-port number"
            elif "?" in (it["in"], it["out"]):
                it["model_note"] = "crossing unknown"
            else:
                it["model"], it["model_note"] = model_crossing(model, circuit, it["unit"], it["in"], it["out"])
        hops = [it for it in items if it["kind"] == "unit"]
        nets = [it for it in items if it["kind"] == "net"]
        per_unit = {}
        for h in hops:
            per_unit[h["unit"]] = per_unit.get(h["unit"], 0.0) + h["logic"] + h["routing"]
        results.append({
            "from": cp.get("from"), "to": cp.get("to"), "clock": clock, "period": period,
            "fmax": fmax.get("achieved"), "constraint": fmax.get("constraint"),
            "delay": sum(s["delay"] for s in path),
            "logic": sum(s["delay"] for s in path if s["type"] != "routing"),
            "routing": sum(s["delay"] for s in path if s["type"] == "routing"),
            "in_units": sum(h["logic"] + h["routing"] for h in hops),
            "between_units": sum(n["delay"] for n in nets),
            "on_channels": sum(n["delay"] for n in nets if n["channel"]),
            "per_unit": per_unit, "items": items,
        })
    return results


def endpoint_table(report, circuit):
    """Worst arrival time at every timing endpoint, from --detailed-timing-report."""
    best = {}
    for net in report.get("detailed_net_timings", []):
        for ep in net.get("endpoints", []):
            # Two arrival times; the larger is the worst one (it matches the
            # critical path delay at the critical endpoint)
            arrival = max(ep["delay"]) if isinstance(ep["delay"], list) else ep["delay"]
            key = (ep["cell"], ep["port"])
            if key not in best or arrival > best[key]["arrival"]:
                clock = ep.get("event", "").split()[-1] if ep.get("event") else None
                c = report.get("fmax", {}).get(clock, {}).get("constraint")
                unit = circuit.unit_of(ep["cell"])
                best[key] = {"cell": ep["cell"], "port": ep["port"], "event": ep.get("event"),
                             "arrival": arrival, "slack": 1000.0 / c - arrival if c else None,
                             "unit": unit, "renamed": unit != circuit.name_unit(ep["cell"])}
    return sorted(best.values(), key=lambda e: -e["arrival"])


def fmt(x):
    return "%.3f" % x if x is not None else "-"


def print_report(results, endpoints, circuit, flattened, top_n, w):
    if flattened is False:
        w("Netlist: hierarchical, cells are attributed to units by their instance path.\n")
    else:
        w("Netlist: %s.\n" % ("flattened" if flattened else "not found, assumed flattened (the synth_ecp5 default)"))
        w("Cells are attributed to the unit they are named after. After flattening, ABC can merge\n"
          "the logic of neighbouring units into one LUT, and Yosys names a LUT after a nearby named\n"
          "net: for LUTs this is a hint, not necessarily the unit the logic comes from.\n")
    if not circuit.widths:
        w("Top-level HDL not found: channel bitwidths are unknown.\n")
    if not results:
        w("\nThe report has no critical path.\n")
    for r in results:
        w("\nCritical path %s -> %s: %.3f ns\n" % (r["from"], r["to"], r["delay"]))
        if r["period"]:
            w("  constraint %.2f MHz (%.3f ns), achieved %s MHz, slack %.3f ns\n"
              % (r["constraint"], r["period"], "%.2f" % r["fmax"] if r["fmax"] else "?", r["period"] - r["delay"]))
        items = r["items"]
        if items[0]["kind"] == "unit" and items[0]["start"]:
            w("  starts at %s in %s\n" % (items[0]["start"], circuit.describe(items[0]["unit"])))
        if items[-1]["kind"] == "unit" and items[-1]["end"]:
            w("  ends at   %s in %s\n" % (items[-1]["end"], circuit.describe(items[-1]["unit"])))
        w("\n  %-3s %-24s %-24s %7s %7s %7s  %-20s %7s %6s\n"
          % ("#", "unit", "op", "logic", "route", "total", "in -> out", "model", "ratio"))
        n = 0
        for it in items:
            if it["kind"] == "unit":
                n += 1
                total = it["logic"] + it["routing"]
                ratio = "%.2f" % (total / it["model"]) if it["model"] else "-"
                name = circuit.describe(it["unit"]) + ("*" if it["renamed"] else "")
                op = it["op"] if it["unit"] in circuit.units else ""
                w("  %-3d %-24s %-24s %7.3f %7.3f %7.3f  %-20s %7s %6s\n"
                  % (n, name, op, it["logic"], it["routing"], total, it["in"] + " -> " + it["out"],
                     fmt(it["model"]), ratio))
                if it["model_note"]:
                    w("      model: %s\n" % it["model_note"])
            elif it["channel"]:
                w("      %-56s %7.3f  %s\n" % (it["net"][:56], it["delay"], circuit.describe_channel(it["channel"])))
            else:
                w("      %-56s %7.3f  net between %s and %s%s, not named after a channel\n"
                  % (it["net"][:56], it["delay"], circuit.describe(it["from_unit"]), circuit.describe(it["to_unit"]),
                     "" if it["adjacent"] else " (not adjacent in the circuit)"))
        if any(it.get("renamed") for it in items):
            w("  * named after another unit; attributed to the unit that drives the channel net it drives\n")
        d = r["delay"] or 1.0
        w("\n  logic %.3f ns (%.0f%%), routing %.3f ns (%.0f%%)\n"
          % (r["logic"], 100 * r["logic"] / d, r["routing"], 100 * r["routing"] / d))
        w("  inside units %.3f ns (%.0f%%), on nets between units %.3f ns (%.0f%%; %.3f ns on recognised channels)\n"
          % (r["in_units"], 100 * r["in_units"] / d, r["between_units"], 100 * r["between_units"] / d,
             r["on_channels"]))
        steps = [it for it in items if it["kind"] == "net"]
        if steps:
            w("  %d of %d steps between units join units that are adjacent in the circuit\n"
              % (sum(1 for it in steps if it["adjacent"]), len(steps)))
        u, t = max(r["per_unit"].items(), key=lambda kv: kv[1])
        whole = " (the whole path is inside it)" if len(r["per_unit"]) == 1 and not steps else ""
        w("  largest share: %s, %.3f ns (%.0f%%)%s\n" % (circuit.describe(u), t, 100 * t / d, whole))

    if not endpoints:
        w("\nNo per-endpoint data (run nextpnr with --detailed-timing-report to get it).\n")
        return
    w("\nWorst endpoints (arrival from --detailed-timing-report; slack = period - arrival, no setup time)\n")
    for e in endpoints[:top_n]:
        unit = circuit.describe(e["unit"]) + ("*" if e["renamed"] else "")
        w("  %7.3f ns  slack %7s  %-24s %s (%s)\n" % (e["arrival"], fmt(e["slack"]), unit, e["cell"], e["port"]))
    w("\nWorst endpoint per unit\n")
    seen = set()
    for e in endpoints:
        if e["unit"] in seen:
            continue
        seen.add(e["unit"])
        if len(seen) > top_n:
            break
        unit = circuit.describe(e["unit"]) + ("*" if e["renamed"] else "")
        w("  %7.3f ns  slack %7s  %-24s %s\n" % (e["arrival"], fmt(e["slack"]), unit, circuit.op(e["unit"])))
    w("  (%d endpoints in %d units)\n" % (len(endpoints), len(set(e["unit"] for e in endpoints))))
    if any(e["renamed"] for e in endpoints):
        w("  * register named after another unit's port, attributed to the unit that drives that channel\n")


def annotate_dot(dot_path, out_path, results, circuit):
    """Writes a copy of the DOT with the units and channels of the critical
    path(s) highlighted, and renders it if Graphviz is installed."""
    unit_delay, edge_label = {}, {}
    for r in results:
        for it in r["items"]:
            if it["kind"] == "unit" and it["unit"] in circuit.units:
                unit_delay[it["unit"]] = unit_delay.get(it["unit"], 0.0) + it["logic"] + it["routing"]
            elif it["kind"] == "net" and it["channel"]:
                for idx in it["channel"]["edges"]:
                    edge_label.setdefault(idx, []).append("%s %.3f ns" % (it["channel"]["signal"], it["delay"]))
    keys = {(e["src"], e["dst"], e["from"], e["to"]): i for i, e in enumerate(circuit.edges)}
    highlight = {"color": "red", "fontcolor": "red", "penwidth": "3"}

    def attr_list(attrs):
        return ", ".join('"%s"="%s"' % kv for kv in attrs.items())

    lines = []
    with open(dot_path) as f:
        for line in f:
            indent = line[:len(line) - len(line.lstrip())]
            m = EDGE_RE.match(line)
            if m:
                attrs = parse_attrs(m.group(3))
                idx = keys.get((m.group(1), m.group(2), attrs.get("from", ""), attrs.get("to", "")))
                if idx in edge_label:
                    attrs.update(highlight, label="\\n".join(edge_label[idx]))
                    line = '%s"%s" -> "%s" [%s]\n' % (indent, m.group(1), m.group(2), attr_list(attrs))
            else:
                m = NODE_RE.match(line)
                if m and m.group(2) in unit_delay:
                    attrs = parse_attrs(m.group(3))
                    label = attrs.get("label", m.group(2)) + "\\n%.3f ns" % unit_delay[m.group(2)]
                    attrs.update(highlight, label=label)
                    line = '%s"%s" [%s]\n' % (indent, m.group(2), attr_list(attrs))
            lines.append(line)
    with open(out_path, "w") as f:
        f.writelines(lines)

    rendered = []
    dot = shutil.which("dot")
    for ext in ("png", "svg") if dot else ():
        image = os.path.splitext(out_path)[0] + "." + ext
        if subprocess.run([dot, "-T" + ext, out_path, "-o", image]).returncode == 0:
            rendered.append(image)
    return rendered


def find_inputs(args):
    """Fills the inputs that are not given from a Dynamatic output directory."""
    out = args.out_dir
    if out:
        dots = [p for p in glob.glob(os.path.join(out, "comp", "*.dot")) if not p.endswith("_CFG.dot")]
        if not args.dot and len(dots) == 1:
            args.dot = dots[0]
        # synth/ is written by 'synthesize --tool yosys'
        for cand in ("synth/nextpnr_report.json", "oss-synth/report.json"):
            if not args.report and os.path.isfile(os.path.join(out, cand)):
                args.report = os.path.join(out, cand)
    if not args.report or not args.dot:
        sys.exit("error: need the nextpnr report and the DOT (pass them, or a Dynamatic output directory)")
    kernel = os.path.splitext(os.path.basename(args.dot))[0]
    for ext in (".v", ".vhd"):
        if not args.hdl and out and os.path.isfile(os.path.join(out, "hdl", kernel + ext)):
            args.hdl = os.path.join(out, "hdl", kernel + ext)
    netlist = os.path.join(os.path.dirname(args.report), kernel + ".json")
    if not args.netlist and os.path.isfile(netlist):
        args.netlist = netlist
    if not args.dot_out:
        args.dot_out = os.path.join(os.path.dirname(args.report), kernel + "_critical_path.dot")


def is_flattened(netlist):
    """True if Yosys flattened the design, None if the netlist is not available."""
    if not netlist:
        return None
    with open(netlist) as f:
        modules = json.load(f).get("modules", {})
    design = [m for m in modules.values() if "blackbox" not in m.get("attributes", {})]
    return len(design) <= 1


def to_json(results, endpoints, circuit, args, flattened):
    for r in results:
        for it in r["items"]:
            ch = it.get("channel")
            if ch:
                ch["dot_edges"] = [circuit.edges[i] for i in ch.pop("edges")]
                ch["width"] = circuit.channel_width(ch["dot_edges"][0])
    return {"report": args.report, "dot": args.dot, "flattened": flattened, "paths": results,
            "endpoints": endpoints}


def main():
    p = argparse.ArgumentParser(description="Map nextpnr's critical path onto Dynamatic's dataflow circuit.")
    p.add_argument("out_dir", nargs="?", help="Dynamatic output directory (with comp/, hdl/ and synth/)")
    p.add_argument("--report", help="nextpnr JSON report (nextpnr --report)")
    p.add_argument("--dot", help="DOT of the circuit (comp/<kernel>.dot)")
    p.add_argument("--hdl", help="top-level HDL, for channel bitwidths (hdl/<kernel>.v or .vhd)")
    p.add_argument("--netlist", help="Yosys JSON netlist, to tell whether the design was flattened")
    p.add_argument("--timing-model", nargs="?", const=DEFAULT_MODEL,
                   help="timing model to compare with (data/components.json if no file is given)")
    p.add_argument("--dot-out", help="annotated DOT (default: <kernel>_critical_path.dot next to the report)")
    p.add_argument("--json", help="also write the analysis to this JSON file")
    p.add_argument("--top", type=int, default=10, help="rows in the per-endpoint tables (default 10)")
    args = p.parse_args()
    find_inputs(args)

    with open(args.report) as f:
        report = json.load(f)
    model = None
    if args.timing_model:
        with open(args.timing_model) as f:
            model = json.load(f)
    flattened = is_flattened(args.netlist)
    circuit = Circuit(args.dot, args.hdl, exact_names=flattened is False)

    results = analyse(report, circuit, model)
    endpoints = endpoint_table(report, circuit)
    print_report(results, endpoints, circuit, flattened, args.top, sys.stdout.write)

    rendered = annotate_dot(args.dot, args.dot_out, results, circuit)
    print("\nAnnotated DOT: " + args.dot_out)
    for image in rendered:
        print("Rendered: " + image)
    if not rendered:
        print("Graph not rendered (Graphviz 'dot' not found or failed)")

    if args.json:
        with open(args.json, "w") as f:
            json.dump(to_json(results, endpoints, circuit, args, flattened), f, indent=1)


if __name__ == "__main__":
    main()
