import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_campaign_m2c_report as report


class M2cReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.support = self.root / "support"
        self.project_report = self.root / "report.json"
        self.project_report.write_text(json.dumps({"units": [{"metadata": {"source_path": "src/a.c"}, "functions": [
            {"name": "exact", "fuzzy_match_percent": 100}, {"name": "open", "fuzzy_match_percent": 80}
        ]}]}), encoding="utf-8", newline="\n")

    def write_batch(self, run_id, ok, baseline="live", at="2026-10-02T10:00:00+00:00"):
        folder = self.support / "batches" / run_id
        folder.mkdir(parents=True)
        rows = [{"id": f"f{i}", "symbol": f"fn{i}", "unit": "u", "pct": 50.0,
                 "status": "ok" if i < ok else "unsupported", "diagnostics": {"unknown_types": i % 2}}
                for i in range(4)]
        value = {"summary": {"run_id": run_id, "finished": True, "updated_at": at, "total": 4,
                              "processed": 4, "baseline": baseline, "tool": {"revision": "rev", "fingerprint": run_id}},
                 "results": rows}
        (folder / "results.json").write_text(json.dumps(value), encoding="utf-8", newline="\n")

    def write_validation(self, run_id, version=2, suffix="", stable=True):
        folder = self.support / "validation" / f"{run_id}{suffix}"
        folder.mkdir(parents=True)
        value = {"summary": {"batch": str(self.support / "batches" / run_id), "finished": True,
                              "updated_at": "2026-10-02T11:00:00+00:00", "total": 4, "processed": 4,
                              "environment_unchanged": stable, "validator_version": version,
                              "compiler_succeeded": 2, "compiled_functions": 2},
                 "results": [{"id": "f0", "symbol": "fn0", "unit": "u", "status": "non_exact",
                              "pct": 91.25, "scope": "full_owner_scoring_unit"},
                             {"id": "f1", "symbol": "fn1", "unit": "u", "status": "compile_error"}]}
        (folder / "results.json").write_text(json.dumps(value), encoding="utf-8", newline="\n")

    def test_builds_history_and_baseline_delta(self):
        self.write_batch("old", 1, at="2026-10-02T09:00:00+00:00")
        self.write_batch("new", 3, baseline=str(self.support / "batches/old"))
        self.write_validation("new")
        value = report.build_report(self.support, self.project_report)
        self.assertEqual(value["history_count"], 2)
        self.assertEqual(value["latest"]["run_id"], "new")
        self.assertEqual(value["latest"]["clean_delta"], 2)
        self.assertTrue(value["latest"]["validation"]["comparable"])
        self.assertEqual(value["latest"]["validation"]["top_candidates"][0]["original_pct"], 50.0)
        self.assertIsNone(value["latest"]["validation"]["instruction_exact"])
        self.assertEqual(value["matched_reference_pool"]["available"], 1)

    def test_completed_refinement_is_attached_to_its_validation(self):
        self.write_batch("run", 2)
        self.write_validation("run", suffix="-environment")
        folder = self.support / "refinement-runs/run-environment"
        folder.mkdir(parents=True)
        (folder / "results.json").write_text(json.dumps({
            "finished": True, "selection": [{"symbol": "fn0", "pct": 91.25},
                                                {"symbol": "fn1", "pct": 80.0}],
            "attempts": [{"symbol": "fn0", "best_pct": 92.0, "object_exact": False,
                           "origin": "current_source"},
                          {"symbol": "fn1", "best_pct": 100.0, "object_exact": True,
                           "origin": "m2c"}],
            "unit_gate": {"ready_units": 3, "links_verified": 1}}), encoding="utf-8", newline="\n")
        refinement = report.build_report(self.support, self.project_report)["latest"]["validation"]["refinement"]
        self.assertTrue(refinement["finished"])
        self.assertEqual(refinement["processed"], 2)
        self.assertEqual(refinement["improved"], 2)
        self.assertEqual(refinement["exact"], 1)
        self.assertEqual(refinement["rows"][0]["delta_pp"], 0.75)
        self.assertEqual(refinement["unit_gate"]["links_verified"], 1)

    def test_live_validation_hands_tracker_to_unit_promotion(self):
        self.write_batch("run", 2)
        self.write_validation("run", suffix="-environment")
        batch_summary = json.loads((self.support / "batches/run/results.json").read_text(encoding="utf-8"))["summary"]
        validation_summary = json.loads(
            (self.support / "validation/run-environment/results.json").read_text(encoding="utf-8"))["summary"]
        (self.support / "batches/run/manifest.json").write_text(json.dumps(batch_summary), encoding="utf-8", newline="\n")
        (self.support / "validation/run-environment/manifest.json").write_text(
            json.dumps(validation_summary), encoding="utf-8", newline="\n")
        gate_output = self.support / "unit-gates/gate"
        gate_output.mkdir(parents=True)
        promotion = gate_output / "promotions/unit"
        promotion.mkdir(parents=True)
        (promotion / "result.json").write_text(json.dumps({"phase": "retail_sha"}), encoding="utf-8", newline="\n")
        (self.support / "unit-gates/active.json").write_text(json.dumps({
            "pid": 123, "output": str(gate_output), "phase": "promotion",
            "processed": 2, "total": 3, "current_unit": "main/unit",
            "promotion": str(promotion), "updated_at": "2026-10-09T10:00:00+00:00"}), encoding="utf-8", newline="\n")
        processes = [{"pid": 123, "parent": 1, "elapsed_seconds": 10, "state": "S",
                      "argv": ["python", "m2c_validate_drafts.py", "--batch",
                               str(self.support / "batches/run")]}]
        active = report.live_runs(self.support, processes, report.datetime.now(report.UTC))
        self.assertEqual(active[0]["stage"], "promotion")
        self.assertEqual(active[0]["current_function"], "main/unit")
        self.assertEqual(active[0]["current_work"], "Promotion: retail sha")
        self.assertEqual((active[0]["processed"], active[0]["total"]), (2, 3))

    def test_unit_gate_preparation_is_not_reported_as_finished_refinement(self):
        self.write_batch("run", 2)
        self.write_validation("run", suffix="-environment")
        batch_summary = json.loads((self.support / "batches/run/results.json").read_text(encoding="utf-8"))["summary"]
        validation_summary = json.loads(
            (self.support / "validation/run-environment/results.json").read_text(encoding="utf-8"))["summary"]
        (self.support / "batches/run/manifest.json").write_text(json.dumps(batch_summary), encoding="utf-8", newline="\n")
        (self.support / "validation/run-environment/manifest.json").write_text(
            json.dumps(validation_summary), encoding="utf-8", newline="\n")
        refinement = self.support / "refinement-runs/run-environment"
        refinement.mkdir(parents=True)
        (refinement / "results.json").write_text(json.dumps({
            "finished": False, "phase": "unit_gate", "updated_at": "2026-10-09T10:00:00+00:00",
            "selection": [{"symbol": "fn0"}], "attempts": [{"symbol": "fn0"}]}), encoding="utf-8", newline="\n")
        processes = [{"pid": 123, "parent": 1, "elapsed_seconds": 10, "state": "S",
                      "argv": ["python", "m2c_validate_drafts.py", "--batch",
                               str(self.support / "batches/run")]}]
        active = report.live_runs(self.support, processes, report.datetime.now(report.UTC))
        self.assertEqual(active[0]["stage"], "unit_gate")
        self.assertEqual(active[0]["current_work"], "Preparing exact candidates and unit ownership")
        self.assertIsNone(active[0]["refinement_progress"])
        value = json.loads((refinement / "results.json").read_text(encoding="utf-8"))
        value.update(finished=True, phase="complete", unit_gate={"promoted": 0})
        (refinement / "results.json").write_text(json.dumps(value), encoding="utf-8", newline="\n")
        active = report.live_runs(self.support, processes, report.datetime.now(report.UTC))
        self.assertEqual(active[0]["stage"], "finalizing")
        self.assertEqual(active[0]["current_work"], "Refreshing retained sweep and promotion reports")

    def test_instruction_exact_is_distinct_from_fuzzy_100(self):
        self.write_batch("run", 2)
        self.write_validation("run")
        path = self.support / "validation/run/results.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["results"] = [
            {"id": "f0", "symbol": "fn0", "unit": "u", "status": "scorer_exact",
             "pct": 100.0, "instruction_exact": False},
            {"id": "f1", "symbol": "fn1", "unit": "u", "status": "scorer_exact",
             "pct": 100.0, "instruction_exact": True},
        ]
        path.write_text(json.dumps(data), encoding="utf-8", newline="\n")
        validation = report.build_report(self.support, self.project_report)["latest"]["validation"]
        self.assertEqual(validation["instruction_exact"], 1)
        self.assertEqual([row["instruction_exact"] for row in validation["top_candidates"]],
                         [False, True])

    def test_legacy_validation_is_visible_but_not_comparable(self):
        self.write_batch("run", 2)
        self.write_validation("run", version=0)
        value = report.build_report(self.support, self.project_report)
        self.assertFalse(value["latest"]["validation"]["comparable"])

    def test_dossier_coverage_is_observed_not_backfilled_into_legacy_runs(self):
        self.write_batch('legacy', 2, at='2026-10-02T09:00:00+00:00')
        self.write_batch('annotated', 3)
        path = self.support / 'batches/annotated/results.json'
        data = json.loads(path.read_text(encoding="utf-8"))
        data['summary']['pipeline'] = {'version': 'm2c-dossiers-v1', 'model_calls': 0}
        data['results'][0]['investigation'] = {'available': True, 'dossier': {'matched_reference_candidates': ['known']}}
        data['results'][1]['investigation'] = {'available': True, 'dossier': {}}
        data['results'][2]['investigation'] = {'available': False}
        path.write_text(json.dumps(data), encoding="utf-8", newline="\n")
        value = report.build_report(self.support, self.project_report)
        self.assertEqual(value['latest']['dossier_functions'], 2)
        self.assertEqual(value['latest']['reference_candidate_functions'], 1)
        self.assertEqual(value['latest']['pipeline']['model_calls'], 0)
        self.assertIsNone(value['runs'][0]['dossier_functions'])

    def test_evidence_and_repair_provenance_are_indexed(self):
        self.write_batch("evidence", 2)
        batch_path = self.support / "batches/evidence/results.json"
        batch = json.loads(batch_path.read_text(encoding="utf-8"))
        batch["results"][0].update(context_mode="typed", references=[{"symbol": "a"}, {"symbol": "b"}],
                                   compiler_profile={"confidence": "high"})
        batch["results"][1].update(status="exact_reuse", exact_reuse={"project": "other"})
        batch_path.write_text(json.dumps(batch), encoding="utf-8", newline="\n")
        self.write_validation("evidence")
        validation_path = self.support / "validation/evidence/results.json"
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
        validation["results"][0]["repair"] = {"changed": True, "transformations": [{"kind": "extern"}]}
        validation_path.write_text(json.dumps(validation), encoding="utf-8", newline="\n")
        latest = report.build_report(self.support, self.project_report)["latest"]
        self.assertEqual(latest["typed_context_functions"], 1)
        self.assertEqual(latest["reference_functions"], 1)
        self.assertEqual(latest["reference_examples"], 2)
        self.assertEqual(latest["inferred_profile_functions"], 1)
        self.assertEqual(latest["high_confidence_profile_functions"], 1)
        self.assertEqual(latest["exact_reuse_functions"], 1)
        self.assertEqual(latest["validation"]["repaired_functions"], 1)
        self.assertEqual(latest["validation"]["repair_transformations"], 1)

    def test_paired_measurement_requires_identical_environment_and_target_ids(self):
        prior = {'comparable': True, 'total': 2, 'environment_sha256': 'same', '_measurements': {
            'f': {'symbol': 'f', 'pct': 80.0, 'scope': 'owner', 'compile_ok': True},
            'g': {'symbol': 'g', 'pct': None, 'scope': 'owner', 'compile_ok': False}}}
        current = {'comparable': True, 'total': 2, 'environment_sha256': 'same', '_measurements': {
            'f': {'symbol': 'f', 'pct': 85.0, 'scope': 'owner', 'compile_ok': True},
            'g': {'symbol': 'g', 'pct': 70.0, 'scope': 'owner', 'compile_ok': True}}}
        comparison = report.compare_validation(current, prior)
        self.assertEqual(comparison['scores_improved'], 1)
        self.assertEqual(comparison['new_compiles'], 1)
        self.assertEqual(comparison['new_scores'], 1)
        self.assertEqual(comparison['newly_scored'][0]['pct'], 70.0)
        current['total'] = 3
        self.assertFalse(report.compare_validation(current, prior)['comparable'])
        current['total'] = 2
        current['_measurements']['other'] = current['_measurements'].pop('g')
        self.assertFalse(report.compare_validation(current, prior)['comparable'])
        current['_measurements']['g'] = current['_measurements'].pop('other')
        current['environment_sha256'] = 'changed'
        self.assertFalse(report.compare_validation(current, prior)['comparable'])
        replay = report.compare_validation(current, prior, allow_environment_change=True)
        self.assertTrue(replay['comparable'])
        self.assertTrue(replay['environment_changed'])
        self.assertEqual(replay['comparison_basis'], 'frozen_target_replay')
        current['environment_sha256'] = prior['environment_sha256'] = None
        self.assertFalse(report.compare_validation(current, prior)['comparable'])

    def test_exact_churn_is_visible_even_when_total_exact_count_is_unchanged(self):
        prior = {'comparable': True, 'total': 2, 'environment_sha256': 'same', '_measurements': {
            'f': {'symbol': 'f', 'pct': 100.0, 'scope': 'owner', 'compile_ok': True},
            'g': {'symbol': 'g', 'pct': 90.0, 'scope': 'owner', 'compile_ok': True}}}
        current = {'comparable': True, 'total': 2, 'environment_sha256': 'same', '_measurements': {
            'f': {'symbol': 'f', 'pct': None, 'scope': 'owner', 'compile_ok': False,
                  'draft_status': 'unsupported', 'status': 'compile_error'},
            'g': {'symbol': 'g', 'pct': 100.0, 'scope': 'owner', 'compile_ok': True}}}
        comparison = report.compare_validation(current, prior)
        self.assertEqual(comparison['new_exact_scores'], 1)
        self.assertEqual(comparison['lost_exact_scores'], 1)
        self.assertEqual(comparison['new_exact_functions'], ['g'])
        self.assertEqual(comparison['lost_exact_functions'], ['f'])
        self.assertEqual(comparison['compile_losses'][0]['draft_status'], 'unsupported')
        current['_measurements']['g']['scope'] = 'standalone'
        self.assertEqual(report.compare_validation(current, prior)['new_exact_scores'], 0)

    def test_matched_regression_run_does_not_replace_unmatched_headline(self):
        self.write_batch("open", 2, at="2026-10-02T09:00:00+00:00")
        self.write_batch("matched", 3)
        path = self.support / "batches/matched/results.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value['summary']['selection'] = 'matched'
        path.write_text(json.dumps(value), encoding="utf-8", newline="\n")
        value = report.refresh_report(self.support, self.project_report)
        self.assertEqual(value['latest']['run_id'], 'open')
        self.assertEqual(value['matched_latest']['run_id'], 'matched')
        self.assertEqual(value['history_count'], 2)

    def test_refresh_writes_compact_report(self):
        self.write_batch("run", 2)
        value = report.refresh_report(self.support, self.project_report)
        self.assertEqual(report.load_report(self.support)["latest"]["run_id"], "run")
        self.assertEqual(json.loads((self.support / report.REPORT_NAME).read_text(encoding="utf-8"))["generated_at"], value["generated_at"])

    def test_live_validation_checkpoint_updates_history_and_headline(self):
        self.write_batch("run", 2)
        self.write_validation("run")
        report.refresh_report(self.support, self.project_report)
        folder = self.support / "validation/run-new"
        folder.mkdir()
        checkpoint = {"batch": str(self.support / "batches/run"), "finished": False,
                      "updated_at": "2026-10-02T12:00:00+00:00", "total": 4, "processed": 2,
                      "compiler_succeeded": 2, "counts": {"scorer_exact": 1, "non_exact": 1},
                      "instruction_exact": 1}
        (folder / "manifest.json").write_text(json.dumps(checkpoint), encoding="utf-8", newline="\n")
        active = [{"run_id": "run-new", "stage": "validation", "state": "running"}]
        with patch.object(report, "process_snapshot", return_value=[]), \
             patch.object(report, "live_runs", return_value=active):
            value = report.load_report(self.support)
        for row in (value["latest"], value["runs"][0]):
            live = row["validation_progress"]
            self.assertEqual(live["processed"], 2)
            self.assertEqual(live["compiler_succeeded"], 2)
            self.assertEqual(live["instruction_exact"], 1)
            self.assertEqual(live["state"], "running")
            self.assertFalse(live["comparable"])
            self.assertTrue(row["validation"]["comparable"])
        with patch.object(report, "process_snapshot", return_value=[]):
            self.assertEqual(report.load_report(self.support)["latest"]["validation_progress"]["state"],
                             "interrupted")
        checkpoint["updated_at"] = "2026-10-02T10:00:00+00:00"
        (folder / "manifest.json").write_text(json.dumps(checkpoint), encoding="utf-8", newline="\n")
        with patch.object(report, "process_snapshot", return_value=[]):
            self.assertNotIn("validation_progress", report.load_report(self.support)["latest"])

    def test_checkpoint_from_other_project_does_not_supply_counts(self):
        self.write_batch("run", 2)
        report.refresh_report(self.support, self.project_report)
        folder = self.support / "validation/run-new"
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps({
            "batch": str(self.root / "other/batches/run"), "finished": False,
            "updated_at": "2026-10-02T12:00:00+00:00", "compiler_succeeded": 99}), encoding="utf-8", newline="\n")
        with patch.object(report, "process_snapshot", return_value=[]):
            self.assertNotIn("validation_progress", report.load_report(self.support)["latest"])

    def test_refresh_retains_history_after_artifact_cleanup(self):
        self.write_batch("run", 2)
        report.refresh_report(self.support, self.project_report)
        (self.support / "batches/run/results.json").unlink()
        value = report.refresh_report(self.support, self.project_report)
        self.assertEqual(value["history_count"], 1)
        self.assertFalse(value["latest"]["artifact_available"])

    def test_support_root_is_per_project_version_and_requires_a_version(self):
        with patch.object(report, "project", return_value=None), \
             patch.object(report, "DECOMP_ROOT", self.root), patch.object(report, "VERSION", "TEST01"):
            self.assertEqual(report.support_root(), self.root / "build/decomp-support/TEST01")
            self.assertEqual(report.support_root("TEST02"), self.root / "build/decomp-support/TEST02")
        with patch.object(report, "VERSION", ""), self.assertRaisesRegex(SystemExit, "No project version"):
            report.support_root()

    @unittest.skipUnless(os.name == "nt", "Windows command-line parsing")
    def test_windows_command_lines_split_like_the_c_runtime(self):
        argv = report._windows_argv('"C:\\Program Files\\Python\\python.exe" harness\\local_campaign_decompile.py --all')
        self.assertEqual(argv, ["C:\\Program Files\\Python\\python.exe", "harness\\local_campaign_decompile.py", "--all"])

    def test_reference_pool_keeps_index_available_after_report_updates(self):
        project_report = self.root / "build/TEST01/report.json"
        project_report.parent.mkdir(parents=True)
        project_report.write_text(json.dumps({"units": [
            {"metadata": {"source_path": "real.c"}, "sections": [{"functions": [
                {"name": "exact", "fuzzy_match_percent": 100}]}]},
            {"metadata": {"source_path": "stub.c", "auto_generated": True}, "functions": [
                {"name": "stub", "fuzzy_match_percent": 100}]}
        ]}), encoding="utf-8", newline="\n")
        index = self.root / "build/simindex/index.pkl"
        index.parent.mkdir(parents=True)
        index.touch()
        os.utime(index, (10, 10))
        os.utime(project_report, (20, 20))
        pool = report._matched_reference_pool(project_report)
        self.assertEqual(pool["available"], 1)
        self.assertTrue(pool["similarity_index_ready"])


if __name__ == "__main__":
    unittest.main()
