"""The decomp-permuter patch series is well formed and applies to its base revision files."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:  # discovered from harness/ as permuter.test_apply_permuter_patches
    from permuter import apply_permuter_patches as patches
except ImportError:  # run directly from harness/permuter/
    sys.path.append(str(Path(__file__).resolve().parent))
    import apply_permuter_patches as patches  # type: ignore[no-redef]


class PermuterPatchTests(unittest.TestCase):
    def test_series_is_lf_and_names_only_permuter_sources(self) -> None:
        self.assertTrue(patches.PATCHES)
        for patch in patches.PATCHES:
            data = patch.read_bytes()
            self.assertNotIn(b"\r\n", data, patch.name)
            touched = {line.split()[2][2:] for line in data.decode().splitlines() if line.startswith("diff --git ")}
            self.assertTrue(touched <= {"src/compiler.py", "src/preprocess.py"}, touched)

    @unittest.skipUnless(os.environ.get("DECOMP_PERMUTER_CHECKOUT"),
                         "set DECOMP_PERMUTER_CHECKOUT to an unpatched decomp-permuter clone")
    def test_applies_to_a_copy_of_a_real_checkout(self) -> None:
        source = Path(os.environ["DECOMP_PERMUTER_CHECKOUT"])
        with tempfile.TemporaryDirectory() as temporary:
            copy = Path(temporary) / "permuter"
            subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "-q", str(source), str(copy)], check=True)
            self.assertEqual(patches.main(["--root", str(copy), "--check"]), 0)
            self.assertEqual(patches.state(copy), "ready")
            self.assertEqual(patches.main(["--root", str(copy)]), 0)
            self.assertEqual(patches.state(copy), "applied")
            self.assertIn("compile_cmd.json", (copy / "src/compiler.py").read_text(encoding="utf-8"))
            shutil.rmtree(copy, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
