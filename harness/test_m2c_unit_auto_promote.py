import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import m2c_unit_auto_promote as promote


class AutoPromoteTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
        source = root / "src/unit.c"
        source.parent.mkdir()
        source.write_text("int unit(void) { return 0; }\n")
        (root / ".gitignore").write_text("artifacts/\nbuild/\n")
        subprocess.run(["git", "add", "src/unit.c", ".gitignore"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=root, check=True, capture_output=True)
        linked = root / "artifacts/link/result.json"
        linked.parent.mkdir(parents=True)
        linked.write_text("{}")
        (linked.parent / "plan.json").write_text(json.dumps({"elf": "build/TEST/main.elf",
            "dol": "build/TEST/main.dol", "expected_sha1": "a" * 40}))
        return root, source, linked

    def proposal(self, root, source, linked):
        def create(project, link_record, output, *args):
            output.mkdir(parents=True)
            payload = output / "change-0.txt"
            payload.write_text("int unit(void) { return 1; }\n")
            patch_path = output / "changes.patch"
            patch_path.write_text("""--- a/src/unit.c
+++ b/src/unit.c
@@ -1 +1 @@
-int unit(void) { return 0; }
+int unit(void) { return 1; }
""")
            record = {"kind": "unit_proposal", "unit": "main/unit",
                "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root,
                                                       text=True).strip(),
                "patch": str(patch_path), "changes": [{"destination": "src/unit.c",
                    "payload": str(payload), "changed": True}]}
            (output / "result.json").write_text(json.dumps(record))
            return record
        return create

    def test_exact_full_build_commits_unit_on_main(self):
        root, source, linked = self.fixture()
        with patch.object(promote, "inspect_record", return_value={"link_verified": True}), \
             patch.object(promote.m2c_unit_proposal, "run", side_effect=self.proposal(root, source, linked)), \
             patch.object(promote, "build_project", return_value={"status": "exact", "actual": "a" * 40}), \
             patch.object(promote, "publish"):
            result = promote.run(root, "TEST", linked, root / "artifacts/promotion")
        self.assertTrue(result["promoted"])
        self.assertEqual(source.read_text(), "int unit(void) { return 1; }\n")
        self.assertEqual(subprocess.check_output(["git", "log", "-1", "--pretty=%s"], cwd=root,
                                                text=True).strip(), "Match main/unit")
        self.assertEqual(promote.status_paths(root), set())

    def test_failed_full_build_rejects_and_restores_source(self):
        root, source, linked = self.fixture()
        with patch.object(promote, "inspect_record", return_value={"link_verified": True}), \
             patch.object(promote.m2c_unit_proposal, "run", side_effect=self.proposal(root, source, linked)), \
             patch.object(promote, "build_project", side_effect=ValueError("bad SHA")), \
             patch.object(promote, "publish"):
            result = promote.run(root, "TEST", linked, root / "artifacts/promotion")
        self.assertFalse(result["promoted"])
        self.assertEqual(result["state"], "rejected")
        self.assertEqual(source.read_text(), "int unit(void) { return 0; }\n")
        self.assertEqual(promote.status_paths(root), set())


if __name__ == "__main__":
    unittest.main()
