from pathlib import Path
from types import SimpleNamespace as Record
import tempfile
import unittest

from m2c_readonly_data import capture_object, owner_path, owner_fingerprints


class ReadonlyDataTests(unittest.TestCase):
    def owner(self):
        return Record(path=Path("unit.o"), data=b"ELF evidence", sections=[
            Record(name="", data=b"", flags=0, type=0),
            Record(name=".text", data=bytes(16), flags=6, type=1),
            Record(name=".sdata2", data=bytes.fromhex("3f80000040200000"), flags=2, type=1),
        ], symbols=[
            Record(name="", type=0, shndx=0, value=0, size=0),
            Record(name="target", type=2, shndx=1, value=0, size=8),
            Record(name="@pool", type=1, shndx=2, value=0, size=8),
            Record(name="sibling", type=2, shndx=1, value=8, size=8),
        ], relocations=[Record(target_section_index=1, offset=0, symbol_index=2)])

    def test_capture_uses_owner_scoped_relocations_and_exact_bytes(self):
        output, info = capture_object(self.owner(), "target", b".text\n")
        self.assertIn(b'.obj "@pool", local', output)
        self.assertIn(b"0x3F, 0x80, 0x00, 0x00, 0x40, 0x20, 0x00, 0x00", output)
        self.assertEqual(info["added"][0]["size"], 8)
        output, info = capture_object(self.owner(), "sibling", b".text\n")
        self.assertEqual(output, b".text\n")
        self.assertEqual(info["added"], [])

    def test_writable_executable_unallocated_and_nobits_sections_are_not_constants(self):
        for flags, section_type in ((3, 1), (6, 1), (0, 1), (2, 8)):
            owner = self.owner()
            owner.sections[2].flags, owner.sections[2].type = flags, section_type
            _, info = capture_object(owner, "target", b".text\n")
            self.assertEqual(info["added"], [])

    def test_relocated_data_and_out_of_bounds_symbols_are_rejected(self):
        for size, reloc in ((9, False), (0, False), (8, True)):
            owner = self.owner()
            owner.symbols[2].size = size
            if reloc:
                owner.relocations.append(Record(target_section_index=2, offset=4, symbol_index=0))
            _, info = capture_object(owner, "target", b".text\n")
            self.assertEqual(info["added"], [])

    def test_existing_captured_data_is_never_overwritten(self):
        source = b'.rodata\n.obj "@pool", local\n.byte 0\n.endobj "@pool"\n'
        output, info = capture_object(self.owner(), "target", source)
        self.assertEqual(output, source)
        self.assertEqual(info["skipped"][0]["reason"], "already captured")

    def test_duplicate_names_and_undefined_symbols_are_not_guessed(self):
        owner = self.owner()
        owner.symbols.append(Record(name="@pool", type=1, shndx=2, value=4, size=4))
        _, info = capture_object(owner, "target", b".text\n")
        self.assertEqual(info["added"], [])
        owner.symbols[2].shndx = 0
        _, info = capture_object(owner, "target", b".text\n")
        self.assertEqual(info["skipped"][0]["reason"], "not defined in target owner")

    def test_missing_target_and_escaping_owner_paths_are_rejected(self):
        with self.assertRaises(ValueError):
            capture_object(self.owner(), "absent", b".text\n")
        project = Path("project").resolve()
        with self.assertRaises(ValueError):
            owner_path(project, "TEST", "../../foreign")
        self.assertEqual(owner_path(project, "TEST", "main/game/unit"),
                         project / "build/TEST/obj/game/unit.o")

    def test_batch_fingerprint_binds_constant_bytes_even_when_assembly_is_unchanged(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            owner = owner_path(root, "TEST", "unit")
            owner.parent.mkdir(parents=True)
            owner.write_bytes(b"same instructions; first constant")
            first = owner_fingerprints(root, "TEST", ["unit", "main/unit"])
            self.assertEqual(len(first), 1)
            owner.write_bytes(b"same instructions; other constant")
            self.assertNotEqual(first, owner_fingerprints(root, "TEST", ["unit"]))


if __name__ == "__main__":
    unittest.main()
