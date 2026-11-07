import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_campaign_decompile_snapshot as snapshot


class SnapshotTests(unittest.TestCase):
    def test_changes_during_capture_are_rejected_and_diagnostics_retained(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            (root / "tools").mkdir(parents=True)
            destination = Path(temporary) / "snapshots"
            with patch.object(snapshot, 'fingerprint', side_effect=['before', 'copied', 'after']):
                with self.assertRaisesRegex(ValueError, 'changed during capture'):
                    snapshot.snapshot(root, destination, 'TEST01')
            folders = list(destination.iterdir())
            self.assertEqual(len(folders), 1)
            manifest = json.loads((folders[0] / 'snapshot.json').read_text(encoding="utf-8"))
            self.assertFalse(manifest['stable'])
            self.assertEqual(manifest['sha256'], 'copied')

    def test_frozen_compiler_source_and_target_survive_live_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            for name in ("src/a.c", "include/a.h", "build/tools/compiler", "build/TEST01/obj/a.o"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(name.encode())
            (root / "tools").mkdir()
            frozen = snapshot.snapshot(root, Path(temporary) / "snapshots", "TEST01")
            manifest = json.loads((frozen / "snapshot.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest['stable'])
            self.assertTrue((frozen / "tools").is_dir())
            for name in ("src/a.c", "include/a.h", "build/tools/compiler", "build/TEST01/obj/a.o"):
                (root / name).write_bytes(b"changed")
                self.assertEqual((frozen / name).read_bytes(), name.encode())
            self.assertEqual(snapshot.fingerprint(frozen, 'TEST01'), manifest['sha256'])
            self.assertNotEqual(snapshot.fingerprint(root, 'TEST01'), manifest['sha256'])


if __name__ == '__main__':
    unittest.main()
