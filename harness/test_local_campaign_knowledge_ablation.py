from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import local_campaign_decompile as decompile
import local_campaign_knowledge as knowledge
import local_campaign_knowledge_ablation as ablation


class AblationTests(unittest.TestCase):
    def coverage_fixture(self):
        split = {"snapshot_id": "snapshot", "split_sha256": "split", "targets": ["old", "new", "control"],
                 "excluded": ["held"]}
        baseline = {"mode": "retrieval_audit", "snapshot_id": "snapshot", "split_sha256": "split",
                    "source_leaks": 0, "results": [
                        {"target_id": key, "reference_ids": ["ref"] if key == "old" else []}
                        for key in split["targets"]]}
        store = Mock()
        store.function.side_effect = lambda key: {"source_path": "src/" + key + ".c", "insns": 10}
        return store, split, baseline

    def test_coverage_delta_freezes_new_hits_and_uncovered_controls(self):
        store, split, baseline = self.coverage_fixture()
        with patch.object(knowledge, "retrieval_pack", side_effect=lambda s, key, **kw:
                          {"references": [] if key == "control" else [{"function_id": "ref"}]}):
            self.assertEqual(ablation.select_coverage_delta(store, split, baseline, 4), ["new", "control"])
            with self.assertRaisesRegex(ValueError, "at least one uncovered control"):
                ablation.select_coverage_delta(store, split, baseline, 1)

    def test_coverage_delta_rejects_wrong_incomplete_duplicate_and_leaking_audits(self):
        import copy
        store, split, baseline = self.coverage_fixture()
        cases = []
        for field, value in (("split_sha256", "other"), ("snapshot_id", "other"),
                             ("source_leaks", 1), ("mode", "paired_pilot")):
            cases.append({**baseline, field: value})
        cases.append({**baseline, "results": baseline["results"][:-1]})
        cases.append({**baseline, "results": baseline["results"] + baseline["results"][:1]})
        leaking = copy.deepcopy(baseline)
        leaking["results"][0]["reference_ids"] = ["held"]
        cases.append(leaking)
        for case in cases:
            with self.subTest(case=case), self.assertRaisesRegex(ValueError, "same holdout"):
                ablation.select_coverage_delta(store, split, case, 4)

    def test_coverage_delta_does_not_invent_tasks_when_coverage_is_unchanged(self):
        store, split, baseline = self.coverage_fixture()
        with patch.object(knowledge, "retrieval_pack", return_value={"references": []}):
            with self.assertRaisesRegex(ValueError, "No newly covered C targets"):
                ablation.select_coverage_delta(store, split, baseline, 4)

    def test_type_context_removes_all_bodies_and_global_initializers(self):
        parser_root = decompile.tool().parent
        if not (parser_root / "m2c_pycparser").is_dir():
            self.skipTest("optional m2c parser is unavailable")
        sys.path.insert(0, str(parser_root))
        try:
            result = ablation.declaration_context(
                "typedef unsigned int u32; typedef struct S { u32 x; } S; "
                "int secret = 12345; int helper(S *s) { return 98765; } "
                "int target(S *s) { return helper(s) + 88888; } "
                "int unused(int x) { return 77777; }",
                {"target", "helper", "secret"})
        finally:
            sys.path.remove(str(parser_root))
        self.assertIn("typedef unsigned int u32;", result)
        self.assertIn("u32 x;", result)
        self.assertIn("int secret;", result)
        self.assertIn("int helper(S *s);", result)
        self.assertIn("int target(S *s);", result)
        for secret in ("12345", "98765", "88888", "77777", "return", "unused"):
            self.assertNotIn(secret, result)

    def test_prompt_pair_changes_only_reference_context(self):
        target = {"id": "target", "symbol": "f", "source_text": "KNOWN_SECRET_BODY"}
        reference = {"function_id": "reference", "symbol": "g", "reason": "SHARED_CALLEE_REFERENCE",
                     "source_sha256": "hash", "source_text": "int g(void) { return 1; }", "assembly": "li r3,1"}
        prompts = ablation.prompt_pair(target, "int f(void)", "blr", "int f(void);", {"references": [reference]},
                                      "c89-symbolic-relocations-target-last-v2")
        separator = "## Retail target assembly"
        self.assertEqual(prompts["on"].split(separator)[-1], prompts["off"].split(separator)[-1])
        self.assertLess(prompts["on"].index("Optional compiler idiom"), prompts["on"].index(separator))
        self.assertIn("C89", prompts["off"])
        self.assertIn("encoded relocation displacement", prompts["off"])
        self.assertNotIn("int g(void)", prompts["off"])
        self.assertIn("int g(void)", prompts["on"])
        self.assertNotIn("KNOWN_SECRET_BODY", "".join(prompts.values()))
        empty = ablation.prompt_pair(target, "int f(void)", "blr", "", {"references": []})
        self.assertEqual(empty["on"], empty["off"])
        with self.assertRaisesRegex(ValueError, "Target source"):
            ablation.prompt_pair(target, "int f(void)", "blr", "", {"references": [{**reference, "function_id": "target"}]})

    def test_revision_context_keeps_references_and_target_but_does_not_ask_for_reconstruction(self):
        target = {"id": "target", "source_text": "ASSIGNED_SECRET"}
        reference = {"function_id": "ref", "symbol": "g", "reason": "SHARED_CALLEE_REFERENCE",
                     "source_sha256": "hash", "source_text": "int g(void) { return 2; }", "assembly": "li r3,2"}
        prompts = ablation.prompt_pair(target, "int f(void)", "li r3,1\nblr", "int f(void);",
            {"references": [reference]}, "c89-symbolic-relocations-target-last-v2", revision_context=True)
        for prompt in prompts.values():
            self.assertIn("Revise the existing generated", prompt)
            self.assertIn("not start a new decompilation", prompt)
            self.assertIn("li r3,1\nblr", prompt)
            self.assertIn("int f(void);", prompt)
            for forbidden in ("Reconstruct this", "previous attempt is supplied", "Return only", "ASSIGNED_SECRET"):
                self.assertNotIn(forbidden, prompt)
        self.assertIn("int g(void)", prompts["on"])
        self.assertNotIn("int g(void)", prompts["off"])

    def test_default_reconstruction_contract_is_unchanged(self):
        expected = (
            "Reconstruct this PowerPC function as C for MWCC. Match the retail instruction bytes.\n"
            "Return only the complete function in a C code block, using the exact signature below.\n"
            "Do not add includes, macros, pragmas, assembly, guessed struct fields or volatile qualifiers.\n"
            "Use the supplied declarations and preserve evaluation order, signedness, constants and branch behavior.\n"
            "No assigned C implementation or previous attempt is supplied. Types are project-provided contracts.\n\n"
            "## Required signature\n```c\nint f(void)\n```\n\n"
            "## Type and symbol declarations (no function bodies or global initializers)\n```c\nint f(void);\n```\n\n"
            "## Retail target assembly\n```asm\nblr\n```\n")
        prompts = ablation.prompt_pair({"id": "target"}, "int f(void)", "blr", "int f(void);", {"references": []})
        self.assertEqual(prompts, {"off": expected, "on": expected})

    def test_inline_contracts_keep_types_but_never_bodies_or_initializers(self):
        parser_root = decompile.tool().parent
        if not (parser_root / "m2c_pycparser").is_dir():
            self.skipTest("optional m2c parser is unavailable")
        sys.path.insert(0, str(parser_root))
        try:
            source = ("typedef float f32; typedef struct Vec { f32 x; } Vec; "
                      "f32 secret = 12345; static inline f32 length(Vec *v) { return 98765; } "
                      "inline f32 square(f32 x); int unused(int x) { return 77777; } "
                      "int target(int x) { return 88888; }")
            before = ablation.declaration_context(source, {"target"})
            after = ablation.declaration_context(source, {"target"}, inline_contracts=True)
            self.assertNotIn("length", before)
            self.assertNotIn("square", before)
            self.assertIn("length(Vec *v);", after)
            self.assertIn("square(f32 x);", after)
            self.assertIn("typedef float f32;", after)
            self.assertIn("f32 x;", after)
            for excluded in ("12345", "98765", "77777", "88888", "return", "unused", "secret"):
                self.assertNotIn(excluded, after)
        finally:
            sys.path.remove(str(parser_root))

    def test_inline_guidance_is_shared_by_both_arms_and_controls_remain_identical(self):
        prompts = ablation.prompt_pair({"id": "target"}, "int f(void)", "blr", "inline int helper(void);",
                                      {"references": []}, inline_contracts=True)
        self.assertEqual(prompts["off"], prompts["on"])
        self.assertIn("without their implementations", prompts["off"])
        self.assertIn("not proof of equivalence", prompts["off"])

    def test_assembly_keeps_labels_and_checks_instruction_identity(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            folder = root / "build/TEST01/asm"
            folder.mkdir(parents=True)
            body = (".fn f, global\n"
                    "/* 80000000 00000000 48 00 00 04 */ b .L_80000004\n"
                    ".L_80000004:\n"
                    "/* 80000004 00000004 4E 80 00 20 */ blr\n.endfn f\n")
            (folder / "sample.s").write_text(body, encoding="utf-8", newline="\n")
            target = {"unit": "main/sample", "symbol": "f", "strict_hash": knowledge.digest("b .L_80000004\nblr ")}
            self.assertIn(".L_80000004:", ablation.extract_assembly(root, target, "TEST01"))
            target["strict_hash"] = "changed"
            with self.assertRaisesRegex(ValueError, "instruction identity"):
                ablation.extract_assembly(root, target, "TEST01")

    def test_model_lock_refuses_a_busy_campaign_runner_and_releases_its_own_lock(self):
        import portable
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            runners = root / "build/local_llm_campaign/runners"
            runners.mkdir(parents=True)
            with (runners / "worker.lock").open("a+") as held:
                portable.flock(held, portable.LOCK_EX | portable.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError, "using the model"):
                    with ablation.model_lock(root):
                        pass
                portable.flock(held, portable.LOCK_UN)
            with ablation.model_lock(root):
                pass
            with ablation.model_lock(root):
                pass

    def test_paired_summary_counts_compile_gains_and_losses_without_verifying(self):
        rows = [
            {"off": {"status": "non_exact", "compile_ok": True, "pct": 80},
             "on": {"status": "scorer_exact", "compile_ok": True, "pct": 100}},
            {"off": {"status": "compile_error", "compile_ok": False},
             "on": {"status": "non_exact", "compile_ok": True, "pct": 70}},
            {"off": {"status": "non_exact", "compile_ok": True, "pct": 90},
             "on": {"status": "compile_error", "compile_ok": False}},
            {"preparation_error": "no context"},
        ]
        result = ablation.paired_summary(rows)
        self.assertEqual(result["paired_scored"], 1)
        self.assertEqual(result["score_wins"], 1)
        self.assertEqual(result["new_compiles"], 1)
        self.assertEqual(result["lost_compiles"], 1)
        self.assertEqual(result["arms"]["on"]["scorer_exact"], 1)
        self.assertIsNone(result["verified_gain"])

    def test_identical_prompt_control_noise_is_reported(self):
        rows = [{"prompt_sha256": {"off": "same", "on": "same"},
                 "off": {"status": "scorer_exact", "pct": 100, "compile_ok": True},
                 "on": {"status": "compile_error", "compile_ok": False}}]
        result = ablation.paired_summary(rows)
        self.assertEqual(result["identical_prompt_controls"], 1)
        self.assertEqual(result["control_outcome_changes"], 1)
        self.assertIn("diverged", result["interpretation_warning"])

    def test_paired_report_labels_context_and_retrieval_experiments(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            summary = {"configuration_sha256": "config", "snapshot_id": "snapshot", "split_sha256": "split",
                       "retrieval_version": "cross-size", "declaration_context_version": "inline-contracts",
                       "coverage_baseline": {"sha256": "earlier-audit"}}
            (root / "results.json").write_text(json.dumps({"summary": summary, "results": []}), encoding="utf-8", newline="\n")
            report = ablation.report_paired_run(root)
            for key in ("retrieval_version", "declaration_context_version", "coverage_baseline"):
                self.assertEqual(report[key], summary[key])


if __name__ == "__main__":
    unittest.main()
