"""Regression checks for ranking evidence and model context provenance."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as Row
import unittest
from unittest.mock import patch

import local_campaign as campaign
import local_campaign_priority as priority


def function(unit, symbol, owner, pct=90):
    return dict(unit=unit, symbol=symbol, owner_source=owner, base_pct=pct,
                size=100, source_sha256='baseline')


class PriorityTests(unittest.TestCase):
    def test_learning_counts_unique_unresolved_beneficiaries(self):
        functions = {('u', name): function('u', name, 'shared.c', pct)
                     for name, pct in [('helper', 90), ('user', 80), ('exact', 100)]}
        graph = {('u', 'user'): {('u', 'helper')}, ('u', 'exact'): {('u', 'helper')}}
        result = priority.rank(functions, graph, set(functions))[('u', 'helper')]
        self.assertEqual(result['potential_beneficiaries'], 1)
        self.assertEqual(result['learning_score'], 10)
        self.assertEqual(result['context_references'][0]['symbol'], 'exact')
        self.assertEqual(result['closure_score'], 50)

    def test_reference_graph_scopes_locals_and_ignores_data_relocations(self):
        functions = {('u', name): function('u', name, 'u.c') for name in ('caller', 'helper')}
        functions[('other', 'helper')] = function('other', 'helper', 'other.c')
        symbols = [Row(name='caller', type=2, size=16, shndx=1, value=0),
                   Row(name='helper', type=2, size=4, shndx=1, value=16)]
        obj = Row(symbols=symbols, relocations=[
            Row(target_section_index=1, offset=4, symbol_index=1),
            Row(target_section_index=1, offset=8, symbol_index=1),
            Row(target_section_index=2, offset=0, symbol_index=1)])
        with patch.object(priority, 'read_elf', return_value=obj):
            graph, covered, missing = priority.reference_graph(Path('.'), [{'name': 'u', 'target_path': 'u.o'}], functions)
        self.assertEqual(graph[('u', 'caller')], {('u', 'helper')})
        self.assertEqual(len(covered), 2)
        self.assertFalse(missing)

    def test_missing_objects_report_unknown_coverage(self):
        functions = {('u', 'f'): function('u', 'f', 'u.c')}
        graph, covered, missing = priority.reference_graph(Path('.'), [{'name': 'u'}], functions)
        self.assertFalse(graph)
        self.assertFalse(covered)
        self.assertEqual(missing[0]['unit'], 'u')

    def test_stale_or_forbidden_examples_do_not_enter_prompt(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = 'int helper(void) { return 1; }'
            (root / 'source.c').write_text(source)
            reference = dict(owner_source='source.c', symbol='helper', source_sha256=campaign.digest(source))
            item = dict(context_references=[reference])
            with patch.object(campaign, 'ROOT', root):
                self.assertIn(source, campaign.reference_context(item))
                (root / 'source.c').write_text(source.replace('1', '2'))
                self.assertEqual(campaign.reference_context(item), '')
                source = '#pragma optimization_level 0\n' + source
                (root / 'source.c').write_text(source)
                reference['source_sha256'] = campaign.digest(source)
                self.assertEqual(campaign.reference_context(item), '')


if __name__ == '__main__':
    unittest.main()
