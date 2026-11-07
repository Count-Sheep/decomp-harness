import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import m2c_unit_queue as queue
import m2c_unit_review as review


class UnitReviewTests(unittest.TestCase):
    def fixture(self, root):
        trial = root / "trial.json"
        proposal = root / "proposal.json"
        trial.write_text(json.dumps({"kind": "unit_project_trial", "unit": "main/unit", "proposal": str(proposal)}))
        proposal.write_text(json.dumps({"link_record": str(root / "link.json")}))
        (root / "plan.json").write_text(json.dumps({"elf": "build/TEST/main.elf"}))
        original = queue.inspect_record

        def inspect(path, project):
            if Path(path) == trial:
                return {"proposal_build_verified": True, "link_verified": True}
            return original(path, project)

        return trial, inspect

    def test_explicit_approval_and_superseding_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            trial, inspect = self.fixture(root)
            with patch.object(queue, "inspect_record", side_effect=inspect):
                approved = review.run(root, trial, root / "approval", "test-reviewer", "approve", "Fixture review")
                self.assertTrue(approved["ready_to_merge"])
                self.assertEqual(queue.snapshot(root, "TEST")["ready_to_merge"], 1)
                rejected = review.run(root, trial, root / "rejection", "test-reviewer", "reject", "Fixture rejection")
                self.assertFalse(rejected["ready_to_merge"])
                self.assertEqual(rejected["state"], "review_rejected")
                old = queue.inspect_record(root / "approval/result.json", root)
                self.assertEqual(old["state"], "stale")
                self.assertFalse(old["ready_to_merge"])
                self.assertEqual(queue.snapshot(root, "TEST")["ready_to_merge"], 0)

    def test_changed_trial_revokes_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            trial, inspect = self.fixture(root)
            with patch.object(queue, "inspect_record", side_effect=inspect):
                review.run(root, trial, root / "approval", "test-reviewer", "approve", "Fixture review")
                trial.write_text("changed")
                result = queue.inspect_record(root / "approval/result.json", root)
                self.assertFalse(result["ready_to_merge"])
                self.assertEqual(result["state"], "stale")

    def test_approval_requires_current_native_evidence_and_explicit_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            trial, inspect = self.fixture(root)
            with patch.object(queue, "inspect_record", return_value={"proposal_build_verified": False, "link_verified": True}):
                with self.assertRaisesRegex(ValueError, "current native-build"):
                    review.run(root, trial, root / "approval", "reviewer", "approve", "Reviewed")
            for reviewer, decision, notes in (("", "approve", "notes"), ("user", "auto", "notes"), ("user", "approve", "")):
                with self.subTest(reviewer=reviewer, decision=decision), self.assertRaises(ValueError):
                    review.run(root, trial, root / "approval", reviewer, decision, notes)
            self.assertFalse((root / "approval").exists())

    def test_stale_native_evidence_revokes_existing_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            trial, inspect = self.fixture(root)
            with patch.object(queue, "inspect_record", side_effect=inspect):
                review.run(root, trial, root / "approval", "test-reviewer", "approve", "Fixture review")
            original = queue.inspect_record
            def stale(path, project):
                return {"proposal_build_verified": False, "link_verified": False} if Path(path) == trial else original(path, project)
            with patch.object(queue, "inspect_record", side_effect=stale):
                result = queue.inspect_record(root / "approval/result.json", root)
                self.assertFalse(result["ready_to_merge"])
                self.assertEqual(result["state"], "stale")


if __name__ == "__main__":
    unittest.main()
