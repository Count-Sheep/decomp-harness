import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from m2c_capture_inputs import Inputs
from m2c_unit_queue import inspect_record, publish, snapshot, sweep_snapshot, summarize_units


class UnitQueueTests(unittest.TestCase):
    def test_history_does_not_inflate_unit_counts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / f"record-{i}.json" for i in range(3)]
            rows = []
            for i, path in enumerate(paths):
                path.write_text('{}')
                publish(root, "TEST", path)
                rows.append({"unit": "main/unit", "artifact": str(path), "state": "link_verified",
                             "object_verified": True, "link_verified": True, "ready_to_merge": False})
            by_path = {row["artifact"]: row for row in rows}
            with patch("m2c_unit_queue.inspect_record", side_effect=lambda path, project: by_path[str(path)]):
                result = snapshot(root, "TEST")
            self.assertEqual(len(result["records"]), 3)
            self.assertEqual(result["unit_count"], 1)
            self.assertEqual(result["link_verified"], 1)
            self.assertEqual(result["record_counts"]["link_verified"], 3)
            self.assertTrue(result["units"][0]["milestones"]["unit_complete"])

    def test_review_rejection_is_visible_above_existing_build_evidence(self):
        rows = [{"unit": "unit", "artifact": "trial", "state": "proposal_build_verified",
                 "object_verified": True, "link_verified": True, "proposal_build_verified": True,
                 "ready_to_merge": False},
                {"unit": "unit", "artifact": "review", "state": "review_rejected",
                 "object_verified": True, "link_verified": True, "ready_to_merge": False}]
        self.assertEqual(summarize_units(rows)[0]["state"], "review_rejected")
        rows.append({"unit": "unit", "artifact": "old-approval", "state": "stale",
                     "object_verified": False, "link_verified": False, "ready_to_merge": False})
        self.assertEqual(summarize_units(rows)[0]["state"], "review_rejected")

    def test_compilation_alone_does_not_claim_unit_completeness(self):
        result = summarize_units([{"unit": "unit", "artifact": "compile", "state": "draft",
                                   "compiled": True, "object_verified": False, "link_verified": False,
                                   "ready_to_merge": False}])[0]
        self.assertTrue(result["milestones"]["compiled"])
        self.assertIsNone(result["milestones"]["function_exact"])
        self.assertIsNone(result["milestones"]["unit_complete"])

    def test_sweep_workspace_wins_over_server_checkout(self):
        with patch("m2c_unit_queue.snapshot", return_value={"records": []}) as collect:
            value = sweep_snapshot({"latest": {"workspace": "/recorded/project"}}, Path("/server/project"), "TEST01")
            collect.assert_called_once_with(Path("/recorded/project").resolve(), "TEST01")
            self.assertEqual(value["workspace"], str(Path("/recorded/project").resolve()))

    def test_compile_approval_revoked_on_input_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "unit.c"
            source.write_text("void unit(void) {}")
            inputs = Inputs()
            inputs.read(source)
            path = root / "result.json"
            record = {"unit": "main/unit", "inputs": inputs.files,
                      "inventory": {"project_root": str(root)}, "state": "object_verified",
                      "compiled": True, "object_verified": True}
            path.write_text(json.dumps(record))
            self.assertTrue(inspect_record(path, root)["object_verified"])
            self.assertFalse(inspect_record(path, root)["ready_to_merge"])
            source.write_text("changed")
            stale = inspect_record(path, root)
            self.assertEqual(stale["state"], "stale")
            self.assertFalse(stale["object_verified"])

    def test_published_record_change_invalidates_registry_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "result.json"
            path.write_text('{}')
            publish(root, "TEST01", path)
            self.assertEqual(len(snapshot(root, "TEST01")["records"]), 1)
            self.assertEqual(snapshot(root, "TEST02")["records"], [])
            path.write_text('{"link_verified": true}')
            record = snapshot(root, "TEST01")["records"][0]
            self.assertEqual(record["state"], "stale")
            self.assertFalse(record["link_verified"])

    def test_missing_or_incomplete_record_never_approves(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "result.json"
            for value in ({}, {"unit": "main/unit", "inputs": {}}, {"unit": "main/unit", "inputs": None}):
                path.write_text(json.dumps(value))
                result = inspect_record(path, root)
                self.assertEqual(result["state"], "stale")
                self.assertFalse(result["link_verified"])

    def test_automatic_promotion_is_visible_as_completed_not_merge_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "promotion.json"
            path.write_text(json.dumps({"kind": "automatic_unit_promotion", "unit": "main/unit"}))
            with patch("m2c_unit_auto_promote.inspect_promotion", return_value={
                    "state": "promoted", "promoted": True, "object_verified": True,
                    "link_verified": True, "proposal_build_verified": True,
                    "ready_to_merge": False}):
                result = inspect_record(path, root)
            self.assertEqual(result["state"], "promoted")
            self.assertTrue(result["link_verified"])
            self.assertFalse(result["ready_to_merge"])


if __name__ == "__main__":
    unittest.main()
