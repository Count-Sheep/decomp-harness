"""Checks for the model benchmark: a fixed, stratified task set and history-free task copies."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import local_campaign as campaign
import local_campaign_bench as bench
from local_campaign_coordination import Coordinator


class BenchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_select_is_stratified_one_per_owner_and_skips_unusable(self):
        state = campaign.blank_state('unused', 'unused')
        for index, pct in enumerate([99.5, 99.1, 95.0, 90.0, 60.0, 50.0]):
            source = f'src/{index}.c'
            (self.root / 'src').mkdir(exist_ok=True)
            (self.root / source).write_text(f'int f{index}(void) {{ return {index}; }}\n')
            state['items'][f't{index}'] = {'id': f't{index}', 'symbol': f'f{index}', 'source': source, 'size': 100,
                                           'base_pct': pct, 'status': 'pending',
                                           'source_sha256': campaign.digest((self.root / source).read_text())}
        state['items']['t1']['owner_source'] = 'src/0.c'          # shares an owner with t0
        state['items']['t5']['status'] = 'blocked_source_context'
        campaign.write_json(self.root / 'state.json', state)
        with patch.object(campaign, 'ROOT', self.root), patch.object(campaign, 'STATE_FILE', self.root / 'state.json'), \
             patch.object(campaign, 'COORDINATOR', Coordinator(self.root)), \
             patch.object(bench, 'BENCH_DIR', self.root / 'bench'), patch.object(bench, 'SET_FILE', self.root / 'bench/set.json'):
            self.assertEqual(bench.select(1.0, 1, 2048, False), 0)
            chosen = json.loads((self.root / 'bench/set.json').read_text())['tasks']
            self.assertEqual(bench.select(1.0, 1, 2048, False), 1)   # an existing set is never replaced silently
        self.assertEqual(sorted(row['id'] for row in chosen), ['t0', 't2', 't3', 't4'])
        self.assertEqual({row['band'] for row in chosen}, {'98-100', '93-98', '85-93', '0-85'})

    def test_fresh_item_drops_campaign_history(self):
        item = {'id': 't', 'symbol': 'f', 'source': 's.c', 'base_pct': 90.0, 'status': 'non_exact', 'attempts': 7,
                'best': {'pct': 95.0}, 'tried': {'k': {}}, 'focus_round': 2, 'source_sha256': 'abc'}
        fresh = bench.fresh_item(item)
        self.assertEqual((fresh['status'], fresh['attempts'], fresh['source_sha256']), ('pending', 0, 'abc'))
        self.assertNotIn('best', fresh)
        self.assertNotIn('tried', fresh)
        self.assertEqual(item['attempts'], 7)   # the campaign's copy is untouched

    def test_summary_counts_improvements_and_rates(self):
        tasks = [
            {'outcome': 'done', 'base_pct': 90.0, 'best_pct': 95.0, 'seconds': 1800,
             'attempt_rows': [{'status': 'non_exact', 'variants': 3, 'generation_seconds': 5},
                              {'status': 'no_change', 'variants': 0, 'generation_seconds': 2}]},
            {'outcome': 'done', 'base_pct': 99.0, 'best_pct': 100.0, 'seconds': 1800,
             'attempt_rows': [{'status': 'review_exact', 'variants': 1, 'generation_seconds': 4}]},
            {'outcome': 'stale', 'base_pct': 80.0, 'best_pct': 80.0, 'seconds': 0},
        ]
        summary = bench.summarise({'name': 'x'}, tasks)
        self.assertEqual((summary['measured'], summary['skipped'], summary['improved'], summary['exact']), (2, 1, 2, 1))
        self.assertEqual(summary['attempts_per_hour'], 3.0)
        self.assertEqual(summary['improved_per_hour'], 2.0)
        self.assertAlmostEqual(summary['rates']['no_change'], 0.333, places=3)
        self.assertEqual(summary['mean_gain_without_top_pp'], 1.0)   # gains 5.0 and 1.0, the top one left out

    def test_prelim_is_a_size_spread_subset_of_the_main_set(self):
        tasks = [{'id': f't{i}', 'symbol': f'f{i}', 'size': size, 'base_pct': 90.0}
                 for i, size in enumerate([400, 72, 1500, 90, 800, 250, 1100, 600, 150])]
        with patch.object(bench, 'BENCH_DIR', self.root), patch.object(bench, 'SET_FILE', self.root / 'set.json'):
            campaign.write_json(self.root / 'set.json', {'created_at': 'x', 'tasks': tasks})
            self.assertEqual(bench.select_prelim(5, False), 0)
            chosen = json.loads(bench.set_file('prelim').read_text())['tasks']
        self.assertEqual([row['size'] for row in chosen], [72, 150, 400, 800, 1500])

    def test_context_calls_are_counted_without_inventing_legacy_history(self):
        rows = [{'status': 'non_exact', 'variants': 1, 'generation_seconds': 2,
                 'model_calls': 2, 'context_lookup_rounds': 1,
                 'investigation_version': 'target-dossier-context-v1'}]
        tasks = [{'outcome': 'done', 'base_pct': 90, 'best_pct': 91, 'seconds': 4, 'attempt_rows': rows}]
        summary = bench.summarise({}, tasks)
        self.assertEqual(summary['model_calls'], 2)
        self.assertEqual(summary['context_lookup_rounds'], 1)
        self.assertEqual(summary['investigation_versions'], ['target-dossier-context-v1'])
        rows.append({'status': 'non_exact', 'variants': 1, 'generation_seconds': 2})
        summary = bench.summarise({}, tasks)
        self.assertIsNone(summary['model_calls'])
        self.assertIsNone(summary['context_lookup_rounds'])
        self.assertEqual(summary['model_calls_recorded'], 1)

    def test_gate_checks_speed_and_answer_health_only(self):
        def run(name, per_hour, unusable=0.0, no_compile=0.0, measured=5):
            return {'summary': {'name': name, 'measured': measured, 'tasks': 5, 'skipped': 5 - measured,
                                'attempts_per_hour': per_hour, 'unusable_rate': unusable,
                                'rates': {'verification_error': no_compile}, 'improved': 0}}
        verdicts = bench.gate([run('fast', 300), run('slow', 120), run('broken', 280, unusable=0.4),
                               run('cut', 300, measured=3)])
        self.assertTrue(verdicts['fast'][0])
        self.assertIn('slow', verdicts['slow'][1])
        self.assertIn('unusable', verdicts['broken'][1])
        self.assertIn('incomplete', verdicts['cut'][1])

    def test_sign_test(self):
        self.assertEqual(bench.sign_test(6, 6), 1.0)
        self.assertLess(bench.sign_test(10, 1), 0.02)
        self.assertEqual(bench.sign_test(0, 0), 1.0)


    def test_settings_label_names_every_setting(self):
        label = bench.settings_label({'think': 'on', 'temperature': 0.6, 'reasoning_tokens': 2048,
                                      'sampling': {'min_p': 0, 'top_k': 20, 'top_p': 0.95},
                                      'template_kwargs': {'reasoning_effort': 'high'},
                                      'server_cmd': "start-server --model X --spec-type draft-mtp"})
        self.assertEqual(label, 'think on · temp 0.6 · top_p 0.95 · top_k 20 · min_p 0 · reasoning effort high · '
                                'reasoning ≤2048 tok · MTP')
        self.assertIn('temp schedule', bench.settings_label({'think': 'off'}))

    def test_queue_server_command_substitutes_raw_extra_and_is_optional(self):
        config = {'host': 'http://127.0.0.1:8080/v1', 'server_cmd': 'start-server --model {model} {extra}'}
        entry = {'name': 'a', 'model': 'm1', 'server_extra': '--spec-type draft-mtp', 'args': ['--think', 'on']}
        self.assertEqual(bench.server_command(config, entry), 'start-server --model m1 --spec-type draft-mtp')
        self.assertEqual(bench.server_command(config, {'name': 'b', 'model': 'm2'}), 'start-server --model m2 ')
        command = bench.queue_command(config, entry, 'prelim')
        self.assertEqual(command[command.index('--server-cmd') + 1], 'start-server --model m1 --spec-type draft-mtp')
        self.assertEqual(command[-2:], ['--think', 'on'])
        for missing in ({'host': config['host']}, {**config, 'server_cmd': ''}):
            self.assertEqual(bench.server_command(missing, entry), '')
            self.assertNotIn('--server-cmd', bench.queue_command(missing, entry, 'main'))

    def test_queue_without_server_or_pre_command_runs_each_entry(self):
        path = self.root / 'queue.json'
        path.write_text(json.dumps({'host': 'http://127.0.0.1:8080/v1', 'server_cmd': '',
                                    'configs': [{'name': 'a', 'model': 'm1', 'args': []},
                                                {'name': 'b', 'model': 'm2', 'args': []}]}))
        calls = []
        with patch.object(bench, 'BENCH_DIR', self.root), patch.object(bench, 'STATUS_FILE', self.root / 'status.json'), \
             patch.object(bench.subprocess, 'run', side_effect=lambda command, **kw: calls.append((command, kw))):
            self.assertEqual(bench.queue(str(path), 'prelim', False, False), 0)
        self.assertEqual([command[3] for command, _ in calls], ['a', 'b'])
        self.assertTrue(all('--server-cmd' not in command and 'shell' not in kw for command, kw in calls))

    def test_status_survives_and_activity_reads_it(self):
        with patch.object(bench, 'BENCH_DIR', self.root), patch.object(bench, 'STATUS_FILE', self.root / 'status.json'):
            bench.set_status('loading', 'loading the model', run='r', model='m')
            first = json.loads((self.root / 'status.json').read_text())
            bench.set_status('loading', 'still loading', run='r', model='m')
            second = json.loads((self.root / 'status.json').read_text())
        self.assertEqual(first['since'], second['since'])           # same phase keeps its start time
        activity = campaign.bench_activity(self.root)
        self.assertEqual((activity['phase'], activity['alive']), ('loading', True))   # this process is alive
        (self.root / 'status.json').write_text(json.dumps({**second, 'pid': 999999}))
        self.assertEqual(campaign.bench_activity(self.root)['phase'], 'dead')

    def test_best_attempt_ranks_by_gain_not_raw_percent(self):
        tasks = [{'symbol': 'easy', 'base_pct': 98.6, 'attempt_rows': [{'pct': 98.6}, {'pct': None}]},
                 {'symbol': 'hard', 'base_pct': 70.0, 'attempt_rows': [{'pct': 69.0}, {'pct': 72.5}, {'pct': 72.5}]}]
        best = bench.best_attempt(tasks)
        self.assertEqual((best['symbol'], best['attempt'], best['pct'], best['gain_pp']), ('hard', 2, 72.5, 2.5))
        self.assertIsNone(bench.best_attempt([{'symbol': 'x', 'base_pct': 50.0, 'attempt_rows': [{'pct': None}]}]))
        self.assertEqual(bench.best_attempt(tasks[:1])['gain_pp'], 0.0)   # nothing gained: still shows the best %

    def test_results_follow_a_trimmed_set(self):
        rows = [{'id': f't{i}', 'symbol': f'f{i}', 'base_pct': 90.0, 'best_pct': 95.0 if i == 2 else 90.0,
                 'outcome': 'done', 'seconds': 10.0, 'attempt_rows': [{'status': 'non_exact', 'pct': 91.0, 'variants': 1, 'generation_seconds': 5.0}]} for i in range(4)]
        results = {'summary': {'name': 'r', 'tasks': 4}, 'tasks': rows}
        with patch.object(bench, 'BENCH_DIR', self.root), patch.object(bench, 'SET_FILE', self.root / 'set.json'):
            (self.root / 'set.json').write_text(json.dumps({'tasks': [{'id': 't0'}, {'id': 't1'}, {'id': 't2'}]}))
            trimmed = bench.restrict_to_set(results, 'main')
            self.assertEqual([t['id'] for t in trimmed['tasks']], ['t0', 't1', 't2'])
            self.assertEqual((trimmed['summary']['tasks'], trimmed['summary']['improved']), (3, 1))
            inside = {'summary': {}, 'tasks': rows[:2]}
            self.assertIs(bench.restrict_to_set(inside, 'main'), inside)  # untouched when every task is in the set

if __name__ == '__main__':
    unittest.main()
