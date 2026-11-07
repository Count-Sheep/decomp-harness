import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import m2c_current_source as current


class CurrentSourceCandidateTests(unittest.TestCase):
    def fixture(self, root: Path):
        source = root / "src/unit.c"
        source.parent.mkdir()
        source.write_text("int helper(void) { return 1; }\nint target(void) { return helper(); }\n")
        target = root / "build/TEST/obj/unit.o"
        base = root / "build/TEST/src/unit.o"
        target.parent.mkdir(parents=True)
        base.parent.mkdir(parents=True)
        target.write_bytes(b"target")
        base.write_bytes(b"base")
        report = {"units": [{"name": "main/unit", "metadata": {"source_path": "src/unit.c"},
            "functions": [{"name": "target", "size": "8", "fuzzy_match_percent": 98.5,
                           "metadata": {"virtual_address": "2147487744"}}]}]}
        (root / "build/TEST/report.json").write_text(json.dumps(report))
        (root / "objdiff.json").write_text(json.dumps({"units": [{"name": "main/unit",
            "target_path": "build/TEST/obj/unit.o", "base_path": "build/TEST/src/unit.o",
            "metadata": {"source_path": "src/unit.c"}}]}))
        return source, target, base

    def test_measures_real_owner_source_and_object(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target, base = self.fixture(root)
            audit = {"status": "checked", "scratch_object_equal": False,
                     "byte_feedback": {"different_words": 1}}
            row = {"symbol": "target", "address": 0x80001000, "size": 8,
                   "compiler_profile": {"selected": {"mw_version": "GC/2.6"}}}
            with patch.object(current.m2c_object_evidence, "compare", return_value=audit) as compare:
                result = current.measure(root, "TEST", row)
            compare.assert_called_once_with(target.resolve(), base.resolve(), "target")
            self.assertEqual(result["pct"], 98.5)
            self.assertEqual(result["candidate"], "int target(void) { return helper(); }\n")
            self.assertEqual(result["scoring_scope"], "project_owner_compiled_object")
            self.assertEqual(result["source_path"], "src/unit.c")
            self.assertIn(source.resolve(), current.input_paths(root, "TEST", [row]))

    def test_missing_source_candidate_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            self.assertIsNone(current.measure(root, "TEST", {"symbol": "absent"}))


if __name__ == "__main__":
    unittest.main()
