import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from m2c_capture_inputs import Inputs
import m2c_unit_proposal as proposal
from m2c_unit_queue import inspect_record


class UnitProposalTests(unittest.TestCase):
    def create(self, root, new_file=False, configuration=False):
        compiled = root / "compiled"
        compiled.mkdir()
        (compiled / "unit.c").write_text("int unit(void) { return 2; }")
        if not new_file:
            (root / "unit.c").write_text("int unit(void) { return 1; }")
        linked = root / "link"
        linked.mkdir()
        (linked / "result.json").write_text(json.dumps({"unit": "main/unit", "compiled": str(compiled), "plan_sha256": "bound"}))
        (linked / "plan.json").write_text(json.dumps({"elf": "build/TEST/main.elf"}))
        inputs = Inputs()
        inputs.read(compiled / "unit.c")
        record = {"unit": "main/unit", "inventory": {"configuration": {"metadata": {"source_path": "unit.c"}}}}
        changes = {}
        if configuration:
            (root / "configure.py").write_text("MATCHED = False")
            (root / "candidate-config.py").write_text("MATCHED = True")
            changes["configure.py"] = root / "candidate-config.py"
        with patch.object(proposal, "head", return_value="revision"), \
             patch.object(proposal, "candidate_inputs", return_value=(record, inputs)), \
             patch("m2c_unit_queue.inspect_record", return_value={"link_verified": True}):
            result = proposal.run(root, linked / "result.json", root / "proposal", configuration=changes)
        return result

    def test_bundle_is_applicable_without_applying_or_claiming_merge_readiness(self):
        for new in (True, False):
            with self.subTest(new_file=new), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                result = self.create(root, new_file=new, configuration=True)
                checked = subprocess.run(["git", "apply", "--check", result["patch"]], cwd=root,
                                         capture_output=True, text=True)
                self.assertEqual(checked.returncode, 0, checked.stderr)
                self.assertFalse(result["ready_to_merge"])
                self.assertFalse(result["proposal_build_verified"])
                self.assertEqual(result["state"], "proposal_review_required")
                self.assertEqual([change["kind"] for change in result["changes"]], ["source", "configuration"])
                self.assertEqual((root / "configure.py").read_text(), "MATCHED = False")
                if new:
                    self.assertFalse((root / "unit.c").exists())
                else:
                    self.assertIn("return 1", (root / "unit.c").read_text())

    def test_changed_destination_or_payload_invalidates_queue_record(self):
        for name in ("unit.c", "proposal/change-0.txt", "proposal/changes.patch"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                self.create(root)
                (root / name).write_text("changed")
                result = inspect_record(root / "proposal/result.json", root)
                self.assertEqual(result["state"], "stale")
                self.assertFalse(result["ready_to_merge"])
                self.assertFalse(result["link_verified"])

    def test_head_absence_and_link_changes_revoke_proposal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            record = self.create(root, new_file=True)
            with patch.object(proposal, "head", return_value="changed"):
                with self.assertRaisesRegex(ValueError, "base commit"):
                    proposal.inspect_proposal(record, root)
            with patch.object(proposal, "head", return_value="revision"), \
                 patch("m2c_unit_queue.inspect_record", return_value={"link_verified": False}):
                with self.assertRaisesRegex(ValueError, "link evidence is stale"):
                    proposal.inspect_proposal(record, root)
                (root / "unit.c").write_text("appeared")
                with self.assertRaisesRegex(ValueError, "appeared"):
                    proposal.inspect_proposal(record, root)

    def test_destinations_cannot_escape_or_modify_git_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for name in ("../outside.c", str(root.parent / "outside.c"), ".git/config", "file\nname"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    proposal.destination(root, name)
            try:
                (root / "link").symlink_to(root / "other")
            except OSError:
                self.skipTest("symbolic links are not permitted on this host")
            with self.assertRaises(ValueError):
                proposal.destination(root, "link")


if __name__ == "__main__":
    unittest.main()
