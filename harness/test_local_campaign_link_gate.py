import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import local_campaign_link_gate as gate


class LinkGateTests(unittest.TestCase):
    def commands(self, objects='obj/a.o "obj/b with spaces.o"'):
        prefix = 'build/tools/wibo build/compilers/GC/1.2.5n/mwldeppc.exe -lcf build/main.lcf -o build/main.elf '
        plain = [{'output': 'build/main.elf', 'command': prefix + '@build/main.elf.rsp'},
                 {'output': 'build/main.dol', 'command': 'build/tools/dtk elf2dol build/main.elf build/main.dol'}]
        expanded = [{'output': 'build/main.elf', 'command': prefix + objects}]
        return plain, expanded

    def test_compdb_response_expansion_preserves_input_order_and_spaces(self):
        plan = gate.parse_commands(*self.commands(), 'build/main.elf')
        self.assertEqual(plan['objects'], ['obj/a.o', 'obj/b with spaces.o'])
        self.assertEqual(plan['response_file'], 'build/main.elf.rsp')
        self.assertEqual(plan['dol'], 'build/main.dol')

    def test_reject_unattested_flags_and_shell_operators(self):
        plain, expanded = self.commands()
        for extra in ('-map', '&& evil'):
            changed = [dict(row) for row in plain]
            changed[0]['command'] = changed[0]['command'].replace('@build/main.elf.rsp', extra + ' @build/main.elf.rsp')
            wide = [dict(row) for row in expanded]
            wide[0]['command'] = wide[0]['command'].replace('obj/a.o', extra + ' obj/a.o')
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                gate.parse_commands(changed, wide, 'build/main.elf')

    def test_native_windows_linker_without_wrapper_is_accepted(self):
        plain, expanded = self.commands()
        for row in [plain[0], expanded[0]]:
            row['command'] = row['command'].replace('build/tools/wibo ', '')
        plain[1]['command'] = 'build/tools/dtk.exe elf2dol build/main.elf build/main.dol'
        plan = gate.parse_commands(plain, expanded, 'build/main.elf')
        self.assertEqual(plan['link_command'][0], 'build/compilers/GC/1.2.5n/mwldeppc.exe')
        self.assertEqual(plan['tools'], ['build/compilers/GC/1.2.5n/mwldeppc.exe', 'build/tools/dtk.exe'])
        self.assertEqual(plan['objects'], ['obj/a.o', 'obj/b with spaces.o'])

    def test_wrapped_linker_captures_the_wrapper_as_a_tool(self):
        plan = gate.parse_commands(*self.commands(), 'build/main.elf')
        self.assertEqual(plan['tools'], ['build/tools/wibo', 'build/compilers/GC/1.2.5n/mwldeppc.exe',
                                         'build/tools/dtk'])

    def test_unknown_program_in_place_of_the_linker_is_rejected(self):
        plain, expanded = self.commands()
        for row in [plain[0], expanded[0]]:
            row['command'] = row['command'].replace('build/tools/wibo ', 'build/tools/other ')
        with self.assertRaises(ValueError):
            gate.parse_commands(plain, expanded, 'build/main.elf')

    def test_hardware_fp_prefix_preserved(self):
        plain, expanded = self.commands()
        for row in [plain[0], expanded[0]]:
            row['command'] = row['command'].replace(' -lcf ', ' -fp hardware -nodefaults -lcf ')
        plan = gate.parse_commands(plain, expanded, 'build/main.elf')
        self.assertEqual(plan['link_command'][2:5], ['-fp', 'hardware', '-nodefaults'])
        self.assertEqual(plan['linker_script'], 'build/main.lcf')
        for row in [plain[0], expanded[0]]:
            row['command'] = row['command'].replace('-fp hardware', '-fp software')
        with self.assertRaises(ValueError):
            gate.parse_commands(plain, expanded, 'build/main.elf')

    def test_reject_duplicate_missing_external_and_nonobject_inputs(self):
        for objects in ('', 'obj/a.o obj/a.o', '../outside.o', '/outside.o', 'file.a', '@nested.o', '-hidden.o'):
            with self.subTest(objects=objects), self.assertRaises(ValueError):
                gate.parse_commands(*self.commands(objects), 'build/main.elf')

    def test_reject_ambiguous_command_or_wrong_converter(self):
        plain, expanded = self.commands()
        with self.assertRaises(ValueError):
            gate.parse_commands(plain + [plain[0]], expanded, 'build/main.elf')
        plain[1]['command'] = 'other-tool elf2dol build/main.elf build/main.dol'
        with self.assertRaises(ValueError):
            gate.parse_commands(plain, expanded, 'build/main.elf')

    def test_checksum_is_exact_and_unambiguous(self):
        digest = 'a' * 40
        text = digest + '  build/main.dol\n' + 'b' * 40 + '  build/other.dol\n'
        self.assertEqual(gate.expected_checksum(text, 'build/main.dol'), digest)
        with self.assertRaises(ValueError):
            gate.expected_checksum(text + digest + '  build/main.dol\n', 'build/main.dol')
        with self.assertRaises(ValueError):
            gate.expected_checksum(text, 'build/missing.dol')

    def test_execution_hashes_actual_output_and_deduplicates_identical_dols(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            workspace = root / 'workspace'
            workspace.mkdir()
            payload = b'generated DOL output'
            plan = {'elf': 'build/main.elf', 'dol': 'build/main.dol', 'response_file': 'build/main.elf.rsp',
                    'objects': ['obj/a.o', 'obj/b with spaces.o'], 'link_command': ['link'],
                    'conversion_command': ['convert'], 'expected_sha1': hashlib.sha1(payload).hexdigest()}
            def run(command, **kwargs):
                if command == ['link']:
                    (workspace / plan['elf']).write_bytes(b'generated ELF')
                else:
                    (workspace / plan['dol']).write_bytes(payload)
                return subprocess.CompletedProcess(command, 0, '', '')
            with patch.object(gate.subprocess, 'run', side_effect=run):
                first = gate.link_once(workspace, plan, root / 'baseline')
                second = gate.link_once(workspace, plan, root / 'candidate')
            self.assertTrue(first['passed'])
            self.assertTrue(second['passed'])
            self.assertEqual(first['sha1'], plan['expected_sha1'])
            self.assertEqual((root / 'baseline/main.dol').stat().st_ino,
                             (root / 'candidate/main.dol').stat().st_ino)
            self.assertIn('"obj/b with spaces.o"', (workspace / plan['response_file']).read_text(encoding="utf-8"))
            blob = next((root / 'dol-cache').iterdir())
            blob.write_bytes(b'corrupted cached artifact')
            with patch.object(gate.subprocess, 'run', side_effect=run):
                changed = gate.link_once(workspace, plan, root / 'changed')
            self.assertFalse(changed['passed'])
            self.assertEqual(changed['status'], 'output_artifact_changed')

    def test_link_failure_cannot_pass_using_stale_dol_and_skips_conversion(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            workspace = root / 'workspace'
            workspace.mkdir()
            (workspace / 'main.dol').write_bytes(b'stale output')
            plan = {'elf': 'main.elf', 'dol': 'main.dol', 'response_file': 'main.elf.rsp',
                    'objects': ['a.o'], 'link_command': ['link'], 'conversion_command': ['convert']}
            with patch.object(gate.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'bad link')) as run:
                result = gate.link_once(workspace, plan, root / 'failure')
            self.assertFalse(result['passed'])
            self.assertEqual(result['status'], 'link_error')
            run.assert_called_once()
            self.assertFalse((workspace / 'main.dol').exists())


if __name__ == '__main__':
    unittest.main()
