import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from m2c_capture_inputs import Inputs
from m2c_unit_candidates import assess, index_drafts, discover_source
from m2c_unit_source import m2c_available

M2C = m2c_available()


class CandidateSelectionTests(unittest.TestCase):
    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_native_discovery_separates_helpers_from_owner_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "unit.c"
            source.write_text('#include "header.h"')
            config = {"name": "main/test", "metadata": {"source_path": "unit.c"}}
            inputs = Inputs()
            unrelated = root / "unrelated.c"
            unrelated.write_text("unrelated")
            inputs.read(unrelated)
            expanded = "static inline int helper(void) { return 1; } int first(void) { return helper(); }"

            def capture(project, unit, path, output, captured):
                self.assertNotIn(str(unrelated), captured.files)
                captured.read(source)
                return expanded

            with patch("m2c_unit_preprocess.run", side_effect=capture) as native:
                result = discover_source(root, config, self.owner(), inputs, root / "preprocess")
            native.assert_called_once()
            self.assertEqual(native.call_args.args[:4], (root, "main/test", source, root / "preprocess"))
            self.assertIsNot(native.call_args.args[4], inputs)
            self.assertIn(str(source), inputs.files)
            self.assertIn(str(unrelated), inputs.files)
            self.assertEqual(result["source_functions"], ["first"])
            self.assertEqual(result["source_inline_helpers"], ["helper"])
            self.assertTrue(result["source_preprocessed"])
            self.assertFalse(assess(self.owner(), {}, result["source_functions"])["implementation_covered"])

    def test_separate_source_capture_cannot_overwrite_prior_input_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "unit.c"
            source.write_text("int first(void) { return 1; }")
            header = root / "header.h"
            header.write_text("old")
            inputs = Inputs()
            inputs.read(header)
            expected = dict(inputs.files[str(header)])
            header.write_text("new")

            def capture(project, unit, path, output, captured):
                captured.read(header)
                return source.read_text()

            with patch("m2c_unit_preprocess.run", side_effect=capture):
                with self.assertRaisesRegex(ValueError, "between source captures"):
                    discover_source(root, {"name": "main/test", "metadata": {"source_path": "unit.c"}},
                                    self.owner(), inputs, root / "output")
            self.assertEqual(inputs.files[str(header)], expected)

    def test_source_can_fill_missing_or_competing_drafts_without_claiming_exactness(self):
        drafts = {"first": {("a", "b"), ("c", "d")}}
        result = assess(self.owner(), drafts, ["first", "second"])
        self.assertFalse(result["draft_covered"])
        self.assertTrue(result["implementation_covered"])
        self.assertFalse(result["unit_complete"])
        self.assertEqual(result["missing_functions"], ["second"])
        self.assertEqual(result["missing_implementations"], [])

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_discovery_binds_recorded_source_and_rejects_wrong_owner_or_headers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "unit.c"
            config = {"metadata": {"source_path": "unit.c"}}
            source.write_text("int first(void) { return 2; }")
            inputs = Inputs()
            found = discover_source(root, config, self.owner(), inputs)
            self.assertEqual(found["source_functions"], ["first"])
            self.assertEqual(found["source_path"], str(source))
            source.write_text("int unrelated(void) { return 2; }")
            with self.assertRaisesRegex(ValueError, "Input changed"):
                inputs.verify()
            with self.assertRaisesRegex(ValueError, "outside target owner"):
                discover_source(root, config, self.owner(), Inputs())
            source.write_text('#include "types.h"\nint first(void) { return 2; }')
            with self.assertRaisesRegex(ValueError, "Preprocessor"):
                discover_source(root, config, self.owner(), Inputs())
            config["metadata"]["source_path"] = "../outside.c"
            with self.assertRaisesRegex(ValueError, "project-local"):
                discover_source(root, config, self.owner(), Inputs())

    def owner(self):
        return {"sections": [{"definitions": [{"name": "first", "type": 2, "size": 4},
                    {"name": "second", "type": 2, "size": 8}, {"name": "data", "type": 1, "size": 4}],
                "unnamed_ranges": [[12, 16]], "relocations": [{}]}]}

    def test_coverage_does_not_prove_data_or_unit_completeness(self):
        result = assess(self.owner(), {"first": {("source", "context")}, "second": {("other", "context")}})
        self.assertTrue(result["draft_covered"])
        self.assertFalse(result["unit_complete"])
        self.assertEqual((result["data_definitions"], result["unnamed_ranges"], result["relocations"]), (1, 1, 1))

    def test_missing_and_competing_contexts_prevent_selection(self):
        result = assess(self.owner(), {"first": {("source", "context1"), ("source", "context2")}})
        self.assertFalse(result["draft_covered"])
        self.assertEqual(result["missing_functions"], ["second"])
        self.assertEqual(result["ambiguous_functions"], ["first"])
        self.assertFalse(assess({"sections": []}, {})["draft_covered"])

    def test_index_requires_finished_batch_and_binds_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            batch = root / "results.json"
            batch.write_text(json.dumps({"summary": {"finished": False}, "results": []}))
            with self.assertRaisesRegex(ValueError, "completed"):
                index_drafts([batch], Inputs())
            (root / "draft.c").write_text("void first(void) {}")
            context = root / "context.c"
            context.write_text("typedef int value;")
            batch.write_text(json.dumps({"summary": {"finished": True}, "results": [
                {"unit": "test", "symbol": "first", "status": "ok", "artifacts": str(root)}]}))
            inputs = Inputs()
            self.assertIn("first", index_drafts([batch], inputs)["main/test"])
            context.write_text("typedef float value;")
            with self.assertRaisesRegex(ValueError, "Input changed"):
                inputs.verify()


if __name__ == "__main__":
    unittest.main()
