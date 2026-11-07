"""Assembly drafts and similarity references are advisory, bounded and reproducible."""

import json
import io
import contextlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_campaign as campaign
import local_campaign_corpus as corpus
import local_campaign_decompile as decompile
import local_campaign_decompile_batch as batch
import local_campaign_templates as templates
from local_campaign_coordination import Coordinator
from harness_paths import VERSION as PROJECT_VERSION


class DecompileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.script = self.root / 'm2c.py'
        self.script.touch()
        self.asm = self.root / 'target.s'
        self.asm.write_text('.fn f, global\nli r3, 1\nblr\n.endfn f\n', encoding="utf-8", newline="\n")

    def test_unit_defines_and_include_directories_use_structured_arguments(self):
        command = 'build/compilers/mwcceppc.exe -i "inc with spaces" -DVERSION=1 -D VALUE=2 -U DEBUG -O4 -c src/f.c && dep-tool'
        self.assertEqual(decompile.context_flags(command),
                         ['-I', 'inc with spaces', '-DVERSION=1', '-D', 'VALUE=2', '-U', 'DEBUG'])

    def test_draft_caches_inputs_and_provenance_without_modifying_source(self):
        original = 'int f(void) { return 0; }\n'
        (self.root / 'f.c').write_text(original, encoding="utf-8", newline="\n")
        output = 'int f(void) { return 1; }\n'
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            if command[0] == 'clang':
                return subprocess.CompletedProcess(command, 0, original, '')
            if command[0] == 'git':
                return subprocess.CompletedProcess(command, 0, 'revision\n', '')
            return subprocess.CompletedProcess(command, 0, output, '')
        with patch.object(decompile, 'tool', return_value=self.script), \
             patch.object(decompile.shutil, 'which', return_value='clang'), \
             patch.object(decompile.subprocess, 'run', side_effect=run):
            first = decompile.draft(self.root, 'f', self.asm, 'mwcc -i include -c f.c', 'f.c', self.root / 'cache')
            second = decompile.draft(self.root, 'f', self.asm, 'mwcc -i include -c f.c', 'f.c', self.root / 'cache')
        self.assertEqual(first, second)
        self.assertEqual(first['status'], 'ok')
        self.assertEqual(sum('--no-unk-inference' in call for call in calls), 1)
        self.assertIn('ppc-mwcc-c', calls[2])
        self.assertIn('--no-cache', calls[2])
        folder = Path(first['artifacts'])
        self.assertEqual((folder / 'context.c').read_text(encoding="utf-8"), original)
        self.assertEqual((folder / 'target.s').read_text(encoding="utf-8"), self.asm.read_text(encoding="utf-8"))
        self.assertEqual((folder / 'draft.c').read_text(encoding="utf-8"), output)
        self.assertEqual((self.root / 'f.c').read_text(encoding="utf-8"), original)

    def test_failed_type_context_does_not_guess_a_signature(self):
        result = subprocess.CompletedProcess([], 1, '', 'unknown include')
        with patch.object(decompile, 'tool', return_value=self.script), \
             patch.object(decompile.shutil, 'which', return_value='clang'), \
             patch.object(decompile.subprocess, 'run', return_value=result) as run:
            row = decompile.draft(self.root, 'f', self.asm, 'mwcc -c f.c', 'f.c', self.root / 'cache')
        self.assertEqual(row['status'], 'context_error')
        run.assert_called_once()
        self.assertEqual(decompile.prompt_context(row), '')

    def test_context_declarations_are_opt_in_and_have_separate_cache_identity(self):
        metadata = {"revision": "revision", "fingerprint": "engine",
                    "capabilities": {"global_declaration_modes": ["all", "used", "used-context", "none"]}}
        context = {"status": "ok", "context": "int f(void);"}
        output = subprocess.CompletedProcess([], 0, "int f(void) { return 1; }", "")
        with patch.object(decompile, "tool", return_value=self.script), \
             patch.object(decompile.subprocess, "run", return_value=output):
            old = decompile.translate(self.root, "f", self.asm.read_bytes(), str(self.asm), context,
                                      self.root / "cache", metadata)
            native = decompile.translate(self.root, "f", self.asm.read_bytes(), str(self.asm), context,
                                         self.root / "cache", metadata, context_declarations=True)
        self.assertNotEqual(old["artifacts"], native["artifacts"])
        self.assertNotIn("used-context", old["command"])
        self.assertIn("used-context", native["command"])
        self.assertTrue(native["context_declarations"])
        self.assertEqual(native["context_declarations_mode"], "used-context")

    def test_cached_result_uses_immutable_cached_assembly_not_batch_input(self):
        metadata = {"revision": "revision", "fingerprint": "engine"}
        context = {"status": "ok", "context": "int f(void);"}
        output = subprocess.CompletedProcess([], 0, "int f(void) { return 1; }", "")
        with patch.object(decompile, "tool", return_value=self.script), \
             patch.object(decompile.subprocess, "run", return_value=output) as run:
            first = decompile.translate(self.root, "f", self.asm.read_bytes(), "/batch/old/input.s",
                                        context, self.root / "cache", metadata)
            second = decompile.translate(self.root, "f", self.asm.read_bytes(), "/batch/new/input.s",
                                         context, self.root / "cache", metadata)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(first["assembly"], second["assembly"])
        self.assertEqual(Path(second["assembly"]).read_bytes(), self.asm.read_bytes())
        self.assertEqual(second["assembly_source"], "/batch/old/input.s")

    def test_missing_context_declaration_capability_is_explicit_and_does_not_run(self):
        metadata = {"revision": "revision", "fingerprint": "engine",
                    "capabilities": {"global_declaration_modes": ["all", "used", "none"]}}
        context = {"status": "ok", "context": "int f(void);"}
        with patch.object(decompile, "tool", return_value=self.script), \
             patch.object(decompile.subprocess, "run") as run:
            row = decompile.translate(self.root, "f", self.asm.read_bytes(), str(self.asm), context,
                                      self.root / "cache", metadata, context_declarations=True)
        run.assert_not_called()
        self.assertEqual(row["status"], "unavailable")
        self.assertIsNone(row["context_declarations_mode"])
        self.assertIn("patch 0015", row["error"])

    def test_tool_capabilities_are_read_from_the_m2c_enum(self):
        package = self.root / "m2c"
        package.mkdir()
        (package / "options.py").write_text(
            'class Options:\n    class GlobalDeclsEnum:\n        USED = "used"\n        NATIVE = "used-context"\n', encoding="utf-8", newline="\n")
        self.assertEqual(decompile.global_declaration_modes(self.script), ["used", "used-context"])

    def test_readonly_capture_is_an_m2c_input_with_distinct_owner_provenance(self):
        metadata = {"revision": "revision", "fingerprint": "engine"}
        context = {"status": "ok", "context": "int f(void);"}
        output = subprocess.CompletedProcess([], 0, "int f(void) { return 1; }", "")
        enriched = self.asm.read_bytes() + b'.rodata\n.obj "@1", local\n.byte 0\n.endobj "@1"\n'
        owner = self.root / "target.o"
        with patch.object(decompile, "tool", return_value=self.script), \
             patch.object(decompile.subprocess, "run", return_value=output), \
             patch.object(decompile.m2c_readonly_data, "capture", return_value=(enriched, {"owner_sha256": "first"})) as capture:
            first = decompile.translate(self.root, "f", self.asm.read_bytes(), str(self.asm), context,
                                        self.root / "cache", metadata, readonly_owner=owner)
            capture.return_value = (enriched, {"owner_sha256": "second"})
            second = decompile.translate(self.root, "f", self.asm.read_bytes(), str(self.asm), context,
                                         self.root / "cache", metadata, readonly_owner=owner)
        self.assertNotEqual(first["artifacts"], second["artifacts"])
        self.assertEqual(Path(first["artifacts"], "target.s").read_bytes(), enriched)
        self.assertEqual(Path(first["artifacts"], "original_target.s").read_bytes(), self.asm.read_bytes())
        self.assertEqual(first["readonly_data"]["owner_sha256"], "first")
        self.assertEqual(Path(first["artifacts"], "draft.c").read_text(encoding="utf-8"), output.stdout)

    def test_legacy_replays_cannot_add_unfrozen_owner_data(self):
        with self.assertRaisesRegex(ValueError, "frozen owner data"):
            batch.run_batch(self.root, baseline=self.root, readonly_data=True)

    def test_explicit_fields_are_opt_in_and_cannot_reuse_macro_output(self):
        metadata = {"revision": "revision", "fingerprint": "engine"}
        context = {"status": "ok", "context": "int f(void *ptr);"}
        output = subprocess.CompletedProcess([], 0, "int f(void *ptr) { return 1; }", "")
        with patch.object(decompile, "tool", return_value=self.script), \
             patch.object(decompile.subprocess, "run", return_value=output) as run:
            macro = decompile.translate(self.root, "f", self.asm.read_bytes(), str(self.asm), context,
                                        self.root / "cache", metadata)
            native = decompile.translate(self.root, "f", self.asm.read_bytes(), str(self.asm), context,
                                         self.root / "cache", metadata, explicit_fields=True)
            self.assertNotEqual(macro["artifacts"], native["artifacts"])
            self.assertNotIn("--explicit-field-access", macro["command"])
            self.assertIn("--explicit-field-access", native["command"])
            self.assertTrue(native["explicit_field_access"])
            self.assertEqual(decompile.translate(self.root, "f", self.asm.read_bytes(), str(self.asm), context,
                                                self.root / "cache", metadata, explicit_fields=True), native)
            self.assertEqual(run.call_count, 2)

    def test_inference_modes_are_cache_keyed_and_explicit(self):
        metadata = {"revision": "revision", "fingerprint": "engine"}
        context = {"status": "ok", "context": "int f(void *ptr);"}
        output = subprocess.CompletedProcess([], 0, "int f(void *ptr) { return 1; }", "")
        with patch.object(decompile, "tool", return_value=self.script), \
             patch.object(decompile.subprocess, "run", return_value=output):
            default = decompile.translate(self.root, "f", self.asm.read_bytes(), str(self.asm), context,
                                          self.root / "cache", metadata)
            inferred = decompile.translate(self.root, "f", self.asm.read_bytes(), str(self.asm), context,
                                           self.root / "cache", metadata, infer_unknowns=True,
                                           deterministic_vars=True)
        self.assertNotEqual(default["artifacts"], inferred["artifacts"])
        self.assertIn("--no-unk-inference", default["command"])
        self.assertNotIn("--no-unk-inference", inferred["command"])
        self.assertIn("--deterministic-vars", inferred["command"])
        self.assertTrue(inferred["unknown_inference"])
        self.assertTrue(inferred["deterministic_vars"])

    def test_unsupported_translation_is_recorded_but_not_shown_to_model(self):
        results = [subprocess.CompletedProcess([], 0, 'int f(void);', ''),
                   subprocess.CompletedProcess([], 0, 'revision', ''),
                   subprocess.CompletedProcess([], 0, 'int f(void) { M2C_ERROR("unset"); }', '')]
        with patch.object(decompile, 'tool', return_value=self.script), \
             patch.object(decompile.shutil, 'which', return_value='clang'), \
             patch.object(decompile.subprocess, 'run', side_effect=results):
            row = decompile.draft(self.root, 'f', self.asm, 'mwcc -c f.c', 'f.c', self.root / 'cache')
        self.assertEqual(row['status'], 'unsupported')
        self.assertEqual(decompile.prompt_context(row), '')
        self.assertTrue((Path(row['artifacts']) / 'result.json').is_file())

    def test_prompt_never_truncates_a_draft_or_presents_it_as_verified(self):
        row = {'status': 'ok', 'draft': 'int f(void) { return 1; }'}
        prompt = decompile.prompt_context(row)
        self.assertIn('not a verified match', prompt)
        self.assertIn('assigned signature', prompt)
        self.assertEqual(decompile.prompt_context(row, budget=10), '')

    def test_opaque_context_transfer_is_unsupported_even_without_other_errors(self):
        output = 'void f(void) { M2C_UNRESOLVED_CONTEXT_TRANSFER(target, lr, sp); }'
        results = [subprocess.CompletedProcess([], 0, 'void f(void);', ''),
                   subprocess.CompletedProcess([], 0, 'revision', ''),
                   subprocess.CompletedProcess([], 0, output, '')]
        with patch.object(decompile, 'tool', return_value=self.script), \
             patch.object(decompile.shutil, 'which', return_value='clang'), \
             patch.object(decompile.subprocess, 'run', side_effect=results):
            row = decompile.draft(self.root, 'f', self.asm, 'mwcc -c f.c', 'f.c', self.root / 'cache')
        self.assertEqual(row['status'], 'unsupported')
        self.assertEqual(row['diagnostics']['unresolved_context_transfers'], 1)
        self.assertEqual(decompile.prompt_context(row), '')

    def test_old_corpus_cache_rebuilt_with_assembly_paths(self):
        report = self.root / 'report.json'
        report.write_text('{"units": []}', encoding="utf-8", newline="\n")
        path = self.root / 'build' / PROJECT_VERSION / 'asm/test.s'
        path.parent.mkdir(parents=True)
        path.write_text(self.asm.read_text(encoding="utf-8"))
        cache = self.root / 'corpus.json'
        cache.write_text(json.dumps({'report_mtime': report.stat().st_mtime, 'functions': {}}), encoding="utf-8", newline="\n")
        index = corpus.load_index(self.root, report, cache)
        self.assertEqual(Path(index['functions']['f']['asm_path']), Path('build', PROJECT_VERSION, 'asm', 'test.s'))
        self.assertEqual(index['version'], corpus.INDEX_VERSION)

    def test_similarity_queries_are_cached_and_report_changes_refresh_matches(self):
        index = self.root / 'build/simindex/index.pkl'
        index.parent.mkdir(parents=True)
        index.touch()
        report = self.root / 'build' / PROJECT_VERSION / 'report.json'
        report.parent.mkdir(parents=True, exist_ok=True)
        report.touch()
        os.utime(report, (10, 10))
        os.utime(index, (20, 20))
        result = subprocess.CompletedProcess([], 0,
            '{"references": [{"symbol": "g", "similarity": 0.9}], "assembly_files": []}', '')
        with patch.object(templates, 'tool_python', return_value=str(self.script)), \
             patch.dict(templates._RESULTS, {}, clear=True), \
             patch.object(templates.subprocess, 'run', return_value=result) as run:
            self.assertEqual(templates.matched_templates(self.root, 'f')[0]['symbol'], 'g')
            templates.matched_templates(self.root, 'f')
            run.assert_called_once()
            os.utime(report, (30, 30))
            self.assertEqual(templates.matched_templates(self.root, 'f')[0]['symbol'], 'g')
            self.assertEqual(run.call_count, 2)

    def test_shape_references_do_not_require_shared_callees(self):
        index = {'functions': {'f': {'callees': [], 'data': [], 'insns': 10},
                               'g': {'pct': 100, 'source': 'other.c', 'callees': [], 'data': [], 'insns': 10}}}
        with patch.object(campaign.corpus, 'load_index', return_value=index), \
             patch.object(campaign.templates, 'matched_templates', return_value=[{'symbol': 'g', 'similarity': 0.9}]), \
             patch.object(campaign, '_clean_body', return_value='int g(void) { return 1; }'), \
             patch.object(campaign, '_headers_text', return_value=''):
            prompt = campaign.corpus_context({'symbol': 'f', 'source': 'f.c'}, '')
        self.assertIn('normalized assembly similarity 90%', prompt)
        self.assertIn('int g(void)', prompt)
        self.assertIn('NOT semantic equality', prompt)

    def test_focused_prompt_lists_region_obligations_and_does_not_leak_sibling_locals(self):
        source = ('void sibling(void) {\n    int not_here;\n}\n'
                  'int f(int x) {\n    int handle;\n    int count;\n'
                  '    handle = x;\n    count = handle + 1;\n    return count;\n}\n')
        prompt = campaign.focused_prompt('base', source, (6, 8))
        self.assertIn('int handle;', prompt)
        self.assertIn('Region declarations needed by untouched lines: count.', prompt)
        self.assertIn('NOT the PPC register names', prompt)
        self.assertNotIn('not_here', prompt)

    def test_bad_brace_boundary_rejected_before_compilation(self):
        source = 'int f(int x) {\n    if (x) {\n        x++;\n    }\n    return x;\n}\n'
        with self.assertRaisesRegex(ValueError, 'brace boundary'):
            campaign.apply_focused(source, 'f', (2, 3), '```c\n    x++;\n```')
        with self.assertRaisesRegex(ValueError, 'brace boundary'):
            campaign.apply_focused(source, 'f', (3, 3), '```c\n    }\n    {\n```')
        correct = campaign.apply_focused(source, 'f', (2, 3), '```c\n    if (x > 0) {\n        x += 1;\n```')
        self.assertIn('if (x > 0)', correct)
        self.assertIn('return x;', correct)
        self.assertEqual(campaign.brace_profile('/* } */ "{"'), (0, 0))

    def test_focused_inventory_excludes_locals_in_completed_blocks(self):
        source = ('void f(void) {\n    int outer;\n    {\n        int expired;\n'
                  '        expired = 1;\n    }\n    outer = 2;\n}\n')
        prompt = campaign.focused_boundaries(source, (7, 7))
        self.assertIn('int outer;', prompt)
        self.assertNotIn('expired', prompt)

    def test_draft_is_skipped_for_exact_and_register_only_tasks(self):
        with patch.object(campaign.decompile, 'draft') as draft:
            self.assertEqual(campaign.assembly_draft_context({'prompt_pct': 100.0}), '')
            self.assertEqual(campaign.assembly_draft_context({'prompt_pct': 99.0, 'structural_hunks': 0}), '')
        draft.assert_not_called()

    def test_real_call_and_getter_are_not_labelled_unwritten(self):
        self.assertFalse(campaign.is_placeholder('void f(char* s) { report(s); }', 12))
        self.assertFalse(campaign.is_placeholder('int f(void) { return table[0]; }', 12))
        self.assertTrue(campaign.is_placeholder('int f(int a) { (void)a; return 0; }', 12))
        self.assertTrue(campaign.is_placeholder('void f(int a) { (void)a; }', 12))
        self.assertFalse(campaign.is_placeholder('int f(void) { // explanation\n return value; }', 12))

    def test_similarity_core_excludes_shared_stack_frame(self):
        frame = ['mflr G0', 'stw G0,S(G1)', 'stwu G1,M(G1)', 'lwz G0,M(G1)',
                 'addi G1,G1,M', 'mtlr G0', 'blr ']
        self.assertEqual(templates.core_shape(frame), [])
        self.assertEqual(templates.core_shape(frame[:3] + ['lwz G2,M(G3)', 'slwi G4,G2,S'] + frame[3:]),
                         ['lwz G2,M(G3)', 'slwi G4,G2,S'])

    def test_preprocessor_resolves_root_relative_scoring_wrappers(self):
        result = subprocess.CompletedProcess([], 0, 'int f(void);', '')
        with patch.object(decompile.shutil, 'which', return_value='clang'), \
             patch.object(decompile.subprocess, 'run', return_value=result) as run:
            decompile.preprocess(self.root, 'mwcc -i include -DBUILD=1 -c wrapper.c', 'wrapper.c')
        command = run.call_args.args[0]
        self.assertIn(str(self.root), command)
        self.assertIn('include', command)
        self.assertIn('-DBUILD=1', command)

    def test_batch_selects_all_inexact_and_deduplicates_report_views(self):
        report = {'units': [
            {'name': 'main/real', 'metadata': {'module_id': 0, 'source_path': 's.c'}, 'functions': [
                {'name': 'f', 'metadata': {'virtual_address': '1'}, 'fuzzy_match_percent': 99.99999},
                {'name': 'exact', 'metadata': {'virtual_address': '2'}, 'fuzzy_match_percent': 100}]},
            {'name': 'main/auto', 'metadata': {'module_id': 0, 'auto_generated': True}, 'functions': [
                {'name': 'f', 'metadata': {'virtual_address': '1'}, 'fuzzy_match_percent': 0}]},
            {'name': 'main/asm_only', 'metadata': {'module_id': 0}, 'sections': [{'functions': [
                {'name': 'unknown', 'metadata': {'virtual_address': '3'}}]}]},
            {'name': 'empty', 'functions': None}]}
        rows = batch.targets(report)
        self.assertEqual({row['symbol'] for row in rows}, {'f', 'unknown'})
        self.assertEqual(next(row['source'] for row in rows if row['symbol'] == 'f'), 's.c')
        self.assertEqual(len({row['id'] for row in rows}), 2)

    def test_batch_compile_commands_preserve_quoted_source_paths(self):
        commands = 'not a compiler\nbuild/compilers/mwcceppc.exe -c "src/a file.c" -i include && dep-tool\n'
        self.assertEqual(batch.command_map(commands)['src/a file.c'], commands.splitlines()[1])

    def test_matched_corpus_requires_a_real_source_and_replays_separately(self):
        report = {'units': [
            {'name': 'real', 'metadata': {'source_path': 's.c'}, 'functions': [
                {'name': 'exact', 'fuzzy_match_percent': 100}, {'name': 'open', 'fuzzy_match_percent': 50}]},
            {'name': 'auto', 'metadata': {'auto_generated': True}, 'functions': [
                {'name': 'asm', 'fuzzy_match_percent': 100}]}]}
        self.assertEqual([row['symbol'] for row in batch.targets(report, 'matched')], ['exact'])
        self.assertEqual({row['symbol'] for row in batch.targets(report, 'all')}, {'exact', 'open', 'asm'})
        with self.assertRaises(ValueError):
            batch.targets(report, 'unknown')

    def test_m2c_local_edits_invalidate_draft_cache(self):
        result = subprocess.CompletedProcess([], 0, 'revision', '')
        with patch.object(decompile, 'tool', return_value=self.script), \
             patch.object(decompile.subprocess, 'run', return_value=result):
            first = decompile.tool_metadata()
            self.script.write_text('# changed implementation\n', encoding="utf-8", newline="\n")
            second = decompile.tool_metadata()
        self.assertEqual(first['revision'], second['revision'])
        self.assertNotEqual(first['fingerprint'], second['fingerprint'])

    def test_untyped_batch_draft_is_never_presented_as_typed_model_context(self):
        self.assertEqual(decompile.prompt_context({'status': 'ok', 'context_mode': 'assembly_only',
                                                  'draft': 'int f(void) { return 1; }'}), '')

    def test_batch_resume_and_replay_use_frozen_context_not_live_source(self):
        report = self.root / 'report.json'
        report.write_text(json.dumps({'units': [{'name': 'main/test', 'metadata': {'source_path': 'f.c'},
                                                'functions': [{'name': 'f', 'fuzzy_match_percent': 50}]}]}), encoding="utf-8", newline="\n")
        artifact = self.root / 'artifact'
        artifact.mkdir()
        (artifact / 'target.s').write_bytes(self.asm.read_bytes())
        (artifact / 'context.c').write_text('int f(void);', encoding="utf-8", newline="\n")
        metadata = {'revision': 'rev', 'fingerprint': 'first'}
        result = {'status': 'ok', 'context_mode': 'typed', 'draft': 'int f(void) { return 1; }',
                  'artifacts': str(artifact), 'error': ''}
        with patch.object(batch.m2c_report, 'support_root', return_value=self.root / 'build/decomp-support'), \
             patch.object(batch, 'ThreadPoolExecutor', side_effect=AssertionError('Serial mode must not start a pool')), \
             patch.object(campaign, 'REPORT_FILE', report), \
             patch.object(campaign, 'COORDINATOR', Coordinator(self.root / 'coord')), \
             patch.object(corpus, 'build_index', return_value={'functions': {'f': {'asm_path': 'target.s'}}}), \
             patch.object(batch.asm_inputs, 'jump_tables', return_value={}), \
             patch.object(decompile, 'tool_metadata', return_value=metadata), \
             patch.object(batch.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'mwcceppc -c f.c', '')), \
             patch.object(decompile, 'preprocess', return_value={'status': 'ok', 'context': 'int f(void);'}) as preprocess, \
             patch.object(decompile, 'translate', return_value=result) as translate, \
             contextlib.redirect_stdout(io.StringIO()):
            batch.run_batch(self.root)
            batch.run_batch(self.root, jobs=1)
            preprocess.assert_called_once()
            translate.assert_called_once()
            baseline = next((self.root / 'build/decomp-support/batches').iterdir())
            metadata['fingerprint'] = 'patched'
            (self.root / 'f.c').write_text('wrong live source', encoding="utf-8", newline="\n")
            batch.run_batch(self.root, jobs=1, baseline=baseline)
        self.assertEqual(translate.call_count, 2)
        self.assertEqual(translate.call_args.args[4]['context'], 'int f(void);')
        summary = json.loads((baseline / 'manifest.json').read_text(encoding="utf-8"))
        self.assertTrue(summary['finished'])
        self.assertEqual(summary['counts'], {'ok': 1})

    def test_one_bad_translation_does_not_discard_other_results(self):
        with patch.object(decompile, 'translate', side_effect=ValueError('bad table')):
            result = batch.translate_target(self.root, {'symbol': 'f'}, ('target.s', b'asm'), {}, self.root, {}, 1, False)
        self.assertEqual(result['status'], 'batch_error')
        self.assertIn('bad table', result['error'])

    def test_batch_dossier_binds_source_report_and_captured_assembly(self):
        assembly = ('.fn f, global\n'
                    '/* 80001000 00001000  4E 80 00 20 */ blr\n.endfn f\n').encode()
        path = self.root / 'build/TEST01/asm/test.s'
        path.parent.mkdir(parents=True)
        path.write_bytes(assembly)
        source = self.root / 'src/test.c'
        source.parent.mkdir()
        source.write_text('int f(void) { return 0; }', encoding="utf-8", newline="\n")
        report = self.root / 'report.json'
        report.write_text('{}', encoding="utf-8", newline="\n")
        report_hash = batch.investigation.digest(report.read_bytes())
        row = {'symbol': 'f', 'source': 'src/test.c', 'unit': 'main/test', 'address': '0x80001000'}
        index = {'functions': {'f': {'asm_path': str(path.relative_to(self.root)), 'source': row['source'],
                 'unit': row['unit'], 'callees': [], 'data': [], 'insns': 1, 'pct': 10}}}
        with patch.object(campaign, 'owner_source', return_value=row['source']):
            evidence = batch.target_evidence(self.root, row, index, report, report_hash, (str(path), assembly))
            self.assertTrue(evidence['available'])
            self.assertEqual(evidence['dossier']['identity']['report_sha256'], report_hash)
            self.assertEqual(evidence['dossier']['identity']['signature'], 'int f(void)')
            changed = batch.target_evidence(self.root, row, index, report, report_hash, (str(path), b'changed'))
            self.assertFalse(changed['available'])
            self.assertIn('Assembly changed', changed['reason'])
            report.write_text('{"changed":true}', encoding="utf-8", newline="\n")
            self.assertFalse(batch.target_evidence(self.root, row, index, report, report_hash, (str(path), assembly))['available'])

    def test_annotation_hashes_draft_but_does_not_invent_a_verified_compilation(self):
        artifact = self.root / 'draft'
        artifact.mkdir()
        draft = b'int f(void) { return 1; }'
        (artifact / 'draft.c').write_bytes(draft)
        row = {'status': 'ok', 'artifacts': str(artifact)}
        batch.annotate_result(row, {'available': False, 'reason': 'missing source'})
        self.assertEqual(row['draft_sha256'], batch.investigation.digest(draft))
        self.assertEqual(row['obligations']['candidate_sha256'], row['draft_sha256'])
        self.assertFalse(row['obligations']['ready'])
        self.assertTrue(all(gate['state'] == 'pending' for gate in row['obligations']['gates']))

    def test_replay_preserves_known_c_reference_when_assembly_is_missing(self):
        baseline = self.root / 'baseline'
        baseline.mkdir()
        reference = {'source': 'f.c', 'function': str(baseline / 'f.c'), 'sha256': 'known-c'}
        row = {'id': 'missing', 'symbol': 'f', 'unit': 'main/f', 'source': 'f.c',
               'module': 'main', 'address': 1, 'pct': 100.0, 'auto_generated': False,
               'status': 'missing_assembly', 'reference': reference,
               'investigation': {'available': False, 'reason': 'Original evidence unavailable'}}
        (baseline / 'results.json').write_text(json.dumps({
            'summary': {'finished': True, 'report_sha256': 'report', 'selection': 'matched'},
            'results': [row]}), encoding="utf-8", newline="\n")
        with patch.object(batch.m2c_report, 'support_root', return_value=self.root / 'build/decomp-support'), \
             patch.object(campaign, 'COORDINATOR', Coordinator(self.root / 'coord')), \
             patch.object(batch.asm_inputs, 'jump_tables', return_value={}), \
             patch.object(decompile, 'tool_metadata', return_value={'fingerprint': 'patched'}), \
             patch.object(decompile, 'translate') as translate, \
             patch.object(batch.m2c_report, 'refresh_report_safely'), \
             contextlib.redirect_stdout(io.StringIO()):
            batch.run_batch(self.root, baseline=baseline)
        translate.assert_not_called()
        replay = next((self.root / 'build/decomp-support/batches').iterdir())
        result = json.loads((replay / 'results.json').read_text(encoding="utf-8"))['results'][0]
        self.assertEqual(result['status'], 'missing_assembly')
        self.assertEqual(result['reference'], reference)
        self.assertEqual(result['investigation'], row['investigation'])

    def test_cli_help_can_format_percent_argument(self):
        with patch('sys.argv', ['draft', '--help']), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as exit:
                decompile.main()
        self.assertEqual(exit.exception.code, 0)


if __name__ == '__main__':
    unittest.main()
