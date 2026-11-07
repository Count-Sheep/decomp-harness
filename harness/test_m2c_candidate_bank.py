import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import m2c_candidate_bank as bank


class CandidateBankTests(unittest.TestCase):
    def test_plans_standalone_exact_auto_function_with_bound_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            winner = root / "winner.c"
            winner.write_text("s32 fn_80001008(void) { return 1; }\n")
            validation = root / "validation.json"
            validation.write_text(json.dumps({"summary": {"finished": True,
                "environment_unchanged": True}, "results": [{
                    "symbol": "fn_80001008", "instruction_exact": True,
                    "winner_path": str(winner), "winner_sha256": bank.digest(winner),
                    "profile": "1.3.2", "compiler_flags": ["-O4,p", "-lang=c"]}]}))
            report = root / "report.json"
            report.write_text(json.dumps({"units": [{
                "name": "main/auto_03_80001000_text", "metadata": {"auto_generated": True},
                "sections": [{"name": ".text"}],
                "functions": [{"name": "fn_80001008", "address": "8", "size": "12"}]}]}))
            rows = bank.candidate_rows(validation, report, "src/game/m2c_candidates")
            self.assertEqual(rows[0]["address"], 0x80001008)
            self.assertEqual(rows[0]["end"], 0x80001014)
            self.assertEqual(rows[0]["profile"], "1.3.2")
            self.assertIn("typedef int s32", rows[0]["source"])
            self.assertEqual(rows[0]["source_sha256"],
                             hashlib.sha256(rows[0]["source"].encode()).hexdigest())

    def test_skips_function_already_visible_from_source(self):
        report = {"units": [{"name": "main/source", "metadata": {"source_path": "src/a.c"},
                   "functions": [{"name": "done"}]}]}
        self.assertEqual(bank.source_backed_symbols(report), {"done"})

    def test_indexes_text_units_with_non_gamecube_section_numbers(self):
        report = {"units": [{
            "name": "main/auto_07_80100000_text", "metadata": {"auto_generated": True},
            "sections": [{"name": ".text"}],
            "functions": [{"name": "wii_fn", "address": "16", "size": "8"}],
        }]}
        self.assertEqual(bank.function_index(report)["wii_fn"]["address"], 0x80100010)

    def test_merge_splits_adds_candidate_in_address_order_and_rejects_overlap(self):
        text = ("Sections:\n\t.text type:code align:4\n\n"
                "game/later.c:\n\t.text       start:0x80000020 end:0x80000024\n")
        row = {"symbol": "early", "path": "game/m2c_candidates/candidate_80000010.c",
               "address": 0x80000010, "end": 0x80000014}
        merged = bank.merge_splits(text, [row], ".text")
        self.assertLess(merged.index("candidate_80000010"), merged.index("game/later.c"))
        overlap = {**row, "symbol": "overlap", "address": 0x80000020, "end": 0x80000024}
        with self.assertRaisesRegex(ValueError, "overlaps configured split"):
            bank.merge_splits(text, [overlap], ".text")

    def test_empty_materialization_does_not_rewrite_splits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".m2c-candidate-bank.json").write_text(json.dumps({"schema_version": 1,
                "versions": {"TEST": {"manifest": "manifest.json", "source_dir": "src/new",
                    "split_file": "splits.txt", "section": ".text"}}}))
            (root / "manifest.json").write_text('{"schema_version":1,"candidates":[]}\n')
            original = "Sections:\n\t.text type:code align:4\n"
            (root / "splits.txt").write_text(original)
            (root / "build/TEST").mkdir(parents=True)
            (root / "build/TEST/report.json").write_text('{"units":[]}')
            validation = root / "validation.json"
            validation.write_text(json.dumps({"summary": {"finished": True,
                "environment_unchanged": True}, "results": []}))
            self.assertEqual(bank.materialize(root, "TEST", validation)["added"], 0)
            self.assertEqual((root / "splits.txt").read_text(), original)

    def test_only_complete_object_reports_are_link_certified(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            report.write_text(json.dumps({"units": [
                {"metadata": {"source_path": "src/game/a.c"},
                 "sections": [{"fuzzy_match_percent": 100}],
                 "functions": [{"fuzzy_match_percent": 100}]},
                {"metadata": {"source_path": "src/game/b.c"},
                 "sections": [{"fuzzy_match_percent": 99}],
                 "functions": [{"fuzzy_match_percent": 100}]},
            ]}))
            self.assertEqual(bank.certified_paths(report, {"game/a.c", "game/b.c"}),
                             {"game/a.c"})

    def test_configure_command_uses_project_tools_and_optional_wrapper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(bank, "compiler_wrapper", return_value=[]):
                command = bank.configure_command(root, "TEST")
            self.assertEqual(command[command.index("--dtk") + 1], "build/tools/dtk")
            self.assertEqual(command[command.index("--objdiff") + 1], "build/tools/objdiff-cli")
            self.assertNotIn("--wrapper", command)
            wrapper = root / "build/tools/wrapper"
            with patch.object(bank, "compiler_wrapper", return_value=[str(wrapper)]):
                command = bank.configure_command(root, "TEST")
            self.assertEqual(command[command.index("--wrapper") + 1], "build/tools/wrapper")

    def test_extracts_only_candidates_named_by_linker_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "link.log"
            log.write_text("undefined: 'local'\nReferenced from 'f' in candidate_80001000.o\n")
            rows = [{"path": "game/m2c_candidates/candidate_80001000.c"},
                    {"path": "game/m2c_candidates/candidate_80002000.c"}]
            self.assertEqual(bank.referenced_candidate_paths(log, rows),
                             {"game/m2c_candidates/candidate_80001000.c"})


if __name__ == "__main__":
    unittest.main()
