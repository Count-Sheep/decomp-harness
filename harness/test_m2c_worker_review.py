"""Review-only export of retained local campaign improvements."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import m2c_worker_review as review


class WorkerReviewTests(unittest.TestCase):
    def test_exports_only_measured_improvements_without_admitting_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src/owner.c"
            candidate = root / "build/local_llm_campaign/candidates/task/attempt-2.c"
            source.parent.mkdir(parents=True)
            candidate.parent.mkdir(parents=True)
            source.write_text("int target(int x) { return x + 1; }\n")
            candidate.write_text("int target(int x) { return 1 + x; }\n")
            source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
            state = {"items": {
                "task": {"symbol": "target", "unit": "main/unit", "owner_source": "src/owner.c",
                         "source_sha256": source_sha, "base_pct": 98.0, "worker": "rpc",
                         "best": {"pct": 99.5, "attempt": 2,
                                  "candidate": "build/local_llm_campaign/candidates/task/attempt-2.c",
                                  "diff_feedback": ["one word differs"], "at": "now"}},
                "no-gain": {"symbol": "other", "source": "src/owner.c", "base_pct": 90.0,
                            "best": {"pct": 90.0, "candidate": "missing.c"}},
            }}
            state_path = root / "build/local_llm_campaign/state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(state))
            result = review.collect(root)
        self.assertEqual(result["summary"], {"retained_improvements": 1,
                                             "instruction_exact_claims": 0,
                                             "current_source_bindings": 1})
        row = result["improvements"][0]
        self.assertEqual(row["delta_pct"], 1.5)
        self.assertTrue(row["source_current"])
        self.assertIn("return 1 + x", row["focused_source_diff"])
        self.assertEqual(row["admission"], "review_only")
        self.assertFalse(row["auto_consumed_by_m2c"])

    def test_rejects_candidate_outside_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state.json"
            state.write_text(json.dumps({"items": {"task": {
                "symbol": "target", "source": "src/owner.c", "base_pct": 1,
                "best": {"pct": 2, "candidate": "../outside.c"},
            }}}))
            with self.assertRaisesRegex(ValueError, "escapes project root"):
                review.collect(root, state)


if __name__ == "__main__":
    unittest.main()
