import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_campaign_templates as templates


class MatchedRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.asm = self.root / "build/TEST01/asm/functions.s"
        self.asm.parent.mkdir(parents=True)
        self.asm.touch()
        os.utime(self.asm, (10, 10))
        self.index = self.root / "build/simindex/index.pkl"
        self.index.parent.mkdir(parents=True)
        self.index.touch()
        os.utime(self.index, (20, 20))
        self.report = self.root / "build/TEST01/report.json"

    def write_report(self, rows):
        units = [{"name": name, "metadata": {"source_path": source, "auto_generated": auto},
                  "sections": [{"functions": [{"name": name, "fuzzy_match_percent": pct,
                       "metadata": {"virtual_address": str(addr)}}]}]}
                 for name, addr, pct, source, auto in rows]
        self.report.write_text(json.dumps({"units": units}))
        os.utime(self.report, (30, 30))

    def test_live_report_promotes_demotes_and_updates_source_before_ranking(self):
        self.write_report([("new", 1, 100, "new.c", False),
                           ("old", 2, 99, "old.c", False),
                           ("stub", 3, 100, "stub.c", True),
                           ("no_source", 4, 100, None, False),
                           ("wrong_addr", 6, 100, "wrong.c", False)])
        index = {"functions": [{"name": name, "addr": addr, "corpus": "test",
                  "asm_file": "functions.s", "fuzzy": 100, "src_path": "stale.c"}
                 for name, addr in (("new", 1), ("old", 2), ("stub", 3),
                                    ("no_source", 4), ("wrong_addr", 5))]}
        templates.refresh_matches(index, self.root, self.index.stat().st_mtime_ns, "TEST01", "test")
        self.assertEqual(index["functions"][0]["src_path"], "new.c")
        self.assertEqual([fn["fuzzy"] for fn in index["functions"]], [100, 0, 0, 0, 0])

    def test_changed_or_missing_assembly_excludes_even_exact_c(self):
        self.write_report([("f", 1, 100, "f.c", False)])
        fn = {"name": "f", "addr": 1, "corpus": "test", "asm_file": "functions.s"}
        index = {"functions": [fn]}
        os.utime(self.asm, (40, 40))
        templates.refresh_matches(index, self.root, self.index.stat().st_mtime_ns, "TEST01", "test")
        self.assertEqual(fn["fuzzy"], 0)
        self.asm.unlink()
        self.assertFalse(templates.assembly_current(self.root, "build/TEST01/asm/functions.s", 20_000_000_000))

    def test_cached_target_and_reference_assembly_are_rechecked(self):
        self.write_report([])
        payload = {"references": [{"symbol": "g"}],
                   "assembly_files": ["build/TEST01/asm/functions.s"]}
        result = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with patch.object(templates, "tool_python", return_value=str(self.index)), \
             patch.dict(templates._RESULTS, {}, clear=True), \
             patch.object(templates.subprocess, "run", return_value=result) as run:
            self.assertEqual(templates.matched_templates(self.root, "f", "TEST01"), [{"symbol": "g"}])
            templates.matched_templates(self.root, "f", "TEST01")
            self.assertEqual(run.call_count, 1)
            os.utime(self.asm, (40, 40))
            run.return_value = subprocess.CompletedProcess([], 0,
                '{"references": [], "assembly_files": []}', "")
            self.assertEqual(templates.matched_templates(self.root, "f", "TEST01"), [])
            self.assertEqual(run.call_count, 2)

    def test_report_race_never_returns_prior_match_status(self):
        self.write_report([])
        def query(*args, **kwargs):
            os.utime(self.report, (31, 31))
            return subprocess.CompletedProcess([], 0,
                '{"references": [{"symbol": "g"}], "assembly_files": []}', "")
        with patch.object(templates, "tool_python", return_value=str(self.index)), \
             patch.dict(templates._RESULTS, {}, clear=True), \
             patch.object(templates.subprocess, "run", side_effect=query):
            self.assertEqual(templates.matched_templates(self.root, "f", "TEST01"), [])


if __name__ == "__main__":
    unittest.main()
