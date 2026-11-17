"""Build HDL and run cocotb with the existing venv; pytest is not required."""

import argparse
import os
import shutil
from pathlib import Path
from xml.etree import ElementTree

from cocotb_tools.runner import get_results, get_runner

ROOT = Path(__file__).resolve().parents[1]


def preserve_python_with_oss_cad_suite():
    """Avoid the suite's vvp wrapper replacing the selected venv's Python."""
    executable = shutil.which("vvp")
    if executable is None:
        return  # Let cocotb report a missing simulator.
    wrapper = Path(executable)
    native_vvp = wrapper.parent.parent / "libexec" / "vvp"
    if native_vvp.is_file() and b"PYTHONHOME" in wrapper.read_bytes()[:8192]:
        tools_dir = ROOT / "sim_build" / "simulator-bin"
        tools_dir.mkdir(parents=True, exist_ok=True)
        link = tools_dir / "vvp"
        if not link.is_symlink() or link.resolve() != native_vvp.resolve():
            link.unlink(missing_ok=True)
            link.symlink_to(native_vvp)
        os.environ["PATH"] = str(tools_dir) + os.pathsep + os.environ["PATH"]
        print(f"Using {native_vvp} directly to preserve the selected Python environment.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sim", choices=("icarus", "verilator"), default=os.getenv("SIM", "icarus"))
    parser.add_argument("--module", choices=("all", "kalman", "fractional_divider"), default="all")
    parser.add_argument("--data-width", type=int, default=16)
    parser.add_argument("--frac-bits", type=int, default=None)
    parser.add_argument("--matrix", action="store_true", help="run (width, fraction) = (16,16), (8,6), (5,1)")
    parser.add_argument("--waves", action="store_true")
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--test-filter", default=None, help="regex selecting @cocotb.test names")
    args = parser.parse_args()
    if args.data_width < 4 or (args.frac_bits is not None and args.frac_bits < 1):
        parser.error("the regression needs data-width >= 4 and frac-bits >= 1")
    if args.sim == "icarus":
        preserve_python_with_oss_cad_suite()

    cases = [(16, 16), (8, 6), (5, 1)] if args.matrix else [
        (args.data_width, args.frac_bits if args.frac_bits is not None else args.data_width)
    ]
    modules = ("fractional_divider", "kalman") if args.module == "all" else (args.module,)
    total_tests = 0
    for width, frac in cases:
        for top in modules:
            parameters = ({"WIDTH": width + 2, "FRAC_BITS": frac} if top == "fractional_divider"
                          else {"DATA_WIDTH": width, "GAIN_FRAC_BITS": frac})
            sources = [ROOT / "srcs" / "fractional_divider.sv"]
            if top == "kalman":
                sources.append(ROOT / "srcs" / "kalman.sv")
            build_dir = ROOT / "sim_build" / f"{args.sim}_{top}_w{width}_f{frac}"
            runner = get_runner(args.sim)
            runner.build(
                sources=sources,
                hdl_toplevel=top,
                parameters=parameters,
                build_dir=build_dir,
                always=True,
                build_args=["-CFLAGS", "-std=c++17"] if args.sim == "verilator" else [],
                waves=args.waves,
                timescale=("1ns", "1ps"),
            )
            results = runner.test(
                hdl_toplevel=top,
                test_module=f"test_{top}",
                test_dir=build_dir,
                results_xml=str(build_dir / "results.xml"),
                extra_env={
                    "PYTHONPATH": str(ROOT / "tb") + os.pathsep + os.getenv("PYTHONPATH", ""),
                    "TEST_DATA_WIDTH": str(width),
                    "TEST_FRAC_BITS": str(frac),
                    "TEST_SEED": str(args.seed),
                },
                seed=args.seed,
                test_filter=args.test_filter,
                waves=args.waves,
            )
            tests, failures = get_results(results)
            skipped = sum(1 for case in ElementTree.parse(results).iter("testcase")
                          if case.find("skipped") is not None)
            executed = tests - skipped
            if executed == 0 or failures:
                raise SystemExit(f"FAIL: {top} width={width}, fraction={frac}: "
                                 f"{failures}/{executed} executed tests failed ({skipped} skipped)")
            total_tests += executed
    print(f"PASS: {total_tests} cocotb tests ({args.sim}); results under {ROOT / 'sim_build'}")


if __name__ == "__main__":
    main()
