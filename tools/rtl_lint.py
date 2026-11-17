#!/usr/bin/env python3
"""Lint Verilog/SystemVerilog with Verilator and optional Yosys structural checks."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time


SKIP_DIRS = {".git", ".venv", ".venv-native", "node_modules", "sim_build", "obj_dir", "__pycache__"}
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
INTEGER_LITERAL = re.compile(r"^[+-]?(?:[0-9][0-9_]*|(?:[0-9]+)?'[sS]?[bBoOdDhH][0-9a-fA-F_xXzZ?]+|'[01xXzZ])$")
CONFIG_KEYS = {"sources", "top", "include_dirs", "defines", "parameters", "suppress", "synth_check"}


class ConfigurationError(Exception):
    pass


@dataclass
class Diagnostic:
    tool: str
    severity: str
    code: str
    message: str
    file: str = None
    line: int = None
    column: int = None


def location(text):
    """Split a tool's optional filename:line[:column]: prefix."""
    match = re.match(r"^(.*?):(\d+)(?::(\d+))?:\s*(.*)$", text)
    if not match:
        return None, None, None, text
    filename, line, column, message = match.groups()
    return str(Path(filename).resolve()), int(line), int(column) if column else None, message


def parse_diagnostics(tool, output):
    diagnostics = []
    for text in output.splitlines():
        if tool == "verilator":
            match = re.match(r"^%(Warning|Error)(?:-([A-Z0-9_]+))?:\s*(.*)$", text)
        else:
            match = re.match(r"^(Warning|ERROR)(?:\s*\[([^]]+)\])?:\s*(.*)$", text)
            if match is None:
                # Yosys frontend errors put the source position BEFORE ERROR.
                match = re.match(r"^(.*?:\d+(?::\d+)?):\s*(ERROR):\s*(.*)$", text)
                if match:
                    source, severity, message = match.groups()
                    filename, line, column, message = location(source + ": " + message)
                    diagnostics.append(Diagnostic(tool, severity.lower(), "FRONTEND", message,
                                                  filename, line, column))
                    continue
        if match is None:
            continue
        severity, code, message = match.groups()
        # Avoid counting Verilator's trailing error-count summary as a new issue.
        if tool == "verilator" and message.startswith("Exiting due to"):
            continue
        filename, line, column, message = location(message)
        diagnostics.append(Diagnostic(tool, severity.lower(), code or "GENERAL", message,
                                      filename, line, column))
    return diagnostics


def assignments(values, option):
    result = {}
    for value in values:
        name, separator, definition = value.partition("=")
        if not IDENTIFIER.fullmatch(name) or (option == "parameter" and not separator):
            raise ConfigurationError("{} must be NAME=VALUE (defines may omit =VALUE): {}".format(option, value))
        definition = definition if separator else "1"
        if not definition or any(c in definition for c in "\x00\r\n"):
            raise ConfigurationError("invalid {} value: {}".format(option, value))
        result[name] = definition
    return result


def string_list(value, name):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigurationError("{} must be an array of strings".format(name))
    return value


def dictionary_assignments(value, name):
    if not isinstance(value, dict):
        raise ConfigurationError("{} must be a JSON object".format(name))
    if any(not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, (str, int))
           for k, v in value.items()):
        raise ConfigurationError("{} keys must be strings and values strings or integers".format(name))
    return assignments(["{}={}".format(k, v) for k, v in value.items()], name)


def resolve_path(value, base):
    if any(c in value for c in "\x00\r\n"):
        raise ConfigurationError("paths cannot contain NUL or newline characters")
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def collect_sources(paths):
    result = []
    for path in paths:
        if path.is_dir():
            found = []
            for root, dirs, files in os.walk(str(path)):
                dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
                found.extend(Path(root) / name for name in sorted(files)
                             if Path(name).suffix.lower() in {".v", ".sv"})
            result.extend(sorted(found))
        elif path.is_file() and path.suffix.lower() in {".v", ".sv"}:
            result.append(path)
        else:
            raise ConfigurationError("not a Verilog/SystemVerilog file or directory: {}".format(path))
    # Keep explicit source ordering (packages must precede their consumers).
    result = list(dict.fromkeys(path.resolve() for path in result))
    if not result:
        raise ConfigurationError("no .v or .sv sources found")
    return result


def settings(args):
    cwd = Path.cwd()
    config_path = resolve_path(args.config, cwd) if args.config else None
    if config_path is None and not args.sources and (cwd / "rtl_lint.json").is_file():
        config_path = cwd / "rtl_lint.json"
    config = {}
    if config_path:
        try:
            config = json.loads(config_path.read_text())
        except (OSError, ValueError) as error:
            raise ConfigurationError("cannot read config {}: {}".format(config_path, error))
        if not isinstance(config, dict) or set(config) - CONFIG_KEYS:
            raise ConfigurationError("config must be an object with keys: " + ", ".join(sorted(CONFIG_KEYS)))
    base = config_path.parent if config_path else cwd
    sources = ([resolve_path(p, cwd) for p in args.sources] if args.sources else
               [resolve_path(p, base) for p in string_list(config.get("sources", []), "sources")])
    sources = collect_sources(sources)
    top = args.top if args.top is not None else config.get("top")
    if top is not None and (not isinstance(top, str) or not IDENTIFIER.fullmatch(top)):
        raise ConfigurationError("top must be a simple Verilog module identifier")
    includes = [resolve_path(p, base) for p in string_list(config.get("include_dirs", []), "include_dirs")]
    includes += [resolve_path(p, cwd) for p in args.include]
    if any(not p.is_dir() for p in includes):
        raise ConfigurationError("every include directory must exist")
    defines = {"SYNTHESIS": "1"}
    defines.update(dictionary_assignments(config.get("defines", {}), "define"))
    defines.update(assignments(args.define, "define"))
    parameters = dictionary_assignments(config.get("parameters", {}), "parameter")
    parameters.update(assignments(args.parameter, "parameter"))
    if any(not INTEGER_LITERAL.fullmatch(value) for value in parameters.values()):
        raise ConfigurationError("parameter values must be integer literals, for example 16 or 8'hff")
    if parameters and not top:
        raise ConfigurationError("parameter overrides require --top or a config top")
    suppress = string_list(config.get("suppress", []), "suppress") + args.suppress
    if any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", code) for code in suppress):
        raise ConfigurationError("suppression codes must be uppercase Verilator warning names")
    configured_synth = config.get("synth_check", False)
    if not isinstance(configured_synth, bool):
        raise ConfigurationError("synth_check must be true or false")
    synth = configured_synth if args.synth_check is None else args.synth_check
    if synth and not top:
        raise ConfigurationError("--synth-check requires --top or a config top")
    if args.json_report and args.json_report != "-":
        report_path = resolve_path(args.json_report, cwd)
        if report_path in sources or report_path == config_path:
            raise ConfigurationError("the JSON report must not overwrite an input source or config")
    return sources, top, includes, defines, parameters, list(dict.fromkeys(suppress)), synth


def tcl_word(value):
    """A literal Tcl word: prevent variable/command substitution in paths."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("$", "\\$").replace("[", "\\[").replace("]", "\\]")
    return '"' + escaped + '"'


def run_backend(tool, command, timeout):
    started = time.monotonic()
    try:
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   universal_newlines=True, timeout=timeout, check=False)
        output, returncode = completed.stdout, completed.returncode
        diagnostics = parse_diagnostics(tool, output)
        tool_failure = returncode < 0
        if returncode and not any(d.severity == "error" for d in diagnostics):
            tool_failure = True
            diagnostics.append(Diagnostic(tool, "error", "TOOL_FAILURE",
                                          "backend exited with status {}; use --verbose for its log".format(returncode)))
    except subprocess.TimeoutExpired as error:
        output = error.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        returncode, tool_failure = None, True
        diagnostics = [Diagnostic(tool, "error", "TIMEOUT", "backend exceeded {} seconds".format(timeout))]
    except OSError as error:
        output, returncode, tool_failure = str(error), None, True
        diagnostics = [Diagnostic(tool, "error", "TOOL_FAILURE", str(error))]
    return {"tool": tool, "command": command, "returncode": returncode,
            "elapsed_seconds": round(time.monotonic() - started, 3), "output": output,
            "tool_failure": tool_failure}, diagnostics


def lint(args, options):
    sources, top, includes, defines, parameters, suppress, synth = options
    diagnostics, backends = [], []
    executable = shutil.which("verilator")
    if not executable:
        raise ConfigurationError("verilator was not found on PATH; install Verilator or load OSS CAD Suite")
    command = [executable, "--lint-only", "--Wall", "-Wno-fatal", "--timing"]
    if top:
        command += ["--top-module", top]
    command += ["-I" + str(p) for p in includes]
    command += ["-D{}={}".format(k, v) for k, v in defines.items()]
    command += ["-G{}={}".format(k, v) for k, v in parameters.items()]
    command += ["-Wno-" + code for code in suppress]
    # Keep any backend scratch files outside the source tree.
    with tempfile.TemporaryDirectory(prefix="rtl-lint-") as scratch:
        command += ["--Mdir", str(Path(scratch) / "obj_dir")]
        command += [str(path) for path in sources]
        backend, found = run_backend("verilator", command, args.timeout)
        backends.append(backend)
        diagnostics.extend(found)
        if synth and not any(d.severity == "error" for d in diagnostics):
            executable = shutil.which("yosys")
            if not executable:
                raise ConfigurationError("--synth-check requested but yosys was not found on PATH")
            # Tcl forwards actual argument boundaries to Yosys. Its plain .ys
            # parser retains quotes on -I/-D values, breaking paths with spaces.
            read = ["yosys", "read_verilog", "-sv", "-defer"]
            read += [tcl_word("-I" + str(path)) for path in includes]
            read += [tcl_word("-D{}={}".format(name, value)) for name, value in defines.items()]
            read += [tcl_word(path) for path in sources]
            hierarchy = ["yosys", "hierarchy", "-check", "-top", tcl_word(top)]
            for name, value in parameters.items():
                hierarchy += ["-chparam", tcl_word(name), value]
            script = "\n".join([" ".join(read), " ".join(hierarchy), "yosys proc", "yosys opt_clean", "yosys check -assert"])
            script_path = Path(scratch) / "check.tcl"
            script_path.write_text(script + "\n")
            backend, found = run_backend("yosys", [executable, "-Q", "-T", "-c", str(script_path)], args.timeout)
            # Preserve the script in reports because its temporary file is removed.
            backend["script"] = script
            backends.append(backend)
            diagnostics.extend(found)
    if any(b["tool_failure"] for b in backends):
        status = 2
    elif any(d.severity == "error" or (d.severity == "warning" and not args.allow_warnings)
             for d in diagnostics):
        status = 1
    else:
        status = 0
    return {"schema_version": 1, "exit_code": status, "top": top,
            "sources": [str(p) for p in sources], "parameters": parameters,
            "diagnostics": [asdict(d) for d in diagnostics], "backends": backends}


def emit(report, args):
    serialized = json.dumps(report, indent=2) + "\n"
    if args.json_report == "-":
        sys.stdout.write(serialized)
    else:
        for diagnostic in report["diagnostics"]:
            where = diagnostic["file"] or ""
            if diagnostic["line"] is not None:
                where += ":{}".format(diagnostic["line"])
                if diagnostic["column"] is not None:
                    where += ":{}".format(diagnostic["column"])
            print("{} [{}-{}] {}{}".format(
                diagnostic["severity"].upper(), diagnostic["tool"].upper(), diagnostic["code"],
                where + ": " if where else "", diagnostic["message"]))
        errors = sum(d["severity"] == "error" for d in report["diagnostics"])
        warnings = sum(d["severity"] == "warning" for d in report["diagnostics"])
        print("{}: {} error(s), {} warning(s), {} source(s).".format(
            "PASS" if report["exit_code"] == 0 else "FAIL", errors, warnings, len(report["sources"])))
        if args.json_report:
            target = Path(args.json_report).expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(serialized)
            print("Report: {}".format(target))
    if args.verbose:
        for backend in report["backends"]:
            print("\n$ " + " ".join(shlex.quote(word) for word in backend["command"]), file=sys.stderr)
            if "script" in backend:
                print(backend["script"], file=sys.stderr)
            print(backend["output"], file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="*", help=".sv/.v files or directories (recursive)")
    parser.add_argument("--config", help="JSON config; defaults to ./rtl_lint.json when no sources are given")
    parser.add_argument("--top", help="top-level module; recommended for all hierarchy checks")
    parser.add_argument("-I", "--include", action="append", default=[], metavar="DIR")
    parser.add_argument("-D", "--define", action="append", default=[], metavar="NAME[=VALUE]")
    parser.add_argument("-G", "--parameter", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--suppress", action="append", default=[], metavar="CODE", help="suppress one Verilator warning category")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--synth-check", dest="synth_check", action="store_true", default=None)
    group.add_argument("--no-synth-check", dest="synth_check", action="store_false")
    parser.add_argument("--allow-warnings", action="store_true", help="warnings remain visible but do not fail lint")
    parser.add_argument("--json", dest="json_report", metavar="FILE", help="write structured results; '-' prints JSON only")
    parser.add_argument("--verbose", action="store_true", help="also print complete backend commands and logs to stderr")
    parser.add_argument("--timeout", type=float, default=60, metavar="SECONDS", help="timeout for each backend (default: 60)")
    args = parser.parse_args(argv)
    if args.timeout <= 0 or not args.timeout < float("inf"):
        parser.error("--timeout must be a finite positive number")
    try:
        report = lint(args, settings(args))
        emit(report, args)
    except (ConfigurationError, OSError) as error:
        print("ERROR [RTL-LINT-SETUP] {}".format(error), file=sys.stderr)
        return 2
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
