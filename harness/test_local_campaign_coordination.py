"""Concurrency and stale-source regression checks without a compiler or LLM."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import local_campaign as campaign
from local_campaign_coordination import ClaimConflict, Coordinator


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.coordinator = Coordinator(self.root)

    def child(self, program):
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).parent))
        return subprocess.Popen(
            [sys.executable, "-c", program, str(self.root)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_atomic_owner_claim_across_processes(self):
        program = """
import json, sys
from pathlib import Path
from local_campaign_coordination import Coordinator, ClaimConflict
try:
    result = Coordinator(Path(sys.argv[1])).claim('src/shared.c', 'worker', 'function')
    print(json.dumps(result))
except ClaimConflict:
    print('conflict')
"""
        children = [self.child(program) for _ in range(4)]
        results = [child.communicate(timeout=10)[0].strip() for child in children]
        self.assertTrue(all(child.returncode == 0 for child in children))
        self.assertEqual(results.count("conflict"), 3)
        claim = next(json.loads(result) for result in results if result != "conflict")
        with self.assertRaises(ClaimConflict):
            self.coordinator.release('src/shared.c', 'incorrect-token')
        self.coordinator.release('src/shared.c', claim['token'])
        self.assertEqual(self.coordinator.snapshot()['claims'], {})

    def test_build_waits_but_other_source_can_be_claimed(self):
        program = """
import sys
from pathlib import Path
from local_campaign_coordination import Coordinator
c = Coordinator(Path(sys.argv[1]))
with c.build('child', 'verify'):
    print('acquired', flush=True)
"""
        with self.coordinator.build('parent', 'compile'):
            child = self.child(program)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                builds = self.coordinator.snapshot()['builds'].values()
                if any(job['worker'] == 'child' for job in builds):
                    break
                time.sleep(0.02)
            else:
                child.kill()
                child.communicate()
                self.fail('child did not join build queue')
            self.assertIsNone(child.poll())
            claim = self.coordinator.claim('src/other.c', 'Codex', 'other')
            self.assertEqual(claim['worker'], 'Codex')
            self.assertEqual(next(job for job in builds if job['worker'] == 'child')['status'], 'waiting')
        stdout, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, stderr)
        self.assertEqual(stdout.strip(), 'acquired')
        self.assertEqual(self.coordinator.snapshot()['builds'], {})

    def test_dead_worker_claim_recovers_but_manual_claim_remains(self):
        child = self.child("""
import sys
from pathlib import Path
from local_campaign_coordination import Coordinator
Coordinator(Path(sys.argv[1])).claim('src/auto.c', 'model', 'a', automatic=True)
""")
        child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0)
        self.coordinator.claim('src/manual.c', 'Codex', 'b')
        self.assertEqual(set(self.coordinator.snapshot()['claims']), {'src/manual.c'})

    def test_writer_rejects_simultaneous_sync(self):
        with self.coordinator.writer():
            with self.assertRaises(RuntimeError):
                with Coordinator(self.root).writer():
                    self.fail('second writer acquired lock')

    def test_parallel_runners_exclude_sync_but_can_save(self):
        child = self.child("""
import sys
from pathlib import Path
from local_campaign_coordination import Coordinator
c = Coordinator(Path(sys.argv[1]))
with c.writer(shared=True), c.writer(shared=True), c.state_file():
    try:
        with c.writer():
            raise AssertionError('sync overlapped runners')
    except RuntimeError:
        print('ok')
with c.writer(), c.state_file():
    print('sync saved')
""")
        try:
            stdout, stderr = child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            self.fail('nested locks deadlocked')
        self.assertEqual(child.returncode, 0, stderr)
        self.assertEqual(stdout.splitlines(), ['ok', 'sync saved'])

    def test_stale_worker_save_preserves_other_worker_and_every_item(self):
        state_file = self.root / 'state.json'
        original = campaign.blank_state('unused', 'unused')
        original['items'] = {name: {'updated_at': '1', 'status': 'pending'} for name in ('a', 'b')}
        campaign.write_json(state_file, original)
        first = json.loads(json.dumps(original))
        second = json.loads(json.dumps(original))
        first['items']['a'].update(updated_at='2', status='running')
        second['items']['b'].update(updated_at='3', status='non_exact')
        with patch.object(campaign, 'STATE_FILE', state_file), patch.object(campaign, 'COORDINATOR', self.coordinator):
            campaign.save_state(first)
            campaign.save_state(second)
        saved = campaign.read_json(state_file, {})
        self.assertEqual(saved['items']['a']['status'], 'running')
        self.assertEqual(saved['items']['b']['status'], 'non_exact')

    def test_runner_locks_are_per_worker(self):
        with patch.object(campaign, 'STATE_DIR', self.root):
            campaign.acquire_lock('worker-b')
            try:
                campaign.acquire_lock('worker-a')
                campaign.release_lock('worker-a')
                with self.assertRaises(RuntimeError):
                    campaign.acquire_lock('worker-b')
            finally:
                campaign.release_lock('worker-b')

    def test_finished_agent_stays_visible_for_review(self):
        self.coordinator.register_agent({
            'id': 'lane', 'worker': 'Codex agent', 'status': 'running', 'pid': 999999,
            'source': 'src/test.c', 'symbols': ['fn_test'],
        })
        lane = self.coordinator.snapshot()['agents']['lane']
        self.assertEqual(lane['status'], 'finished')
        self.assertIn('review', lane['detail'])

    def test_failed_agent_start_is_not_reported_as_review_ready(self):
        log = self.root / 'failed.log'
        log.write_text('error: invalid option\nUsage: codex exec\n')
        self.coordinator.register_agent({
            'id': 'failed', 'worker': 'Codex agent', 'status': 'running', 'pid': 999999,
            'source': 'src/test.c', 'symbols': ['fn_test'], 'log': str(log),
        })
        lane = self.coordinator.snapshot()['agents']['failed']
        self.assertEqual(lane['status'], 'failed')

    def test_running_agent_exposes_latest_command(self):
        log = self.root / 'running.jsonl'
        log.write_text(json.dumps({
            'type': 'item.started',
            'item': {'command': 'ninja -j 1 build/TEST01/report.json'},
        }) + '\n')
        self.coordinator.register_agent({
            'id': 'running', 'worker': 'Codex agent', 'status': 'running',
            'pid': os.getpid(), 'source': 'src/test.c', 'symbols': ['fn_test'],
            'log': str(log),
        })
        lane = self.coordinator.snapshot()['agents']['running']
        self.assertEqual(lane['status'], 'running')
        self.assertEqual(lane['live_detail'], 'Running: ninja -j 1 build/TEST01/report.json')

    def test_reviewed_agent_outcome_is_preserved(self):
        self.coordinator.register_agent({
            'id': 'reviewed', 'worker': 'Codex agent', 'status': 'running',
            'pid': os.getpid(), 'source': 'src/test.c', 'symbols': ['fn_test'],
        })
        self.coordinator.update_agent('reviewed', status='rejected', detail='No strict survivor.')
        lane = self.coordinator.snapshot()['agents']['reviewed']
        self.assertEqual(lane['status'], 'rejected')
        self.assertEqual(lane['detail'], 'No strict survivor.')

    def test_ollama_request_has_a_generation_cap(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *unused):
                return False

            def __iter__(self):
                return iter([b'{"response":"candidate","done":true}\n'])

        updates = []
        with patch('urllib.request.urlopen', return_value=Response()) as urlopen:
            answer = campaign.ollama('http://ollama.test', 'model', 'prompt', 1, 123, lambda **update: updates.append(update))
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(answer, 'candidate')
        self.assertEqual(payload['options']['num_predict'], 123)
        self.assertEqual(updates[-1]['response_chars'], len('candidate'))

    def test_ollama_stops_a_repeated_response(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *unused):
                return False

            def __iter__(self):
                fragment = 'f0 = f0 - f3; f2 = f2 * f4; f1 = f1 * f5; '
                return iter([
                    json.dumps({'response': fragment, 'done': False}).encode() + b'\n'
                    for _ in range(32)
                ])

        with patch('urllib.request.urlopen', return_value=Response()):
            with self.assertRaises(campaign.RepeatedResponse):
                campaign.ollama('http://ollama.test', 'model', 'prompt', 1, 1024, lambda **unused: None)

    def test_schema_format_is_sent_only_to_compatible_endpoint(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *unused):
                return False

            def __iter__(self):
                return iter([b'data: {"choices":[{"delta":{"content":"candidate"},"finish_reason":"stop"}]}\n'])

        schema = {"type": "json_schema", "json_schema": {"schema": {"type": "string"}}}
        for response_format in (None, schema):
            with self.subTest(response_format=response_format), patch('urllib.request.urlopen', return_value=Response()) as urlopen:
                answer = campaign.ollama('http://rpc.test/v1', 'model', 'prompt', 1, 123, lambda **unused: None,
                                         response_format=response_format)
                payload = json.loads(urlopen.call_args.args[0].data)
                self.assertEqual(answer, 'candidate')
                if response_format:
                    self.assertEqual(payload['response_format'], schema)
                else:
                    self.assertNotIn('response_format', payload)
        with patch('urllib.request.urlopen') as urlopen, self.assertRaisesRegex(ValueError, '/v1 endpoint'):
            campaign.ollama('http://ollama.test', 'model', 'prompt', 1, 123, lambda **unused: None,
                             response_format=schema)
        urlopen.assert_not_called()
        with patch('urllib.request.urlopen') as urlopen, self.assertRaisesRegex(ValueError, 'nested json_schema.schema'):
            campaign.ollama('http://rpc.test/v1', 'model', 'prompt', 1, 123, lambda **unused: None,
                             response_format={"type": "json_schema", "schema": {"type": "object"}})
        urlopen.assert_not_called()

    def test_edit_during_generation_never_applies_candidate(self):
        source = self.root / 'source.c'
        source.write_text('original')
        item = {'source': 'source.c', 'symbol': 'f', 'source_sha256': campaign.digest('original')}
        source.write_text('manual edit')
        with patch.object(campaign, 'ROOT', self.root), patch.object(campaign, 'COORDINATOR', self.coordinator):
            with patch.object(campaign, 'verify_unlocked') as verifier:
                with self.assertRaises(campaign.StaleSource):
                    campaign.verify(item, self.root / 'candidate.c')
                verifier.assert_not_called()
        self.assertEqual(source.read_text(), 'manual edit')
        self.assertEqual(self.coordinator.snapshot()['builds'], {})

    def test_runner_skips_manual_owner_and_continues_worklist(self):
        state = campaign.blank_state('http://unused', 'unused')
        for name in ('reserved', 'available'):
            state['items'][name] = {
                'id': name, 'source': f'src/{name}.c', 'symbol': name,
                'status': 'pending', 'base_pct': 99 if name == 'reserved' else 90,
                'residual_functions': 1, 'size': 100,
            }
        self.coordinator.claim('src/reserved.c', 'Codex', 'reserved')
        state_file = self.root / 'state.json'
        campaign.write_json(state_file, state)
        processed = []
        def process(state, item, *args):
            processed.append(item['symbol'])
            item['status'] = 'non_exact'
        with patch.object(campaign, 'COORDINATOR', self.coordinator), \
             patch.object(campaign, 'STATE_FILE', state_file), \
             patch.object(campaign, 'acquire_lock'), patch.object(campaign, 'release_lock'), \
             patch.object(campaign, 'process', side_effect=process), patch('builtins.print'):
            campaign.run(state, 'http://unused', 'unused', 1, 1, 0, 'worker-a', 1024)
        self.assertEqual(processed, ['available'])
        self.assertEqual(state['items']['reserved']['status'], 'pending')
        self.assertEqual(set(self.coordinator.snapshot()['claims']), {'src/reserved.c'})


if __name__ == '__main__':
    unittest.main()
