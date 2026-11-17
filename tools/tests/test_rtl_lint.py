"""Run with: python3 -m unittest discover -s tools/tests -v."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import rtl_lint


class DiagnosticTests(unittest.TestCase):
    def test_verilator_source_and_summary(self):
        output = "%Warning-WIDTHTRUNC: /tmp/a file.sv:12:7: truncates value\n%Error: Exiting due to 1 warning(s)\n"
        found = rtl_lint.parse_diagnostics("verilator", output)
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].severity, found[0].code, found[0].line, found[0].column),
                         ("warning", "WIDTHTRUNC", 12, 7))
        self.assertEqual(found[0].file, str(Path("/tmp/a file.sv").resolve()))

    def test_yosys_frontend_position(self):
        found = rtl_lint.parse_diagnostics("yosys", "/tmp/broken.sv:3: ERROR: syntax error\n")
        self.assertEqual((found[0].severity, found[0].line, found[0].message),
                         ("error", 3, "syntax error"))

    def test_backend_failure_cannot_pass_silently(self):
        with patch.object(rtl_lint.subprocess, "run", return_value=subprocess.CompletedProcess([], 9, "unrecognized failure")):
            backend, found = rtl_lint.run_backend("verilator", ["dummy"], 1)
        self.assertTrue(backend["tool_failure"])
        self.assertEqual(found[0].code, "TOOL_FAILURE")

    def test_timeout_preserves_partial_log(self):
        with patch.object(rtl_lint.subprocess, "run", side_effect=subprocess.TimeoutExpired(["dummy"], 1, b"partial")):
            backend, found = rtl_lint.run_backend("verilator", ["dummy"], 1)
        self.assertTrue(backend["tool_failure"])
        self.assertEqual(backend["output"], "partial")
        self.assertEqual(found[0].code, "TIMEOUT")


@unittest.skipUnless(shutil.which("verilator"), "Verilator is required for integration tests")
class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rtl-lint-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def source(self, name, body):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        return target

    def run_lint(self, *args, expected=0, env=None):
        result = subprocess.run([sys.executable, str(TOOLS / "rtl_lint.py"), *map(str, args)],
                                cwd=str(self.root), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def good_source(self):
        return self.source("good.sv", "module good(input logic [7:0] a, output logic [7:0] y); assign y=a; endmodule\n")

    def test_clean_design_and_json_stdout(self):
        result = self.run_lint(self.good_source(), "--top", "good", "--json", "-")
        report = json.loads(result.stdout)
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["diagnostics"], [])
        self.assertEqual(report["backends"][0]["returncode"], 0)

    def test_syntax_error_has_source_location(self):
        source = self.source("broken.sv", "module broken(input logic a, output logic y); assign y = ; endmodule\n")
        report = json.loads(self.run_lint(source, "--top", "broken", "--json", "-", expected=1).stdout)
        self.assertTrue(any(d["severity"] == "error" and d["file"] == str(source) and d["line"] == 1
                            for d in report["diagnostics"]))

    def test_width_warnings_fail_by_default_and_can_be_allowed_or_suppressed(self):
        source = self.source("width.sv", "module width(input logic [15:0] a, output logic [7:0] y); assign y=a; endmodule\n")
        report = json.loads(self.run_lint(source, "--top", "width", "--json", "-", expected=1).stdout)
        codes = {d["code"] for d in report["diagnostics"] if d["severity"] == "warning"}
        self.assertTrue(any(code.startswith("WIDTH") for code in codes))
        allowed = json.loads(self.run_lint(source, "--top", "width", "--allow-warnings", "--json", "-").stdout)
        self.assertTrue(allowed["diagnostics"])
        options = [option for code in sorted(codes) for option in ("--suppress", code)]
        suppressed = json.loads(self.run_lint(source, "--top", "width", *options, "--json", "-").stdout)
        self.assertEqual(suppressed["diagnostics"], [])

    def test_latch_is_reported(self):
        source = self.source("latch.sv", "module latch(input logic en,a, output logic y); always_comb if(en) y=a; endmodule\n")
        report = json.loads(self.run_lint(source, "--top", "latch", "--json", "-", expected=1).stdout)
        self.assertTrue(any(d["code"] == "LATCH" for d in report["diagnostics"]))

    def test_undriven_signal_is_reported(self):
        source = self.source("undriven.sv", "module undriven(output wire y); wire missing; assign y=missing; endmodule\n")
        report = json.loads(self.run_lint(source, "--top", "undriven", "--json", "-", expected=1).stdout)
        self.assertTrue(any(d["code"] == "UNDRIVEN" for d in report["diagnostics"]))

    def test_implicit_net_is_reported(self):
        source = self.source("implicit_net.sv", "module implicit_net(input wire a, output wire y); assign typo=a; assign y=typo; endmodule\n")
        report = json.loads(self.run_lint(source, "--top", "implicit_net", "--json", "-", expected=1).stdout)
        self.assertTrue(any(d["code"] == "IMPLICIT" for d in report["diagnostics"]))

    def test_missing_module_is_an_error(self):
        source = self.source("missing.sv", "module missing(input logic a, output logic y); nonexistent u(.a(a),.y(y)); endmodule\n")
        report = json.loads(self.run_lint(source, "--top", "missing", "--json", "-", expected=1).stdout)
        self.assertTrue(any(d["severity"] == "error" for d in report["diagnostics"]))

    def test_configuration_paths_and_parameter_override(self):
        source = self.source("design/param_good.sv", "module param_good #(parameter WIDTH=16)(input logic [7:0] a, output logic [WIDTH-1:0] y); assign y=a; endmodule\n")
        config = self.root / "design" / "config.json"
        config.write_text(json.dumps({"sources": [source.name], "top": "param_good", "parameters": {"WIDTH": 16}}))
        report = json.loads(self.run_lint("--config", config, "-G", "WIDTH=8", "--json", "-").stdout)
        self.assertEqual(report["parameters"]["WIDTH"], "8")
        self.assertEqual(report["sources"], [str(source)])

    def test_directory_discovery_skips_generated_sources(self):
        good = self.good_source()
        self.source("sim_build/broken.sv", "definitely invalid SystemVerilog\n")
        report = json.loads(self.run_lint(self.root, "--top", "good", "--json", "-").stdout)
        self.assertEqual(report["sources"], [str(good)])

    def test_missing_input_and_invalid_config_return_setup_status(self):
        self.run_lint("missing.sv", expected=2)
        config = self.root / "bad.json"
        config.write_text('{"soruces": []}')
        self.run_lint("--config", config, expected=2)

    def test_missing_tool_returns_setup_status(self):
        # Absolute Python path still works when the simulator PATH is empty.
        self.run_lint(self.good_source(), env={"PATH": str(self.root)}, expected=2)

    def test_report_cannot_overwrite_source(self):
        source = self.good_source()
        original = source.read_text()
        self.run_lint(source, "--top", "good", "--json", source, expected=2)
        self.assertEqual(source.read_text(), original)

    def test_json_report_file_and_human_diagnostics(self):
        source = self.source("width.sv", "module width(input logic [15:0] a, output logic [7:0] y); assign y=a; endmodule\n")
        report_file = self.root / "reports" / "lint.json"
        result = self.run_lint(source, "--top", "width", "--json", report_file, expected=1)
        self.assertIn("WARNING [VERILATOR-", result.stdout)
        self.assertEqual(json.loads(report_file.read_text())["exit_code"], 1)

    def test_invalid_timeout_is_rejected(self):
        for value in ("0", "-1", "nan", "inf"):
            self.run_lint(self.good_source(), "--timeout", value, expected=2)

    @unittest.skipUnless(shutil.which("yosys"), "Yosys required")
    def test_yosys_includes_defines_and_parameter_literals(self):
        includes = self.root / "include directory"
        includes.mkdir()
        (includes / "width.svh").write_text("`define LOCAL_WIDTH `PORT_WIDTH\n")
        source = self.source("included.sv", '`include "width.svh"\nmodule included #(parameter WIDTH=16)(input logic [`LOCAL_WIDTH-1:0] a, output logic [WIDTH-1:0] y); assign y=a; endmodule\n')
        result = self.run_lint(source, "--top", "included", "--synth-check", "-I", includes,
                               "-D", "PORT_WIDTH=8", "-G", "WIDTH=8", "--json", "-")
        report = json.loads(result.stdout)
        self.assertEqual([b["tool"] for b in report["backends"]], ["verilator", "yosys"])
        self.assertIn('-chparam "WIDTH" 8', report["backends"][1]["script"])

    @unittest.skipUnless(shutil.which("yosys"), "Yosys required")
    def test_dollar_sign_in_module_name_is_literal(self):
        source = self.source("dollar$top.sv", "module dollar$top #(parameter WIDTH=16)(input logic [7:0] a, output logic [WIDTH-1:0] y); assign y=a; endmodule\n")
        self.run_lint(source, "--top", "dollar$top", "-G", "WIDTH=8", "--synth-check")

    @unittest.skipUnless(shutil.which("yosys"), "Yosys required")
    def test_structural_driver_conflicts_fail_even_when_warnings_allowed(self):
        source = self.source("drivers.sv", "module drivers(input logic clk,a,b, output logic y); always @(posedge clk) y<=a; always @(negedge clk) y<=b; endmodule\n")
        report = json.loads(self.run_lint(source, "--top", "drivers", "--synth-check", "--allow-warnings", "--json", "-", expected=1).stdout)
        self.assertTrue(any(d["tool"] == "yosys" and d["severity"] == "error" for d in report["diagnostics"]))

    @unittest.skipUnless(shutil.which("yosys"), "Yosys required")
    def test_spaces_and_semicolon_in_source_directory_are_literal(self):
        source = self.source("directory with spaces; $literal[command]/good.sv", "module good(input logic a, output logic y); assign y=a; endmodule\n")
        self.run_lint(source, "--top", "good", "--synth-check")


if __name__ == "__main__":
    unittest.main()
