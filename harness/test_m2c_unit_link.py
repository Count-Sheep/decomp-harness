import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from m2c_capture_inputs import Inputs
import m2c_unit_link as link


class UnitLinkTests(unittest.TestCase):
    def trial(self, baseline=True, candidate=True, mutate=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            compiled, output = root / "compiled", root / "trial"
            compiled.mkdir()
            (compiled / "unit.o").write_bytes(b"candidate")
            (root / "original.o").write_bytes(b"original")
            (root / "build.ninja").write_bytes(b"graph")
            (root / "checksum").write_bytes(b"expected")
            inputs = Inputs()
            inputs.read(compiled / "unit.o")
            record = {"unit": "main/test", "inventory": {"target_path": "original.o"}}
            digest = lambda data: hashlib.sha256(data).hexdigest()
            plan = {"elf": "build/TEST/main.elf", "units": {"main/test": {"target_path": "original.o"}},
                    "objects": ["original.o"], "files": [{"source": "original.o", "slot": "original.o",
                    "sha256": digest(b"original")}], "checksum_file_sha256": digest(b"expected"),
                    "build_ninja_sha256": digest(b"graph")}

            def capture(*args):
                (output / "workspace").mkdir(parents=True)
                (output / "workspace/original.o").write_bytes(b"original")
                (output / "plan.json").write_text(json.dumps(plan))
                return plan

            calls = []

            def execute(workspace, plan, artifact):
                calls.append(artifact.name)
                if artifact.name == "candidate":
                    self.assertEqual((workspace / "original.o").read_bytes(), b"candidate")
                    if mutate:
                        (compiled / "unit.o").write_bytes(b"changed")
                    return {"passed": candidate}
                self.assertEqual((workspace / "original.o").read_bytes(), b"original")
                return {"passed": baseline}

            with patch.object(link, "candidate_inputs", return_value=(record, inputs)), \
                 patch.object(link.gate, "capture_plan", side_effect=capture), \
                 patch.object(link.gate, "link_once", side_effect=execute):
                result = link.run(root, compiled, output, "main.elf", "checksum")
            self.assertEqual((output / "workspace/original.o").read_bytes(), b"original")
            self.assertEqual((root / "original.o").read_bytes(), b"original")
            self.assertFalse(result["ready_to_merge"])
            return result, calls

    def test_baseline_precedes_candidate_and_does_not_approve_merge(self):
        result, calls = self.trial()
        self.assertEqual(calls, ["baseline", "candidate"])
        self.assertTrue(result["link_verified"])

    def test_failed_baseline_prevents_candidate_link(self):
        result, calls = self.trial(baseline=False)
        self.assertEqual(calls, ["baseline"])
        self.assertFalse(result["link_verified"])

    def test_candidate_failure_is_not_verified(self):
        result, _ = self.trial(candidate=False)
        self.assertEqual(result["state"], "link_failed")

    def test_mutation_invalidates_successful_link(self):
        result, _ = self.trial(mutate=True)
        self.assertEqual(result["state"], "invalid")
        self.assertFalse(result["link_verified"])


if __name__ == "__main__":
    unittest.main()
