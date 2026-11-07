import tempfile
import unittest
from pathlib import Path

from local_campaign_evidence_cache import EvidenceCache, ReadAhead, fingerprint


class EvidenceCacheTests(unittest.TestCase):
    def test_cache_is_bounded_and_returns_independent_values(self):
        cache = EvidenceCache(1)
        cache.put("a", {"score": 5})
        cache.get("a")["score"] = 99
        self.assertEqual(cache.get("a")["score"], 5)
        cache.put("b", {})
        self.assertIsNone(cache.get("a"))

    def test_source_header_tool_and_flags_invalidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            names = ["src/a.c", "include/a.h", "build/compilers/compiler.exe",
                     "target.o", "diff", "build.ninja", "objdiff.json"]
            for name in names:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("original")
            def key(flags=None):
                return fingerprint(root, flags or ["-i", "include"], root / "target.o", root / "diff", "f")
            previous = key()
            self.assertIsNotNone(previous)
            for name in names:
                (root / name).write_text("changed")
                current = key()
                self.assertNotEqual(previous, current, name)
                previous = current
            self.assertNotEqual(key(), key(["-O4"]))

    def test_read_ahead_never_serves_another_input(self):
        read_ahead = ReadAhead()
        try:
            read_ahead.submit("first", lambda: "prepared")
            read_ahead.future.result(timeout=2)
            self.assertIsNone(read_ahead.take("changed"))
            self.assertEqual(read_ahead.take("first"), "prepared")
        finally:
            read_ahead.executor.shutdown()
