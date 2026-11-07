import json
import hashlib
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import local_campaign_m2c_report as report


class LiveSweepTests(unittest.TestCase):
    def test_standalone_gate_uses_pinned_validation_even_when_manifest_is_old(self):
        batch = self.root / 'batches/run'
        manifest = self.manifest('validation', 'run-env', {
            'batch': str(batch), 'finished': True}, timestamp=10)
        validation = manifest.parent / 'results.json'
        validation.write_text(json.dumps({'summary': {'batch': str(batch)}}))
        gates = self.root / 'unit-gates'
        gates.mkdir()
        (gates / 'active.json').write_text(json.dumps({'pid': 5, 'output': str(gates / 'retry'),
            'phase': 'owner_trial', 'processed': 3, 'total': 10, 'current_unit': 'main/unit'}))
        command = ['harness/m2c_unit_autogate.py', '--root', str(self.root.parents[2]),
                   '--version', self.root.name, '--validation', str(validation)]
        rows = report.live_runs(self.root, [self.process(command)], self.now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['stage'], 'owner_trial')
        self.assertEqual(rows[0]['processed'], 3)
        self.assertEqual(rows[0]['current_function'], 'main/unit')
        command[-1] = str(self.root / 'other/results.json')
        self.assertEqual(report.live_runs(self.root, [self.process(command)], self.now), [])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # The real shape: <project>/build/decomp-support/<VERSION>, independent of the temp dir depth.
        self.root = Path(temporary.name) / "project/build/decomp-support/TEST01"
        self.root.mkdir(parents=True)
        self.now = datetime.fromtimestamp(150, UTC)

    def manifest(self, kind, name, value, timestamp=120):
        path = self.root / kind / name / 'manifest.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        os.utime(path, (timestamp, timestamp))
        return path

    def process(self, argv, pid=5, parent=1, state='S'):
        return {'pid': pid, 'parent': parent, 'elapsed_seconds': 50, 'state': state,
                'argv': ['python', *argv]}

    def test_generation_finds_current_checkpoint_and_excludes_interrupted_predecessor(self):
        baseline = str(self.root / 'batches/baseline')
        self.manifest('batches', 'stopped', {'baseline': baseline, 'processed': 90}, timestamp=80)
        self.manifest('batches', 'active', {'baseline': baseline, 'selection': 'matched',
            'processed': 30, 'total': 100, 'counts': {'ok': 20, 'unsupported': 10}, 'finished': False})
        processes = [self.process(['harness/local_campaign_decompile.py', '--replay', baseline]),
                     self.process(['m2c.py', '-f', 'current_function'], pid=6, parent=5)]
        rows = report.live_runs(self.root, processes, self.now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['run_id'], 'active')
        self.assertEqual(rows[0]['processed'], 30)
        self.assertEqual(rows[0]['current_function'], 'current_function')
        self.assertEqual(rows[0]['selection'], 'matched')
        self.assertEqual(report.live_runs(self.root, [], self.now), [])

    def test_validation_uses_batch_corpus_and_live_build_detail(self):
        batch = self.root / 'batches/run'
        self.manifest('batches', 'run', {'selection': 'matched', 'finished': True}, timestamp=80)
        workspace = self.root / 'workspace'
        coordination = workspace / 'build/local_llm_campaign/coordination.json'
        coordination.parent.mkdir(parents=True)
        coordination.write_text(json.dumps({'builds': {'ticket': {
            'pid': 5, 'status': 'waiting', 'detail': 'MWCC/objdiff: function'}}}))
        self.manifest('validation', 'run', {'batch': str(batch), 'workspace': str(workspace),
            'processed': 10, 'total': 100, 'compiler_succeeded': 7, 'compiled_functions': 6})
        rows = report.live_runs(self.root, [self.process([
            'harness/local_campaign_decompile_validate.py', str(batch)])], self.now)
        self.assertEqual(rows[0]['stage'], 'validation')
        self.assertEqual(rows[0]['selection'], 'matched')
        self.assertEqual(rows[0]['state'], 'waiting_for_build_lock')
        self.assertEqual(rows[0]['current_function'], 'function')
        self.assertEqual(rows[0]['compiler_succeeded'], 7)

    def test_project_runner_is_scoped_to_project_root_and_version(self):
        project = self.root.parents[2]
        self.manifest('batches', 'run', {
            'selection': 'unmatched', 'processed': 4, 'total': 10, 'finished': False})
        command = ['harness/m2c_drafts.py', '--root', str(project),
                   '--version', self.root.name]
        self.assertEqual(len(report.live_runs(self.root, [self.process(command)], self.now)), 1)
        command[command.index('--root') + 1] = str(project / 'other-project')
        self.assertEqual(report.live_runs(self.root, [self.process(command)], self.now), [])
        command[command.index('--root') + 1] = str(project)
        command[command.index('--version') + 1] = 'OTHER01'
        self.assertEqual(report.live_runs(self.root, [self.process(command)], self.now), [])

    def test_project_validator_without_batch_tracks_latest_project_validation(self):
        project = self.root.parents[2]
        batch = self.root / 'batches/run'
        self.manifest('batches', 'run', {'selection': 'unmatched', 'finished': True}, timestamp=80)
        self.manifest('validation', 'run-environment', {
            'batch': str(batch), 'processed': 7, 'total': 10, 'finished': False})
        command = ['harness/m2c_validate_drafts.py', '--root', str(project),
                   '--version', self.root.name]
        rows = report.live_runs(self.root, [self.process(command)], self.now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['run_id'], 'run-environment')
        self.assertEqual(rows[0]['stage'], 'validation')
        self.assertEqual(rows[0]['processed'], 7)

    def test_durable_pipeline_uses_its_journal_and_live_validation(self):
        project = self.root.parents[2]
        batch = self.root / 'batches/run'
        self.manifest('batches', 'run', {'selection': 'unmatched', 'finished': True}, timestamp=80)
        self.manifest('validation', 'run-environment', {
            'batch': str(batch), 'processed': 7, 'total': 10, 'finished': False})
        (self.root / 'pipeline-state.json').write_text(json.dumps({
            'status': 'active', 'stage': 'validation', 'batch': str(batch)}))
        command = ['harness/m2c_pipeline_runner.py', '--root', str(project),
                   '--version', self.root.name]
        rows = report.live_runs(self.root, [self.process(command)], self.now)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['run_id'], 'run-environment')
        self.assertEqual(rows[0]['stage'], 'validation')
        self.assertEqual(rows[0]['processed'], 7)

    def test_durable_pipeline_is_hidden_when_journal_is_complete(self):
        project = self.root.parents[2]
        (self.root / 'pipeline-state.json').write_text(json.dumps({
            'status': 'complete', 'stage': 'complete'}))
        command = ['harness/m2c_pipeline_runner.py', '--root', str(project),
                   '--version', self.root.name]
        self.assertEqual(report.live_runs(self.root, [self.process(command)], self.now), [])

    def test_finished_validation_exposes_live_refinement_checkpoint(self):
        batch = self.root / 'batches/run'
        self.manifest('batches', 'run', {'selection': 'unmatched', 'finished': True}, timestamp=80)
        validation = self.manifest('validation', 'run-environment', {
            'batch': str(batch), 'workspace': str(self.root / 'workspace'),
            'processed': 100, 'total': 100, 'finished': True})
        refinement = self.root / 'refinement-runs' / validation.parent.name / 'results.json'
        refinement.parent.mkdir(parents=True)
        refinement.write_text(json.dumps({'finished': False,
            'validation_sha256': 'validation-hash', 'source_variants': 48,
            'selection': [{'symbol': 'done'}, {'symbol': 'current', 'pct': 99.0}],
            'attempts': [{'symbol': 'done', 'best_pct': 99.5, 'object_exact': True}]}))
        key = hashlib.sha256(('validation-hash' + 'current' + str(refinement.parent.resolve())).encode()).hexdigest()[:20]
        search = self.root / 'refinements' / f'auto-{key}' / 'results.json'
        search.parent.mkdir(parents=True)
        search.write_text(json.dumps({
            'max_variants': 48, 'max_depth': 3, 'best_attempt': 1,
            'attempts': [
                {'pct': 99.0, 'compile_ok': True, 'mismatch_classification': {
                    'counts': {'register': 2}, 'total': 2,
                    'mismatches': [{'source_line': 7}, {'source_line': 7}]}},
                {'pct': 99.25, 'compile_ok': True, 'search_depth': 1,
                 'variant': {'kind': 'expression_shape', 'detail': 'swap operands',
                             'targeted': True, 'target_distance': 0},
                 'mismatch_classification': {'counts': {'register': 1}, 'total': 1,
                                             'mismatches': [{'source_line': 7}]}}
            ]}))
        rows = report.live_runs(self.root, [self.process([
            'harness/m2c_validate_drafts.py', '--batch', str(batch)])], self.now)
        self.assertEqual(rows[0]['stage'], 'refinement')
        self.assertEqual(rows[0]['processed'], 1)
        self.assertEqual(rows[0]['total'], 2)
        self.assertEqual(rows[0]['current_function'], 'current')
        self.assertEqual(rows[0]['counts'], {'object_exact': 1})
        progress = rows[0]['refinement_progress']['current']
        self.assertEqual(progress['variants_tested'], 1)
        self.assertEqual(progress['variant_budget'], 48)
        self.assertEqual(progress['best_pct'], 99.25)
        self.assertEqual(progress['delta_pp'], 0.25)
        self.assertEqual(progress['validation_pct'], 99.0)
        self.assertEqual(progress['mismatch_counts'], {'register': 1})
        self.assertEqual(progress['source_lines'], [7])

    def test_paused_finishing_and_single_function_translation_states(self):
        batch = str(self.root / 'batches/baseline')
        self.manifest('batches', 'run', {'baseline': batch, 'finished': True, 'processed': 100, 'total': 100})
        process = self.process(['harness/local_campaign_decompile.py', '--replay', batch])
        self.assertEqual(report.live_runs(self.root, [process], self.now)[0]['state'], 'finishing')
        process['state'] = 'T'
        self.assertEqual(report.live_runs(self.root, [process], self.now)[0]['state'], 'paused')
        self.assertEqual(report.live_runs(self.root, [self.process([
            'harness/local_campaign_decompile.py', 'one_function'])], self.now), [])
        process['argv'][-1] = str(self.root / 'other/batches/baseline')
        self.assertEqual(report.live_runs(self.root, [process], self.now), [])

    def test_load_keeps_history_immutable_and_distinguishes_unavailable_observation(self):
        self.root.joinpath(report.REPORT_NAME).write_text('{"history_count": 4, "runs": []}')
        before = self.root.joinpath(report.REPORT_NAME).read_bytes()
        with patch.object(report, 'process_snapshot', return_value=None):
            value = report.load_report(self.root)
        self.assertFalse(value['activity_available'])
        self.assertEqual(value['history_count'], 4)
        self.assertEqual(self.root.joinpath(report.REPORT_NAME).read_bytes(), before)
        with patch.object(report, 'process_snapshot', return_value=[]):
            self.assertTrue(report.load_report(self.root)['activity_available'])

    def test_process_parser_accepts_day_elapsed_and_quoted_paths(self):
        output = '5 1 1-02:03:04 S python "directory with spaces/local_campaign_decompile.py" --all\n'
        # The ps parser is the POSIX path; Windows uses psutil or a CIM query instead.
        with patch.object(report, 'WINDOWS', False), \
             patch.object(report.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output)):
            rows = report.process_snapshot()
        self.assertEqual(rows[0]['elapsed_seconds'], 93784)
        self.assertEqual(rows[0]['argv'][1], 'directory with spaces/local_campaign_decompile.py')
        with patch.object(report, 'WINDOWS', False), \
             patch.object(report.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ps', 3)):
            self.assertIsNone(report.process_snapshot())


if __name__ == '__main__':
    unittest.main()
