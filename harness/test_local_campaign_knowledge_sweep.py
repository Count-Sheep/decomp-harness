import unittest
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import local_campaign_knowledge as knowledge
from local_campaign_knowledge_sweep import summarize, untested_targets, unmatched_selection, validate_selection
from test_local_campaign_knowledge import function


class SweepTests(unittest.TestCase):
    def unmatched_fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        report = {"units": [{"name": "main/f", "metadata": {"source_path": "src/f.c"},
                             "functions": [{"name": "f", "fuzzy_match_percent": 42,
                                            "metadata": {"virtual_address": "1"}}]},
                            {"name": "main/missing", "metadata": {"auto_generated": True},
                             "functions": [{"name": "missing", "fuzzy_match_percent": 0,
                                            "metadata": {"virtual_address": "2"}}]}]}
        file = root / "build/TEST01/report.json"
        file.parent.mkdir(parents=True)
        file.write_text(json.dumps(report), encoding="utf-8", newline="\n")
        store = knowledge.KnowledgeStore(root / "knowledge.sqlite3")
        self.addCleanup(store.close)
        snapshot = store.add_snapshot({"workspace": str(root), "version": "TEST01", "report_sha256": knowledge.digest(file.read_bytes())})
        target = store.add_function(snapshot, {**function("f", 1, "src/f.c", eligible=False), "pct": 42})
        return store, snapshot, target, file

    def test_unmatched_cohort_retains_missing_warehouse_targets_instead_of_filtering(self):
        store, snapshot, target, _ = self.unmatched_fixture()
        selection = unmatched_selection(store, snapshot)
        self.assertEqual(selection["targets"], [target])
        self.assertEqual(selection["excluded"], [target])
        self.assertEqual(selection["reported_scores"], {target: 42})
        self.assertEqual(selection["missing_warehouse_targets"][0]["symbol"], "missing")
        self.assertIn("not a blind holdout", selection["notice"])
        validate_selection(store, selection)

    def test_unmatched_cohort_rejects_report_identity_and_selection_drift(self):
        store, snapshot, target, file = self.unmatched_fixture()
        selection = unmatched_selection(store, snapshot)
        changed = {**selection, "excluded": []}
        with self.assertRaisesRegex(ValueError, "selection or corpus changed"):
            validate_selection(store, changed)
        original = store.function(target)
        with patch.object(store, "function", return_value={**original, "unit": "main/other"}):
            with self.assertRaisesRegex(ValueError, "immutable warehouse identity"):
                unmatched_selection(store, snapshot)
        file.write_text('{}', encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(ValueError, "report changed"):
            unmatched_selection(store, snapshot)

    def test_actual_gains_require_a_fresh_successful_nonexact_baseline(self):
        rows = [{"baseline_measurement": {"compile_ok": True, "pct": 80}, "measurement": {"pct": 100}},
                {"baseline_measurement": {"compile_ok": True, "pct": 100}, "measurement": {"pct": 100}},
                {"baseline_measurement": {"compile_ok": False, "pct": 80}, "measurement": {"pct": 100}},
                {"measurement": {"pct": 100}}]
        summary = summarize(rows)
        self.assertEqual(summary["scorer_exact"], 4)
        self.assertEqual(summary["baseline_already_exact"], 1)
        self.assertEqual(summary["actual_unmatched_scorer_gains"], 1)
        self.assertEqual(summary["ready"], 0)

    def test_selection_keeps_frozen_order_and_does_not_filter_failures(self):
        split = {"split_sha256": "hash", "targets": ["large", "a", "cpp", "small", "b"]}
        result = untested_targets(split, [{"split_sha256": "hash", "targets": ["a", "b"]}])
        self.assertEqual(result, ["large", "cpp", "small"])

    def test_selection_rejects_changed_split_unknown_duplicates_and_empty_remainder(self):
        split = {"split_sha256": "hash", "targets": ["a", "b"]}
        for selection in ({"split_sha256": "other", "targets": ["a"]},
                          {"split_sha256": "hash", "targets": ["other"]},
                          {"split_sha256": "hash", "targets": ["a", "a"]},
                          {"split_sha256": "hash", "targets": []},
                          {"split_sha256": "hash", "targets": ["a", "b"]}):
            with self.assertRaises(ValueError):
                untested_targets(split, [selection])

    def test_summary_does_not_call_scorer_exact_verified_or_ready(self):
        rows = [{"reference_ids": ["ref"], "measurement": {"status": "scorer_exact", "pct": 100, "compile_ok": True}},
                {"measurement": {"status": "preparation_error"}},
                {"measurement": {"status": "non_exact", "pct": 99, "compile_ok": True,
                                 "verification": {"raw_candidate_bytes": True, "relocations": False, "owner_object": True}}}]
        result = summarize(rows)
        self.assertEqual(result["processed"], 3)
        self.assertEqual(result["compiled"], 2)
        self.assertEqual(result["scorer_exact"], 1)
        self.assertEqual(result["object_verified"], 0)
        self.assertEqual(result["ready"], 0)
        self.assertIsNone(result["retrieval_effect"])


if __name__ == "__main__":
    unittest.main()
