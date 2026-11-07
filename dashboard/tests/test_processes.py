"""Portable process listing used for worker telemetry."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401  (isolated harness config)

import processes


class ProcessListingTests(unittest.TestCase):
    def test_this_interpreter_is_listed_with_its_arguments(self):
        rows = processes.list_processes()
        self.assertIsNotNone(rows, "processes should be observable on Windows, Linux and macOS")
        mine = next((row for row in rows if row["pid"] == os.getpid()), None)
        self.assertIsNotNone(mine)
        self.assertTrue(Path(mine["argv"][0]).name.lower().startswith("py"), mine["argv"])
        self.assertEqual(mine["parent"], os.getppid())
        self.assertGreaterEqual(mine["elapsed_seconds"], 0)
        self.assertFalse(mine["state"].startswith("Z"))
        if sys.argv and sys.argv[0]:
            self.assertGreater(len(mine["argv"]), 1)

    def test_linux_proc_parser(self):
        with tempfile.TemporaryDirectory() as folder:
            proc = Path(folder)
            (proc / "uptime").write_text("1000.0 50.0\n")
            entry = proc / "321"
            entry.mkdir()
            (entry / "cmdline").write_bytes(b"python3\0/x/m2c_drafts.py\0--root\0/p q\0")
            # The command name may contain spaces and parentheses; fields resume after the last ')'.
            fields = ["S", "7"] + ["0"] * 17 + ["50000"]
            (entry / "stat").write_text(f"321 (py (x) y) {' '.join(fields)} 0 0\n")
            kernel = proc / "2"
            kernel.mkdir()
            (kernel / "cmdline").write_bytes(b"")
            (kernel / "stat").write_text("2 (kthreadd) S 0\n")
            (proc / "self").mkdir()
            rows = processes._linux(proc, ticks=100)
        self.assertEqual(rows, [{"pid": 321, "parent": 7, "elapsed_seconds": 500, "state": "S",
                                 "argv": ["python3", "/x/m2c_drafts.py", "--root", "/p q"]}])

    def test_unavailable_lister_is_none_not_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(processes._linux(Path(folder), ticks=100))

    def test_shared_snapshot_reuses_one_scan(self):
        calls, clock = [], [0.0]
        snapshot = processes.SharedSnapshot(lambda: calls.append(1) or [{"pid": 1, "argv": ["python"]}],
                                            max_age=1.0, clock=lambda: clock[0])
        first = snapshot()
        first[0]["argv"].append("mutated")
        self.assertEqual(snapshot()[0]["argv"], ["python"])
        self.assertEqual(len(calls), 1)
        clock[0] = 2.0
        snapshot()
        self.assertEqual(len(calls), 2)
        self.assertIsNone(processes.SharedSnapshot(lambda: None)())

    def test_argument_helpers(self):
        argv = ["python3", "/h/m2c_draft_attempts.py", "--root", "/a", "--version=TEST01", "--root=/b"]
        self.assertEqual(processes.option(argv, "--root"), "/b")
        self.assertEqual(processes.option(argv, "--version"), "TEST01")
        self.assertIsNone(processes.option(argv, "--batch"))
        self.assertEqual(processes.script(argv, {"m2c_draft_attempts.py"}), ("m2c_draft_attempts.py", 1))
        self.assertIsNone(processes.script(["node", "m2c_draft_attempts.py"], {"m2c_draft_attempts.py"}))
        self.assertIsNone(processes.script([], {"x.py"}))


if __name__ == "__main__":
    unittest.main()
