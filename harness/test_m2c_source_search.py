import unittest
from pathlib import Path
from unittest.mock import patch
import tempfile

import m2c_source_search as search


class SourceSearchTests(unittest.TestCase):
    def test_search_budget_controls_are_bounded(self):
        with self.assertRaisesRegex(ValueError, "variant budget"):
            search.run(Path('.'), 'TEST01', {}, Path('unused'), 65)
        with self.assertRaisesRegex(ValueError, "Beam width"):
            search.run(Path('.'), 'TEST01', {}, Path('unused'), 1, beam_width=0)
        with self.assertRaisesRegex(ValueError, "Search depth"):
            search.run(Path('.'), 'TEST01', {}, Path('unused'), 1, max_depth=7)

    def test_transform_pool_never_exceeds_generator_cap(self):
        source = "int target(void) { return 0; }\n"
        with patch.object(search.transforms, "variants", return_value=[]) as variants:
            self.assertEqual(search.candidates(source, 64), [])
        variants.assert_called_once_with(source, None, 64)

    def test_candidates_include_bounded_declarations_and_safe_rewrites(self):
        source = """int target(int a, int b) {
    int first;
    int second;
    first = a + b;
    return first;
}
"""
        variants = search.candidates(source, 8)
        self.assertLessEqual(len(variants), 8)
        self.assertEqual(len({item["sha256"] for item in variants}), len(variants))
        self.assertIn("declaration_order", {item["kind"] for item in variants})
        self.assertIn("expression_shape", {item["kind"] for item in variants})
        self.assertTrue(all(item["code"] != source for item in variants))

    def test_rank_requires_object_identity_before_fuzzy_score(self):
        fuzzy = {"compile_ok": True, "pct": 100, "object_audit": {
            "scratch_object_equal": False, "byte_feedback": {"different_words": 1}}}
        exact = {"compile_ok": True, "pct": 99, "object_audit": {
            "scratch_object_equal": True, "byte_feedback": {"different_words": 0}}}
        self.assertGreater(search.rank(exact), search.rank(fuzzy))
        with self.assertRaisesRegex(ValueError, "between zero and 64"):
            search.candidates("void f(void) {}", 65)

    def test_owner_search_finds_transitive_inline_helpers_for_register_residuals(self):
        files = {"src/unit.c": """static inline int inner(int value) {
    int first;
    int second;
    return value;
}
static inline int outer(int value) { return inner(value); }
int target(int value) { return outer(value); }
"""}
        helpers = search.inline_helpers(files, "target", "int target(int value) { return outer(value); }")
        self.assertEqual([row["symbol"] for row in helpers], ["outer", "inner"])
        classification = {"counts": {"register": 2}, "total": 2, "register_only": True}
        variants = search.owner_candidates(files, "target",
            "int target(int value) { return outer(value); }", [], classification, 8)
        self.assertTrue(variants)
        self.assertTrue(all(row["kind"].startswith("inline_helper_") for row in variants))
        self.assertTrue(all(row["owner_rewrites"] for row in variants))
        structural = {"counts": {"register": 2, "branch": 1}, "total": 3,
                      "register_only": True}
        self.assertEqual(search.owner_candidates(files, "target",
            "int target(int value) { return outer(value); }", [], structural, 8), [])

    def test_external_seed_is_scored_before_its_variants(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            seed = root / "winner.c"
            seed.write_text("int target(void) { return 2; }\n")
            output = root / "out"
            baseline = {"candidate": "int target(void) { return 1; }\n", "source_path": "src/unit.c",
                        "unit": "main/unit", "compiled": True, "pct": 80,
                        "object_audit": {"scratch_object_equal": False}}
            found = {"owner": {"base_path": "build/unit.o"},
                     "paths": {"target": root / "target.o"}}
            index = unittest.mock.Mock()
            index.locate.return_value = found
            measured = {"compile_ok": True, "pct": 90, "compile_output": str(root / "candidate.o")}
            (root / "candidate.o").write_bytes(b"object")
            with patch.object(search.m2c_current_source, "Index", return_value=index), \
                 patch.object(search.m2c_current_source, "measure", return_value=baseline), \
                 patch.object(search.subprocess, "run", return_value=unittest.mock.Mock(
                     stdout="mwcceppc -c src/unit.c -o build/unit.o")), \
                 patch.object(search.validator, "validate_one", return_value=measured), \
                 patch.object(search.m2c_object_evidence, "compare", return_value={
                     "scratch_object_equal": False}), \
                 patch.object(search, "candidates", return_value=[]):
                result = search.run(root, "TEST", {"symbol": "target"}, output, seed_path=seed,
                                    seed_origin="m2c")
            self.assertEqual(result["attempts"][0]["origin"], "m2c")
            self.assertIn("return 2", result["attempts"][0]["candidate"])

    def test_beam_search_combines_deterministic_steps_without_model_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            seed = root / "winner.c"
            seed.write_text("int target(void) { return 1; }\n")
            baseline = {"candidate": seed.read_text(), "source_path": "src/unit.c", "unit": "main/unit",
                        "compiled": True, "pct": 80, "object_audit": {"scratch_object_equal": False}}
            found = {"owner": {"base_path": "build/unit.o"},
                     "paths": {"target": root / "target.o", "base": root / "base.o"}}
            index = unittest.mock.Mock()
            index.locate.return_value = found
            (root / "candidate.o").write_bytes(b"object")
            def variants(source, limit, classification=None):
                value = int(source.split("return ")[1].split(";")[0]) + 1
                code = f"int target(void) {{ return {value}; }}\n"
                return [{"code": code, "kind": "expression_shape", "detail": f"step {value}",
                         "sha256": search.hashlib.sha256(code.encode()).hexdigest()}]
            def measure(root, row, command, owner, fallback, folder, **kwargs):
                code = (Path(row["artifacts"]) / "draft.c").read_text()
                value = int(code.split("return ")[1].split(";")[0])
                return {"compile_ok": True, "pct": 80 + value, "compile_output": str(root / "candidate.o"),
                        "mismatch_classification": {"register_only": False}}
            with patch.object(search.m2c_current_source, "Index", return_value=index), \
                 patch.object(search.m2c_current_source, "measure", return_value=baseline), \
                 patch.object(search.subprocess, "run", return_value=unittest.mock.Mock(
                     stdout="mwcceppc -c src/unit.c -o build/unit.o")), \
                 patch.object(search.validator, "validate_one", side_effect=measure), \
                 patch.object(search.m2c_object_evidence, "compare", return_value={"scratch_object_equal": False}), \
                 patch.object(search, "candidates", side_effect=variants):
                result = search.run(root, "TEST", {"symbol": "target"}, root / "out", 2,
                                    seed_path=seed, seed_origin="m2c", max_depth=2)
            self.assertEqual(result["schema_version"], 2)
            self.assertEqual(result["model_inputs"], [])
            self.assertEqual(result["attempts"][-1]["search_depth"], 2)
            self.assertEqual(len(result["attempts"][-1]["lineage"]), 2)


if __name__ == "__main__":
    unittest.main()
