import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import local_campaign_decompile_reuse as reuse


class ReuseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.baseline, self.batch, self.output = root / 'baseline', root / 'replay', root / 'validation'
        self.baseline.mkdir()
        self.batch.mkdir()
        self.draft = root / 'draft'
        self.draft.mkdir()
        (self.draft / 'draft.c').write_text('int f(void) { return 1; }\n', encoding="utf-8", newline="\n")
        self.prior = {'summary': {'finished': True, 'environment_unchanged': True, 'validator_version': 2,
                                 'environment_sha256': 'environment', 'batch': str(root / 'generation')},
                      'results': [{'id': 'f', 'input_sha256': 'validator-must-check-this',
                                   'draft_sha256': hashlib.sha256((self.draft / 'draft.c').read_bytes()).hexdigest()}]}
        self.replay = {'summary': {'finished': True, 'baseline': str(root / 'generation')},
                       'results': [{'id': 'f', 'artifacts': str(self.draft)}]}

    def save(self):
        (self.baseline / 'results.json').write_text(json.dumps(self.prior), encoding="utf-8", newline="\n")
        (self.batch / 'results.json').write_text(json.dumps(self.replay), encoding="utf-8", newline="\n")

    def test_identical_draft_seeds_provenance_without_publishing_summary(self):
        self.save()
        self.assertEqual(reuse.seed(self.baseline, self.batch, self.output), 1)
        result = json.loads((self.output / 'replay/f/result.json').read_text(encoding="utf-8"))
        self.assertEqual(result['reused_from_validation'], str(self.baseline.resolve()))
        self.assertEqual(result['input_sha256'], 'validator-must-check-this')
        self.assertFalse((self.output / 'replay/results.json').exists())
        self.assertEqual(reuse.seed(self.baseline, self.batch, self.output), 0)

    def test_changed_draft_is_not_seeded(self):
        self.save()
        (self.draft / 'draft.c').write_text('int f(void) { return 2; }\n', encoding="utf-8", newline="\n")
        self.assertEqual(reuse.seed(self.baseline, self.batch, self.output), 0)

    def test_unstable_baseline_and_unrelated_generation_are_rejected(self):
        self.prior['summary']['environment_unchanged'] = False
        self.save()
        with self.assertRaisesRegex(ValueError, 'stable'):
            reuse.seed(self.baseline, self.batch, self.output)
        self.prior['summary']['environment_unchanged'] = True
        self.replay['summary']['baseline'] = 'different'
        self.save()
        with self.assertRaisesRegex(ValueError, 'replayed'):
            reuse.seed(self.baseline, self.batch, self.output)


if __name__ == '__main__':
    unittest.main()
