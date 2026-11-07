import unittest
import json
import hashlib
from pathlib import Path
import tempfile
from unittest.mock import patch

import m2c_refinement_pipeline as pipeline


class RefinementPipelineTests(unittest.TestCase):
    def test_native_search_skips_owners_without_strict_supported_compiler(self):
        snapshot = {"rows": [
            {"symbol": "supported", "compiler_profile": {"supported": True, "profiles": [{}]}},
            {"symbol": "unknown", "compiler_profile": {"supported": False, "profiles": []}},
            {"symbol": "empty", "compiler_profile": {"supported": True, "profiles": []}},
        ]}
        self.assertEqual(pipeline.native_supported_symbols(snapshot), ["supported"])

    def test_validation_fingerprint_ignores_refinement_annotations(self):
        validation = {"summary": {"batch": "/batch", "environment_sha256": "a",
            "final_environment_sha256": "a", "updated_at": "one"},
            "results": [{"id": "1", "symbol": "f", "status": "non_exact", "pct": 99}]}
        before = pipeline.validation_fingerprint(validation)
        validation["summary"].update(updated_at="two", refinement={"exact": 1}, retention={"removed": 2})
        self.assertEqual(pipeline.validation_fingerprint(validation), before)
        validation["results"][0]["pct"] = 100
        self.assertNotEqual(pipeline.validation_fingerprint(validation), before)

    def test_selects_highest_scoring_durable_winners(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = []
            for name, pct, status in (("low", 98.0, "non_exact"), ("top", 99.9, "non_exact"),
                                      ("exact", 100, "scorer_exact")):
                path = root / (name + ".c")
                path.write_text(f"void {name}(void) {{}}")
                rows.append({"symbol": name, "pct": pct, "status": status, "winner_path": str(path)})
            selected = pipeline.selected_rows({"results": rows}, 1, 98)
            self.assertEqual([row["symbol"] for row in selected], ["top"])
            self.assertEqual(pipeline.selected_rows({"results": rows}, 3, 99), [rows[1]])
            with self.assertRaisesRegex(ValueError, "between zero and 32"):
                pipeline.selected_rows({}, 33, 0)

    def test_resume_keeps_completed_symbol_and_retries_only_interrupted_symbol(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            batch = root / "batch"
            batch.mkdir()
            winners = []
            for symbol in ("first", "second"):
                winner = root / f"{symbol}.c"
                winner.write_text(f"int {symbol}(void) {{ return 0; }}\n")
                winners.append({"symbol": symbol, "address": len(winners) * 4, "size": 4,
                    "pct": 99.0, "status": "non_exact", "candidate_origin": "current_source",
                    "winner_path": str(winner),
                    "winner_sha256": hashlib.sha256(winner.read_bytes()).hexdigest()})
            validation = {"summary": {"finished": True, "environment_unchanged": True,
                "batch": str(batch), "environment_sha256": "same",
                "final_environment_sha256": "same"}, "results": winners}
            validation_path = root / "validation.json"
            validation_path.write_text(json.dumps(validation))
            output = root / "pipeline"
            output.mkdir()
            selection = [{key: row.get(key) for key in ("symbol", "address", "size", "pct",
                         "candidate_origin", "winner_path", "winner_sha256")} for row in winners]
            state = {"schema_version": 1, "project_root": str(root), "version": "TEST",
                "validation": str(validation_path),
                "validation_sha256": pipeline.validation_fingerprint(validation),
                "selection": selection, "source_variants": 2, "native_variants": 0,
                "auto_promote": False,
                "refinement_engine": "deterministic_compiler_guided", "model_inputs": [],
                "attempts": [{"symbol": "first", "origin": "current_source",
                    "artifact": "done", "best_pct": 99.1, "object_exact": False}], "finished": False}
            (output / "results.json").write_text(json.dumps(state))
            destination = root / "build/decomp-support/TEST/refinements" / (
                "auto-" + hashlib.sha256((state["validation_sha256"] + "second" + str(output)).encode()).hexdigest()[:20])
            destination.mkdir(parents=True)
            (destination / "partial").write_text("discard")

            def source_run(*args, **kwargs):
                self.assertFalse(destination.exists())
                destination.mkdir(parents=True)
                return {"attempts": [{"pct": 99.2, "object_audit": {
                    "scratch_object_equal": False}}], "best_attempt": 0, "finished": True}

            with patch.object(pipeline.m2c_source_search, "run", side_effect=source_run) as search, \
                 patch.object(pipeline.m2c_unit_autogate, "run", return_value={
                     "ready_units": 0, "gates_run": 0, "links_verified": 0}):
                result = pipeline.run(root, "TEST", validation_path, output, limit=2,
                                      source_variants=2, native_variants=0)
            self.assertEqual(search.call_count, 1)
            self.assertEqual([row["symbol"] for row in result["attempts"]], ["first", "second"])
            self.assertTrue(result["finished"])

    def test_source_failure_is_recorded_and_unit_gate_still_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            batch = root / "batch"
            batch.mkdir()
            winner = root / "winner.c"
            winner.write_text("int target(void) { return 0; }\n")
            validation = {"summary": {"finished": True, "environment_unchanged": True,
                "batch": str(batch), "environment_sha256": "same",
                "final_environment_sha256": "same"}, "results": [{
                    "symbol": "target", "address": 0, "size": 4, "pct": 99.0,
                    "status": "non_exact", "candidate_origin": "current_source",
                    "winner_path": str(winner),
                    "winner_sha256": hashlib.sha256(winner.read_bytes()).hexdigest()}]}
            validation_path = root / "validation.json"
            validation_path.write_text(json.dumps(validation))
            gated = {"ready_units": 1, "gates_run": 0, "links_verified": 0, "promoted": 0}
            with patch.object(pipeline.m2c_source_search, "run", side_effect=ValueError("no source")), \
                 patch.object(pipeline.m2c_unit_autogate, "run", return_value=gated) as unit_gate:
                result = pipeline.run(root, "TEST", validation_path, root / "pipeline", limit=1,
                                      source_variants=2, native_variants=0)
            self.assertEqual(result["attempts"][0]["status"], "skipped")
            self.assertIn("no source", result["attempts"][0]["error"])
            self.assertEqual(unit_gate.call_count, 1)
            self.assertTrue(result["finished"])


if __name__ == "__main__":
    unittest.main()
