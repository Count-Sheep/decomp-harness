import json
import tempfile
from pathlib import Path
import unittest

import m2c_artifact_retention as retention


class RetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.support = Path(temporary.name)

    def batch(self, name, updated, rows, baseline="live"):
        folder = self.support / "batches" / name
        folder.mkdir(parents=True)
        (folder / "results.json").write_text(json.dumps({"summary": {
            "run_id": name, "finished": True, "updated_at": updated,
            "selection": "unmatched", "baseline": baseline,
            "counts": {"ok": len(rows)}, "total": len(rows),
        }, "results": rows}))
        (folder / "manifest.json").write_text("{}")
        return folder

    def draft(self, name, text):
        folder = self.support / "drafts" / name
        folder.mkdir(parents=True)
        (folder / "draft.c").write_text(text)
        return folder

    def validation(self, batch, updated, rows):
        folder = self.support / "validation" / batch
        folder.mkdir(parents=True)
        (folder / "results.json").write_text(json.dumps({"summary": {
            "batch": str(self.support / "batches" / batch), "finished": True,
            "updated_at": updated, "counts": {}, "total": len(rows),
        }, "results": rows}))

    def interrupted_batch(self, name, updated):
        folder = self.support / "batches" / name
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps({
            "run_id": name, "finished": False, "updated_at": updated,
            "selection": "unmatched", "total": 10, "processed": 2,
        }))
        return folder

    def interrupted_validation(self, batch, suffix, updated):
        folder = self.support / "validation" / f"{batch}-{suffix}"
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text(json.dumps({
            "batch": str(self.support / "batches" / batch), "finished": False,
            "updated_at": updated, "total": 10, "processed": 2,
        }))
        return folder

    def test_keeps_newest_and_best_per_symbol_and_prunes_orphan_drafts(self):
        old_draft = self.draft("old", "old")
        best_draft = self.draft("best", "best")
        new_draft = self.draft("new", "new")
        self.batch("old", "1", [{"symbol": "f", "status": "ok", "artifacts": str(old_draft)}])
        self.batch("best", "2", [{"symbol": "f", "status": "ok", "artifacts": str(best_draft)}])
        self.batch("new", "3", [{"symbol": "f", "status": "ok", "artifacts": str(new_draft)}])
        self.validation("best", "2", [{"symbol": "f", "instruction_exact": True,
                                          "compile_ok": True, "pct": 100.0}])
        result = retention.prune(self.support, keep_recent=1, apply=True)
        self.assertEqual({Path(path).name for path in result["remove_batches"]}, {"old"})
        self.assertFalse(old_draft.exists())
        self.assertTrue(best_draft.exists())
        self.assertTrue(new_draft.exists())
        report = json.loads((self.support / "m2c-report.json").read_text())
        old = next(row for row in report["runs"] if row["run_id"] == "old")
        self.assertFalse(old["artifact_available"])

    def test_kept_replay_preserves_its_baseline(self):
        self.batch("base", "1", [{"symbol": "a", "status": "ok"}])
        self.batch("replay", "2", [{"symbol": "a", "status": "ok"}], baseline="base")
        result = retention.plan(self.support, keep_recent=1)
        self.assertEqual(result["keep_batches"], ["base", "replay"])

    def test_external_unit_record_protects_named_batch(self):
        old = self.batch("old", "1", [{"symbol": "a", "status": "ok"}])
        self.batch("new", "2", [{"symbol": "a", "status": "ok"}])
        record = self.support / "unit-promotions/record.json"
        record.parent.mkdir()
        record.write_text(json.dumps({"batch": str(old / "results.json")}))
        result = retention.plan(self.support, keep_recent=1)
        self.assertIn("old", result["keep_batches"])

    def test_compacts_attempt_state_without_changing_dashboard_fields(self):
        source = self.support / "attempts/f/001/candidate.c"
        source.parent.mkdir(parents=True)
        source.write_text("void f(void) {}\n")
        missing = self.support / "attempts/g/001/candidate.c"
        state = {"version": "TEST", "current": "f", "items": {
            "f": {"symbol": "f", "best_pct": 90.0, "best_source": str(source),
                  "attempts": [{"index": 1, "at": "2", "pct": 90.0, "delta": 5.0,
                                "compiled": True, "profile": "p", "source": str(source),
                                "diff": "large", "feedback": {"large": "payload"}}]},
            "g": {"symbol": "g", "best_pct": 20.0, "best_source": str(missing),
                  "attempts": [{"index": 0, "at": "1", "pct": 20.0,
                                "compiled": False, "source": str(missing),
                                "profile_attempts": [{"flags": ["many"]}]}]},
        }}
        path = self.support / "attempts/state.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(state, indent=2))
        result = retention.compact_attempt_state(self.support, apply=True)
        compact = json.loads(path.read_text())
        self.assertEqual(result["items"], 2)
        self.assertEqual(result["attempts"], 2)
        self.assertGreater(result["reclaimed_bytes"], 0)
        self.assertEqual(compact["items"]["f"]["best_source"], str(source))
        self.assertIsNone(compact["items"]["g"]["best_source"])
        self.assertEqual(compact["items"]["f"]["attempts"][0]["source"], str(source))
        self.assertIsNone(compact["items"]["g"]["attempts"][0]["source"])
        self.assertNotIn("feedback", compact["items"]["f"]["attempts"][0])
        self.assertNotIn("profile_attempts", compact["items"]["g"]["attempts"][0])

    def test_keeps_only_newest_interrupted_generation_and_validation(self):
        self.batch("complete", "1", [{"symbol": "f", "status": "ok"}])
        old_batch = self.interrupted_batch("interrupted-old", "2")
        new_batch = self.interrupted_batch("interrupted-new", "3")
        old_validation = self.interrupted_validation("complete", "old", "2")
        new_validation = self.interrupted_validation("complete", "new", "3")
        result = retention.prune(self.support, keep_recent=1, apply=True)
        self.assertFalse(old_batch.exists())
        self.assertTrue(new_batch.exists())
        self.assertFalse(old_validation.exists())
        self.assertTrue(new_validation.exists())


if __name__ == "__main__":
    unittest.main()
