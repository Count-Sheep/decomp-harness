from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from permuter import farm_config, gen_workunits
except ImportError:  # run directly from this directory
    import farm_config
    import gen_workunits

SHAPED_OWNER = """int before(void) { return 1; }
#pragma optimization_level 4
#pragma peephole off
int fn_target(int x) {
    return x + 1;
}
#pragma peephole on
int after(void) { return 2; }
"""


class GeneratorTests(unittest.TestCase):
    def test_parse_ninja_normalises_windows_paths_and_reads_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ninja = Path(temporary) / "build.ninja"
            ninja.write_text(
                "build build\\TEST01\\src\\game\\unit.o: mwcc_sjis src\\game\\unit.c $\n"
                "    | build\\tools\\sjiswrap.exe\n"
                "  mw_version = GC/1.3.2\n"
                "  cflags = -nodefaults -i build\\TEST01\\include -DVERSION=0\n"
                "build build/TEST01/src/other.o: mwcc src/other.c\n"
                "  mw_version = GC/2.0\n",
                encoding="utf-8",
            )
            units = gen_workunits.parse_ninja(ninja)
        unit = units["build/TEST01/src/game/unit.o"]
        self.assertEqual((unit["rule"], unit["src"], unit["mw_version"]),
                         ("mwcc_sjis", "src/game/unit.c", "GC/1.3.2"))
        self.assertEqual(gen_workunits.split_flags(unit["cflags"]),
                         ["-nodefaults", "-i", "build/TEST01/include", "-DVERSION=0"])
        self.assertEqual(units["build/TEST01/src/other.o"]["mw_version"], "GC/2.0")

    def test_unit_paths_follow_the_requested_version(self) -> None:
        with mock.patch.object(gen_workunits, "REPO", Path("project")):
            paths = gen_workunits.unit_to_paths("main/game/unit", "TEST01")
        self.assertEqual(paths["obj"], "build/TEST01/src/game/unit.o")
        self.assertEqual(paths["asm"], Path("project/build/TEST01/asm/game/unit.s"))
        self.assertEqual(paths["src"], Path("project/src/game/unit.c"))

    def test_owner_context_policy_is_opt_in_and_named(self) -> None:
        self.assertEqual(gen_workunits.OWNER_CONTEXT_POLICIES, {})
        with self.assertRaisesRegex(gen_workunits.OwnerSourceError, "no reviewed clean-context policy"):
            gen_workunits.clean_owner_target_context(SHAPED_OWNER, "fn_target")
        with self.assertRaisesRegex(gen_workunits.OwnerSourceError, "unknown clean-context policy"):
            gen_workunits.clean_owner_target_context(SHAPED_OWNER, "fn_target", {"fn_target": "other"})
        clean, record = gen_workunits.clean_owner_target_context(
            SHAPED_OWNER, "fn_target", {"fn_target": "pragma-clean-v1"})
        self.assertNotIn("peephole off", clean)
        self.assertNotIn("optimization_level", clean)
        self.assertIn("#pragma peephole on\nint fn_target(int x)", clean)
        self.assertTrue(clean.endswith("#pragma peephole on\nint after(void) { return 2; }\n"))
        self.assertEqual((record["policy"], record["function"]), ("pragma-clean-v1", "fn_target"))
        unshaped = SHAPED_OWNER.replace("#pragma optimization_level 4\n", "")
        with self.assertRaisesRegex(gen_workunits.OwnerSourceError, "shaped target prefix"):
            gen_workunits.clean_owner_target_context(unshaped, "fn_target",
                                                     {"fn_target": "pragma-clean-v1"})

    def test_policies_and_allowlists_come_from_config_then_cli(self) -> None:
        settings = {"owner_context_policies": {"fn_a": "pragma-clean-v1"},
                    "intrinsic_allowlists": {"fn_a": ["__x"]}}
        with mock.patch.object(farm_config, "setting",
                               side_effect=lambda key, default=None: settings.get(key, default)):
            self.assertEqual(farm_config.owner_context_policies(["fn_b=pragma-clean-v1"]),
                             {"fn_a": "pragma-clean-v1", "fn_b": "pragma-clean-v1"})
            self.assertEqual(farm_config.intrinsic_allowlists(["fn_a=__y", "fn_b=__z"]),
                             {"fn_a": frozenset({"__x", "__y"}), "fn_b": frozenset({"__z"})})
        with mock.patch.object(farm_config, "setting", return_value={}):
            self.assertEqual(farm_config.owner_context_policies(), {})
            self.assertEqual(farm_config.intrinsic_allowlists(), {})
            with self.assertRaises(ValueError):
                farm_config.owner_context_policies(["missing-separator"])

    def test_locations_default_inside_the_project_build_directory(self) -> None:
        root = Path(tempfile.gettempdir()).resolve() / "project"
        with mock.patch.object(farm_config, "setting", return_value=None), \
             mock.patch.object(farm_config, "_tool", return_value=None):
            farm = farm_config.farm_root(root=root)
            self.assertEqual(farm, root / "build" / "permuter-farm")
            self.assertEqual(farm_config.results_dir(root=root), root / "build" / "permuter_results")
            self.assertEqual(farm_config.permuter_checkout(farm=farm), farm / "decomp-permuter")
            self.assertEqual(farm_config.binutils_dir(root=root), root / "build" / "binutils")

    def test_depfile_targets_may_carry_a_drive_letter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "include").mkdir()
            header = root / "include" / "a b.h"
            header.write_text("", encoding="utf-8")
            source = root / "unit.c"
            source.write_text("", encoding="utf-8")
            depfile = root / "unit.d"
            depfile.write_text(f"C:\\out\\_pp.c: unit.c \\\n  include/a\\ b.h\n", encoding="utf-8")
            with mock.patch.object(gen_workunits, "REPO", root):
                self.assertEqual(gen_workunits.parse_depfile(depfile), [header, source])

    def test_command_word_quotes_only_paths_with_whitespace(self) -> None:
        self.assertEqual(gen_workunits.command_word(Path("/opt/tools/objdump")), "/opt/tools/objdump")
        self.assertEqual(gen_workunits.command_word(Path("/opt/my tools/objdump")),
                         '"/opt/my tools/objdump"')


if __name__ == "__main__":
    unittest.main()
