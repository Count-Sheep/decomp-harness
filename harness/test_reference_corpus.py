import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import reference_corpus as corpus

FAKE_CORPUS = {"name": "fake-sdk", "url": "https://example.invalid/fake-sdk.git", "license": "CC0-1.0",
               "role": "test fixture", "jobs": {"kind": "sources", "sources": ["src/**/*.c"],
                                                "compiler": "GC/1.0", "flags": "-O4,p -i include"}}


FAKE_PROJECT = '''
class Object:
    def __init__(self, completed, name, **options):
        self.completed, self.name = completed, name
        self.options = {"cflags": None, "extra_cflags": [], "mw_version": None, "shift_jis": None,
                        "source": name, "src_dir": None}
        self.options.update(options)

    def resolve(self, config, lib):
        obj = Object(self.completed, self.name, **lib)
        for key, value in self.options.items():
            if value is not None or key not in obj.options:
                obj.options[key] = value
        for key, value in (("mw_version", config.linker_version), ("shift_jis", True), ("src_dir", "src")):
            if obj.options.get(key) is None:
                obj.options[key] = value
        from pathlib import Path
        obj.src_path = Path(obj.options["src_dir"]) / obj.options["source"]
        return obj


def generate_build(config):
    raise SystemExit("the probe must capture generate_build")
'''

FAKE_CONFIGURE = '''
from types import SimpleNamespace
from tools.project import Object, generate_build
config = SimpleNamespace(linker_version="GC/1.3", libs=[
    {"lib": "sdk", "mw_version": "GC/1.2.5", "cflags": ["-O4,p", '-pragma "cats off"', "-cwd source"],
     "objects": [Object(True, "sdk/os.c", extra_cflags=["-DX=1"]), Object(False, "sdk/skip.c")]},
    {"lib": "game", "cflags": ["-O2"], "objects": [Object(True, "game/main.c", shift_jis=False),
                                                     Object(True, "game/missing.c")]},
])
generate_build(config)
'''


class ReferenceCorpusTests(unittest.TestCase):
    def test_dtk_jobs_read_matching_objects_without_a_disc(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tools").mkdir()
            (root / "tools/__init__.py").write_text("", encoding="utf-8")
            (root / "tools/project.py").write_text(FAKE_PROJECT, encoding="utf-8")
            (root / "configure.py").write_text(FAKE_CONFIGURE, encoding="utf-8")
            for name in ("sdk/os.c", "sdk/skip.c", "game/main.c"):
                (root / "src" / name).parent.mkdir(parents=True, exist_ok=True)
                (root / "src" / name).write_text("int f(void) { return 0; }\n", encoding="utf-8")
            jobs = corpus.dtk_jobs("fake", root, {}, [])
        rows = {job["source"].relative_to(root.resolve()).as_posix(): job for job in jobs}
        self.assertEqual(sorted(rows), ["src/game/main.c", "src/sdk/os.c"])
        self.assertEqual(rows["src/sdk/os.c"]["compiler_version"], "GC/1.2.5")
        self.assertEqual(rows["src/sdk/os.c"]["flags"],
                         ["-O4,p", "-pragma", "cats off", "-cwd", "source", "-DX=1"])
        self.assertTrue(rows["src/sdk/os.c"]["sjis"])
        self.assertEqual(rows["src/game/main.c"]["compiler_version"], "GC/1.3")
        self.assertFalse(rows["src/game/main.c"]["sjis"])


    def test_configure_parser_only_accepts_literal_matching_objects_in_named_libraries(self):
        source = """
Matching = True
NonMatching = False
config.libs = [
    SdkLib("os", [Object(Matching, "sdk/os/OS.c"),
                  Object(NonMatching, "sdk/os/No.c")]),
    SdkLibOld("mtx", [Object(Matching, "sdk/mtx/vec.c")]),
    {"objects": [Object(Matching, "project/not_sdk.c")]},
]
"""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "configure.py"
            path.write_text(source, encoding="utf-8", newline="\n")
            self.assertEqual(corpus.configure_objects(path, {"SdkLib": "GC/1.1", "SdkLibOld": "GC/1.0"}), [
                ("sdk/mtx/vec.c", "GC/1.0"),
                ("sdk/os/OS.c", "GC/1.1"),
            ])

    def test_configure_jobs_map_objects_to_sources_and_per_object_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "configure.py").write_text('Lib("a", [Object(Matching, "sdk/os/OS.c"), '
                                               'Object(Matching, "sdk/os/Missing.c")])\n', encoding="utf-8")
            source = root / "libs/sdk/src/os/OS.c"
            source.parent.mkdir(parents=True)
            source.write_text("void f(void) {}\n", encoding="utf-8")
            jobs = corpus.configure_jobs("fake", root, {
                "libraries": {"Lib": "GC/1.0"}, "source": "libs/{library}/src/{relative}",
                "flags": ["-O4"], "object_flags": {"sdk/os/OS.c": "-inline auto"}})
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["source"], source)
        self.assertEqual(jobs[0]["flags"], ["-O4", "-inline", "auto"])
        self.assertEqual(jobs[0]["object"], root / "build/reference-objects/fake/sdk/os/OS.o")

    def test_source_jobs_use_the_configured_globs_compiler_and_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("src/a.c", "src/nested/b.c", "src/skip.h"):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("", encoding="utf-8")
            jobs = corpus.corpus_jobs({**FAKE_CORPUS, "root": str(root)})
        self.assertEqual([job["source"].relative_to(root).as_posix() for job in jobs], ["src/a.c", "src/nested/b.c"])
        self.assertTrue(all(job["compiler_version"] == "GC/1.0" for job in jobs))
        self.assertEqual(jobs[0]["flags"], ["-O4,p", "-i", "include"])
        self.assertEqual(jobs[0]["project"], "fake-sdk")

    def test_no_default_corpora_and_clear_error_when_unconfigured(self):
        with patch.object(corpus, "setting", return_value=[]):
            with self.assertRaisesRegex(SystemExit, "No reference corpora are configured"):
                corpus.repositories()
        self.assertEqual(corpus.repositories([FAKE_CORPUS])[0]["name"], "fake-sdk")
        for broken in ({**FAKE_CORPUS, "url": ""}, {**FAKE_CORPUS, "name": "../escape"}):
            with self.subTest(entry=broken), self.assertRaises(SystemExit):
                corpus.repositories([broken])
        with self.assertRaisesRegex(SystemExit, "Duplicate"):
            corpus.repositories([FAKE_CORPUS, FAKE_CORPUS])

    def test_unconfigured_jobs_stage_source_only(self):
        with patch.object(corpus, "sync", return_value=[{**FAKE_CORPUS, "jobs": None, "root": "unused"}]), \
             patch.object(corpus, "write_json") as write:
            registry = corpus.build()
        self.assertEqual(registry["projects"], [])
        self.assertEqual(registry["staged_projects"][0]["status"], "source_only")
        write.assert_called_once()

    def test_scratch_compiler_ids(self):
        self.assertEqual(corpus.scratch_compiler("GC/1.2.5n"), "mwcc_233_163n")
        self.assertEqual(corpus.scratch_compiler("GC/9.9", {"GC/9.9": "custom"}), "custom")
        self.assertEqual(corpus.scratch_compiler("GC/9.9"), "9.9")

    def test_disassembly_parser_keeps_nonempty_text_functions(self):
        text = """# .text:0x0 | size: 0x8
.fn First, global
/* 00000000 00000000  38 60 00 01 */ li r3, 1
/* 00000004 00000004  4E 80 00 20 */ blr
.endfn First
# .text:0x8 | size: 0x0
.fn Empty, global
.endfn Empty
"""
        self.assertEqual(corpus.parse_disassembly(text), [{
            "name": "First", "virtual_address": 0, "size": 8,
            "fuzzy_match_percent": 100.0,
        }])


if __name__ == "__main__":
    unittest.main()
