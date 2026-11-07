"""Draft validation retains independent scratch evidence without promotion."""

import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import m2c_validate_drafts as validator


class DraftValidationTests(unittest.TestCase):
    def test_parallel_validation_is_bounded_and_keeps_result_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            batch = root / "build/decomp-support/TEST01/batches/batch-one"
            batch.mkdir(parents=True)
            rows = [{"id": str(index), "symbol": f"fn_{index}", "unit": "unit",
                     "address": 0x80001000 + index * 4, "status": "unsupported"}
                    for index in range(4)]
            (batch / "results.json").write_text(json.dumps({"summary": {"finished": True},
                                                              "results": rows}))
            active = 0
            peak = 0
            lock = threading.Lock()

            def validate(_root, _version, row, _folder, _environment, _linked, _index):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.02 if row["id"] == "0" else 0.005)
                with lock:
                    active -= 1
                return {"id": row["id"], "symbol": row["symbol"], "unit": row["unit"],
                        "status": "missing_draft", "compile_ok": False,
                        "instruction_exact": False}

            with patch.object(validator, "build_check", return_value={"status": "exact"}), \
                 patch.object(validator, "fingerprint", return_value="f" * 64), \
                 patch.object(validator, "validate_one", side_effect=validate), \
                 patch.object(validator.m2c_report, "refresh_report"):
                summary = validator.run(root, "TEST01", batch, prune_superseded=False, jobs=3)
            output = batch.parent.parent / "validation" / ("batch-one-" + "f" * 12) / "results.json"
            saved = json.loads(output.read_text())
            self.assertGreaterEqual(peak, 2)
            self.assertLessEqual(peak, 3)
            self.assertEqual(summary["jobs"], 3)
            self.assertEqual([row["id"] for row in saved["results"]], ["0", "1", "2", "3"])

    def test_explicit_seed_migrates_matching_result_and_winner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft = root / "draft"
            draft.mkdir()
            (draft / "draft.c").write_text("void target(void) {}\n")
            row = {"id": "one", "symbol": "target", "unit": "unit",
                   "address": 0x80001000, "artifacts": str(draft)}
            seed = root / "old"
            source_item = seed / "80001000"
            source_item.mkdir(parents=True)
            winner = source_item / "winner.c"
            winner.write_text("void target(void) {}\n")
            old = {"id": "one", "symbol": "target", "unit": "unit", "status": "non_exact",
                   "draft_sha256": hashlib.sha256((draft / "draft.c").read_bytes()).hexdigest(),
                   "input_sha256": "old", "winner_path": str(winner)}
            (source_item / "result.json").write_text(json.dumps(old))
            item = root / "new" / "80001000"
            result = validator.cached_result(item, row, "new-environment", False, seed)
            self.assertEqual(result["reused_from_validation"], str(seed.resolve()))
            self.assertEqual(Path(result["winner_path"]).read_text(), "void target(void) {}\n")
            self.assertTrue(Path(result["winner_path"]).is_relative_to(item))
            self.assertEqual(json.loads((item / "result.json").read_text()), result)

    def test_linked_source_skips_stale_historical_draft(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "build/TEST01/report.json"
            report.parent.mkdir(parents=True)
            report.write_text(json.dumps({"units": [{
                "metadata": {"source_path": "src/exact.c"},
                "functions": [{"name": "fn_linked", "fuzzy_match_percent": 100.0}],
            }]}))
            self.assertEqual(validator.linked_symbols(root, "TEST01"), {"fn_linked"})
            row = {"id": "one", "symbol": "fn_linked", "unit": "old", "status": "ok"}
            result = validator.validate_one(root, "TEST01", row, root / "scratch", "env",
                                            {"fn_linked"})
            self.assertEqual(result["status"], "linked_elsewhere")
            self.assertTrue(result["linked"])
            self.assertFalse(result["promoted"])

    def test_nonmatching_source_function_is_still_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = root / "build/TEST01/report.json"
            report.parent.mkdir(parents=True)
            report.write_text(json.dumps({"units": [{
                "metadata": {"source_path": "src/in_progress.c"},
                "functions": [{"name": "fn_in_progress", "fuzzy_match_percent": 89.0}],
            }]}))
            self.assertEqual(validator.linked_symbols(root, "TEST01"), set())

    def test_cross_project_exact_source_bypasses_m2c_without_claiming_target_linkage(self):
        row = {"id": "one", "symbol": "KnownSDK", "unit": "auto", "status": "exact_reuse",
               "exact_reuse": {"project": "ref", "source": "src/sdk.c"}}
        result = validator.validate_one(Path("/unused"), "TEST01", row, Path("/unused"), "env", set())
        self.assertEqual(result["status"], "exact_reuse_available")
        self.assertEqual(result["scope"], "retail_byte_identity")
        self.assertFalse(result["compile_ok"])
        self.assertFalse(result["linked"])
        self.assertIsNone(result["pct"])

    def test_complete_batch_scores_and_reuses_unchanged_drafts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            batch = root / "build/decomp-support/TEST01/batches/batch-one"
            batch.mkdir(parents=True)
            draft = root / "draft-one"
            draft.mkdir()
            (draft / "draft.c").write_text("void fn_one(void) {}\n")
            rows = [
                {"id": "one", "symbol": "fn_one", "unit": "unit", "address": 0x80001000,
                 "status": "ok", "artifacts": str(draft)},
                {"id": "two", "symbol": "fn_two", "unit": "unit", "address": 0x80001020,
                 "status": "unsupported"},
            ]
            (batch / "results.json").write_text(json.dumps({"summary": {"finished": True}, "results": rows}))
            outcome = {"compiled": True, "profile": "1.2.5n", "pct": 100.0, "error": "", "diff": "",
                       "feedback": {"exact": True, "target_count": 1, "compiled_count": 1}}
            with patch.object(validator, "build_check", return_value={"status": "exact"}), \
                 patch.object(validator, "fingerprint", return_value="f" * 64), \
                 patch.object(validator.attempts, "score", return_value=outcome) as score, \
                 patch.object(validator.m2c_report, "refresh_report"):
                summary = validator.run(root, "TEST01", batch)
                again = validator.run(root, "TEST01", batch)
            self.assertEqual(score.call_count, 1)
            self.assertEqual(summary["counts"], {"scorer_exact": 1, "missing_draft": 1})
            self.assertEqual(summary["compiler_succeeded"], 1)
            self.assertEqual(summary["instruction_exact"], 1)
            self.assertTrue(again["environment_unchanged"])
            result = batch.parent.parent / "validation" / ("batch-one-" + "f" * 12) / "results.json"
            saved = json.loads(result.read_text())
            self.assertFalse(saved["results"][0]["promoted"])
            self.assertFalse(saved["results"][0]["review_eligible"])
            self.assertEqual(saved["results"][0]["draft_sha256"],
                             hashlib.sha256((draft / "draft.c").read_bytes()).hexdigest())
            self.assertTrue(saved["results"][0]["instruction_exact"])

    def test_fuzzy_100_with_instruction_diff_is_not_instruction_exact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            batch = root / "build/decomp-support/TEST01/batches/batch-one"
            batch.mkdir(parents=True)
            draft = root / "draft-one"
            draft.mkdir()
            (draft / "draft.c").write_text("void fn_one(void) {}\n")
            (batch / "results.json").write_text(json.dumps({"summary": {"finished": True}, "results": [
                {"id": "one", "symbol": "fn_one", "unit": "unit", "address": 0x80001000,
                 "status": "ok", "artifacts": str(draft)},
            ]}))
            outcome = {"compiled": True, "profile": "1.3.2", "pct": 100.0,
                       "error": "", "diff": "-lfd target\\n+lfd local"}
            with patch.object(validator, "build_check", return_value={"status": "exact"}), \
                 patch.object(validator, "fingerprint", return_value="f" * 64), \
                 patch.object(validator.attempts, "score", return_value=outcome), \
                 patch.object(validator.m2c_report, "refresh_report"):
                summary = validator.run(root, "TEST01", batch)
            self.assertEqual(summary["counts"], {"scorer_exact": 1})
            self.assertEqual(summary["instruction_exact"], 0)

    def test_exact_bootstrap_source_is_scored_as_an_independent_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "draft"
            artifact.mkdir()
            (artifact / "draft.c").write_text("void target(void) {}\n")
            row = {"id": "one", "symbol": "target", "unit": "unit", "status": "ok",
                   "artifacts": str(artifact), "references": [{
                       "symbol": "Known", "source_snippet": "void Known(void) {}",
                       "required_macros": ["#define REQUIRED(x) (x)"],
                       "required_declarations": ["volatile u16 HW[] : (0xCC000000);"],
                       "compiler": {"compiler": "mwcc", "flags": ["-O4,p"]},
                       "code_comparison": {"status": "exact_bytes"},
                   }]}
            fuzzy = {"compiled": True, "profile": "p", "pct": 80.0, "error": "", "diff": "diff",
                     "feedback": {"exact": False, "target_count": 1, "compiled_count": 1}}
            exact = {"compiled": True, "profile": "p", "pct": 100.0, "error": "", "diff": "",
                     "feedback": {"exact": True, "target_count": 1, "compiled_count": 1}}
            with patch.object(validator.attempts, "score", side_effect=[fuzzy, exact]) as score:
                result = validator.validate_one(root, "TEST01", row, root / "validation", "env", set())
            self.assertEqual(result["status"], "scorer_exact")
            self.assertEqual(result["candidate_origin"], "code_verified_reference_0")
            self.assertIn("volatile u16 HW[] : (0xCC000000);", score.call_args_list[1].args[3])
            self.assertIn("#define REQUIRED(x) (x)", score.call_args_list[1].args[3])
            self.assertIn("void target(void)", score.call_args_list[1].args[3])
            self.assertFalse(score.call_args_list[1].kwargs["apply_repair"])

    def test_exact_bootstrap_source_survives_m2c_generation_failure(self):
        row = {"id": "one", "symbol": "target", "unit": "unit", "status": "decompile_error",
               "error": "m2c failed", "artifacts": "/missing", "references": [{
                   "symbol": "Known", "source_snippet": "int Known(void) { return 1; }",
                   "compiler": {"compiler": "mwcc", "flags": ["-O4,p"]},
                   "code_comparison": {"status": "exact_bytes"},
               }]}
        exact = {"compiled": True, "profile": "p", "pct": 100.0, "error": "", "diff": "",
                 "feedback": {"exact": True, "target_count": 1, "compiled_count": 1}}
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(validator.attempts, "score", return_value=exact) as score:
            result = validator.validate_one(Path(temporary), "TEST01", row,
                                            Path(temporary) / "validation", "env", set())
        self.assertEqual(result["status"], "scorer_exact")
        self.assertEqual(result["candidate_origin"], "code_verified_reference_0")
        self.assertEqual(score.call_count, 1)

    def test_current_project_source_can_beat_fresh_m2c(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "draft"
            artifact.mkdir()
            (artifact / "draft.c").write_text("void target(void) {}\n")
            row = {"id": "one", "symbol": "target", "unit": "unit", "status": "ok",
                   "artifacts": str(artifact)}
            m2c = {"compiled": True, "profile": "p", "pct": 58.4, "error": "", "diff": "x",
                   "feedback": {"exact": False}}
            source = {"compiled": True, "profile": "p", "pct": 98.5, "error": "", "diff": "",
                      "feedback": {"exact": False}, "scoring_scope": "project_owner_compiled_object",
                      "object_audit": {"scratch_object_equal": False},
                      "candidate": "void target(void) {}\n"}
            with patch.object(validator.m2c_current_source, "measure", return_value=source), \
                 patch.object(validator.attempts, "score", return_value=m2c):
                result = validator.validate_one(root, "TEST01", row, root / "validation", "env", set())
            self.assertEqual(result["candidate_origin"], "current_source")
            self.assertEqual(result["pct"], 98.5)
            self.assertEqual(Path(result["winner_path"]).read_text(), "void target(void) {}\n")
            self.assertEqual(result["winner_sha256"],
                             hashlib.sha256(Path(result["winner_path"]).read_bytes()).hexdigest())
            variants = {item["origin"]: item for item in result["candidate_variants"]}
            self.assertEqual(variants["m2c"]["pct"], 58.4)
            self.assertEqual(variants["current_source"]["scoring_scope"], "project_owner_compiled_object")

    def test_missing_or_unfinished_batch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            batch = root / "batch"
            batch.mkdir()
            (batch / "results.json").write_text(json.dumps({"summary": {"finished": False}}))
            with patch.object(validator, "build_check", return_value={"status": "exact"}):
                with self.assertRaisesRegex(ValueError, "not complete"):
                    validator.run(root, "TEST01", batch)

    def test_missing_or_empty_disassembly_is_not_instruction_exact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "draft.c").write_text("void fn(void) {}\n")
            row = {"symbol": "fn", "status": "ok", "artifacts": str(root)}
            for feedback in ({}, {"exact": True, "target_count": 0, "compiled_count": 0}):
                with self.subTest(feedback=feedback), patch.object(validator.attempts, "score", return_value={
                    "compiled": True, "profile": "1.2.5n", "pct": 100.0,
                    "diff": "", "feedback": feedback,
                }):
                    result = validator.validate_one(root, "TEST01", row, root / "scratch", "env", set())
                self.assertEqual(result["status"], "scorer_exact")
                self.assertFalse(result["instruction_exact"])
