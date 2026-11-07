import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import m2c_unit_autogate as gate


class UnitAutoGateTests(unittest.TestCase):
    def test_source_less_partial_owner_gets_one_grouped_draft_trial(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            report = root / "build/TEST/report.json"
            report.parent.mkdir(parents=True)
            report.write_text(json.dumps({"units": [{"name": "main/game/unit", "functions": [
                {"name": name, "fuzzy_match_percent": 0} for name in ("one", "two", "missing")]}]}))
            (root / "objdiff.json").write_text(json.dumps({"units": [{"name": "main/game/unit"}]}))
            split = root / "config/TEST/splits.txt"
            split.parent.mkdir(parents=True)
            split.write_text("game/unit.cpp:\n\t.text start:0x80001000 end:0x8000100C\n")
            batch = root / "batch"
            batch.mkdir()
            (batch / "results.json").write_text('{}')
            rows = []
            for name in ("one", "two"):
                winner = root / (name + ".c")
                winner.write_text(f"void {name}(void) {{}}")
                rows.append({"symbol": name, "instruction_exact": True, "winner_path": str(winner),
                             "winner_sha256": hashlib.sha256(winner.read_bytes()).hexdigest()})
            validation = root / "validation.json"
            validation.write_text(json.dumps({"summary": {"finished": True,
                "environment_unchanged": True, "batch": str(batch)}, "results": rows}))
            with patch.object(gate.m2c_unit_pipeline, "run", return_value={"state": "draft"}) as pipeline:
                result = gate.run(root, "TEST", validation, [], root / "gates")
            pipeline.assert_called_once()
            self.assertEqual(set(pipeline.call_args.kwargs["function_sources"]), {"one", "two"})
            self.assertNotIn("unit_source", pipeline.call_args.kwargs)
            self.assertEqual(result["owner_trials_run"], 1)
            self.assertEqual(result["owner_trials"][0]["symbols"], ["one", "two"])

    def test_validation_evidence_is_hashed_once_for_all_exact_functions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = []
            for symbol in ("one", "two"):
                winner = root / f"{symbol}.c"
                winner.write_text(f"int {symbol}(void) {{ return 1; }}\n")
                rows.append({"symbol": symbol, "instruction_exact": True,
                             "winner_path": str(winner),
                             "winner_sha256": hashlib.sha256(winner.read_bytes()).hexdigest()})
            validation = root / "validation.json"
            validation.write_text(json.dumps({"summary": {"finished": True,
                "environment_unchanged": True}, "results": rows}))
            original = gate.digest
            calls = []
            def measured(path):
                calls.append(Path(path))
                return original(Path(path))
            with patch.object(gate, "digest", side_effect=measured):
                found = gate.exact_validation(validation)
            self.assertEqual(set(found), {"one", "two"})
            self.assertEqual(calls.count(validation), 1)

    def test_inventory_requires_every_function_and_only_gates_overrides(self):
        report = {"units": [{"name": "main/unit", "metadata": {"source_path": "src/unit.c"},
            "functions": [{"name": "already", "fuzzy_match_percent": 100},
                          {"name": "new", "fuzzy_match_percent": 99}]},
            {"name": "main/done", "metadata": {"source_path": "src/done.c"},
             "functions": [{"name": "done", "fuzzy_match_percent": 100}]}]}
        rows = gate.inventory(report, {})
        self.assertFalse(rows[0]["ready"])
        rows = gate.inventory(report, {"new": {}})
        self.assertTrue(rows[0]["gate_required"])
        self.assertTrue(rows[1]["ready"])
        self.assertFalse(rows[1]["gate_required"])

    def test_configured_missing_source_can_become_a_complete_unit_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            split = root / "config/TEST/splits.txt"
            split.parent.mkdir(parents=True)
            split.write_text("Sections:\n\t.text type:code align:4\n\n"
                             "game/unit.cpp:\n\t.text start:0x80001000 end:0x80001004\n")
            report = {"units": [{"name": "main/game/unit", "metadata": {},
                "functions": [{"name": "exact", "fuzzy_match_percent": 0}]}]}
            paths = gate.configured_source_paths(root, "TEST", report)
            rows = gate.inventory(report, {"exact": {}}, paths, root)
            self.assertEqual(rows[0]["source_path"], "src/game/unit.cpp")
            self.assertFalse(rows[0]["source_exists"])
            self.assertTrue(rows[0]["gate_required"])

    def test_exact_refinement_requires_object_certificate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "artifact"
            artifact.mkdir()
            (artifact / "draft.c").write_text("int exact(void) { return 1; }\n", newline="\n")
            helper = artifact / "helper.c"
            helper.write_text("static inline int helper(void) { return 2; }\n")
            path = root / "results.json"
            path.write_text(json.dumps({"results": [{"symbol": "exact", "best_attempt": 0,
                "attempts": [{"artifact": str(artifact), "object_audit": {
                    "scratch_object_equal": True}, "owner_rewrite_artifacts": [{
                        "path": "src/unit.c", "symbol": "helper", "source_path": str(helper),
                        "sha256": hashlib.sha256(helper.read_bytes()).hexdigest()}]}]}]}))
            found = gate.exact_refinements([path])
            self.assertEqual(found["exact"]["source_sha256"],
                             hashlib.sha256((artifact / "draft.c").read_bytes()).hexdigest())
            self.assertEqual(found["exact"]["helper_rewrites"][0]["symbol"], "helper")
            data = json.loads(path.read_text())
            data["results"][0]["attempts"][0]["object_audit"]["scratch_object_equal"] = False
            path.write_text(json.dumps(data))
            self.assertEqual(gate.exact_refinements([path]), {})

    def test_ready_owner_automatically_invokes_link_pipeline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "src/unit.c"
            source.parent.mkdir()
            source.write_text("int exact(void) { return 0; }")
            report = root / "build/TEST/report.json"
            report.parent.mkdir(parents=True)
            report.write_text(json.dumps({"units": [{"name": "main/unit",
                "metadata": {"source_path": "src/unit.c"},
                "functions": [{"name": "exact", "fuzzy_match_percent": 99}]}]}))
            (root / "objdiff.json").write_text(json.dumps({"units": [{"name": "main/unit",
                "metadata": {"source_path": "src/unit.c"}}]}))
            batch = root / "batch"
            batch.mkdir()
            (batch / "results.json").write_text('{}')
            validation = root / "validation.json"
            validation.write_text(json.dumps({"summary": {"finished": True, "environment_unchanged": True,
                "batch": str(batch)}, "results": []}))
            artifact = root / "artifact"
            artifact.mkdir()
            (artifact / "draft.c").write_text("int exact(void) { return 1; }\n")
            helper = artifact / "helper.c"
            helper.write_text("static inline int helper(void) { return 2; }\n")
            refinement = root / "refinement.json"
            refinement.write_text(json.dumps({"results": [{"symbol": "exact", "best_attempt": 0,
                "attempts": [{"artifact": str(artifact), "object_audit": {
                    "scratch_object_equal": True}, "owner_rewrite_artifacts": [{
                        "path": "src/unit.c", "symbol": "helper", "source_path": str(helper),
                        "sha256": hashlib.sha256(helper.read_bytes()).hexdigest()}]}]}]}))
            measured = {"state": "link_trial_complete", "object_verified": True,
                        "link_verified": True}
            with patch.object(gate.m2c_unit_pipeline, "run", return_value=measured) as pipeline:
                result = gate.run(root, "TEST", validation, [refinement], root / "gates")
            self.assertEqual(result["links_verified"], 1)
            self.assertFalse((root / "gates/active.json").exists())
            self.assertEqual(pipeline.call_args.kwargs["link"], True)
            self.assertEqual(set(pipeline.call_args.kwargs["function_sources"]), {"exact"})
            self.assertEqual(set(pipeline.call_args.kwargs["helper_sources"]), {"helper"})

    def test_link_verified_unit_can_enter_automatic_promotion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "src/unit.c"
            source.parent.mkdir()
            source.write_text("int exact(void) { return 0; }")
            report = root / "build/TEST/report.json"
            report.parent.mkdir(parents=True)
            report.write_text(json.dumps({"units": [{"name": "main/unit",
                "metadata": {"source_path": "src/unit.c"},
                "functions": [{"name": "exact", "fuzzy_match_percent": 99}]}]}))
            (root / "objdiff.json").write_text(json.dumps({"units": [{"name": "main/unit",
                "metadata": {"source_path": "src/unit.c"}}]}))
            batch = root / "batch"
            batch.mkdir()
            (batch / "results.json").write_text('{}')
            validation = root / "validation.json"
            validation.write_text(json.dumps({"summary": {"finished": True,
                "environment_unchanged": True, "batch": str(batch)}, "results": []}))
            artifact = root / "artifact"
            artifact.mkdir()
            (artifact / "draft.c").write_text("int exact(void) { return 1; }\n")
            refinement = root / "refinement.json"
            refinement.write_text(json.dumps({"results": [{"symbol": "exact", "best_attempt": 0,
                "attempts": [{"artifact": str(artifact), "object_audit": {
                    "scratch_object_equal": True}}]}]}))
            measured = {"state": "link_trial_complete", "object_verified": True,
                        "link_verified": True}
            promoted = {"state": "promoted", "promoted": True, "commit": "abc"}
            with patch.object(gate.m2c_unit_pipeline, "run", return_value=measured), \
                 patch.object(gate.m2c_unit_auto_promote, "run", return_value=promoted) as auto:
                result = gate.run(root, "TEST", validation, [refinement], root / "gates",
                                  auto_promote=True)
            self.assertEqual(auto.call_count, 1)
            self.assertEqual(result["promoted"], 1)

    def test_exact_function_in_incomplete_owner_gets_incremental_owner_trial(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "src/unit.c"
            source.parent.mkdir()
            source.write_text("int exact(void) { return 0; }\nint remaining(void) { return 0; }\n")
            report = root / "build/TEST/report.json"
            report.parent.mkdir(parents=True)
            report.write_text(json.dumps({"units": [{"name": "main/unit",
                "metadata": {"source_path": "src/unit.c"}, "functions": [
                    {"name": "exact", "fuzzy_match_percent": 99},
                    {"name": "remaining", "fuzzy_match_percent": 50}]}]}))
            (root / "objdiff.json").write_text(json.dumps({"units": [{"name": "main/unit",
                "metadata": {"source_path": "src/unit.c"}}]}))
            batch = root / "batch"
            batch.mkdir()
            (batch / "results.json").write_text('{}')
            winner = root / "winner.c"
            winner.write_text("int exact(void) { return 1; }\n")
            validation = root / "validation.json"
            validation.write_text(json.dumps({"summary": {"finished": True,
                "environment_unchanged": True, "batch": str(batch)}, "results": [{
                    "symbol": "exact", "instruction_exact": True,
                    "winner_path": str(winner),
                    "winner_sha256": hashlib.sha256(winner.read_bytes()).hexdigest()}]}))
            measured = {"state": "link_trial_complete", "object_verified": True,
                        "link_verified": True}
            promoted = {"state": "promoted", "promoted": True, "commit": "abc"}
            with patch.object(gate.m2c_unit_pipeline, "run", return_value=measured) as pipeline, \
                 patch.object(gate.m2c_unit_auto_promote, "run", return_value=promoted) as auto:
                result = gate.run(root, "TEST", validation, [], root / "gates", auto_promote=True)
            self.assertEqual(result["owner_trials_run"], 1)
            self.assertEqual(result["owner_objects_verified"], 1)
            self.assertEqual(result["owner_links_verified"], 1)
            self.assertEqual(result["owner_promoted"], 1)
            self.assertEqual(result["gates_run"], 0)
            self.assertEqual(result["promoted"], 1)
            self.assertEqual(pipeline.call_args.kwargs["function_sources"].keys(), {"exact"})
            self.assertEqual(auto.call_count, 1)


if __name__ == "__main__":
    unittest.main()
