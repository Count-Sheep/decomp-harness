"""Target evidence is bounded, scoped, recorded, and never an acceptance certificate."""

import json
from pathlib import Path
import tempfile
import unittest

import local_campaign_investigation as investigation
from local_campaign_obligations import ledger

ASM = '''.fn target, global
/* 80001000 00001000  A0 63 00 00 */ lhz r3, 0(r3)
/* 80001004 00001004  3C 80 80 00 */ lis r4, value@ha
/* 80001008 00001008  48 00 00 01 */ bl helper
/* 8000100C 0000100C  4E 80 04 21 */ bctrl
/* 80001010 00001010  4E 80 00 20 */ blr
.endfn target
.obj value, local
 .4byte 0x3F800000
.endobj value
.fn caller, global
/* 80002000 00002000  48 00 00 01 */ bl target
/* 80002004 00002004  4E 80 00 20 */ blr
.endfn caller
'''


class DossierTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "build/TEST01/asm/test.s"
        self.path.parent.mkdir(parents=True)
        self.path.write_text(ASM)
        (self.root / "src").mkdir()
        self.index = {"functions": {
            "target": {"asm_path": str(self.path.relative_to(self.root)), "callees": ["helper"], "data": ["value"],
                       "source": "src/test.c", "unit": "main/test", "insns": 5, "pct": 10},
            "caller": {"asm_path": str(self.path.relative_to(self.root)), "callees": ["target"]},
            "example": {"callees": ["helper"], "data": [], "source": "src/example.c", "pct": 100, "insns": 3},
        }}

    def session(self, **options):
        return investigation.Session(self.root, self.index, "target", "int target(void) { return 0; }", "int target(void)",
                                     declaration=lambda name: "int helper(void);",
                                     reference_body=lambda *args: "int example(void) { return helper(); }", **options)

    def test_dossier_keeps_target_and_source_authority_separate(self):
        dossier = self.session().dossier()
        self.assertEqual(dossier["instruction_count"], 5)
        self.assertEqual(dossier["memory_width_counts"], {"2": 1})
        self.assertEqual(dossier["direct_callees"], ["helper"])
        self.assertEqual(dossier["referenced_data"], ["value"])
        self.assertEqual(dossier["caller_candidates"], ["caller"])
        self.assertEqual(dossier["matched_reference_candidates"], ["example"])
        self.assertEqual(dossier["indirect_flows"][0]["opcode"], "bctrl")
        self.assertTrue(dossier["unknowns"])

    def test_instruction_queries_stay_in_the_exact_target(self):
        session = self.session()
        result = session.query({"kind": "instructions", "start": "0x80001008", "limit": 24})
        self.assertEqual(len(result["instructions"]), 3)
        self.assertEqual(result["remaining"], 0)
        self.assertEqual(session.query({"kind": "instructions", "start": "0x80002000", "limit": 1})["status"], "unknown")

    def test_related_queries_and_fresh_caller_checks(self):
        session = self.session()
        self.assertEqual(session.query({"kind": "callee", "symbol": "helper"})["status"], "observed")
        self.assertEqual(session.query({"kind": "caller", "symbol": "caller"})["instructions"][0]["text"], "bl target")
        self.assertEqual(session.query({"kind": "matched_reference", "symbol": "example"})["status"], "reference")
        self.assertEqual(session.query({"kind": "callee", "symbol": "unrelated"})["status"], "unknown")

    def test_literal_constant_is_big_endian_without_inferred_type(self):
        result = self.session().query({"kind": "constant", "symbol": "value"})
        self.assertEqual(result["bytes_hex"], "3f800000")
        self.assertEqual(result["interpretations"][0]["f32_interpretation"], "1.0")
        self.assertIn("type unproven", result["authority"])

    def test_double_view_is_bound_to_both_big_endian_words(self):
        self.path.write_text(ASM.replace("0x3F800000", "0x3FF00000\n .4byte 0x00000000"))
        result = self.session().query({"kind": "constant", "symbol": "value"})
        self.assertEqual(result["bytes_hex"], "3ff0000000000000")
        self.assertEqual(result["double_interpretations"], [{"offset": 0, "f64_interpretation": "1.0"}])

    def test_report_exact_callee_can_supply_a_known_implementation_reference(self):
        self.index["functions"]["helper"] = {"callees": [], "data": [], "source": "src/helper.c", "pct": 100, "insns": 1}
        session = self.session()
        self.assertIn("helper", session.dossier()["matched_reference_candidates"])
        self.assertEqual(session.query({"kind": "matched_reference", "symbol": "helper"})["status"], "reference")

    def test_symbolic_or_ambiguous_constants_stay_unknown(self):
        for replacement in ("helper", "0x3F800000\n .rel helper"):
            self.path.write_text(ASM.replace("0x3F800000", replacement))
            self.assertEqual(self.session().query({"kind": "constant", "symbol": "value"})["status"], "unknown")
        self.path.write_text(ASM + '.obj value, local\n .4byte 0x00000000\n.endobj value\n')
        self.assertEqual(self.session().query({"kind": "constant", "symbol": "value"})["status"], "unknown")

    def test_stale_assembly_and_wrong_target_identity_rejected(self):
        session = self.session()
        self.path.write_text(ASM.replace("lhz", "lha"))
        with self.assertRaisesRegex(ValueError, "changed"):
            session.query({"kind": "callee", "symbol": "helper"})
        with self.assertRaisesRegex(ValueError, "address"):
            self.session(expected_address=0x80001004)
        with self.assertRaisesRegex(ValueError, "scoring unit"):
            self.session(expected_unit="main/other")

    def test_report_changes_invalidate_reference_authority(self):
        report = self.root / "report.json"
        report.write_text('{"version":1}')
        session = self.session(report_file=report, report_sha256=investigation.digest(report.read_bytes()))
        report.write_text('{"version":2}')
        with self.assertRaisesRegex(ValueError, "report changed"):
            session.query({"kind": "matched_reference", "symbol": "example"})

    def test_ambiguous_function_and_assembly_path_escape_rejected(self):
        self.path.write_text(ASM + ASM)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.session()
        self.index["functions"]["target"]["asm_path"] = "../../outside.s"
        with self.assertRaisesRegex(ValueError, "outside"):
            self.session()

    def test_symlink_escape_and_reference_escape_rejected(self):
        outside = self.root / "outside.s"
        outside.write_text(ASM)
        self.path.unlink()
        try:
            self.path.symlink_to(outside)
        except OSError:  # symlinks need extra privileges on Windows; the reference check still runs
            pass
        else:
            with self.assertRaisesRegex(ValueError, "outside"):
                self.session()
            self.path.unlink()
        self.path.write_text(ASM)
        session = self.session()
        self.index["functions"]["example"]["source"] = "../escape.c"
        with self.assertRaisesRegex(ValueError, "outside"):
            session.query({"kind": "matched_reference", "symbol": "example"})


class ProtocolTests(unittest.TestCase):
    def test_code_response_is_unchanged_and_request_json_is_strict(self):
        self.assertIsNone(investigation.parse_requests("```c\nint target(void) { return 1; }\n```"))
        request = {"context_requests": [{"kind": "callee", "symbol": "helper"}]}
        self.assertEqual(investigation.parse_requests("```json\n" + json.dumps(request) + "\n```"), request["context_requests"])
        invalid = ["{", '{"context_requests":[],"context_requests":[]}',
                   json.dumps({"context_requests": [{"kind": [], "symbol": "helper"}]}),
                   json.dumps({"context_requests": [{"kind": {}, "symbol": "helper"}]}),
                   json.dumps({"context_requests": [{"kind": "exec", "symbol": "helper"}]}),
                   json.dumps({"context_requests": [{"kind": "callee", "symbol": "../../secret"}]}),
                   json.dumps({"context_requests": [{"kind": "instructions", "start": "0x80001000", "limit": True}]}),
                   json.dumps({"context_requests": [{"kind": "callee", "symbol": "helper", "path": "x"}]}),
                   json.dumps({"context_requests": [{"kind": "callee", "symbol": "helper"}] * 4}),
                   json.dumps(request) + '\n```c\nvoid target(void) {}\n```']
        for response in invalid:
            with self.subTest(response=response), self.assertRaises(ValueError):
                investigation.parse_requests(response)

    def test_exchange_uses_one_deadline_and_records_all_calls(self):
        now = [0]
        records, inputs, timeouts = [], [], []
        responses = [json.dumps({"context_requests": [{"kind": "callee", "symbol": "helper"}]}), "```c\nint target(void) { return helper(); }\n```"]
        def send(prompt, round_number, remaining):
            inputs.append(prompt); timeouts.append(remaining); now[0] += 4
            return responses[round_number], prompt
        result, _ = investigation.exchange("original C contract", send, lambda r: {"status": "unknown"}, records.append,
                                            timeout=10, clock=lambda: now[0])
        self.assertEqual(result, responses[1])
        self.assertEqual(timeouts, [10, 6])
        self.assertEqual(sum(r["model_call"] for r in records), 2)
        self.assertIn("unknown", inputs[1])

    def test_round_limit_and_malformed_request_never_reach_source_extraction(self):
        response = json.dumps({"context_requests": [{"kind": "callee", "symbol": "helper"}]})
        records = []
        with self.assertRaisesRegex(ValueError, "round limit"):
            investigation.exchange("C", lambda p, r, t: (response, p), lambda r: {}, records.append, timeout=10)
        self.assertEqual(sum(r["model_call"] for r in records), 3)
        records = []
        with self.assertRaises(ValueError):
            investigation.exchange("C", lambda p, r, t: ("{broken", p), lambda r: {}, records.append, timeout=10)
        self.assertEqual(len(records), 1)

    def test_expired_deadline_prevents_another_model_call(self):
        now = [0]
        def send(p, r, t):
            now[0] = 20
            return json.dumps({"context_requests": [{"kind": "callee", "symbol": "helper"}]}), p
        with self.assertRaises(TimeoutError):
            investigation.exchange("C", send, lambda r: {}, lambda r: None, timeout=10, clock=lambda: now[0])

    def test_result_budget_preserves_explicit_truncation(self):
        calls = []
        def send(p, r, t):
            calls.append(p)
            return (json.dumps({"context_requests": [{"kind": "callee", "symbol": "helper"}]}) if r == 0 else "```c\nvoid target(void) {}\n```", p)
        investigation.exchange("C", send, lambda r: {"source": "x" * 9000}, lambda r: None, timeout=10)
        self.assertIn('"status":"truncated"', calls[1])
        self.assertLess(len(calls[1]), 1000)

    def test_multiple_results_reserve_room_for_truncation_records(self):
        records = []
        request = json.dumps({"context_requests": [{"kind": "callee", "symbol": "helper"}] * 3})
        investigation.exchange("C", lambda p, r, t: (request if r == 0 else "```c\nvoid target(void) {}\n```", p),
                               lambda r: {"source": "x" * 5600}, records.append, timeout=10)
        results = next(row["results"] for row in records if not row["model_call"])
        self.assertLessEqual(len(investigation.encoded(results)), investigation.RESULT_BUDGET)
        self.assertTrue(any(row.get("status") == "truncated" for row in results))


class ObligationTests(unittest.TestCase):
    def gates(self, result):
        return {row["id"]: row["state"] for row in result["gates"]}

    def test_score_exact_and_legacy_linked_do_not_establish_ready_c(self):
        result = ledger({"pct": 100, "returncode": 0, "linked": True, "ready": True})
        gates = self.gates(result)
        self.assertEqual(gates["scorer"], "passed")
        self.assertEqual(gates["bytes"], "pending")
        self.assertEqual(gates["link"], "pending")
        self.assertEqual(gates["review"], "pending")
        self.assertFalse(result["ready"])

    def test_candidate_binding_and_dependencies_fail_closed(self):
        measurement = {"compile_ok": True, "pct": 100, "draft_sha256": "a", "verification": {
            "candidate_sha256": "a", "raw_candidate_bytes": True, "relocations": False,
            "owner_object": True, "linked_checksum": True, "policy_screen_passed": False}}
        gates = self.gates(ledger(measurement, candidate_sha256="a"))
        self.assertEqual(gates["bytes"], "passed")
        self.assertEqual(gates["relocations"], "failed")
        self.assertEqual(gates["link"], "blocked")
        self.assertEqual(gates["policy"], "failed")
        measurement["draft_sha256"] = "other"
        self.assertEqual(self.gates(ledger(measurement, candidate_sha256="a"))["bytes"], "pending")

    def test_non_exact_legacy_returncode_does_not_mean_compile_failure(self):
        gates = self.gates(ledger({"pct": 70, "returncode": 1}))
        self.assertEqual(gates["compile"], "passed")
        self.assertEqual(gates["scorer"], "failed")

    def test_unknown_boolean_and_invalid_score_never_pass(self):
        for value in (None, True, float("nan"), float("inf"), 101, -1):
            with self.subTest(value=value):
                self.assertEqual(self.gates(ledger({"pct": value}))["scorer"], "pending")
        self.assertEqual(self.gates(ledger({"compile_ok": "true"}))["compile"], "pending")


if __name__ == "__main__":
    unittest.main()
