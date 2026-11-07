import copy
from types import SimpleNamespace as Record
import unittest

from m2c_unit_evidence import compare_objects, collection_difference


class UnitEvidenceTests(unittest.TestCase):
    def test_diagnostics_locate_byte_layout_and_relocation_changes(self):
        target = self.example()
        candidate = copy.deepcopy(target)
        candidate.sections[2].data = b"axcd"
        candidate.sections[2].addralign = 8
        candidate.relocations[0].addend = 4
        result = compare_objects(target, candidate)
        self.assertFalse(result["object_verified"])
        rows = {row["name"]: row for row in result["diagnostics"]["sections"]}
        self.assertEqual(rows[".data"]["bytes"]["first_difference"], 1)
        self.assertEqual(rows[".data"]["bytes"]["different_overlap_bytes"], 1)
        self.assertEqual(rows[".data"]["attributes"]["alignment"], {"target": 4, "candidate": 8})
        self.assertEqual(rows[".text"]["relocations"]["added_count"], 1)
        self.assertEqual(rows[".text"]["relocations"]["added"][0][2], 4)

    def test_diagnostics_distinguish_size_from_byte_changes(self):
        target = self.example()
        candidate = copy.deepcopy(target)
        candidate.sections[2].size = 8
        candidate.sections[2].data += b"efgh"
        row = compare_objects(target, candidate)["diagnostics"]["sections"][0]
        self.assertEqual(row["bytes"]["different_overlap_bytes"], 0)
        self.assertEqual(row["bytes"]["candidate_only_bytes"], 4)
        self.assertEqual(row["bytes"]["first_difference"], 4)

    def test_collection_diagnostics_are_bounded_and_preserve_duplicates(self):
        result = collection_difference([[1]] * 20, [[2]] * 12)
        self.assertEqual(result["removed_count"], 20)
        self.assertEqual(result["added_count"], 12)
        self.assertEqual(len(result["removed"]), 8)
        self.assertEqual(len(result["added"]), 8)

    def example(self):
        sections = [Record(index=0, name="", flags=0),
                    Record(index=1, name=".text", flags=6, type=1, size=4,
                           addralign=4, entsize=0, data=bytes.fromhex("4e800020")),
                    Record(index=2, name=".data", flags=3, type=1, size=4,
                           addralign=4, entsize=0, data=b"abcd")]
        symbols = [Record(index=0, name="function", shndx=1, value=0, size=4,
                          type=2, bind=1, info=18, other=0),
                   Record(index=1, name="global", shndx=2, value=0, size=4,
                          type=1, bind=1, info=17, other=0)]
        relocations = [Record(target_section_index=1, offset=0, type=1, addend=0, symbol_index=1)]
        return Record(data=b"object", flags=0, sections=sections, symbols=symbols, relocations=relocations)

    def test_equal_owner_is_not_link_verified(self):
        owner = self.example()
        result = compare_objects(owner, copy.deepcopy(owner))
        self.assertTrue(result["object_verified"])
        self.assertFalse(result["link_verified"])

    def test_identical_functions_do_not_hide_data_or_layout_changes(self):
        owner = self.example()
        for attribute, value in (("data", b"abce"), ("addralign", 8), ("flags", 2), ("type", 8)):
            with self.subTest(attribute=attribute):
                candidate = copy.deepcopy(owner)
                setattr(candidate.sections[2], attribute, value)
                self.assertFalse(compare_objects(owner, candidate)["object_verified"])

    def test_symbol_binding_visibility_and_identity_are_required(self):
        owner = self.example()
        for attribute, value in (("info", 1), ("other", 2), ("name", "different"), ("size", 0)):
            with self.subTest(attribute=attribute):
                candidate = copy.deepcopy(owner)
                setattr(candidate.symbols[1], attribute, value)
                self.assertFalse(compare_objects(owner, candidate)["object_verified"])

    def test_relocation_target_addend_and_type_are_required(self):
        owner = self.example()
        for attribute, value in (("symbol_index", 0), ("addend", 4), ("type", 109)):
            with self.subTest(attribute=attribute):
                candidate = copy.deepcopy(owner)
                setattr(candidate.relocations[0], attribute, value)
                self.assertFalse(compare_objects(owner, candidate)["object_verified"])

    def test_symbol_table_indices_are_not_link_identity(self):
        owner = self.example()
        candidate = copy.deepcopy(owner)
        candidate.symbols.reverse()
        for index, symbol in enumerate(candidate.symbols):
            symbol.index = index
        candidate.relocations[0].symbol_index = 0
        self.assertTrue(compare_objects(owner, candidate)["object_verified"])

    def test_extra_function_and_removed_bss_are_not_complete_units(self):
        owner = self.example()
        candidate = copy.deepcopy(owner)
        extra = copy.deepcopy(candidate.symbols[0])
        extra.name = "extra_function"
        extra.index = 2
        candidate.symbols.append(extra)
        self.assertFalse(compare_objects(owner, candidate)["object_verified"])
        candidate = copy.deepcopy(owner)
        owner.sections[2].type = candidate.sections[2].type = 8
        owner.sections[2].data = candidate.sections[2].data = b""
        owner.sections[2].size = 8
        self.assertFalse(compare_objects(owner, candidate)["object_verified"])

    def test_unknown_semantics_fail_closed(self):
        for change in ("implicit_addend", "section_flags", "duplicate_name", "truncated"):
            with self.subTest(change=change):
                owner = self.example()
                if change == "implicit_addend":
                    owner.relocations[0].addend = None
                elif change == "section_flags":
                    owner.sections[1].flags |= 0x200
                elif change == "duplicate_name":
                    owner.sections[2].name = ".text"
                else:
                    owner.sections[1].data = b""
                result = compare_objects(owner, owner)
                self.assertFalse(result["object_verified"])
                self.assertEqual(result["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
