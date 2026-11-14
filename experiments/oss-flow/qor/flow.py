#!/usr/bin/env python3
"""Run one Dynamatic kernel through compile -> write-hdl -> simulate (GHDL) ->
synthesize (Yosys + nextpnr-ecp5) and collect quality-of-results metrics.

Usable as a module (``from flow import run_flow``) or from the command line
(``python flow.py --help``). Standard library only.

Each step runs as its own ``bin/dynamatic --exit-on-failure --run step.dyn``
process (in its own process group, so a timeout kills the whole tree: the
frontend, the shell scripts, cbc, ghdl, yosys, nextpnr). Results go to
``<out>/metrics.json``.
"""

import argparse
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

DYN = os.environ.get("DYNAMATIC_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))
ENV_SH = f"{DYN}/experiments/oss-flow/env.sh"
OSS_BIN = f"{DYN}/experiments/oss-flow/oss-synth/bin"
DEFAULT_TIMING_MODELS = f"{DYN}/data/components.json"
# Directory name passed to set-output-dir (relative to the kernel copy in <out>)
OUT_SUBDIR = "out"
STEPS = ("compile", "write-hdl", "simulate", "synthesize")
# Retry once after this delay when a step dies at start-up (the binary or the
# scripts may be rebuilt by another agent while we run)
RETRY_DELAY_S = 90
STARTUP_FAIL_S = 5

METRIC_KEYS = (
    "kernel", "target_period_ns", "timing_models", "buffer_algorithm",
    "back_annotation", "status", "error", "buffers", "buffer_slots",
    "buffers_by_type", "est_cp_ns", "cp_ns", "fmax_mhz", "cycles", "sim_pass",
    "exec_time_ns", "lut4", "ff", "dsp", "bram", "wall_s", "out_dir",
    "comp_dir", "nextpnr_report",
)

_ERR_RE = re.compile(r"\[FATAL\]|\[ERROR\]|error:|LLVM ERROR|Assertion|"
                     r"Segmentation|Abort|No such file|not found|Unknown",
                     re.IGNORECASE)


def _dyn_header(src, period):
    return (f"set-dynamatic-path {DYN}\n"
            f"set-src {src}\n"
            f"set-clock-period {period}\n"
            f"set-output-dir {OUT_SUBDIR}\n")


def _error_line(log_path, fallback):
    """Return the most informative single line of a failed step's log."""
    try:
        with open(log_path, errors="replace") as f:
            lines = [ln.rstrip() for ln in f if ln.strip()]
    except OSError:
        return fallback
    hits = [ln for ln in lines if _ERR_RE.search(ln)]
    if hits:
        # Prefer a FATAL line: the frontend scripts print the cause there
        fatal = [ln for ln in hits if "[FATAL]" in ln]
        return (fatal[0] if fatal else hits[0]).strip()[:400]
    return (lines[-1].strip()[:400] if lines else fallback)


def _run_step(name, dyn_text, out, timeout):
    """Run one frontend step. Returns (ok, error_line, wall_s)."""
    logs = os.path.join(out, "logs")
    os.makedirs(logs, exist_ok=True)
    dyn_path = os.path.join(logs, f"{name}.dyn")
    log_path = os.path.join(logs, f"{name}.log")
    with open(dyn_path, "w") as f:
        f.write(dyn_text + "exit\n")
    cmd = (f"source '{ENV_SH}' && export PATH='{OSS_BIN}':\"$PATH\" && "
           f"exec nice -n 10 '{DYN}/bin/dynamatic' --exit-on-failure "
           f"--run '{dyn_path}'")

    def attempt():
        t0 = time.time()
        with open(log_path, "w") as log:
            proc = subprocess.Popen(["bash", "-c", cmd], cwd=out, stdout=log,
                                    stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL,
                                    start_new_session=True)
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                return None, time.time() - t0
        return rc, time.time() - t0

    def has_fatal():
        try:
            with open(log_path, errors="replace") as f:
                return any("[FATAL]" in ln for ln in f)
        except OSError:
            return False

    rc, wall = attempt()
    if rc is None:
        return False, f"timeout after {timeout} s", wall
    ok = rc == 0 and not has_fatal()
    if not ok and (wall < STARTUP_FAIL_S
                   or not os.path.exists(f"{DYN}/bin/dynamatic")):
        # Looks like a binary being rebuilt: wait and retry once
        time.sleep(RETRY_DELAY_S)
        rc, wall = attempt()
        if rc is None:
            return False, f"timeout after {timeout} s", wall
        ok = rc == 0 and not has_fatal()
    if ok:
        return True, None, wall
    return False, _error_line(log_path, f"exit code {rc}"), wall


def parse_buffers(export_mlir):
    """Count buffer ops in handshake_export.mlir: (count, slots, by_type)."""
    rx = re.compile(r"=\s*buffer\s.*?bufferType\s*=\s*(\w+)\s*,\s*"
                    r"numSlots\s*=\s*(\d+)")
    n, slots, by_type = 0, 0, {}
    with open(export_mlir) as f:
        for ln in f:
            m = rx.search(ln)
            if m:
                n += 1
                slots += int(m.group(2))
                by_type[m.group(1)] = by_type.get(m.group(1), 0) + 1
    return n, slots, dict(sorted(by_type.items()))


_PATH_VAR = re.compile(r"^(data|valid|ready)Path(In|Out)_\S+$")


def _max_path_var(pairs):
    best = None
    for name, val in pairs:
        if _PATH_VAR.match(name) and (best is None or val > best[0]):
            best = (val, name)
    return best


def _cbc_resolves(comp_dir):
    """Yield (lp, sol) for the CBC re-solves made by calculatePathDelays.

    They are the models whose objective contains only path variables
    (BufferPlacementMILP::calculatePathDelays minimizes the sum of all
    {data,valid,ready}Path{In,Out} variables).
    """
    for lp in sorted(glob.glob(os.path.join(comp_dir, "cbc_model_*.lp"))):
        with open(lp) as f:
            obj = []
            for ln in f:
                if ln.startswith("Subject to"):
                    break
                if not ln.startswith("Maximize"):
                    obj.append(ln)
        names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", " ".join(obj))
        if names and all(_PATH_VAR.match(n) for n in names):
            sol = lp.replace("cbc_model_", "cbc_solution_")[:-3] + ".sol"
            if os.path.exists(sol):
                yield lp, sol


def parse_est_cp(comp_dir):
    """Largest arrival time the buffer-placement MILP believes in.

    Returns (value, (file, variable)) or (None, None).

    The documented source is the *_accurate_delays.log files written by
    BufferPlacementMILP::calculatePathDelays (CbcSolver::writeSol: one
    "<var> = <value>" line per MILP variable, then the objective alone on the
    last line). The arrival times are the variables
    {data,valid,ready}Path{In,Out}_<channel>. With CBC these files are
    unreliable: CbcSoluParser never clears its result map between solves and
    CBC's "solu" lists only non-zero variables, so a variable that is 0 in the
    re-solve keeps its value from the first solve (seen on loop_multiply:
    sink channels at 8.0 = target period). So, when the CBC re-solve files
    are in comp/, read the re-solve solutions directly (missing variable =
    0); otherwise (e.g. Gurobi) fall back to the logs.
    """
    best = None
    for _, sol in _cbc_resolves(comp_dir):
        with open(sol) as f:
            status = f.readline()
            if not (status.startswith("Optimal")
                    or status.startswith("Stopped")):
                continue
            pairs = []
            for ln in f:
                parts = ln.split()
                if len(parts) >= 3:
                    try:
                        pairs.append((parts[1], float(parts[2])))
                    except ValueError:
                        pass
        b = _max_path_var(pairs) or (0.0, None)
        if best is None or b[0] > best[0]:
            best = (b[0], (os.path.basename(sol), b[1]))
    if best is not None:
        return best
    return parse_accurate_delays_logs(comp_dir)


def parse_accurate_delays_logs(comp_dir):
    """Max path variable as written in the *_accurate_delays.log files."""
    best = None
    files = glob.glob(os.path.join(comp_dir, "buffer-placement",
                                   "*_accurate_delays.log"))
    for path in files:
        pairs = []
        with open(path) as f:
            for ln in f:
                if "=" in ln:
                    name, _, val = ln.partition("=")
                    try:
                        pairs.append((name.strip(), float(val)))
                    except ValueError:
                        pass
        b = _max_path_var(pairs)
        if b and (best is None or b[0] > best[0]):
            best = (b[0], (os.path.basename(path), b[1]))
    return best if best else (None, None)


def parse_sim(sim_dir):
    """Return (cycles, sim_pass) from the hls-verifier output.

    cycles comes from the testbench line "Simulation done! Latency = N
    cycles": the number of clock cycles between reset release and the
    rising edge at which the joined end/memory-done handshake fires.
    sim_pass is True when hls-verifier logs "C and VHDL outputs match".
    """
    cycles, sim_pass = None, None
    texts = []
    for p in [os.path.join(sim_dir, "report.txt")] + sorted(
            glob.glob(os.path.join(sim_dir, "HLS_VERIFY", "*.log"))):
        try:
            with open(p, errors="replace") as f:
                texts.append(f.read())
        except OSError:
            pass
    text = "\n".join(texts)
    lat = re.findall(r"Simulation done! Latency = (\d+) cycles", text)
    if lat:
        cycles = int(lat[-1])
    if "C and VHDL outputs match" in text:
        sim_pass = True
    elif "outputs do not match" in text:
        sim_pass = False
    return cycles, sim_pass


def parse_nextpnr(report):
    with open(report) as f:
        d = json.load(f)
    fmax = d.get("fmax", {}).get("clk", {}).get("achieved")
    util = {k: v.get("used", 0) for k, v in d.get("utilization", {}).items()}
    return fmax, util


def run_flow(src, out, period, timing_models=None, buffer_algorithm="fpl22",
             milp_solver="cbc", back_annotation=None, hdl="vhdl", sim=True,
             synth=True, compile_timeout=1200, calculate_path_delays=False,
             write_hdl_timeout=600, sim_timeout=1800, synth_timeout=1800):
    """Run the flow on one kernel and return the metrics dict.

    src: absolute path of the kernel .c (its .h, if any, is copied too).
    out: absolute output directory (created; the kernel is copied there and
         the Dynamatic outputs land in <out>/out/).
    """
    src = os.path.abspath(src)
    out = os.path.abspath(out)
    kernel = os.path.splitext(os.path.basename(src))[0]
    os.makedirs(out, exist_ok=True)
    for ext in (".c", ".h"):
        p = os.path.splitext(src)[0] + ext
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(out, kernel + ext))
    ksrc = os.path.join(out, kernel + ".c")
    out_dir = os.path.join(out, OUT_SUBDIR)
    comp_dir = os.path.join(out_dir, "comp")
    report = os.path.join(out_dir, "synth", "nextpnr_report.json")

    m = {k: None for k in METRIC_KEYS}
    m.update(kernel=kernel, target_period_ns=float(period),
             timing_models=os.path.abspath(timing_models) if timing_models
             else DEFAULT_TIMING_MODELS,
             buffer_algorithm=buffer_algorithm,
             back_annotation=os.path.abspath(back_annotation)
             if back_annotation else None,
             status="ok", wall_s={}, out_dir=out_dir, comp_dir=comp_dir)

    header = _dyn_header(ksrc, period)
    copts = [f"--buffer-algorithm {buffer_algorithm}"]
    if milp_solver:
        copts.append(f"--milp-solver {milp_solver}")
    if timing_models:
        copts.append(f"--timing-models {os.path.abspath(timing_models)}")
    if back_annotation:
        copts.append(f"--back-annotation {os.path.abspath(back_annotation)}")
    if calculate_path_delays:
        copts.append("--calculate-path-delays")

    plan = [("compile", f"compile {' '.join(copts)}\n", compile_timeout),
            ("write-hdl", f"write-hdl --hdl {hdl}\n", write_hdl_timeout)]
    if sim:
        if hdl != "vhdl":
            m["error"] = "simulate skipped: GHDL needs --hdl vhdl"
        else:
            plan.append(("simulate", "simulate --simulator ghdl\n",
                         sim_timeout))
    if synth:
        plan.append(("synthesize", "synthesize --tool yosys\n",
                     synth_timeout))

    for name, body, tmo in plan:
        ok, err, wall = _run_step(name, header + body, out, tmo)
        m["wall_s"][name] = round(wall, 1)
        if name == "compile" and ok:
            export = os.path.join(comp_dir, "handshake_export.mlir")
            if not os.path.exists(export):
                ok, err = False, "handshake_export.mlir not produced"
        if name == "synthesize" and ok and not os.path.exists(report):
            ok, err = False, "nextpnr_report.json not produced"
        # Collect what each step produced, even on failure of later steps
        if name == "compile" and ok:
            m["buffers"], m["buffer_slots"], m["buffers_by_type"] = \
                parse_buffers(os.path.join(comp_dir, "handshake_export.mlir"))
            est, where = parse_est_cp(comp_dir)
            raw, raw_where = parse_accurate_delays_logs(comp_dir)
            m["est_cp_ns"] = est
            with open(os.path.join(out, "est_cp.json"), "w") as f:
                json.dump({"est_cp_ns": est, "source": where,
                           "accurate_delays_log_max": raw,
                           "accurate_delays_log_source": raw_where}, f,
                          indent=1)
        if name == "simulate":
            m["cycles"], m["sim_pass"] = parse_sim(os.path.join(out_dir,
                                                                "sim"))
            if ok and m["sim_pass"] is False:
                ok, err = False, "C and VHDL outputs do not match"
        if name == "synthesize" and ok:
            fmax, util = parse_nextpnr(report)
            m["nextpnr_report"] = report
            m["fmax_mhz"] = round(fmax, 3) if fmax else None
            m["cp_ns"] = round(1000.0 / fmax, 3) if fmax else None
            m["lut4"] = util.get("TRELLIS_COMB")
            m["ff"] = util.get("TRELLIS_FF")
            m["dsp"] = util.get("MULT18X18D")
            m["bram"] = util.get("DP16KD")
        if not ok:
            m["status"], m["error"] = name, err
            break

    if m["cycles"] is not None and m["cp_ns"] is not None:
        m["exec_time_ns"] = round(m["cycles"] * m["cp_ns"], 1)

    with open(os.path.join(out, "metrics.json"), "w") as f:
        json.dump(m, f, indent=1)
    return m


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", required=True, help="absolute path of kernel .c")
    ap.add_argument("--out", required=True, help="absolute output directory")
    ap.add_argument("--period", required=True, type=float, help="target ns")
    ap.add_argument("--timing-models", default=None)
    ap.add_argument("--buffer-algorithm", default="fpl22")
    ap.add_argument("--milp-solver", default="cbc")
    ap.add_argument("--back-annotation", default=None)
    ap.add_argument("--hdl", default="vhdl")
    ap.add_argument("--no-sim", action="store_true")
    ap.add_argument("--no-synth", action="store_true")
    ap.add_argument("--compile-timeout", type=int, default=1200)
    ap.add_argument("--calculate-path-delays", action="store_true")
    a = ap.parse_args(argv)
    m = run_flow(a.src, a.out, a.period, timing_models=a.timing_models,
                 buffer_algorithm=a.buffer_algorithm,
                 milp_solver=a.milp_solver, back_annotation=a.back_annotation,
                 hdl=a.hdl, sim=not a.no_sim, synth=not a.no_synth,
                 compile_timeout=a.compile_timeout,
                 calculate_path_delays=a.calculate_path_delays)
    print(json.dumps(m, indent=1))
    return 0 if m["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
