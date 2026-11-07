import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_campaign_asm_inputs as asm


class AssemblyInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = ('.text\n.fn f, global\n'
                       '/* 80000000 00000000  3C 60 00 00 */ lis r3, jumptable_x@ha\n'
                       '/* 80000004 00000004  38 63 00 00 */ addi r3, r3, jumptable_x@l\n'
                       '/* 80000008 00000008  4E 80 04 20 */ bctr\n'
                       '/* 8000000C 0000000C  4E 80 00 20 */ blr\n.endfn f\n').encode()

    def test_external_offset_table_uses_real_instruction_addresses(self):
        table = '.obj jumptable_x, global\n .4byte f+0xC\n .4byte f+0x8\n.endobj jumptable_x'
        with patch.object(asm, 'jump_tables', return_value={'jumptable_x': [table]}):
            transformed, info = asm.prepare(self.root, 'f', self.source)
        self.assertIn(b'.L_m2c_8000000C:\n/* 8000000C', transformed)
        self.assertIn(b'.4byte .L_m2c_80000008', transformed)
        self.assertEqual(info['added_tables'], ['jumptable_x'])
        self.assertEqual(info['normalized_entries'], 2)
        self.assertEqual(info['added_labels'], 2)
        self.assertEqual(self.source.count(b'.L_m2c_'), 0)

    def test_unresolved_variant_is_not_selected(self):
        invalid = '.obj jumptable_x, local\n .rel f, .L_missing\n.endobj jumptable_x'
        valid = '.obj jumptable_x, global\n .4byte f+0xC\n.endobj jumptable_x'
        with patch.object(asm, 'jump_tables', return_value={'jumptable_x': [invalid, valid]}):
            transformed, info = asm.prepare(self.root, 'f', self.source)
        self.assertNotIn(b'.L_missing', transformed)
        self.assertEqual(info['missing_tables'], [])
        self.assertEqual(info['normalized_entries'], 1)

    def test_cases_outside_captured_function_are_not_invented(self):
        table = '.obj jumptable_x, global\n .4byte f+0x100\n.endobj jumptable_x'
        with patch.object(asm, 'jump_tables', return_value={'jumptable_x': [table]}):
            transformed, info = asm.prepare(self.root, 'f', self.source)
        self.assertEqual(info['missing_tables'], ['jumptable_x'])
        self.assertNotIn(b'.4byte', transformed)

    def test_existing_inline_offset_table_is_normalized_not_duplicated(self):
        table = b'.data\n.obj jumptable_x, global\n .4byte f+0xC\n.endobj jumptable_x\n'
        with patch.object(asm, 'jump_tables', return_value={}):
            transformed, info = asm.prepare(self.root, 'f', self.source + table)
        self.assertEqual(transformed.count(b'.obj jumptable_x'), 1)
        self.assertEqual(info['added_tables'], [])
        self.assertIn(b'.4byte .L_m2c_8000000C', transformed)

    def test_already_named_relative_cases_preserved(self):
        source = self.source.replace(b'/* 8000000C', b'.L_case:\n/* 8000000C')
        table = '.obj jumptable_x, local\n .rel f, .L_case\n.endobj jumptable_x'
        with patch.object(asm, 'jump_tables', return_value={'jumptable_x': [table]}):
            transformed, info = asm.prepare(self.root, 'f', source)
        self.assertIn(b'.rel f, .L_case', transformed)
        self.assertEqual(info['normalized_entries'], 0)

    def test_only_tables_referenced_by_assigned_function_are_added(self):
        source = self.source + ('.fn sibling, global\n'
                               '/* 80000020 00000020  3C 60 00 00 */ lis r3, jumptable_other@ha\n'
                               '.endfn sibling\n').encode()
        with patch.object(asm, 'jump_tables', return_value={}):
            _, info = asm.prepare(self.root, 'f', source)
        self.assertEqual(info['missing_tables'], ['jumptable_x'])

    def test_quoted_anonymous_switch_table_preserves_symbol_identity(self):
        source = self.source.replace(b'jumptable_x', b'"@87_80310C94"')
        table = '.obj "@87_80310C94", global\n .4byte f+0xC\n .4byte f+0x8\n.endobj "@87_80310C94"'
        with patch.object(asm, 'jump_tables', return_value={'@87_80310C94': [table]}):
            transformed, info = asm.prepare(self.root, 'f', source)
        self.assertEqual(info['added_tables'], ['@87_80310C94'])
        self.assertIn(b'lis r3, "@87_80310C94"@ha', transformed)
        self.assertIn(b'.4byte .L_m2c_8000000C', transformed)

    def test_catalog_excludes_numeric_data_and_retains_named_offset_tables(self):
        directory = self.root / 'build/TEST01/asm'
        directory.mkdir(parents=True)
        (directory / 'data.s').write_text(
            '.obj values, global\n .4byte 0\n .4byte 1\n.endobj values\n'
            '.obj "@table", global\n .4byte f+0x8\n .4byte f+0xC\n.endobj "@table"\n', encoding="utf-8", newline="\n")
        catalog = asm.jump_tables(self.root, 'TEST01')
        self.assertNotIn('values', catalog)
        self.assertIn('@table', catalog)

    def test_catalog_isolated_by_project_version(self):
        for version, table in [('TEST01', 'jumptable_first'), ('TEST02', 'jumptable_second')]:
            directory = self.root / 'build' / version / 'asm'
            directory.mkdir(parents=True)
            (directory / 'table.s').write_text(
                f'.obj {table}, global\n .4byte f+0x8\n .4byte f+0xC\n.endobj {table}\n', encoding="utf-8", newline="\n")
        self.assertIn('jumptable_second', asm.jump_tables(self.root, 'TEST02'))
        self.assertNotIn('jumptable_second', asm.jump_tables(self.root, 'TEST01'))


if __name__ == '__main__':
    unittest.main()
