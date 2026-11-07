"""Headless Ghidra bootstrap must bind its output to the input binary."""

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import m2c_ghidra_inventory as inventory


class GhidraInventoryTests(unittest.TestCase):
    def test_import_then_refresh_command_and_hash_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "main.dol"
            binary.write_bytes(b"retail")
            tool = root / "analyzeHeadless"
            tool.touch()
            scripts = root / "scripts"
            scripts.mkdir()
            output = root / "out/inventory.json"
            log = root / "out/export.log"
            workspace = root / "project"

            def execute(command, **kwargs):
                self.assertIn("-import", command)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps({"input_sha256": hashlib.sha256(b"retail").hexdigest(),
                                              "function_count": 12, "schema_version": 2}))
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(inventory.subprocess, "run", side_effect=execute):
                result = inventory.run(binary, workspace, "Game", output, scripts, log, tool)
            self.assertEqual(result["mode"], "import")
            self.assertEqual(result["functions"], 12)

            (workspace / "Game.gpr").touch()
            refresh = inventory.command(tool, binary, workspace, "Game", output, scripts, log, True)
            self.assertEqual(refresh[3:6], ["-process", "main.dol", "-readOnly"])

    def test_mismatched_inventory_is_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "main.dol"; binary.write_bytes(b"retail")
            tool = root / "analyzeHeadless"; tool.touch()
            scripts = root / "scripts"; scripts.mkdir()
            output = root / "inventory.json"

            def execute(command, **kwargs):
                output.write_text(json.dumps({"input_sha256": "0" * 64}))
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(inventory.subprocess, "run", side_effect=execute):
                with self.assertRaisesRegex(ValueError, "input hash"):
                    inventory.run(binary, root / "project", "Game", output, scripts,
                                  root / "log", tool)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
