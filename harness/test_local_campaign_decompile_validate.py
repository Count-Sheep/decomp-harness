"""Draft diagnostics run serially, retain failures and never mutate project source."""

import json
import re
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import local_campaign as campaign
import local_campaign_decompile_validate as validate


class ValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'src').mkdir()
        (self.root / 'build/tools').mkdir(parents=True)
        (self.root / 'build/tools/objdiff-cli').write_text('test tool', encoding="utf-8", newline="\n")
        self.source = '/* prefix longer than the generated signature */\nint f(void) { return 0; }\nint g(void) { return 2; }\n'
        (self.root / 'src/a.c').write_text(self.source, encoding="utf-8", newline="\n")
        self.artifact = self.root / 'draft'
        self.artifact.mkdir()
        (self.artifact / 'draft.c').write_text('int f(void) { return 1; }\n', encoding="utf-8", newline="\n")
        self.row = {'id': 'f-id', 'symbol': 'f', 'unit': 'main/a', 'source': 'src/a.c',
                    'status': 'ok', 'artifacts': str(self.artifact)}
        self.command = 'compiler/mwcceppc.exe -i include -MMD -c src/a.c -o build/a && dep-tool'

    def compiler(self, args, timeout, cwd=None):
        if cwd is not None:
            self.assertEqual(cwd, self.root)
        destination = Path(args[args.index('-o') + 1])
        source = Path(args[args.index('-c') + 1])
        (destination / source.with_suffix('.o').name).write_bytes(b'object')
        return subprocess.CompletedProcess(args, 0, '', '')

    def test_structured_compile_args_redirect_only_source_output(self):
        work = self.root / 'work'
        args, output = validate.compile_args(self.command, work / 'a.c', work / 'objects')
        self.assertEqual(args[args.index('-c') + 1], str(work / 'a.c'))
        self.assertEqual(args[args.index('-o') + 1], str(work / 'objects'))
        self.assertNotIn('-MMD', args)
        self.assertNotIn('dep-tool', args)
        self.assertEqual(output, work / 'objects/a.o')
        self.assertEqual(args[args.index('-i') + 1], str(work))

    def test_windows_ninja_paths_keep_their_backslashes(self):
        command = r'build\compilers\GC\1.0\mwcceppc.exe -i "inc dir" -c src\a.c -o build\a && dep-tool'
        with patch.object(validate.decompile, 'WINDOWS', True):
            args = validate.decompile.split_command(command.split(' && ', 1)[0])
        self.assertEqual(args, [r'build\compilers\GC\1.0\mwcceppc.exe', '-i', 'inc dir',
                                '-c', r'src\a.c', '-o', r'build\a'])

    def test_debug_companion_adds_symbols_without_changing_normal_compile(self):
        source = self.root / 'src/a.c'
        output = self.root / 'objects'
        output.mkdir()
        with patch.object(campaign, 'checked_command', side_effect=self.compiler):
            _, _, normal = validate.run_compile(self.command, source, output, [], self.root)
            _, _, debug = validate.run_compile(self.command, source, output, [], self.root, sym_on=True)
        self.assertNotIn('-sym', normal)
        self.assertEqual(debug[debug.index('-sym') + 1], 'on')

    def test_wrapper_compiler_reads_staged_owner_not_original_owner(self):
        (self.root / 'src/b.c').write_text('#include "src/a.c"\n', encoding="utf-8", newline="\n")
        self.row['source'] = 'src/b.c'
        (self.artifact / 'draft.c').write_text('int f(void) { DRAFT_SYNTAX_ERROR; }\n', encoding="utf-8", newline="\n")
        command = self.command.replace('src/a.c', 'src/b.c')
        resolved = []

        def mwcc_search(args, timeout, cwd=None):
            self.assertEqual(cwd, self.root)
            source = Path(args[args.index('-c') + 1])
            included = re.search(r'#include "([^"]+)"', source.read_text(encoding="utf-8"))
            directories = [Path(args[i + 1]) for i, arg in enumerate(args) if arg == '-i']
            owner = next(directory / included[1] for directory in directories
                         if (directory / included[1]).is_file())
            resolved.append(owner)
            if 'DRAFT_SYNTAX_ERROR' in owner.read_text(encoding="utf-8"):
                return subprocess.CompletedProcess(args, 1, '', 'draft rejected')
            return self.compiler(args, timeout, cwd)

        with patch.object(campaign, 'ROOT', self.root), \
             patch.object(campaign, 'checked_command', side_effect=mwcc_search):
            result = validate.validate_one(self.root, self.row, command, {}, '', self.root / 'check')
        self.assertEqual(result['status'], 'compile_error')
        self.assertTrue(result['baseline']['compile_ok'])
        self.assertTrue(all(self.root / 'src' != path.parent for path in resolved))
        self.assertEqual((self.root / 'src/a.c').read_text(encoding="utf-8"), self.source)

    def test_long_artifact_paths_compile_through_short_lived_alias(self):
        # Long enough to need the alias, short enough for hosts without long-path support.
        folder = self.root / ('long-' + 'a' * 60) / ('long-' + 'b' * 60)
        source = folder / 'overlay/src/a.c'
        source.parent.mkdir(parents=True)
        source.write_text('int f(void) { return 1; }', encoding="utf-8", newline="\n")
        output = folder / 'objects'
        output.mkdir()
        observed = []

        def compile_short(args, timeout, cwd=None):
            staged = Path(args[args.index('-c') + 1])
            self.assertLess(len(str(staged)), 150)
            self.assertEqual(staged.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
            observed.append(staged)
            return self.compiler(args, timeout, cwd)

        with patch.object(campaign, 'checked_command', side_effect=compile_short):
            result, obj, args = validate.run_compile(self.command, source, output, [])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(obj, output / 'a.o')
        self.assertTrue(obj.is_file())
        self.assertFalse(observed[0].exists())
        self.assertTrue(source.exists())

    def test_overlay_rewrites_wrapper_includes_to_short_relative_paths(self):
        (self.root / 'src/b.c').write_text('#define VARIANT\n#include "src/a.c"\n', encoding="utf-8", newline="\n")
        with patch.object(campaign, 'ROOT', self.root):
            owner, files = validate.snapshot_owner(self.root, 'src/b.c', 'f')
            stage = validate.stage_owner(self.root, 'src/b.c', owner, files, 'f',
                                         'int f(void) { return 9; }', self.root / 'overlay')
        self.assertIn('#include "a.c"', stage.read_text(encoding="utf-8"))
        self.assertIn('int g(void) { return 2; }', (stage.parent / 'a.c').read_text(encoding="utf-8"))
        self.assertIn('return 9;', (stage.parent / 'a.c').read_text(encoding="utf-8"))
        self.assertEqual((self.root / 'src/a.c').read_text(encoding="utf-8"), self.source)

    def test_baseline_uses_original_function_offsets_not_draft_offsets(self):
        with patch.object(campaign, 'ROOT', self.root), \
             patch.object(campaign, 'checked_command', side_effect=self.compiler):
            result = validate.validate_one(self.root, self.row, self.command, {}, '', self.root / 'check')
        baseline = Path(result['baseline']['artifacts']) / 'overlay/src/a.c'
        self.assertEqual(baseline.read_text(encoding="utf-8"), self.source)
        self.assertTrue(result['baseline']['compile_ok'])
        self.assertEqual(result['status'], 'compiled_without_target')

    def test_owner_helper_rewrite_is_staged_but_baseline_stays_original(self):
        source = 'static inline int helper(void) { return 1; }\nint f(void) { return helper(); }\n'
        (self.root / 'src/a.c').write_text(source, encoding="utf-8", newline="\n")
        rewrite = {'path': 'src/a.c', 'symbol': 'helper',
                   'source': 'static inline int helper(void) { return 9; }\n'}
        with patch.object(campaign, 'ROOT', self.root), \
             patch.object(campaign, 'checked_command', side_effect=self.compiler):
            result = validate.validate_one(self.root, self.row, self.command, {}, '',
                                           self.root / 'check', owner_rewrites=[rewrite])
        self.assertIn('return 9', (self.root / 'check/overlay/src/a.c').read_text(encoding="utf-8"))
        baseline = Path(result['baseline']['artifacts']) / 'overlay/src/a.c'
        self.assertIn('return 1', baseline.read_text(encoding="utf-8"))
        self.assertNotIn('return 9', baseline.read_text(encoding="utf-8"))
        self.assertEqual(result['owner_rewrites'][0]['symbol'], 'helper')

    def test_owner_helper_rewrite_must_preserve_signature(self):
        files = {'src/a.c': 'static inline int helper(void) { return 1; }\nint f(void) { return helper(); }\n'}
        rewrite = {'path': 'src/a.c', 'symbol': 'helper',
                   'source': 'static inline unsigned helper(void) { return 1; }\n'}
        with self.assertRaisesRegex(ValueError, 'changed signature'):
            validate.apply_owner_rewrites(files, 'f', [rewrite])

    def test_debug_mismatch_lines_are_relative_to_candidate_function(self):
        target = self.root / 'target.o'
        target.write_bytes(b'target')
        measured = {'status': 'non_exact', 'pct': 99.0, 'mismatch_classification': {
            'counts': {'register': 1}, 'total': 1, 'register_only': True,
            'mismatches': [{'index': 0, 'target': 'mr r3, r4', 'candidate': 'mr r3, r5',
                            'source_line': 5, 'kind': 'register'}]}}
        with patch.object(campaign, 'ROOT', self.root), \
             patch.object(campaign, 'checked_command', side_effect=self.compiler), \
             patch.object(validate, 'score_object', return_value=measured):
            result = validate.validate_one(self.root, self.row, self.command,
                                           {'target_path': 'target.o'}, '', self.root / 'check',
                                           source_lines=True)
        mismatch = result['mismatch_classification']['mismatches'][0]
        self.assertTrue(result['debug_compile_ok'])
        self.assertEqual(mismatch['owner_source_line'], 5)
        self.assertEqual(mismatch['source_line'], 1)

    def test_debug_companion_is_skipped_for_structural_mismatch(self):
        target = self.root / 'target.o'
        target.write_bytes(b'target')
        measured = {'status': 'non_exact', 'pct': 80.0, 'mismatch_classification': {
            'counts': {'opcode': 1}, 'total': 1, 'register_only': False,
            'mismatches': [{'index': 0, 'target': 'add r3, r4, r5',
                            'candidate': 'subf r3, r5, r4', 'source_line': None,
                            'kind': 'opcode'}]}}
        with patch.object(campaign, 'ROOT', self.root), \
             patch.object(campaign, 'checked_command', side_effect=self.compiler) as compiler, \
             patch.object(validate, 'score_object', return_value=measured):
            result = validate.validate_one(self.root, self.row, self.command,
                                           {'target_path': 'target.o'}, '', self.root / 'check',
                                           source_lines=True)
        self.assertEqual(result['debug_compile_skipped'], 'mismatch_not_register_allocatable')
        self.assertNotIn('debug_compile_ok', result)
        self.assertEqual(compiler.call_count, 2)  # baseline and candidate

    def test_owner_field_macro_precedes_guarded_fallback(self):
        macro = '#define M2C_FIELD(base, type, offset) (*(type)((char *)(base) + (offset)))\n'
        source = macro + self.source
        staged = validate.stage_owner(self.root, 'src/a.c', 'src/a.c', {'src/a.c': source},
                                      'f', 'int f(void) { return 9; }', self.root / 'overlay')
        text = staged.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(macro))
        self.assertIn(validate.FIELD_FALLBACK, text)
        self.assertLess(text.index(macro), text.index(validate.FIELD_FALLBACK))

    def test_untouched_baseline_keeps_original_trailing_whitespace(self):
        source = self.source.replace('return 0; }', 'return 0; }  \t')
        staged = validate.stage_owner(self.root, 'src/a.c', 'src/a.c', {'src/a.c': source},
                                      'f', None, self.root / 'overlay')
        self.assertEqual(staged.read_text(encoding="utf-8"), source)

    def test_compiler_failure_is_recorded_and_source_unchanged(self):
        failure = subprocess.CompletedProcess([], 1, '', 'MWCC syntax error')
        with patch.object(campaign, 'ROOT', self.root), \
             patch.object(campaign, 'checked_command', return_value=failure):
            result = validate.validate_one(self.root, self.row, self.command, {}, '', self.root / 'check')
        self.assertEqual(result['status'], 'baseline_compile_error')
        self.assertIn('MWCC syntax error', result['error'])
        self.assertEqual((self.root / 'src/a.c').read_text(encoding="utf-8"), self.source)

    def test_unsupported_operations_are_not_stubbed_to_zero(self):
        (self.artifact / 'draft.c').write_text('int f(void) { return M2C_ERROR("unset"); }', encoding="utf-8", newline="\n")
        with patch.object(campaign, 'ROOT', self.root), \
             patch.object(campaign, 'checked_command', side_effect=self.compiler):
            result = validate.validate_one(self.root, self.row, self.command, {}, '', self.root / 'check')
        self.assertIn('M2C_ERROR', result['unresolved_macros'])
        generated = (self.root / 'check/overlay/src/a.c').read_text(encoding="utf-8")
        self.assertNotIn('#define M2C_ERROR', generated)
        self.assertIn('M2C_ERROR("unset")', generated)

    def test_empty_failure_comment_cannot_pass_as_a_compiled_draft(self):
        (self.artifact / 'draft.c').write_text('/* m2c failed */\n', encoding="utf-8", newline="\n")
        with patch.object(campaign, 'ROOT', self.root), \
             patch.object(campaign, 'checked_command', side_effect=self.compiler):
            result = validate.validate_one(self.root, self.row, self.command, {}, '', self.root / 'check')
        self.assertEqual(result['status'], 'no_generated_function')

    def test_context_transfer_stays_unresolved_and_cannot_be_review_eligible(self):
        (self.artifact / 'draft.c').write_text(
            'int f(void) { M2C_UNRESOLVED_CONTEXT_TRANSFER(target, lr, sp); }', encoding="utf-8", newline="\n")
        with patch.object(campaign, 'ROOT', self.root), \
             patch.object(campaign, 'checked_command', side_effect=self.compiler):
            result = validate.validate_one(self.root, self.row, self.command, {}, '', self.root / 'check')
        self.assertIn('M2C_UNRESOLVED_CONTEXT_TRANSFER', result['unresolved_macros'])
        generated = (self.root / 'check/overlay/src/a.c').read_text(encoding="utf-8")
        self.assertNotIn('#define M2C_UNRESOLVED_CONTEXT_TRANSFER', generated)
        self.assertFalse(result['review_eligible'])
        self.assertFalse(result['promoted'])

    def test_changed_environment_invalidates_cached_validation(self):
        with patch.object(campaign, 'ROOT', self.root), \
             patch.object(campaign, 'checked_command', side_effect=self.compiler) as compiler:
            first = validate.validate_one(self.root, self.row, self.command, {}, '', self.root / 'check', environment='first')
            (self.root / 'check/result.json').write_text(json.dumps(first), encoding="utf-8", newline="\n")
            again = validate.validate_one(self.root, self.row, self.command, {}, '', self.root / 'check', environment='first')
            self.assertEqual(compiler.call_count, 2)
            second = validate.validate_one(self.root, self.row, self.command, {}, '', self.root / 'check', environment='changed')
        self.assertEqual(first, again)
        self.assertNotEqual(first['input_sha256'], second['input_sha256'])
        self.assertEqual(compiler.call_count, 4)

    def test_header_content_is_part_of_environment_fingerprint(self):
        (self.root / 'include').mkdir()
        header = self.root / 'include/types.h'
        header.write_text('typedef int Value;', encoding="utf-8", newline="\n")
        first = validate.environment_fingerprint(self.root, {})
        header.write_text('typedef float Value;', encoding="utf-8", newline="\n")
        self.assertNotEqual(validate.environment_fingerprint(self.root, {}), first)

    def test_shared_baseline_is_reused_across_candidate_folders(self):
        with patch.object(campaign, 'ROOT', self.root), \
             patch.object(campaign, 'checked_command', side_effect=self.compiler) as compiler:
            first = validate.validate_one(self.root, self.row, self.command, {}, '', self.root / 'one',
                                          environment='fixed', baseline_root=self.root / 'shared')
            second = validate.validate_one(self.root, self.row, self.command, {}, '', self.root / 'two',
                                           environment='fixed', baseline_root=self.root / 'shared')
        self.assertEqual(compiler.call_count, 3)
        self.assertEqual(first['baseline'], second['baseline'])
        self.assertEqual(first['baseline']['object_sha256'], validate.digest(b'object'))

    def test_shared_baseline_rebuilds_missing_changed_or_corrupted_artifacts(self):
        owner, files = validate.snapshot_owner(self.root, 'src/a.c', 'f')
        with patch.object(campaign, 'checked_command', side_effect=self.compiler) as compiler:
            baseline = validate.compile_baseline(self.root, 'src/a.c', owner, files, 'f', self.command, 'fixed', self.root / 'shared')
            for kind in ('object', 'overlay', 'metadata', 'log'):
                folder = Path(baseline['artifacts'])
                if kind == 'object':
                    Path(baseline['object']).unlink()
                elif kind == 'overlay':
                    (folder / 'overlay/src/a.c').write_text('tampered', encoding="utf-8", newline="\n")
                elif kind == 'metadata':
                    (folder / 'result.json').write_text('invalid JSON', encoding="utf-8", newline="\n")
                else:
                    (folder / 'compile.log').write_text('tampered', encoding="utf-8", newline="\n")
                baseline = validate.compile_baseline(self.root, 'src/a.c', owner, files, 'f', self.command, 'fixed', self.root / 'shared')
            self.assertEqual(compiler.call_count, 5)
        self.assertEqual(Path(baseline['source']).read_text(encoding="utf-8"), self.source)

    def test_bound_negative_baseline_is_reused_without_claiming_a_compile(self):
        owner, files = validate.snapshot_owner(self.root, 'src/a.c', 'f')
        failure = subprocess.CompletedProcess([], 1, '', 'baseline failure')
        with patch.object(campaign, 'checked_command', return_value=failure) as compiler:
            first = validate.compile_baseline(self.root, 'src/a.c', owner, files, 'f', self.command, 'fixed', self.root / 'shared')
            second = validate.compile_baseline(self.root, 'src/a.c', owner, files, 'f', self.command, 'fixed', self.root / 'shared')
            self.assertEqual(compiler.call_count, 1)
        self.assertFalse(first['compile_ok'])
        self.assertEqual(first, second)

    def test_objdiff_near_exact_is_not_exact_and_missing_function_is_not_a_pass(self):
        directory = self.root / 'score'
        directory.mkdir()
        data = {'left': {'symbols': [{'name': 'f', 'match_percent': 99.99999}]},
                'right': {'symbols': [{'name': 'f'}]}}
        def score(args, timeout, cwd=None):
            self.assertEqual(cwd, self.root)
            Path(args[args.index('-o') + 1]).write_text(json.dumps(data), encoding="utf-8", newline="\n")
            return subprocess.CompletedProcess(args, 0, '', '')
        with patch.object(campaign, 'objdiff_cli', return_value=Path('objdiff')), \
             patch.object(campaign, 'checked_command', side_effect=score):
            result = validate.score_object(self.root, Path('retail.o'), Path('draft.o'), 'f', directory)
            self.assertEqual(result['status'], 'non_exact')
            data['left']['symbols'][0]['match_percent'] = 100.0
            self.assertEqual(validate.score_object(self.root, Path('retail.o'), Path('draft.o'), 'f', directory)['status'],
                             'scorer_exact')
            data['right']['symbols'] = []
            self.assertEqual(validate.score_object(self.root, Path('retail.o'), Path('draft.o'), 'f', directory)['status'],
                             'missing_function')

    def test_objdiff_is_bound_to_selected_decomp_root(self):
        directory = self.root / 'score-root'
        directory.mkdir()
        observed = []

        def score(args, timeout, cwd=None):
            observed.append(Path(args[0]))
            output = {'left': {'symbols': [{'name': 'f', 'match_percent': 100.0}]},
                      'right': {'symbols': [{'name': 'f'}]}}
            Path(args[args.index('-o') + 1]).write_text(json.dumps(output), encoding="utf-8", newline="\n")
            return subprocess.CompletedProcess(args, 0, '', '')

        with patch.object(campaign, 'ROOT', self.root / 'wrong-project'), \
             patch.object(campaign, 'objdiff_cli', side_effect=AssertionError('global lookup used')), \
             patch.object(campaign, 'checked_command', side_effect=score):
            result = validate.score_object(self.root, Path('retail.o'), Path('draft.o'), 'f', directory)
        self.assertEqual(result['status'], 'scorer_exact')
        self.assertEqual(observed, [self.root / 'build/tools/objdiff-cli'])

    def test_objdiff_records_instruction_mismatch_classes(self):
        directory = self.root / 'score-classes'
        directory.mkdir()
        instruction = lambda text, line=None: {'instruction': {'formatted': text, 'line_number': line}}
        data = {'left': {'symbols': [{'name': 'f', 'match_percent': 99.5,
                                      'instructions': [instruction('mr r31, r3'), instruction('blr')]}]},
                'right': {'symbols': [{'name': 'f',
                                       'instructions': [instruction('mr r30, r3', 12), instruction('blr', 13)]}]}}

        def score(args, timeout, cwd=None):
            Path(args[args.index('-o') + 1]).write_text(json.dumps(data), encoding="utf-8", newline="\n")
            return subprocess.CompletedProcess(args, 0, '', '')

        with patch.object(campaign, 'objdiff_cli', return_value=Path('objdiff')), \
             patch.object(campaign, 'checked_command', side_effect=score):
            result = validate.score_object(self.root, Path('retail.o'), Path('draft.o'), 'f', directory)
        classification = result['mismatch_classification']
        self.assertTrue(classification['register_only'])
        self.assertEqual(classification['counts'], {'register': 1})
        self.assertEqual(classification['mismatches'][0]['source_line'], 12)

    def test_missing_objdiff_output_does_not_reuse_previous_score(self):
        directory = self.root / 'score'
        directory.mkdir()
        (directory / 'objdiff.json').write_text('{"left":{"symbols":[{"name":"f","match_percent":100}]}}', encoding="utf-8", newline="\n")
        with patch.object(campaign, 'objdiff_cli', return_value=Path('objdiff')), \
             patch.object(campaign, 'checked_command', return_value=subprocess.CompletedProcess([], 0, '', '')):
            result = validate.score_object(self.root, Path('retail.o'), Path('draft.o'), 'f', directory)
        self.assertEqual(result['status'], 'objdiff_error')


if __name__ == '__main__':
    unittest.main()
