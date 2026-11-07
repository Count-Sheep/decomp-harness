import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import claude_m2c_engineer as engineer


class ClaudeM2CEngineerTests(unittest.TestCase):
    def test_load_api_key_requires_private_file(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True):
            path = Path(temporary) / ".env"
            path.write_text("ANTHROPIC_API_KEY=test-key\n")
            path.chmod(0o600)
            self.assertTrue(engineer.load_api_key(path))
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "test-key")
            del os.environ["ANTHROPIC_API_KEY"]
            if engineer.harness_paths.WINDOWS:
                return  # POSIX permission bits do not exist; the file ACL governs access.
            path.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "group/other"):
                engineer.load_api_key(path)

    def test_dossier_selects_cluster_and_bounds_large_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "snapshot.json"
            path.write_text(json.dumps({"rows": [
                {"symbol": "large", "size": 40, "categories": ["syntax"],
                 "draft": "x" * 9000, "context": "c" * 5000, "assembly": "a" * 7000,
                 "compiler_messages": ["expected ;"], "outcome": {}},
                {"symbol": "other", "size": 4, "categories": ["type"], "outcome": {}},
            ]}))
            dossier, symbols = engineer.build_dossier(path, ["syntax"], 3)
            self.assertEqual(symbols, ["large"])
            self.assertIn("...[truncated]", dossier)
            self.assertLess(len(dossier), 16000)

    def test_selection_offset_advances_without_repeating_rows(self):
        snapshot = {"rows": [
            {"symbol": f"f{index}", "size": index, "categories": ["syntax"]}
            for index in range(7)
        ]}
        first = engineer.select_rows(snapshot, ["syntax"], 3, 0)
        second = engineer.select_rows(snapshot, ["syntax"], 3, 3)
        self.assertEqual([row["symbol"] for row in first], ["f0", "f1", "f2"])
        self.assertEqual([row["symbol"] for row in second], ["f3", "f4", "f5"])

    def test_selection_can_exclude_an_active_campaign_category(self):
        snapshot = {"rows": [
            {"symbol": "syntax-only", "size": 1, "categories": ["syntax"]},
            {"symbol": "overlap", "size": 2, "categories": ["syntax", "type"]},
            {"symbol": "type-only", "size": 3, "categories": ["type"]},
        ]}
        rows = engineer.select_rows(snapshot, ["type"], 4, excluded_categories=["syntax"])
        self.assertEqual([row["symbol"] for row in rows], ["type-only"])

    def test_current_preflight_replaces_stale_evidence_and_skips_exact_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "snapshot.json"
            snapshot.write_text("{}")
            output = root / "preflight"
            rows = [
                {"symbol": "exact", "categories": ["old_syntax"], "draft": "old"},
                {"symbol": "fuzzy", "categories": ["old_type"], "draft": "old"},
            ]

            def fake_run(*_args, **_kwargs):
                for index, draft in enumerate(("exact draft", "fresh fuzzy draft")):
                    folder = output / f"{index:05d}/baseline"
                    folder.mkdir(parents=True)
                    (folder / "draft.c").write_text(draft)
                output.mkdir(exist_ok=True)
                (output / "results.json").write_text(json.dumps({
                    "summary": {"baseline": {"compiled": 2}},
                    "results": [
                        {"symbol": "exact", "baseline": {
                            "outcome": {"compiled": True, "pct": 100}, "categories": [],
                            "compiler_messages": [], "evidence": {"instruction_exact": True}}},
                        {"symbol": "fuzzy", "baseline": {
                            "outcome": {"compiled": True, "pct": 72}, "categories": [],
                            "compiler_messages": [], "evidence": {"instruction_exact": False}}},
                    ]}))

            with patch.object(engineer.experiment, "run", side_effect=fake_run) as invoked:
                active, skipped, summary = engineer.refresh_current_rows(
                    snapshot, root, rows, output)
            self.assertEqual(skipped, ["exact"])
            self.assertEqual([row["symbol"] for row in active], ["fuzzy"])
            self.assertEqual(active[0]["draft"], "fresh fuzzy draft")
            self.assertEqual(active[0]["historical_categories"], ["old_type"])
            self.assertEqual(active[0]["outcome"]["pct"], 72)
            self.assertEqual(summary["baseline"]["compiled"], 2)
            self.assertEqual(invoked.call_args.kwargs["symbols"], ["exact", "fuzzy"])

    def test_changed_path_policy_is_narrow(self):
        self.assertEqual(engineer.disallowed_paths([
            "m2c/flow_graph.py", "m2c_pycparser/c_parser.py", "tests/unit/test_new.py"]), [])
        self.assertEqual(engineer.disallowed_paths(["README.md", "special_cases.json"]),
                         ["README.md", "special_cases.json"])

    def test_unit_tests_use_the_current_virtualenv_interpreter(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(engineer, "command", return_value=completed) as invoked:
            result = engineer.run_unit_tests(Path("m2c"))
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(invoked.call_args.args[0][0], sys.executable)

    def test_ownership_hints_point_to_mnemonic_implementation_and_tests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "m2c").mkdir()
            (root / "tests/unit").mkdir(parents=True)
            (root / "m2c/arch.py").write_text('handlers = {"psq_st": store_quantized}\n')
            (root / "tests/unit/test_stack.py").write_text('self.translate("psq_st f1,8(r1)")\n')
            hints = engineer.ownership_hints(root, [{
                "assembly": "/* 0000 0000 00 00 00 00 */ psq_st f1, 8(r1)",
                "compiler_messages": ["undefined identifier 'sp8'"]}])
            self.assertIn("m2c/arch.py", hints)
            self.assertIn("tests/unit/test_stack.py", hints)

    def test_assessment_accepts_gain_without_losses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "results.json").write_text(json.dumps({"results": [
                self.pair("gain", False, 0, False, True, 80, False),
                self.pair("control", True, 100, True, True, 100, True),
            ], "summary": {}}))
            result = engineer.assess_ab(root)
            self.assertTrue(result["admitted"])
            self.assertEqual(result["compile_gains"], ["gain"])

    def test_assessment_rejects_neutral_and_any_regression(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "results.json").write_text(json.dumps({"results": [
                self.pair("neutral", True, 50, False, True, 50, False),
            ]}))
            self.assertFalse(engineer.assess_ab(root)["admitted"])
            (root / "results.json").write_text(json.dumps({"results": [
                self.pair("gain", True, 50, False, True, 60, False),
                self.pair("loss", True, 100, True, True, 99, False),
            ]}))
            result = engineer.assess_ab(root)
            self.assertFalse(result["admitted"])
            self.assertTrue(result["has_gain"])
            self.assertTrue(result["has_loss"])
            self.assertEqual(result["exact_losses"], ["loss"])

    @staticmethod
    def pair(symbol, old_compiled, old_pct, old_exact, new_compiled, new_pct, new_exact):
        return {"symbol": symbol,
                "baseline": {"outcome": {"compiled": old_compiled, "pct": old_pct},
                             "evidence": {"instruction_exact": old_exact}},
                "candidate": {"outcome": {"compiled": new_compiled, "pct": new_pct},
                              "evidence": {"instruction_exact": new_exact}}}


if __name__ == "__main__":
    unittest.main()
