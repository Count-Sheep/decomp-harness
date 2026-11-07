"""Prompt context must describe the source being edited, not an earlier object."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_campaign as campaign
from local_campaign_coordination import Coordinator


class PromptTests(unittest.TestCase):
    def test_near_exact_first_attempt_uses_focused_edit_and_records_timings(self):
        source = 'int f(int a) {\n    int b;\n    b = a + 1;\n    return b;\n}\n'
        (self.root / 's.c').write_text(source)
        state = campaign.blank_state('unused', 'unused')
        item = {'id': 'task', 'symbol': 'f', 'source': 's.c', 'base_pct': 99.5,
                'source_sha256': campaign.digest(source), 'status': 'pending'}
        state['items']['task'] = item
        def build(item, current, worker):
            item.update(prompt_pct=99.5, structural_hunks=1,
                        focus_hunks=[[3]], focus_hunk_texts=['[structural] mismatch'])
            return 'brief', '## Assigned function source\n' + current
        def generate(host, model, prompt, *args, **kwargs):
            self.assertIn('## Focused edit', prompt)
            self.assertIn('fresh baseline is near exact', prompt)
            return '```c\n    int b;\n    b = 1 + a;\n    return b;\n```'
        with patch.object(campaign, 'ROOT', self.root), patch.object(campaign, 'STATE_DIR', self.root), \
             patch.object(campaign, 'STATE_FILE', self.root / 'state.json'), \
             patch.object(campaign, 'COORDINATOR', Coordinator(self.root)), \
             patch.object(campaign, 'build_prompt', side_effect=build), \
             patch.object(campaign, 'ollama', side_effect=generate), \
             patch.object(campaign, 'verify', return_value={'pct': 99.6, 'deltas': 1, 'diff_feedback': []}):
            campaign.process(state, item, 'unused', 'unused', 1, 'worker')
        report = campaign.read_json(self.root / 'candidates/task/attempt-001.report.json', {})
        self.assertIn('total', report['stage_seconds'])
        self.assertEqual((self.root / 's.c').read_text(), source)

    def test_focused_contract_retains_policy_without_whole_function_instruction(self):
        prompt = ('## Assigned function source\nint f(void) { return 1; }\n'
                  '## Response contract\nReturn exactly one complete replacement.\n'
                  + campaign.source_policy_contract())
        focused = campaign.focused_prompt(prompt, 'int f(void) {\n return 1;\n}\n', (2, 2))
        self.assertNotIn('Return exactly one complete replacement', focused)
        self.assertIn('Never add pragmas', focused)
        self.assertIn('alternative replacements', focused)

    def test_guidance_distinguishes_register_residue_and_missing_evidence(self):
        register = campaign.refinement_guidance({'prompt_pct': 99.9}, ['[register-only]\nmr'])
        self.assertIn('displayed residue is register-only', register)
        self.assertNotIn('Investigate the first structural', register)
        missing = campaign.refinement_guidance({'prompt_pct': 99.9}, [])
        self.assertIn('Do not infer byte equality', missing)
        self.assertIn('relocation', register)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_scratch_measurement_replaces_stale_score_and_table(self):
        source = 'int f(void) { return 2; }\n'
        brief = ('# Task\n- **Current match**: 100.00%\n- **Instruction deltas**: 0 of 1\n'
                 '## Instruction diff (`>>` = mismatch)\n```\n    0  li r3, 1   li r3, 1\n```\n')
        pair = ({'match_percent': 75.125, 'instructions': [
                    {'instruction': {'formatted': 'li r3, 1'}}]},
                {'instructions': [{'instruction': {'formatted': 'li r3, 2', 'line_number': 1}}]})
        result = subprocess.CompletedProcess([], 0, brief, '')
        tool = self.root / 'gen_brief.py'
        tool.write_text('')
        with patch.object(campaign, 'BRIEF_TOOL', tool), \
             patch.object(campaign.subprocess, 'run', return_value=result), \
             patch.object(campaign, '_scratch_symbols', return_value=pair) as scratch, \
             patch.object(campaign, 'type_context', return_value=''), \
             patch.object(campaign, 'same_file_context', return_value=''), \
             patch.object(campaign, 'corpus_context', return_value=''), \
             patch.object(campaign, 'library_reference', return_value=''), \
             patch.object(campaign, 'register_replay_section', return_value=''):
            measured, prompt = campaign.build_prompt_unlocked({'symbol': 'f', 'source': 's.c'}, source)
        self.assertIn('75.125000%', measured)
        self.assertIn('1 of 1', measured)
        self.assertNotIn('100.00%', prompt)
        self.assertIn('li r3, 2', measured)
        self.assertIn('int f(void) { return 2; }', prompt)
        scratch.assert_called_once()

    def test_missing_brief_tool_falls_back_to_scratch_measurement(self):
        pair = ({'match_percent': 50.0, 'instructions': [{'instruction': {'formatted': 'li r3, 1'}}]},
                {'instructions': [{'instruction': {'formatted': 'li r3, 2', 'line_number': 1}}]})
        with patch.object(campaign, 'BRIEF_TOOL', self.root / 'absent.py'), \
             patch.object(campaign.subprocess, 'run', side_effect=AssertionError('brief tool run')), \
             patch.object(campaign, '_scratch_symbols', return_value=pair), \
             patch.object(campaign, 'type_context', return_value=''), \
             patch.object(campaign, 'same_file_context', return_value=''), \
             patch.object(campaign, 'corpus_context', return_value=''), \
             patch.object(campaign, 'library_reference', return_value=''), \
             patch.object(campaign, 'investigation_context', return_value=''), \
             patch.object(campaign, 'assembly_draft_context', return_value=''), \
             patch.object(campaign, 'register_replay_section', return_value=''):
            measured, _ = campaign.build_prompt_unlocked({'symbol': 'f', 'source': 's.c', 'unit': 'u'},
                                                         'int f(void) { return 2; }\n')
        self.assertIn('50.000000%', measured)
        self.assertIn('1 of 1', measured)
        self.assertIn('li r3, 2', measured)

    def test_missing_scratch_measurement_does_not_send_stale_diff(self):
        result = subprocess.CompletedProcess([], 0, '## Instruction diff\n```\nold\n```', '')
        tool = self.root / 'gen_brief.py'
        tool.write_text('')
        with patch.object(campaign, 'BRIEF_TOOL', tool), patch.object(campaign.subprocess, 'run', return_value=result), \
             patch.object(campaign, '_scratch_symbols', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'stale instruction diff'):
                campaign.build_prompt_unlocked({'symbol': 'f', 'source': 's.c'}, 'int f(void) {}')

    def test_prompt_substitution_restores_owner_bytes_even_on_failure(self):
        original = b'int f(void) { return 1; }\r\n'
        changed = 'int f(void) { return 2; }\n'
        owner = self.root / 's.c'
        owner.write_bytes(original)
        item = {'symbol': 'f', 'source': 's.c', 'source_sha256': campaign.digest(owner.read_text())}
        def build(item, source):
            self.assertEqual(owner.read_text(), changed)
            raise RuntimeError('test compilation failure')
        with patch.object(campaign, 'ROOT', self.root), patch.object(campaign, 'STATE_DIR', self.root), \
             patch.object(campaign, 'COORDINATOR', Coordinator(self.root)), \
             patch.object(campaign, 'build_prompt_unlocked', side_effect=build):
            with self.assertRaisesRegex(RuntimeError, 'test compilation failure'):
                campaign.build_prompt(item, changed)
        self.assertEqual(owner.read_bytes(), original)

    def test_whole_function_retry_uses_best_source_and_preserves_other_functions(self):
        source = 'int f(int a) { return a + 1; }\nint sibling(void) { return 7; }\n'
        best = 'int f(int a) { return 1 + a; }\nint sibling(void) { return 999; }\n'
        (self.root / 's.c').write_text(source)
        (self.root / 'best.c').write_text(best)
        state = campaign.blank_state('unused', 'unused')
        item = {'id': 'task', 'symbol': 'f', 'source': 's.c', 'base_pct': 90.0, 'attempts': 1,
                'source_sha256': campaign.digest(source), 'status': 'non_exact',
                'best': {'pct': 95.0, 'attempt': 1, 'candidate': 'best.c'}}
        state['items']['task'] = item
        def build(item, current, worker):
            self.assertIn('return 1 + a;', current)
            self.assertIn('return 7;', current)
            self.assertNotIn('999', current)
            return 'brief', '## Assigned function source\n' + current
        def generate(host, model, prompt, *args, **kwargs):
            self.assertIn('best retained candidate (95.0%)', prompt)
            return 'int f(int a) { int b; b = 1 + a; return b; }'
        with patch.object(campaign, 'ROOT', self.root), patch.object(campaign, 'STATE_DIR', self.root), \
             patch.object(campaign, 'STATE_FILE', self.root / 'state.json'), \
             patch.object(campaign, 'COORDINATOR', Coordinator(self.root)), \
             patch.object(campaign, 'build_prompt', side_effect=build), \
             patch.object(campaign, 'ollama', side_effect=generate), \
             patch.object(campaign, 'verify', return_value={'pct': 96.0, 'deltas': 1, 'diff_feedback': []}):
            campaign.process(state, item, 'unused', 'unused', 1, 'worker', 'feedback')
        self.assertEqual(item['best']['pct'], 96.0)
        candidate = (self.root / 'candidates/task/attempt-002.c').read_text()
        self.assertIn('return 7;', candidate)
        self.assertNotIn('999', candidate)
        self.assertEqual((self.root / 's.c').read_text(), source)

    def test_continuation_rejects_missing_regressed_or_signature_changed_best(self):
        source = 'int f(int a) { return a; }'
        (self.root / 'bad.c').write_text('void f(int a) {}')
        item = {'symbol': 'f', 'source': 's.c', 'base_pct': 90.0}
        with patch.object(campaign, 'ROOT', self.root):
            for score, candidate in ((89.0, 'bad.c'), (95.0, 'missing.c'), (95.0, 'bad.c')):
                item['best'] = {'pct': score, 'candidate': candidate}
                self.assertEqual(campaign.continuation_source(item, source), source)

    def test_already_exact_source_skips_inference_but_still_requires_verification(self):
        source = 'int f(void) { return 1; }\n'
        (self.root / 's.c').write_text(source)
        def build(item, current, worker):
            item['prompt_pct'] = 100.0
            return 'brief', 'prompt'
        for verified_pct, expected in ((100.0, 'review_exact'), (99.99999, 'non_exact')):
            state = campaign.blank_state('unused', 'unused')
            item = {'id': 'task', 'symbol': 'f', 'source': 's.c', 'base_pct': 90.0,
                    'source_sha256': campaign.digest(source), 'status': 'pending'}
            state['items']['task'] = item
            with patch.object(campaign, 'ROOT', self.root), patch.object(campaign, 'STATE_DIR', self.root), \
                 patch.object(campaign, 'STATE_FILE', self.root / 'state.json'), \
                 patch.object(campaign, 'COORDINATOR', Coordinator(self.root)), \
                 patch.object(campaign, 'build_prompt', side_effect=build), \
                 patch.object(campaign, 'ollama') as generate, \
                 patch.object(campaign, 'verify', return_value={'pct': verified_pct, 'deltas': 0}) as verify:
                campaign.process(state, item, 'unused', 'unused', 1, 'worker')
            generate.assert_not_called()
            verify.assert_called_once()
            self.assertEqual(item['status'], expected)
            self.assertEqual((self.root / 's.c').read_text(), source)
            report = campaign.read_json(self.root / 'candidates/task/attempt-001.report.json', {})
            self.assertTrue(report['inference_skipped'])
            if expected == 'review_exact':
                self.assertIn('skipped', item['last_report']['link_gate'])

    def test_near_exact_prompt_measurement_still_uses_the_model(self):
        source = 'int f(void) { return 1; }\n'
        (self.root / 's.c').write_text(source)
        state = campaign.blank_state('unused', 'unused')
        item = {'id': 'task', 'symbol': 'f', 'source': 's.c', 'base_pct': 90.0,
                'source_sha256': campaign.digest(source), 'status': 'pending'}
        state['items']['task'] = item
        def build(item, current, worker):
            item['prompt_pct'] = 99.99999
            return 'brief', 'prompt'
        with patch.object(campaign, 'ROOT', self.root), patch.object(campaign, 'STATE_DIR', self.root), \
             patch.object(campaign, 'STATE_FILE', self.root / 'state.json'), \
             patch.object(campaign, 'COORDINATOR', Coordinator(self.root)), \
             patch.object(campaign, 'build_prompt', side_effect=build), \
             patch.object(campaign, 'ollama', return_value=source) as generate:
            campaign.process(state, item, 'unused', 'unused', 1, 'worker')
        generate.assert_called_once()
        self.assertEqual(item['status'], 'no_change')

    def test_lowercase_types_dependencies_and_sdk_types_are_available(self):
        headers = ('typedef unsigned char u8;\ntypedef struct { u8 flag; } node;\n'
                   'typedef node* node_ptr;\ntypedef struct { node_ptr head; } OSQueue;\n')
        with patch.object(campaign, '_headers_text', return_value=headers):
            context = campaign.type_context('void f(OSQueue* q) { node_ptr p = q->head; }', '')
            self.assertIn('u8 flag;', context)
            self.assertIn('typedef unsigned char u8;', context)
            self.assertIn('node_ptr head;', context)
            self.assertEqual(context.count('typedef node* node_ptr;'), 1)
            small = campaign.type_context('node_ptr p;', '', budget=25)
            self.assertNotIn('u8 flag;', small)

    def test_type_context_ignores_comments_and_handles_recursive_types(self):
        headers = 'typedef struct { node* next; } node;\ntypedef int unused;\n'
        with patch.object(campaign, '_headers_text', return_value=headers):
            context = campaign.type_context('node* p; /* unused */', '')
        self.assertEqual(context.count('node* next;'), 1)
        self.assertNotIn('typedef int unused;', context)

    def test_compiler_hints_are_selected_from_mismatches(self):
        registers = campaign.mwcc_context(['[register-only]\n-> mr r31, r3   mr r30, r3'])
        self.assertIn('declaration order', registers)
        self.assertNotIn('integer width', registers)
        self.assertNotIn('boundary of an inlined helper', registers)
        widths = campaign.mwcc_context(['[structural]\n-> cmpwi r3, 0   cmplwi r3, 0'])
        self.assertIn('signedness', widths)
        self.assertNotIn('callee-saved registers', widths)
        self.assertEqual(campaign.mwcc_context([]), '')


if __name__ == '__main__':
    unittest.main()
