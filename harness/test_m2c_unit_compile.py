import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import m2c_unit_compile as compile_unit
from m2c_unit_source import m2c_available

M2C = m2c_available()


class UnitCompileTests(unittest.TestCase):
    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_changed_input_invalidates_successful_object_check(self):
        from permuter import owner_extract
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("build/tools/launcher", "build/compilers/GC/1.3/mwcceppc.exe", "build/TEST/target.o"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
            (root / "objdiff.json").write_text(json.dumps({"units": [{"name": "main/test", "scratch": {
                "compiler": "mwcc_242_53", "c_flags": "-O4,p"}}]}))
            source = root / "input.c"
            source.write_text("void test(void) {}")
            owner = {"target_path": "build/TEST/target.o", "target_sha256": hashlib.sha256(b"fixture").hexdigest()}

            def execute(command, **kwargs):
                Path(command[-1]).write_bytes(b"compiled")
                source.write_text("changed")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.object(compile_unit, "capture", return_value=owner), \
                 patch.object(compile_unit, "compiler_launcher", return_value=[str(root / "build/tools/launcher")]), \
                 patch.object(compile_unit.subprocess, "run", side_effect=execute), \
                 patch.object(owner_extract, "read_elf"), \
                 patch.object(compile_unit, "compare_objects", return_value={"object_verified": True}):
                result = compile_unit.run(root, "main/test", source, root / "output")
            self.assertTrue(result["compiled"])
            self.assertFalse(result["object_verified"])
            self.assertFalse(result["link_verified"])
            self.assertEqual(result["state"], "invalid")
            self.assertIn("Input changed", result["error"])
            self.assertTrue((root / "output/compile.log").is_file())
            self.assertEqual(result["command"][0], str(root / "build/tools/launcher"))
            self.assertIn(str(root / "build/tools/launcher"), result["inputs"])

    def test_launcher_is_empty_without_wrapper_and_absolute_otherwise(self):
        root = Path(tempfile.gettempdir()).resolve()
        with patch.object(compile_unit.harness_paths, "compiler_wrapper", return_value=[]):
            self.assertEqual(compile_unit.compiler_launcher(root), [])
        with patch.object(compile_unit.harness_paths, "compiler_wrapper", return_value=["build/tools/wrapper"]):
            self.assertEqual(compile_unit.compiler_launcher(root), [str(root / "build/tools/wrapper")])


if __name__ == "__main__":
    unittest.main()
