import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import harness_paths
from m2c_capture_inputs import Inputs
from m2c_unit_preprocess import dependency_paths, native_command, run, split_command

PROJECT = Path(tempfile.gettempdir()).resolve() / "project"


class NativePreprocessTests(unittest.TestCase):
    def test_dependency_paths_preserve_spaces_and_normalize_host_paths(self):
        root = PROJECT
        # Native MWCC on Windows reports real absolute paths; under wibo the host root is Z:.
        absolute = (str(root) if harness_paths.WINDOWS else "Z:" + str(root)).replace("/", "\\")
        text = "unit.o: src\\unit.c \\\n\t" + absolute + "\\include\\my header.h\n"
        self.assertEqual(dependency_paths(root, text), sorted([root / "src/unit.c", root / "include/my header.h"]))
        outside = "unit.o: " + str(Path(root.anchor) / "outside.h") if harness_paths.WINDOWS else "unit.o: C:\\outside.h"
        for bad in ("garbage", "unit.o: ../escape.h", outside):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                dependency_paths(root, bad)

    def test_split_command_keeps_windows_separators_and_posix_quoting(self):
        self.assertEqual(split_command(r"build\compilers\GC\1.3\mwcceppc.exe -i build\TEST01\include -c src\a.c"),
                         ["build/compilers/GC/1.3/mwcceppc.exe", "-i", "build/TEST01/include", "-c", "src/a.c"])
        self.assertEqual(split_command("cc -DNAME='a b' my\\ file.c"), ["cc", "-DNAME=a b", "my file.c"])

    def test_native_command_requires_exact_profile_except_recorded_includes(self):
        root = PROJECT
        selected = {"binary": str(root / "build/compilers/GC/1.3/mwcceppc.exe")}
        command = ("build/tools/wibo build/tools/sjiswrap.exe build/compilers/GC/1.3/mwcceppc.exe "
                   "-nodefaults -nosyspath -i include -DTEST=1 -MMD -c src/unit.c -o build/src && transform_dep")
        flags = ("-nodefaults", "-nosyspath", "-DTEST=1")
        result = native_command(root, command, root / "src/unit.c", selected, flags)
        self.assertIn("include", result)
        self.assertNotIn("-MMD", result)
        for bad in (command.replace("-DTEST=1", "-DTEST=2"), command.replace("include", "../outside"),
                    command.replace("build/tools/wibo", "unknown-wrapper"), command + "\n" + command):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                native_command(root, bad, root / "src/unit.c", selected, flags)

    def test_native_command_accepts_unwrapped_windows_commands(self):
        root = PROJECT
        selected = {"binary": str(root / "build/compilers/GC/1.3/mwcceppc.exe")}
        flags = ("-nodefaults", "-nosyspath", "-DTEST=1")
        for prefix in ("", "build\\tools\\sjiswrap.exe "):
            command = (prefix + "build\\compilers\\GC\\1.3\\mwcceppc.exe -nodefaults -nosyspath -i include "
                       "-DTEST=1 -MMD -c src\\unit.c -o build\\TEST01\\src && transform_dep")
            with self.subTest(prefix=prefix):
                result = native_command(root, command, root / "src/unit.c", selected, flags)
                self.assertEqual(Path(result[len(prefix.split())]), root / "build/compilers/GC/1.3/mwcceppc.exe")
                self.assertEqual(result[-3:], ["-i", "include", "-DTEST=1"])

    def fixture(self, root):
        files = {"objdiff.json": json.dumps({"units": [{"name": "main/test", "base_path": "unit.o",
                  "metadata": {"source_path": "unit.c"}}]}), "build.ninja": "graph", "unit.c": '#include "types.h"',
                 "types.h": "typedef int value;", "build/tools/wibo": "launcher", "compiler": "compiler"}
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def exercise(self, root, mutation=None, changed_graph=False):
        self.fixture(root)
        calls = []
        def execute(command, **kwargs):
            calls.append(command)
            if command[0] == "ninja":
                stdout = "compile command"
            elif "-M" in command:
                stdout = "unit.o: unit.c \\\n\ttypes.h\n"
                if changed_graph and len(calls) == 4:
                    stdout = "unit.o: unit.c\n"
            else:
                stdout = "typedef int value; value test(void) { return 1; }"
                if mutation:
                    mutation(root)
            return subprocess.CompletedProcess(command, 0, stdout, "")
        inputs = Inputs()
        with patch("m2c_unit_preprocess.compiler_profile", return_value={}), \
             patch("m2c_unit_preprocess.compiler_choices", return_value=[("profile", (), {})]), \
             patch("m2c_unit_preprocess.native_command", return_value=[str(root / "build/tools/wibo"), str(root / "compiler")]), \
             patch("m2c_unit_preprocess.subprocess.run", side_effect=execute):
            text = run(root, "main/test", root / "unit.c", root / "output", inputs)
        return text, inputs

    def test_captures_dependencies_output_logs_and_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            text, inputs = self.exercise(root)
            self.assertIn("test(void)", text)
            for name in ("unit.c", "types.h", "build.ninja", "output/result.json", "output/source.c",
                         "output/preprocessed.stdout", "output/dependencies-after.stdout"):
                self.assertIn(str(root / name), inputs.files)
            (root / "types.h").write_text("changed")
            with self.assertRaisesRegex(ValueError, "Input changed"):
                inputs.verify()

    def test_header_mutation_and_changed_dependency_graph_are_rejected(self):
        for changed_graph in (True, False):
            with self.subTest(changed_graph=changed_graph), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                with self.assertRaisesRegex(ValueError, "changed"):
                    self.exercise(root, changed_graph=changed_graph,
                                  mutation=None if changed_graph else lambda path: (path / "types.h").write_text("changed"))
                self.assertEqual(json.loads((root / "output/result.json").read_text())["state"], "invalid")


if __name__ == "__main__":
    unittest.main()
