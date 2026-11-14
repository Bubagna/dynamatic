import os
from multiprocessing import Pool
from utils import VhdlInterfaceInfo, NUM_CORES
from nextpnr_backend import is_ecp5

def _synth_worker(args):
    synth_tool, tcl_file, log_file = args
    if is_ecp5(synth_tool):
        # The ecp5 backend writes a shell script that runs yosys and nextpnr itself
        if os.system(f"bash {tcl_file} > {log_file} 2>&1") != 0:
            print("\033[91m" + f"[ERROR] {tcl_file} failed (see {log_file})" + "\033[0m")
    else:
        os.system(f"{synth_tool} -mode batch -source {tcl_file} > {log_file}")

def run_synthesis(tcl_files, synth_tool, log_file, num_jobs=NUM_CORES):
    """
    Run synthesis for the given TCL files using the specified synthesis tool in parallel (if num_jobs > 1).
    Args:
        tcl_files (list): List of TCL files to run synthesis on.
        synth_tool (str): Synthesis tool to use (e.g., 'vivado').
        num_jobs (int): Number of synthesis jobs run in parallel.
    """
    # Run synthesis in parallel using Vivado
    args_list = [(synth_tool, tcl_file, f"{log_file}{i}") for i, tcl_file in enumerate(tcl_files)]
    with Pool(processes=num_jobs) as pool:
        pool.map(_synth_worker, args_list)

def write_sdc_constraints(sdc_file, period_ns):
    """
    Write the SDC constraints file with the specified period.
    
    Args:
        sdc_file (str): Path to the SDC file.
        period_ns (float): Period in nanoseconds.
    """
    with open(sdc_file, 'w') as f:
        f.write(f"create_clock -name clk -period {period_ns} -waveform {{0.000 {period_ns/2}}} [get_ports clk]\n")
        f.write("set_property HD.CLK_SRC BUFGCTRL_X0Y0 [get_ports clk]\n")
