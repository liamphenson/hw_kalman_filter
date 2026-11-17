# RTL lint tool

`tools/rtl_lint.py` checks Verilog and SystemVerilog before simulation or FPGA
implementation. It presents errors and warnings with codes and source locations,
similar to a compiler or Vivado's Messages view. It uses established HDL parsers
and elaborators rather than trying to recognize HDL errors with text patterns.

The default engine is Verilator. An optional Yosys pass adds generic structural
checks. This is a command-line tool, not a reproduction of Vivado's GUI or its
device-specific design rule checks. It does not edit RTL files.

## Requirements

- Python 3.7 or newer; no additional Python packages or activated venv required.
- `verilator` on PATH.
- For `--synth-check`, `yosys` with Tcl support on PATH. The installed OSS CAD
  Suite provides both engines and the required Tcl support.

Check availability with:

```sh
python3 --version
verilator --version
yosys -V
```

Unlike cocotb, this script does not load Python into the simulator. Python and
the backend executables therefore do not need matching CPU architectures.

## Quick start: this project

Run these commands from the project root:

```sh
cd /Users/liamhenson/hw_kalman_filter

# Lint the Kalman filter and divider using rtl_lint.json.
python3 tools/rtl_lint.py

# Also elaborate a generic netlist and check its structure.
python3 tools/rtl_lint.py --synth-check

# Check another elaboration of the same filter.
python3 tools/rtl_lint.py --synth-check -G DATA_WIDTH=12 -G GAIN_FRAC_BITS=10
```

A clean result looks like:

```text
PASS: 0 error(s), 0 warning(s), 2 source(s).
```

## Lint any RTL module

Pass the module and all its dependencies, with the top-level **module name**:

```sh
python3 tools/rtl_lint.py srcs/fractional_divider.sv --top fractional_divider

python3 tools/rtl_lint.py srcs/fractional_divider.sv srcs/kalman.sv --top kalman

# Recursively collect .v and .sv files from a directory.
python3 tools/rtl_lint.py srcs --top kalman
```

For RTL outside this project, use an absolute path to the script and your source:

```sh
python3 /Users/liamhenson/hw_kalman_filter/tools/rtl_lint.py \
    /path/to/design/top.sv /path/to/design/helper.sv --top top
```

When explicit sources are supplied, the project config is **not** automatically
loaded. Use `--config` explicitly if you want its other settings to apply. In that
case, positional sources replace the config's source list.

An explicit top is recommended. Without it, Verilator chooses the top; multiple
unrelated modules may produce a MULTITOP warning. Parameter overrides and the
Yosys pass require a top. Run separate invocations to lint independent tops.

Directory discovery excludes hidden subdirectories, virtual environments,
`sim_build`, `obj_dir`, `node_modules`, and Python caches. Header files (`.vh`,
`.svh`) are resolved through includes, not compiled as standalone modules. Files
are deduplicated. Explicit file order is preserved; directory discovery is sorted.
Put packages before consumers using an explicit ordered source list if necessary.

## Includes, defines, and parameters

Repeat `-I`, `-D`, and `-G` as needed:

```sh
python3 tools/rtl_lint.py srcs --top kalman \
    -I include -I "shared headers" \
    -D FEATURE_ENABLED -D ADC_BITS=12 \
    -G DATA_WIDTH=12 -G GAIN_FRAC_BITS=10
```

- `-I DIR` adds an existing include directory.
- `-D NAME` defines a preprocessor macro as 1; `-D NAME=VALUE` supplies its value.
- `-G NAME=VALUE` overrides a top-level parameter at elaboration. This wrapper
  accepts integer literals, including decimal and sized Verilog constants. Quote
  apostrophes for your shell: `-G "INITIAL_ESTIMATE=16'h0010"`. String parameters
  and parameter expressions are not supported by this wrapper.
- The tool defines `SYNTHESIS=1` by default for both engines, selecting the RTL's
  synthesis view. Backend-specific predefined macros still differ.

No operating-system shell is used to run the engines. The Yosys pass uses a
generated Tcl script with literal argument quoting so spaces and special
characters in paths do not become commands.

## Reusable project configuration

The included `rtl_lint.json` contains:

```json
{
  "sources": ["srcs/fractional_divider.sv", "srcs/kalman.sv"],
  "top": "kalman",
  "include_dirs": [],
  "defines": {},
  "parameters": {},
  "suppress": [],
  "synth_check": false
}
```

Create a similar JSON file for another design and run:

```sh
python3 tools/rtl_lint.py --config /path/to/design/rtl_lint.json
```

Config paths are relative to the config file, while command-line paths are
relative to the current working directory. With no positional sources or explicit
config, the tool looks for `rtl_lint.json` in the current working directory.

Command-line top and parameter values override config values. Includes and
warning suppressions are combined; command-line macro values override matching
config definitions. Use `--no-synth-check` to override a config that enables
structural checking. Unknown config keys are rejected to catch spelling mistakes.

## Understanding and managing diagnostics

Example message:

```text
WARNING [VERILATOR-WIDTHTRUNC] /path/to/width.sv:3:14: Operator ASSIGNW expects 8 bits ...
FAIL: 0 error(s), 1 warning(s), 1 source(s).
```

Read it as severity, engine/rule, source position, and explanation. Diagnostic
positions are absolute and canonicalized. Some global or structural messages do
not have a source location. Use `--verbose` for original messages, source context,
continuation lines, instance details, and exact backend commands:

```sh
python3 tools/rtl_lint.py --verbose
```

Typical findings and useful first actions:

| Category | What to inspect |
| --- | --- |
| Syntax/unknown module | Missing punctuation, dependency files, include paths, module names |
| WIDTH / WIDTHTRUNC / WIDTHEXPAND | Operand sizes, casts, signedness, intermediate precision |
| IMPLICIT | Misspelled or undeclared signals; declare nets explicitly |
| UNDRIVEN | Missing assignment or missing connection |
| UNUSEDSIGNAL / UNUSEDPARAM | Unused logic or intentionally unused signals |
| LATCH | Missing combinational assignments or incomplete branches |
| CASEINCOMPLETE / CASEOVERLAP | State coverage and overlapping selections |
| MULTIDRIVEN / Yosys driver conflicts | Multiple processes driving the same signal |
| UNOPTFLAT / Yosys logic loops | Circular combinational dependencies |

Warnings fail the command by default. To keep warnings visible without failing:

```sh
python3 tools/rtl_lint.py --allow-warnings
```

To intentionally suppress one Verilator category:

```sh
python3 tools/rtl_lint.py --suppress UNUSEDSIGNAL
```

Suppressions can also go in the config's `suppress` array or use Verilator's
localized `lint_off`/`lint_on` source directives. Prefer fixing unintended issues
or using a narrowly scoped waiver. `--allow-warnings` never hides errors and does
not override a failing Yosys `check -assert` structural check.

## Optional structural checks

`--synth-check` runs only after Verilator finishes without errors. It performs:

```text
read_verilog -sv -defer ...
hierarchy -check -top ...
proc
opt_clean
check -assert
```

This checks the elaborated, generic hardware for conflicting drivers, used but
undriven wires, and combinational loops. It stops before technology mapping or
place-and-route and does not write a netlist. Yosys's native SystemVerilog frontend
supports a subset of the language. A legal construct accepted by Verilator may
therefore be rejected by this optional backend; inspect the original diagnostic.
Do not treat that rejection alone as proof that the RTL is illegal SystemVerilog.

This pass does not load vendor primitive libraries automatically. Vendor cells
need suitable supplied models or black-box declarations for hierarchy checking.

## JSON reports, automation, and exit codes

```sh
# Human messages on stdout; complete machine-readable report in a file.
python3 tools/rtl_lint.py --synth-check --json lint_reports/kalman.json

# JSON only on stdout; --verbose logs, if enabled, go to stderr.
python3 tools/rtl_lint.py --json -

# Allow more time for a larger design (per backend).
python3 tools/rtl_lint.py --timeout 180
```

Reports include source files, top, parameter overrides, diagnostics, backend exit
codes, run times, commands, and original logs. Yosys's generated script text is
included because temporary files are removed after linting. Setup/argument errors
are printed to stderr and may not produce a report; always check the process exit
code before consuming a report left from a previous run. The report destination
cannot be one of the source files or the config file.

| Exit code | Meaning |
| --- | --- |
| 0 | No errors and no disallowed warnings |
| 1 | Backend reported errors or warnings rejected by the default policy |
| 2 | Invalid arguments/config, missing tools/files, timeout, or backend execution failure |

CI can invoke the same command and use the exit code to decide whether to proceed
to simulation. Keep JSON reports as build artifacts. `lint_reports/` is ignored by
this repository. The tool writes temporary backend scratch files outside the
source tree and cleans them afterward.

## Checking the linter itself

```sh
python3 -m unittest discover -s tools/tests -v
```

The tests use temporary RTL fixtures and verify clean designs, syntax errors,
width warnings, latches, implicit nets, undriven signals, missing dependencies,
configuration/parameter overrides, includes/macros, quoted paths, structural
driver conflicts, report output, missing tools, timeouts, and exit policies.
Integration cases require their corresponding engines; unittest marks them
skipped if those engines are unavailable.

Lint supplements the cocotb tests. It does not establish numerical Kalman-filter
accuracy, prove behavior over all input sequences, replace functional coverage,
or check FPGA timing constraints, pin assignments, electrical rules, CDC/RDC, or
device-specific implementation rules. Continue to run simulation and the chosen
FPGA implementation tools.

## Engine references

- [Verilator lint and command options](https://verilator.org/guide/latest/exe_verilator.html)
- [Yosys structural check pass](https://yosyshq.readthedocs.io/projects/yosys/en/latest/cmd/check.html)
- [Yosys native SystemVerilog support](https://yosyshq.readthedocs.io/projects/yosys/en/latest/using_yosys/verilog.html)
