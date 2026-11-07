"""Dashboard timestamps distinguish source edits from built measurements."""

from __future__ import annotations

import os
import hashlib
import tempfile
import unittest
from pathlib import Path

from local_campaign_freshness import freshness

VERSION = "TEST01"


class FreshnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.decomp = Path(self.temporary.name) / "decomp"
        self.decomp.mkdir()

    def freshness(self) -> dict:
        return freshness(self.decomp, VERSION)

    def touch(self, root: Path, name: str, second: int) -> None:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test", encoding="utf-8")
        os.utime(path, ns=(second * 1_000_000_000, second * 1_000_000_000))

    def manifest(self, *outputs: tuple[str, bytes]) -> None:
        manifest = self.decomp / f"config/{VERSION}/build.sha1"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("".join(f"{hashlib.sha1(data).hexdigest()}  {name}\n" for name, data in outputs),
                            encoding="utf-8")

    def test_report_staleness_uses_source_mtimes(self) -> None:
        self.touch(self.decomp, f"build/{VERSION}/report.json", 100)
        self.touch(self.decomp, "src/game/main.c", 110)
        result = self.freshness()
        self.assertTrue(result["decomp"]["report_behind_source"])
        self.assertIsNone(result["decomp"]["build"])
        self.assertEqual(result["latest_work"]["path"], "src/game/main.c")
        self.assertNotIn("mtime_ns", result["latest_work"])

        self.manifest((f"build/{VERSION}/main.dol", b"test"))
        os.utime(self.decomp / f"config/{VERSION}/build.sha1", ns=(105 * 1_000_000_000, 105 * 1_000_000_000))
        self.touch(self.decomp, f"build/{VERSION}/report.json", 150)
        self.touch(self.decomp, f"build/{VERSION}/main.dol", 155)
        result = self.freshness()
        self.assertFalse(result["decomp"]["report_behind_source"])
        self.assertEqual(result["decomp"]["build"]["path"], f"build/{VERSION}/main.dol")
        self.assertEqual(result["latest_work"]["project"], "decomp")
        self.assertNotIn("mtime_ns", result["decomp"]["build"])

    def test_docs_count_as_work_not_source_needing_rebuild(self) -> None:
        self.touch(self.decomp, f"build/{VERSION}/report.json", 200)
        self.touch(self.decomp, "src/game/main.c", 180)
        self.touch(self.decomp, "docs/notes.md", 220)
        result = self.freshness()
        self.assertEqual(result["decomp"]["work"]["path"], "docs/notes.md")
        self.assertFalse(result["decomp"]["report_behind_source"])
        self.assertIsNone(result["decomp"]["retail_hashes"])

    def test_retail_hash_status_tracks_every_manifest_output(self) -> None:
        dol = self.decomp / f"build/{VERSION}/main.dol"
        rel = self.decomp / f"build/{VERSION}/module/module.rel"
        dol.parent.mkdir(parents=True)
        rel.parent.mkdir(parents=True)
        dol.write_bytes(b"dol")
        rel.write_bytes(b"rel")
        self.manifest((f"build/{VERSION}/main.dol", b"dol"), (f"build/{VERSION}/module/module.rel", b"rel"))
        hashes = self.freshness()["decomp"]["retail_hashes"]
        self.assertEqual(hashes["status"], "match")
        self.assertEqual(len(hashes["files"]), 2)
        rel.write_bytes(b"wrong")
        self.assertEqual(self.freshness()["decomp"]["retail_hashes"]["status"], "mismatch")
        rel.unlink()
        self.assertEqual(self.freshness()["decomp"]["retail_hashes"]["status"], "missing")

    def test_without_a_version_only_source_activity_is_reported(self) -> None:
        self.touch(self.decomp, "src/a.c", 100)
        result = freshness(self.decomp, "")
        self.assertIsNone(result["decomp"]["report"])
        self.assertIsNone(result["decomp"]["build"])
        self.assertIsNone(result["decomp"]["retail_hashes"])
        self.assertEqual(result["decomp"]["source"]["path"], "src/a.c")


if __name__ == "__main__":
    unittest.main()
