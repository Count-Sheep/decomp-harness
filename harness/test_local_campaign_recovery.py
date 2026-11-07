import json
from pathlib import Path
from contextlib import nullcontext
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import local_campaign_knowledge as knowledge
import local_campaign_recovery as recovery
from local_campaign_decompile import tool

M2C_PARSER = (tool().parent / "m2c_pycparser").is_dir()

class RecoveryTests(unittest.TestCase):
    @unittest.skipUnless(M2C_PARSER, "optional m2c parser is unavailable")
    def test_declaration_candidates_are_bounded_deterministic_and_bound_to_one_base(self):
        source = "int f(void) {\n    int a;\n    int b;\n    int c;\n\n    return 1;\n}"
        block = recovery.c89.declaration_order_block(source, "", "f")
        pairs = list(recovery.declaration_candidates(source, block, cap=2, seed=1234))
        self.assertEqual(pairs, list(recovery.declaration_candidates(source, block, cap=2, seed=1234)))
        self.assertEqual(len(pairs), 2)
        self.assertEqual(len({candidate for candidate, _ in pairs}), 2)
        start, end, lines = block
        for candidate, recipe in pairs:
            self.assertNotEqual(candidate, source)
            self.assertEqual(candidate[:start], source[:start])
            self.assertEqual(candidate[end:], source[end:])
            self.assertEqual(sorted(recipe["order"]), [0, 1, 2])
            self.assertEqual(candidate[start:end], "".join(lines[i] for i in recipe["order"]))
            self.assertEqual(recipe["base_candidate_sha256"], knowledge.digest(source))
        self.assertEqual(len(list(recovery.declaration_candidates(source, block, cap=5, seed=1))), 5)
        self.assertEqual(list(recovery.declaration_candidates(source, block, cap=0, seed=1)), [])
        with self.assertRaisesRegex(ValueError, "bind uniquely"):
            list(recovery.declaration_candidates("changed" + source, block, cap=2, seed=1))

    def test_object_search_does_not_stop_on_scorer_percentage_alone(self):
        self.assertFalse(recovery.object_verified({"pct": 100, "verification": {"policy_screen_passed": True}}))
        axes = {key: True for key in ("policy_screen_passed", "raw_candidate_bytes", "relocations", "owner_object")}
        self.assertTrue(recovery.object_verified({"compile_ok": True, "verification": axes}))
        self.assertFalse(recovery.object_verified({"compile_ok": False, "verification": axes}))
        for axis in axes:
            self.assertFalse(recovery.object_verified({"compile_ok": True, "verification": {**axes, axis: False}}))

    def test_declaration_budget_rejects_unbounded_runs_before_creating_artifacts_or_requesting_model(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            prior = root / "prior"
            self.native_fixture(prior)
            with patch.object(recovery.campaign, "ollama") as model:
                for budget in (-1, 721):
                    output = root / str(budget)
                    with self.subTest(budget=budget), self.assertRaisesRegex(ValueError, "budget"):
                        recovery.run(prior, output, "unused", "unused", declaration_orderings=budget)
                    self.assertFalse(output.exists())
            model.assert_not_called()

    def test_nested_schema_envelope_and_nonce_probe_fail_closed(self):
        schema = {"type": "object", "properties": {"x": {"const": "ok"}}}
        envelope = recovery.schema_format(schema)
        self.assertEqual(envelope["json_schema"]["schema"], schema)
        self.assertNotIn("schema", envelope)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with patch.object(recovery.secrets, "token_hex", return_value="nonce"), \
                 patch.object(recovery.campaign, "ollama", return_value='{"probe":"nonce"}') as model:
                evidence = recovery.schema_probe("http://rpc/v1", "model", root / "passed")
            self.assertTrue(evidence["passed"])
            self.assertEqual(model.call_args.kwargs["response_format"]["json_schema"]["schema"]["properties"]["probe"],
                             {"const": "nonce"})
            self.assertNotIn("nonce", model.call_args.args[2])
            with patch.object(recovery.secrets, "token_hex", return_value="nonce"), \
                 patch.object(recovery.campaign, "ollama", return_value='{"probe":"wrong"}'):
                with self.assertRaisesRegex(ValueError, "did not enforce"):
                    recovery.schema_probe("http://rpc/v1", "model", root / "failed")
            self.assertFalse(json.loads((root / "failed/result.json").read_text(encoding="utf-8"))["passed"])

    def test_schema_enumerates_unique_body_anchors_not_the_signature(self):
        source = "int f(void) /* { */ {\n    int value;\n    value = 1;\n    return value;\n}"
        schema = recovery.edit_schema(source, "f")
        spec = schema["properties"]["edits"]
        self.assertEqual(spec["minItems"], 1)
        self.assertEqual(spec["maxItems"], 1)
        anchors = spec["items"]["properties"]["old"]["enum"]
        self.assertTrue(anchors)
        self.assertTrue(all(source.count(anchor) == 1 and len(anchor) <= 512 and "int f(" not in anchor for anchor in anchors))
        old = "    value = 1;\n"
        response = json.dumps({"edits": [{"old": old, "new": "    value = 2;\n"}]})
        revised = recovery.apply_edits(source, response, "f", schema=schema)
        self.assertEqual(revised, source.replace(old, "    value = 2;\n"))

    def test_schema_limits_are_checked_locally_even_if_server_ignores_them(self):
        source = "int f(void) {\n    int value;\n    value = 1;\n    return value;\n}"
        schema = recovery.edit_schema(source, "f")
        for edits in ([{"old": "value = 1;", "new": "value = 2;"}],
                      [{"old": "    value = 1;\n", "new": "x" * 513}],
                      [{"old": "    value = 1;\n", "new": "    value = 2;\n"},
                       {"old": "    return value;\n", "new": "    return 2;\n"}]):
            with self.subTest(edits=edits), self.assertRaises(ValueError):
                recovery.apply_edits(source, json.dumps({"edits": edits}), "f", schema=schema)

    def test_schema_excludes_duplicate_lines_and_comment_only_body(self):
        source = "void f(void) {\n    call();\n    call();\n}"
        anchors = recovery.edit_schema(source, "f")["properties"]["edits"]["items"]["properties"]["old"]["enum"]
        self.assertNotIn("    call();\n", anchors)
        with self.assertRaisesRegex(ValueError, "No unique"):
            recovery.edit_schema("void f(void) {\n    /* comment */\n}", "f")

    def test_schema_prompt_has_one_small_edit_contract(self):
        prompt = recovery.recovery_prompt("base", "candidate", "feedback", revision_protocol="schema-edits-v1")
        self.assertIn("exactly one focused edit", prompt)
        self.assertNotIn("one to four", prompt)
        self.assertIn("512 characters", prompt)

    def test_json_edits_preserve_unedited_bytes_and_use_original_offsets(self):
        source = "int f(void) {\n    int value;\n    value = 1;\n    return value;\n}"
        edits = {"edits": [{"old": "value = 1;", "new": "value = 12345;"},
                           {"old": "return value;", "new": "return value + 1;"}]}
        result = recovery.apply_edits(source, "```json\n" + json.dumps(edits) + "\n```", "f")
        self.assertEqual(result, "int f(void) {\n    int value;\n    value = 12345;\n    return value + 1;\n}")

    def test_json_edits_reject_ambiguous_overlapping_missing_or_external_anchors(self):
        source = "int f(void) { int value; value = 1; return value; }"
        cases = [
            [{"old": "value", "new": "other"}],
            [{"old": "missing", "new": "other"}],
            [{"old": "int f(void)", "new": "void f(void)"}],
            [{"old": "value = 1;", "new": "value = 2;"}, {"old": "= 1;", "new": "= 3;"}],
            [{"old": "return value;", "new": "return value;"}],
            [{"old": "", "new": "other"}],
            [{"old": "return value;", "new": "x" * 2001}],
            [], [{"old": "return value;", "new": "", "extra": True}],
        ]
        for edits in cases:
            with self.subTest(edits=edits), self.assertRaises(ValueError):
                recovery.apply_edits(source, json.dumps({"edits": edits}), "f")

    def test_json_edits_reject_duplicate_keys_schema_errors_and_commentary(self):
        for response in ('{"edits": [], "edits": []}', '{"edits": "wrong"}', '[]',
                         '```c\nint f(void) { return 2; }\n```',
                         'Here is a patch: {"edits": []}'):
            with self.subTest(response=response), self.assertRaises(ValueError):
                recovery.apply_edits("int f(void) { return 1; }", response, "f")

    def test_edit_prompt_has_a_single_json_response_contract(self):
        prompt = recovery.recovery_prompt("base", "candidate", "feedback", revision_protocol="edits-v1")
        self.assertIn("Return only one JSON object", prompt)
        self.assertNotIn("Return only the complete", prompt)
        self.assertIn('"old":', prompt)
        self.assertIn("all refer to the current candidate", prompt)

    def native_fixture(self, prior, source="int f(void) { return 1; }"):
        key = "a" * 64
        assembly, declarations = "glabel f\nli r3, 1\nblr\n", "int f(void);"
        pack = {"target_id": key, "references": [], "reference_content_chars": 0}
        item = {"target_id": key, "symbol": "f", "signature": "int f(void)",
                "source": "src/f.c", "unit": "main/f", "assembly_sha256": knowledge.digest(assembly),
                "declarations_sha256": knowledge.digest(declarations),
                "references_sha256": knowledge.digest(knowledge.encoded(pack)), "reference_ids": [],
                "baseline_measurement": {"pct": 50, "compile_ok": True}}
        config = {"mode": "actual_unmatched_project_m2c_baseline", "native_c": True,
                  "snapshot_id": "snapshot", "split_sha256": "split", "workspace_sha256": "workspace",
                  "environment_sha256": "environment", "baseline_cache_root": str(prior / "baselines")}
        selection = {"configuration": config, "targets": [key], "excluded": [key], "split_sha256": "split"}
        data = {"configuration": config, "inputs": [item]}
        previous = {"summary": {"targets": [key], "finished": True, "environment_unchanged": True,
                                "configuration_sha256": knowledge.digest(knowledge.encoded(config))},
                    "results": [{**item, "native_declarations_sha256": knowledge.digest(declarations),
                        "measurement": {"draft_sha256": knowledge.digest(source), "verification": {
                            "candidate_sha256": knowledge.digest(source),
                            "declarations_sha256": knowledge.digest(declarations)}}}]}
        prior.mkdir()
        folder = prior / key
        folder.mkdir()
        for name, text in (("target.s", assembly), ("declarations.c", declarations),
                           ("native-declarations.c", declarations), ("draft.c", source)):
            (folder / name).write_text(text, encoding="utf-8", newline="\n")
        for path, record in ((folder / "references.json", pack), (prior / "inputs.json", data),
                             (prior / "results.json", previous), (prior / "selection.json", selection),
                             (prior / "cohort.json", {"excluded": [key], "split_sha256": "split"})):
            path.write_text(json.dumps(record), encoding="utf-8", newline="\n")
        return data, previous, selection

    def test_native_capture_uses_measured_context_seed_and_target_without_assigned_source(self):
        with tempfile.TemporaryDirectory() as name:
            prior = Path(name) / "prior"
            data, previous, selection = self.native_fixture(prior)
            item = data["inputs"][0]
            capture = recovery.native_inputs(prior, data, previous, selection, [item])[item["target_id"]]
            self.assertEqual(capture["seed"], "int f(void) { return 1; }")
            self.assertIn("Retail target assembly", capture["base_prompt"])
            self.assertIn("int f(void);", capture["base_prompt"])
            self.assertNotIn("return 1", capture["base_prompt"])
            for protocol in ("function", "edits-v1"):
                prompt = recovery.recovery_prompt(capture["base_prompt"], capture["seed"], "measured",
                                                 revision_protocol=protocol)
                self.assertEqual(prompt.count("Return only"), 1)
                self.assertEqual(prompt.count(capture["seed"]), 1)
                self.assertNotIn("Reconstruct this", prompt)
                self.assertNotIn("No assigned C implementation or previous attempt", prompt)

    def test_native_capture_rejects_changed_artifacts_before_inference(self):
        for name in ("draft.c", "target.s", "declarations.c", "native-declarations.c", "references.json"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                prior = Path(directory) / "prior"
                data, previous, selection = self.native_fixture(prior)
                item = data["inputs"][0]
                path = prior / item["target_id"] / name
                if name == "references.json":
                    record = json.loads(path.read_text(encoding="utf-8"))
                    record["references"] = [{"function_id": item["target_id"]}]
                    path.write_text(json.dumps(record), encoding="utf-8", newline="\n")
                else:
                    path.write_text(path.read_text(encoding="utf-8") + "changed")
                with self.assertRaises(ValueError):
                    recovery.native_inputs(prior, data, previous, selection, [item])

    def test_native_capture_rejects_identity_and_selection_changes(self):
        for field in ("symbol", "signature", "source", "unit"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as name:
                prior = Path(name) / "prior"
                data, previous, selection = self.native_fixture(prior)
                previous["results"][0][field] = "changed"
                with self.assertRaisesRegex(ValueError, "identity"):
                    recovery.native_inputs(prior, data, previous, selection, data["inputs"])
        with tempfile.TemporaryDirectory() as name:
            prior = Path(name) / "prior"
            data, previous, selection = self.native_fixture(prior)
            previous["summary"]["targets"] = []
            with self.assertRaisesRegex(ValueError, "selection"):
                recovery.native_inputs(prior, data, previous, selection, data["inputs"])

    def test_native_seed_is_freshly_measured_and_shared_baseline_is_used(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            prior, output = root / "prior", root / "output"
            data, previous, selection = self.native_fixture(prior)
            (root / "objdiff.json").write_text(json.dumps({"units": [{"name": "main/f"}]}), encoding="utf-8", newline="\n")
            store = types.SimpleNamespace(connection=types.SimpleNamespace(execute=lambda *args:
                types.SimpleNamespace(fetchone=lambda: [json.dumps({"workspace": str(root), "source_root": str(root)})])))
            coordinator = types.SimpleNamespace(build=lambda *args: nullcontext())
            with patch.object(recovery.campaign, "ROOT", root), \
                 patch.object(knowledge, "KnowledgeStore", return_value=nullcontext(store)), \
                 patch("local_campaign_knowledge_sweep.validate_selection"), \
                 patch.object(recovery, "fingerprint", return_value="workspace"), \
                 patch.object(recovery, "Coordinator", return_value=coordinator), \
                 patch.object(recovery, "model_lock", return_value=nullcontext()) as lock, \
                 patch.object(recovery.subprocess, "run", return_value=types.SimpleNamespace(stdout="")), \
                 patch.object(recovery, "command_map", return_value={"src/f.c": "compile"}), \
                 patch.object(recovery.validator, "environment_fingerprint", return_value="environment"), \
                 patch.object(recovery.decompile, "tool_metadata", return_value={"fingerprint": "engine"}), \
                 patch.object(recovery.decompile, "translate", return_value={"status": "error", "error": "no hypothesis"}), \
                 patch.object(recovery.decompile, "prompt_context", return_value=""), \
                 patch.object(recovery.validator, "validate_one", return_value={"compile_ok": True, "pct": 80,
                              "status": "non_exact"}) as measure, \
                 patch.object(recovery, "object_evidence", return_value={"policy_screen_passed": True}), \
                 patch.object(recovery.campaign, "ollama") as model:
                summary = recovery.run(prior, output, "unused", "unused", attempts=0)
                lock.side_effect = RuntimeError("model is occupied")
                with self.assertRaisesRegex(RuntimeError, "occupied"):
                    recovery.run(prior, root / "failed", "unused", "unused", attempts=0)
                failure = json.loads((root / "failed/manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(failure["phase"], "failed")
                self.assertTrue(failure["finished"])
                self.assertFalse(failure["environment_unchanged"])
            self.assertEqual(measure.call_count, 1)
            model.assert_not_called()
            self.assertEqual(measure.call_args.kwargs["baseline_root"], prior / "baselines")
            self.assertEqual(summary["compiled"], 1)
            self.assertTrue(summary["environment_unchanged"])
            self.assertEqual(summary["actual_unmatched_scorer_gains"], 0)
            self.assertEqual(summary["ready"], 0)
            self.assertEqual(summary["mode"], "actual_unmatched_native_development_recovery")
            self.assertTrue((output / data["inputs"][0]["target_id"] / "seed.c").is_file())

    @unittest.skipUnless(M2C_PARSER, "optional m2c parser is unavailable")
    def test_scripted_search_freshly_measures_every_recipe_and_retains_best_without_rpc(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            prior, output = root / "prior", root / "output"
            source = "int f(void) {\n    int a;\n    int b;\n    int c;\n\n    return 1;\n}"
            data, _, _ = self.native_fixture(prior, source=source)
            (root / "objdiff.json").write_text(json.dumps({"units": [{"name": "main/f"}]}), encoding="utf-8", newline="\n")
            store = types.SimpleNamespace(connection=types.SimpleNamespace(execute=lambda *args:
                types.SimpleNamespace(fetchone=lambda: [json.dumps({"workspace": str(root), "source_root": str(root)})])))
            coordinator = types.SimpleNamespace(build=lambda *args: nullcontext())
            with patch.object(recovery.campaign, "ROOT", root), \
                 patch.object(knowledge, "KnowledgeStore", return_value=nullcontext(store)), \
                 patch("local_campaign_knowledge_sweep.validate_selection"), \
                 patch.object(recovery, "fingerprint", return_value="workspace"), \
                 patch.object(recovery, "Coordinator", return_value=coordinator), \
                 patch.object(recovery, "model_lock", return_value=nullcontext()), \
                 patch.object(recovery.subprocess, "run", return_value=types.SimpleNamespace(stdout="")), \
                 patch.object(recovery, "command_map", return_value={"src/f.c": "compile"}), \
                 patch.object(recovery.validator, "environment_fingerprint", return_value="environment"), \
                 patch.object(recovery.decompile, "tool_metadata", return_value={"fingerprint": "engine"}), \
                 patch.object(recovery.decompile, "translate", return_value={"status": "error", "error": "no hypothesis"}), \
                 patch.object(recovery.decompile, "prompt_context", return_value=""), \
                 patch.object(recovery.validator, "validate_one", side_effect=[
                     {"compile_ok": True, "pct": pct, "status": "non_exact"} for pct in (80, 90, 70)]) as measure, \
                 patch.object(recovery, "object_evidence", return_value={"policy_screen_passed": True, "owner_object": True}) as gate, \
                 patch.object(recovery.campaign, "ollama") as model:
                summary = recovery.run(prior, output, "unused", "unused", attempts=0, declaration_orderings=2)
                self.assertEqual(measure.call_count, 3)
                measure.reset_mock(side_effect=True)
                measure.return_value = {"compile_ok": True, "pct": 80, "status": "non_exact"}
                gate.return_value = {"policy_screen_passed": True, "owner_object": False}
                skipped = recovery.run(prior, root / "skipped", "unused", "unused", attempts=0, declaration_orderings=2)
                self.assertEqual(skipped["declaration_attempts"], 0)
                skipped_row = json.loads((root / "skipped" / data["inputs"][0]["target_id"] / "result.json").read_text(encoding="utf-8"))
                self.assertEqual(skipped_row["declaration_search"]["reason"], "Base has not passed the owner/sibling audit")
                self.assertEqual(measure.call_count, 1)
            model.assert_not_called()
            self.assertEqual(summary["declaration_attempts"], 2)
            self.assertTrue(summary["environment_unchanged"])
            self.assertEqual(summary["ready"], 0)
            self.assertEqual(summary["object_verified"], 0)
            row = json.loads((output / data["inputs"][0]["target_id"] / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(row["best"]["pct"], 90)
            self.assertEqual(row["declaration_search"]["status"], "complete")
            self.assertEqual([a["pct"] for a in row["attempts"]], [80, 90, 70])
            for record in row["attempts"][1:]:
                self.assertEqual(record["transformation"]["base_candidate_sha256"], knowledge.digest(source))
                self.assertEqual(record["c89_repairs"], 0)
            self.assertEqual((output / data["inputs"][0]["target_id"] / "review.c").read_text(encoding="utf-8"),
                             Path(row["best"]["candidate_path"]).read_text(encoding="utf-8"))

    def test_best_candidate_never_regresses(self):
        good = {"compile_ok": True, "pct": 90}
        self.assertGreater(recovery.rank(good), recovery.rank({"compile_ok": False, "pct": 100}))
        self.assertGreater(recovery.rank(good), recovery.rank({"compile_ok": True, "pct": 80}))

    def test_feedback_preserves_aligned_gaps_and_omits_baseline_source(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            def cell(text):
                return {"instruction": {"formatted": text}} if text else {}
            data = {"left": {"symbols": [{"name": "f", "instructions": [cell("li r3,1"), cell("blr")]}]},
                    "right": {"symbols": [{"name": "f", "instructions": [cell("li r3,2"), cell("")]}]}}
            (folder / "objdiff.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")
            feedback = recovery.compiler_feedback({"compile_ok": True, "pct": 70, "artifacts": str(folder),
                                                   "baseline": {"error": "ASSIGNED_BODY"}}, "f")
            self.assertIn("li r3,1 | li r3,2", feedback)
            self.assertIn("blr | <gap>", feedback)
            self.assertNotIn("ASSIGNED_BODY", feedback)

    def test_baseline_errors_are_not_target_feedback(self):
        feedback = recovery.compiler_feedback({"status": "baseline_compile_error", "error": "ASSIGNED_BODY"}, "f")
        self.assertNotIn("ASSIGNED_BODY", feedback)
        self.assertIn("environment problem", feedback)

    def test_prompt_is_generated_candidate_plus_feedback_not_oracle(self):
        prompt = recovery.recovery_prompt("DECLARATIONS_AND_TARGET", "int f(void) { return 2; }", "li r3,1 | li r3,2")
        self.assertIn("NOT the assigned implementation", prompt)
        self.assertIn("C89", prompt)
        self.assertIn("return 2", prompt)
        self.assertIn("li r3,1 | li r3,2", prompt)
        prompt = recovery.recovery_prompt("base", "candidate", "feedback", "previous syntax error")
        self.assertIn("Last rejected or regressing revision", prompt)
        self.assertIn("previous syntax error", prompt)

    def test_symbolic_relocations_are_feedback_not_pointer_displacements(self):
        with tempfile.TemporaryDirectory() as name:
            row = {"compile_ok": True, "pct": 90, "artifacts": name,
                   "verification": {"target_records": {"retail": {"relocations": [
                       ["TARGET", 8, 109, 0, ["UND", "callbacks", 0, 1, 0, 0, 0]]]}}}}
            feedback = recovery.compiler_feedback(row, "f")
            self.assertIn("+0x8", feedback)
            self.assertIn("callbacks", feedback)
            self.assertIn("not C pointer offsets", feedback)

    def fake_verifiers(self, root, *, sibling_fail=False, target_fail=False):
        parser = types.SimpleNamespace(__name__="test_elf", __file__=__file__)
        parser.read_elf = lambda path: types.SimpleNamespace(path=path)
        def find(owner, symbol):
            if target_fail:
                raise ValueError("No sized target symbol")
            return symbol
        parser.find_target = find
        parser.target_record = lambda owner, target: {"size": 4, "bytes_sha256": "same", "relocations": []}
        def siblings(*args):
            if sibling_fail:
                raise ValueError("Sibling data changed")
            return {"state": "passed"}
        parser.audit_siblings = siblings
        policy = types.SimpleNamespace(__name__="test_policy", __file__=__file__,
                                       validate_definition_only=lambda *args: None)
        return types.SimpleNamespace(owner_extract=parser, owner_source=policy)

    def evidence(self, *, sibling_fail=False, target_fail=False, binding_fail=False):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            for file in ("candidate.o", "retail.o", "baseline.o"):
                (root / file).write_bytes(b"mock elf bytes")
            source = "int f(void) { return 1; }"
            result = {"compile_ok": True, "scope": "full_owner_scoring_unit", "compile_output": str(root / "candidate.o"),
                      "draft_sha256": "wrong" if binding_fail else knowledge.digest(source),
                      "baseline": {"compile_ok": True, "object": str(root / "baseline.o")}}
            module = self.fake_verifiers(root, sibling_fail=sibling_fail, target_fail=target_fail)
            with patch.dict(sys.modules, {"permuter": module}):
                return recovery.object_evidence(root, {"symbol": "f", "signature": "int f(void)"}, source, "", result,
                                                 {"target_path": "retail.o"})

    def test_object_proofs_never_imply_link_or_policy_review(self):
        evidence = self.evidence()
        self.assertTrue(evidence["raw_candidate_bytes"])
        self.assertTrue(evidence["relocations"])
        self.assertTrue(evidence["owner_object"])
        self.assertTrue(evidence["policy_screen_passed"])
        self.assertIsNone(evidence["policy_approved"])
        self.assertIsNone(evidence["linked_checksum"])
        self.assertIsNone(evidence["human_review"])
        self.assertFalse(evidence["ready"])

    def test_sibling_failure_and_unreadable_targets_fail_closed(self):
        evidence = self.evidence(sibling_fail=True)
        self.assertFalse(evidence["owner_object"])
        self.assertIn("Sibling data changed", evidence["errors"]["owner_object"])
        evidence = self.evidence(target_fail=True)
        self.assertIsNone(evidence["raw_candidate_bytes"])
        self.assertIsNone(evidence["relocations"])
        self.assertFalse(evidence["ready"])

    def test_mismatched_candidate_cannot_receive_compiled_object_proof(self):
        evidence = self.evidence(binding_fail=True)
        self.assertIn("input_binding", evidence["errors"])
        self.assertIsNone(evidence["raw_candidate_bytes"])
        self.assertIsNone(evidence["owner_object"])


if __name__ == "__main__":
    unittest.main()
