import copy
import json
from pathlib import Path
import tempfile
import unittest

import m2c_native_search as native


def result(options=None, pct=90, before="fc0100bc", after="fc0100b8", *, exact=False):
    return {"options": options or {"mwcc_fused_subtract": False, "reg_vars": []},
            "measurement": {"outcome": {"compiled": True, "profile": "2.6", "pct": pct}},
            "object_audit": {"status": "checked", "scratch_object_equal": exact,
                "byte_feedback": {"target_bytes": 16, "candidate_bytes": 16, "different_words": 2,
                    "first_difference": {"offset": 4, "target_word": before, "candidate_word": after}}}}


class NativeSearchTests(unittest.TestCase):
    def test_string_variant_requires_failed_native_symbol_evidence(self):
        failed = result(pct=0)
        failed["object_audit"] = {"status": "unavailable"}
        failed["measurement"].update(generation_returncode=0, categories=["invalid_symbol_name"])
        failed["measurement"]["outcome"].update(compiled=False, profile=None)
        proposed = native.proposals(failed)
        self.assertTrue(proposed[0]["options"]["heuristic_strings"])
        self.assertNotEqual(native.variant_key(proposed[0]["options"]), native.variant_key(failed["options"]))
        for change in ({"stage": "template"}, {"options": proposed[0]["options"]}):
            self.assertEqual(native.proposals({**failed, **change}), [])
        failed["measurement"]["generation_returncode"] = 1
        self.assertEqual(native.proposals(failed), [])

    def test_search_can_follow_a_failed_repair_to_a_second_diagnostic(self):
        def evaluate(options, reason, index):
            record = result(options, pct=0)
            if options.get("ppc_stack_storage"):
                return result(options, pct=75)
            record["object_audit"] = {"status": "unavailable"}
            record["measurement"].update(generation_returncode=0)
            record["measurement"]["outcome"].update(compiled=False, profile=None)
            if options.get("heuristic_strings"):
                record["measurement"]["compiler_messages"] = ["undefined identifier 'sp8'"]
            else:
                record["measurement"]["categories"] = ["invalid_symbol_name"]
            return record

        report = native.search(evaluate, 2)
        self.assertEqual(len(report["attempts"]), 3)
        self.assertEqual(report["best_attempt"], 2)
        self.assertTrue(report["attempts"][2]["options"]["heuristic_strings"])
        self.assertTrue(report["attempts"][2]["options"]["ppc_stack_storage"])
        self.assertEqual(native.search(evaluate, 1)["best_attempt"], 0)

    def test_preserved_load_register_feedback_keeps_storage_mode(self):
        record = result({"ppc_stack_storage": True, "reg_vars": []}, before="83e10008", after="83c10008")
        proposals = native.proposals(record)
        self.assertEqual([p["options"]["reg_vars"] for p in proposals], [["r31"], ["r30"]])
        self.assertTrue(all(p["options"]["ppc_stack_storage"] for p in proposals))
        for before, after in (("83e10008", "83c1000c"), ("80610008", "80810008"),
                              ("93e10008", "93c10008")):
            self.assertEqual(native.proposals(result(before=before, after=after)), [])

    def test_near_exact_load_cluster_proposes_low_temporary_registers(self):
        record = result(pct=99.48, before="8001000c", after="80c1000c")
        record["object_audit"]["byte_feedback"].update(different_words=3, differences=[
            {"target_word": "8001000c", "candidate_word": "80c1000c"},
            {"target_word": "a8a50002", "candidate_word": "a8050002"},
            {"target_word": "7c050214", "candidate_word": "7c060214"},
        ])
        proposed = native.proposals(record)
        self.assertEqual([row["options"]["reg_vars"] for row in proposed], [["r6"], ["r5"]])
        self.assertTrue(all(row["reason"]["kind"] == "near_exact_load_temporary" for row in proposed))

        record["measurement"]["outcome"]["pct"] = 97.99
        self.assertEqual(native.proposals(record), [])

    def test_missing_stack_local_proposes_one_native_storage_variant(self):
        failed = result(pct=0)
        failed["object_audit"] = {"status": "unavailable"}
        failed["measurement"].update(generation_returncode=0,
                                      compiler_messages=["undefined identifier 'spC'"])
        failed["measurement"]["outcome"].update(compiled=False, profile=None)
        proposed = native.proposals(failed)
        self.assertTrue(proposed[0]["options"]["ppc_stack_storage"])
        self.assertNotEqual(native.variant_key(failed["options"]), native.variant_key(proposed[0]["options"]))
        for change in ({"stage": "template"}, {"options": proposed[0]["options"]}):
            self.assertEqual(native.proposals({**failed, **change}), [])
        failed["measurement"]["compiler_messages"] = ["undefined identifier 'M2C_UNK'"]
        self.assertEqual(native.proposals(failed), [])

    def test_collapsed_compiled_stack_proposes_native_storage(self):
        record = result(pct=99.0)
        record["measurement"]["outcome"]["feedback"] = {
            "target_stack_frame": 352, "compiled_stack_frame": 48,
        }
        proposed = native.proposals(record)
        self.assertEqual(len(proposed), 1)
        self.assertTrue(proposed[0]["options"]["ppc_stack_storage"])
        self.assertEqual(proposed[0]["reason"]["kind"], "collapsed_stack_storage")
        record["options"]["ppc_stack_storage"] = True
        self.assertNotIn("collapsed_stack_storage",
                         {row["reason"]["kind"] for row in native.proposals(record)})

    def test_exact_template_skips_native_generation(self):
        calls = []

        def template(candidate, index):
            calls.append((candidate, index))
            return result(pct=100, exact=True)

        def native_generator(*args):
            self.fail("Native generator must not run after verified exact template")

        report = native.route([{"source": "first"}, {"source": "second"}], template, native_generator, 3)
        self.assertEqual(len(calls), 1)
        self.assertTrue(report["native_generation_skipped"])
        self.assertEqual(report["native_generations"], 0)
        self.assertIsNone(report["improved"])
        self.assertFalse(report["linked"])

    def test_unknown_stack_type_requires_local_declaration_evidence(self):
        failed = result(pct=0)
        failed["object_audit"] = {"status": "unavailable"}
        failed["measurement"].update(generation_returncode=0,
                                      compiler_messages=["undefined identifier 'M2C_UNK'"])
        outcome = failed["measurement"]["outcome"]
        outcome.update(compiled=False, profile=None, error="#      23: M2C_UNK sp4C;\n")
        proposed = native.proposals(failed)
        self.assertEqual(proposed[0]["reason"]["kind"], "unknown_stack_storage")
        self.assertTrue(proposed[0]["options"]["ppc_stack_storage"])
        for error in ("", "# 23: M2C_UNK global;\n", "# 23: M2C_UNK *sp4C;\n",
                      "# 23: extern M2C_UNK sp4C;\n", "/* M2C_UNK sp4C; */"):
            outcome["error"] = error
            self.assertEqual(native.proposals(failed), [])
        outcome["error"] = "# 23: M2C_UNK sp4C;\n"
        for change in ({"stage": "template"}, {"options": proposed[0]["options"]}):
            self.assertEqual(native.proposals({**failed, **change}), [])
        failed["measurement"]["generation_returncode"] = 1
        self.assertEqual(native.proposals(failed), [])

    def test_template_score_100_without_byte_identity_cannot_skip_native(self):
        calls = []

        def generator(options, reason, index):
            calls.append(index)
            return result(options, pct=98)

        report = native.route([{}], lambda *args: result(pct=100), generator, 0)
        self.assertEqual(calls, [0])
        self.assertFalse(report["native_generation_skipped"])
        self.assertEqual(report["best_attempt"], 0)
        self.assertEqual(report["route"], "template")

    def test_bad_templates_fall_back_to_native_without_losing_best(self):
        failed = result(pct=0)
        failed["measurement"]["outcome"].update(compiled=False, profile=None)
        report = native.route([{}], lambda *args: failed, lambda options, *args: result(options, exact=True), 3)
        self.assertEqual(report["best_attempt"], 1)
        self.assertEqual(report["route"], "native")
        self.assertEqual(report["template_attempts"], 1)
        self.assertEqual(report["native_generations"], 1)

    def test_duplicate_rows_cannot_hide_a_missing_requested_symbol(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "inputs.json"
            snapshot.write_text(json.dumps({"capture_kind": "native_m2c_batch_inputs",
                "root": str(root), "version": "TEST", "rows": [{"symbol": "leaf"}, {"symbol": "leaf"}]}))
            with self.assertRaisesRegex(ValueError, "Missing or ambiguous"):
                native.run(snapshot, root / "engine", root / "out", ["leaf", "missing"])
            self.assertFalse((root / "out").exists())

    def test_fused_proposal_requires_matching_operand_bits(self):
        proposal = native.proposals(result())[0]
        self.assertTrue(proposal["options"]["mwcc_fused_subtract"])
        self.assertEqual(proposal["reason"]["offset"], 4)
        for before, after in (("fc0100bc", "fc2100b8"), ("80630000", "80640000"),
                              ("fc0100bc", ""), ("garbage!", "fc0100b8")):
            with self.subTest(before=before, after=after):
                self.assertEqual(native.proposals(result(before=before, after=after)), [])

    def test_operand_order_proposes_native_register_variables(self):
        record = result({"mwcc_fused_subtract": True, "reg_vars": []},
                        before="fc431078", after="fc4110f8")
        proposals = native.proposals(record)
        self.assertEqual([row["options"]["reg_vars"] for row in proposals], [["f3"], ["f1"]])
        self.assertTrue(all(row["options"]["mwcc_fused_subtract"] for row in proposals))
        self.assertEqual(record["options"]["reg_vars"], [])

    def test_unavailable_feedback_never_creates_variants(self):
        record = result()
        record["object_audit"]["status"] = "unavailable"
        self.assertEqual(native.proposals(record), [])

    def test_search_retains_best_and_respects_budget(self):
        calls = []

        def evaluate(options, reason, index):
            calls.append(copy.deepcopy(options))
            if index == 0:
                return result(options)
            return result(options, pct=95 if index == 1 else 80,
                          before="fc431078", after="fc4110f8")

        report = native.search(evaluate, 3)
        self.assertEqual(len(calls), 4)
        self.assertEqual(report["best_attempt"], 1)
        self.assertTrue(report["improved"])
        self.assertEqual(report["model_calls"], 0)
        self.assertFalse(report["promoted"])
        self.assertEqual(calls[-2]["reg_vars"], ["f3"])
        self.assertEqual(calls[-1]["reg_vars"], ["f1"])

    def test_no_variants_after_scratch_exact_or_zero_budget(self):
        for exact, budget in ((True, 8), (False, 0)):
            calls = []

            def evaluate(options, reason, index):
                calls.append(index)
                return result(options, exact=exact)

            report = native.search(evaluate, budget)
            self.assertEqual(calls, [0])
            self.assertFalse(report["linked"])
        for budget in (-1, 9):
            with self.assertRaises(ValueError):
                native.search(None, budget)

    def test_score_100_does_not_stop_without_object_identity(self):
        calls = []

        def evaluate(options, reason, index):
            calls.append(index)
            return result(options, pct=100, exact=index == 1)

        report = native.search(evaluate, 3)
        self.assertEqual(calls, [0, 1])
        self.assertEqual(report["best_attempt"], 1)

    def test_compiler_failure_cannot_replace_measured_candidate(self):
        failed = result(pct=100)
        failed["measurement"]["outcome"].update(compiled=False, profile=None)
        self.assertLess(native.rank(failed), native.rank(result(pct=1)))


if __name__ == "__main__":
    unittest.main()
