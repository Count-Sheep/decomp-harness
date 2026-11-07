import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from m2c_capture_inputs import Inputs
import m2c_unit_project_trial as trial

# PyYAML reads the project's dtk config; it is a project build dependency, not a harness one.
HAS_YAML = importlib.util.find_spec("yaml") is not None


class ProjectTrialTests(unittest.TestCase):
    def test_configure_command_passes_only_present_tools_and_host_wrapper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for name in ("build/compilers/GC/1.3/mwcceppc.exe", "build/tools/sjiswrap.exe"):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_bytes(b"tool")
            with patch.object(trial.harness_paths, "compiler_wrapper", return_value=[]):
                command = trial.configure_command(root, "TEST01")
            self.assertEqual(command[1:5], ["configure.py", "--version", "TEST01", "--no-progress"])
            self.assertIn("--compilers", command)
            self.assertEqual(command[command.index("--sjiswrap") + 1], "build/tools/sjiswrap.exe")
            for flag in ("--wrapper", "--dtk", "--binutils"):
                self.assertNotIn(flag, command)
            with patch.object(trial.harness_paths, "compiler_wrapper",
                              return_value=[str(root / "build/tools/wrapper")]):
                command = trial.configure_command(root, "TEST01")
            self.assertEqual(command[command.index("--wrapper") + 1], "build/tools/wrapper")

    def test_command_failure_retains_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "command failed"):
                trial.execute([sys.executable, "-c", "print('failure detail'); raise SystemExit(1)"],
                              root, root / "log")
            self.assertIn("failure detail", (root / "log").read_text())

    def test_timeout_terminates_trial_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "timed out"):
                trial.execute([sys.executable, "-c", "import time; time.sleep(10)"], root, root / "log", timeout=0.05)
            self.assertTrue((root / "log").exists())

    @unittest.skipUnless(HAS_YAML, "PyYAML is not installed")
    def test_snapshot_copies_only_required_retail_input_and_binds_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            workspace = Path(temporary) / "workspace"
            root.mkdir()
            workspace.mkdir()
            files = {"unit.c": "source", "config/TEST/config.yml": "name: main\nobject_base: orig/TEST\nobject: sys/main.dol\n",
                     "orig/TEST/sys/main.dol": "retail", "orig/TEST/game.iso": "excluded",
                     "build/tools/dtk": "tool", "build/decomp-support/large-file": "excluded"}
            for name, value in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value)
            inputs = Inputs()
            with patch.object(trial.subprocess, "check_output", return_value=b"unit.c\0config/TEST/config.yml\0"):
                trial.capture(root, workspace, "TEST", inputs)
            self.assertEqual((workspace / "orig/TEST/sys/main.dol").read_text(), "retail")
            self.assertFalse((workspace / "orig/TEST/game.iso").exists())
            self.assertFalse((workspace / "build/decomp-support").exists())
            self.assertEqual((workspace / "build/tools/dtk").read_text(), "tool")
            (root / "unit.c").write_text("changed")
            with self.assertRaisesRegex(ValueError, "Input changed"):
                inputs.verify()

    def test_trial_reader_checks_proposal_checksum_and_head_without_approving_merge(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "proposal.json").write_text(json.dumps({"kind": "unit_proposal", "unit": "main/unit",
                                                           "link_record": str(root / "link.json")}))
            raw = b"retail"
            digest = hashlib.sha1(raw).hexdigest()
            (root / "plan.json").write_text(json.dumps({"expected_sha1": digest}))
            (root / "baseline.dol").write_bytes(raw)
            (root / "candidate.dol").write_bytes(raw)
            record = {"project_root": str(root), "unit": "main/unit", "base_commit": "head",
                      "project_paths": ["unit.c"],
                      "proposal": str(root / "proposal.json"), "proposal_build_verified": True,
                      "object_evidence": {"object_verified": True},
                      "baseline": {"passed": True, "sha1": digest, "dol": str(root / "baseline.dol")},
                      "candidate": {"passed": True, "sha1": digest, "dol": str(root / "candidate.dol")}}
            with patch.object(trial, "head", return_value="head"), \
                 patch.object(trial, "project_paths", return_value=["unit.c"]), \
                 patch.object(trial, "inspect_record", return_value={"state": "proposal_review_required", "link_verified": True}):
                result = trial.inspect_trial(record, root)
                self.assertTrue(result["proposal_build_verified"])
                self.assertFalse(result["ready_to_merge"])
                (root / "candidate.dol").write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "output changed"):
                    trial.inspect_trial(record, root)
            with patch.object(trial, "head", return_value="other"):
                with self.assertRaisesRegex(ValueError, "base commit"):
                    trial.inspect_trial(record, root)

    @unittest.skipUnless(HAS_YAML, "PyYAML is not installed")
    def test_unnamed_main_dol_is_supported_but_rel_and_other_names_are_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            config = root / "config/TEST/config.yml"
            config.parent.mkdir(parents=True)
            retail = root / "orig/TEST/sys/main.dol"
            retail.parent.mkdir(parents=True)
            retail.write_bytes(b"retail")
            base = "object_base: orig/TEST\nobject: sys/main.dol\n"
            config.write_text(base)
            with patch.object(trial, "project_paths", return_value=["config/TEST/config.yml"]):
                workspace = Path(temporary) / "supported"
                trial.capture(root, workspace, "TEST", Inputs())
                self.assertEqual((workspace / "orig/TEST/sys/main.dol").read_bytes(), b"retail")
                for index, value in enumerate((base + "name: other\n", base + "name: null\n",
                                               base + "modules: [{object: files/a.rel}]\n",
                                               base.replace("main.dol", "main.rel"))):
                    config.write_text(value)
                    with self.subTest(config=value), self.assertRaisesRegex(ValueError, "main-only"):
                        trial.capture(root, Path(temporary) / str(index), "TEST", Inputs())

    def test_project_paths_expands_clean_gitlink_and_binds_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "unit.c").write_text("source")
            (root / "extern/library").mkdir(parents=True)
            commands = []

            def output(command, **kwargs):
                commands.append(command)
                if command[:3] == ["git", "ls-files", "--cached"] and kwargs.get("cwd") == root:
                    return b"unit.c\0extern/library\0"
                if command == ["git", "rev-parse", "HEAD"]:
                    return "abc123\n"
                if command[:3] == ["git", "status", "--porcelain"]:
                    return ""
                if command[:3] == ["git", "ls-files", "--cached"]:
                    return b"src/a.c\0include/a.h\0"
                raise AssertionError(command)

            completed = trial.subprocess.CompletedProcess([], 0, "160000 abc123 0\textern/library\n", "")
            with patch.object(trial.subprocess, "check_output", side_effect=output), \
                 patch.object(trial.subprocess, "run", return_value=completed):
                paths = trial.project_paths(root)
            self.assertEqual(paths, ["\0gitlink:extern/library:abc123", "extern/library/include/a.h",
                                     "extern/library/src/a.c", "unit.c"])
            self.assertIn(["git", "status", "--porcelain", "--untracked-files=all"], commands)

    def test_project_paths_rejects_dirty_or_wrong_gitlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "extern/library").mkdir(parents=True)
            listing = b"extern/library\0"
            completed = trial.subprocess.CompletedProcess([], 0, "160000 abc123 0\textern/library\n", "")
            for head, status in (("wrong\n", ""), ("abc123\n", " M src/a.c\n")):
                with self.subTest(head=head, status=status), \
                     patch.object(trial.subprocess, "run", return_value=completed), \
                     patch.object(trial.subprocess, "check_output",
                         side_effect=[listing, head, status]):
                    with self.assertRaisesRegex(ValueError, "initialized, clean"):
                        trial.project_paths(root)


if __name__ == "__main__":
    unittest.main()
