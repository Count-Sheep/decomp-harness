from types import SimpleNamespace as Record
import unittest

from m2c_unit_source import assemble, m2c_available

M2C = m2c_available()


class UnitDataTests(unittest.TestCase):
    def pointer_owner(self):
        owner = self.owner(payload=bytes(8))
        owner.symbols.append(Record(index=1, name="target", type=1, shndx=0, bind=1, other=0, value=0, size=0))
        owner.relocations.append(Record(target_section_index=1, offset=0, type=1, addend=0, symbol_index=1))
        return owner

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_absolute_pointer_table_and_null_entry(self):
        source, recovered = self.generate("extern int target; extern int *values[];", self.pointer_owner())
        self.assertIn("int *values[2] = {(int *) (&target), (int *) 0};", source)
        self.assertEqual(recovered[0]["kind"], "absolute_pointer_storage")
        self.assertEqual(recovered[0]["relocations"], [{"offset": 0, "type": 1, "symbol": "target", "addend": 0}])

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_pointer_alias_and_function_address(self):
        owner = self.pointer_owner()
        owner.symbols[1].type = 2
        source, recovered = self.generate("int target(void); typedef int (*Handler)(void); extern Handler values[2];", owner)
        self.assertEqual(len(recovered), 1)
        self.assertIn("&target", source)
        self.assertNotIn("extern Handler values", source)

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_ambiguous_or_unsupported_pointer_relocations_are_not_recovered(self):
        for attr, value in (("addend", 4), ("type", 4), ("offset", 2), ("symbol_index", 100)):
            owner = self.pointer_owner()
            setattr(owner.relocations[0], attr, value)
            with self.subTest(attr=attr):
                self.assertEqual(self.generate("extern int target; extern int *values[2];", owner)[1], [])
        owner = self.pointer_owner()
        owner.relocations.append(owner.relocations[0])
        self.assertEqual(self.generate("extern int target; extern int *values[2];", owner)[1], [])
        self.assertEqual(self.generate("extern int *values[2];", self.pointer_owner())[1], [])
        owner = self.pointer_owner()
        owner.symbols.append(Record(**{**vars(owner.symbols[1]), "index": 2}))
        self.assertEqual(self.generate("extern int target; extern int *values[2];", owner)[1], [])

    def owner(self, payload=b"\x01\x02\xff\xfe", binding=1, bss=False):
        section = Record(index=1, name=".bss" if bss else ".data", type=8 if bss else 1,
                         flags=3, size=len(payload), data=b"" if bss else payload)
        symbol = Record(index=0, name="values", type=1, shndx=1, bind=binding,
                        other=0, value=0, size=len(payload))
        return Record(sections=[Record(flags=0), section], symbols=[symbol], relocations=[])

    def generate(self, declaration, owner):
        recovered = []
        source = assemble([{"symbol": "f", "context": declaration,
                            "source": "int f(void) { return 0; }"}], ["f"],
                          data_owner=owner, recovered_data=recovered)
        return source, recovered

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_big_endian_signed_array_and_binding(self):
        source, recovered = self.generate("extern signed short values[2];", self.owner(binding=0))
        self.assertIn("static signed short values[2] = {258, -2};", source)
        self.assertEqual(recovered[0]["element_width"], 2)

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_alias_width_and_unsized_array_from_symbol_extent(self):
        source, recovered = self.generate("typedef unsigned char u8; extern u8 values[];", self.owner())
        self.assertIn("u8 values[4] = {1U, 2U, 255U, 254U};", source)
        self.assertEqual(len(recovered), 1)

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_bss_is_tentative_storage_not_file_bytes(self):
        source, recovered = self.generate("extern unsigned int values;", self.owner(bss=True))
        self.assertIn("unsigned int values;", source)
        self.assertNotIn("extern", source)
        self.assertTrue(recovered[0]["zero_fill"])

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_multidimensional_row_major_integer_array(self):
        source, recovered = self.generate("extern signed short values[2][2];",
                                          self.owner(payload=b"\x00\x01\xff\xfe\x01\x02\x00\x04"))
        self.assertIn("values[2][2] = {{1, -2}, {258, 4}};", source)
        self.assertEqual(recovered[0]["dimensions"], [2, 2])

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_unsized_outer_dimension_and_three_dimensions(self):
        source, recovered = self.generate("typedef unsigned char u8; extern u8 values[][1][2];", self.owner())
        self.assertIn("values[2][1][2] = {{{1U, 2U}}, {{255U, 254U}}};", source)
        self.assertEqual(recovered[0]["dimensions"], [2, 1, 2])

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_multidimensional_bss_preserves_layout_and_local_binding(self):
        source, recovered = self.generate("extern unsigned char values[][2];", self.owner(binding=0, bss=True))
        self.assertIn("static unsigned char values[2][2];", source)
        self.assertTrue(recovered[0]["zero_fill"])

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_readonly_multidimensional_storage_keeps_const(self):
        owner = self.owner()
        owner.sections[1].flags = 2
        owner.sections[1].name = ".rodata"
        source, recovered = self.generate("extern const unsigned char values[2][2];", owner)
        self.assertIn("const unsigned char values[2][2] = {{1U, 2U}, {255U, 254U}};", source)
        self.assertEqual(recovered[0]["section"], ".rodata")

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_multidimensional_relocation_storage_is_not_treated_as_integers(self):
        owner = self.owner()
        owner.relocations.append(Record(target_section_index=1, offset=0))
        source, recovered = self.generate("extern unsigned char values[2][2];", owner)
        self.assertEqual(recovered, [])
        self.assertIn("extern", source)

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_invalid_multidimensional_extents_are_not_guessed(self):
        for declaration in ("extern short values[2][2];", "extern short values[][3];",
                            "extern short values[2][];", "extern short values[][0];",
                            "extern short values[][1+1];", "extern short values[0][2];"):
            with self.subTest(declaration=declaration):
                source, recovered = self.generate(declaration, self.owner())
                self.assertEqual(recovered, [])
                self.assertIn("extern", source)

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_unknown_types_pointers_size_conflicts_not_recovered(self):
        for declaration in ("extern void *values;", "extern char values[4];",
                            "extern unsigned short values[3];", "extern float values;"):
            with self.subTest(declaration=declaration):
                source, recovered = self.generate(declaration, self.owner())
                self.assertEqual(recovered, [])
                self.assertIn("extern", source)

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_relocations_and_overlapping_symbols_are_not_raw_data(self):
        owner = self.owner()
        owner.relocations.append(Record(target_section_index=1, offset=0))
        self.assertEqual(self.generate("extern int values;", owner)[1], [])
        owner = self.owner()
        owner.symbols.append(Record(index=1, name="alias", type=1, shndx=1, value=0, size=4))
        self.assertEqual(self.generate("extern int values;", owner)[1], [])


if __name__ == "__main__":
    unittest.main()
