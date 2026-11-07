import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import m2c_pipeline_runner as runner


class PipelineRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tool = patch.object(runner.decompile, "tool_metadata", return_value={
            "revision": "test-revision", "fingerprint": "test-fingerprint"})
        self.tool.start()

    def tearDown(self):
        self.tool.stop()

    def test_restart_after_generation_skips_generation_and_pins_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            batch = root / "build/decomp-support/TEST/batches/run"
            generated = {"folder": str(batch), "run_id": "run"}
            with patch.object(runner.m2c_drafts, "run", return_value=generated) as drafts, \
                 patch.object(runner.m2c_validate_drafts, "run", side_effect=RuntimeError("stop")):
                with self.assertRaisesRegex(RuntimeError, "stop"):
                    runner.run(root, "TEST")
            state_path = root / "build/decomp-support/TEST/pipeline-state.json"
            state = json.loads(state_path.read_text())
            self.assertEqual(state["stage"], "validation")
            self.assertEqual(state["batch"], str(batch))
            self.assertIn("RuntimeError", state["last_error"])
            self.assertEqual(state["config"]["tool_fingerprint"], "test-fingerprint")

            with patch.object(runner.m2c_drafts, "run") as drafts_again, \
                 patch.object(runner.m2c_validate_drafts, "run", return_value={"finished": True}) as validate:
                completed = runner.run(root, "TEST")
            drafts_again.assert_not_called()
            self.assertEqual(validate.call_args.args[2], batch)
            self.assertEqual(completed["status"], "complete")

    def test_active_state_rejects_changed_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "build/decomp-support/TEST/pipeline-state.json"
            runner.write_json(state, {"status": "active", "config_sha256": "different"})
            with self.assertRaisesRegex(ValueError, "different configuration"):
                runner.run(root, "TEST")

    def test_tool_upgrade_changes_pipeline_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "build/decomp-support/TEST/pipeline-state.json"
            with patch.object(runner.m2c_drafts, "run", side_effect=RuntimeError("stop")):
                with self.assertRaisesRegex(RuntimeError, "stop"):
                    runner.run(root, "TEST")
            first = json.loads(state.read_text())["config_sha256"]
            self.tool.stop()
            self.tool = patch.object(runner.decompile, "tool_metadata", return_value={
                "revision": "new-revision", "fingerprint": "new-fingerprint"})
            self.tool.start()
            with self.assertRaisesRegex(ValueError, "different configuration"):
                runner.run(root, "TEST")
            self.assertEqual(json.loads(state.read_text())["config_sha256"], first)


if __name__ == "__main__":
    unittest.main()
