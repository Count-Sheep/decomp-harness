"""Evidence bootstrap must preserve the line between hints and proof."""

import unittest
import tempfile
from pathlib import Path

import m2c_bootstrap_evidence as bootstrap


class BootstrapEvidenceTests(unittest.TestCase):
    def test_exact_anchors_create_bounded_region_without_claiming_boundaries(self):
        ghidra = {"functions": [
            {"address": "0x80001000", "name": "FUN_80001000", "body_bytes": 16,
             "source": "DEFAULT", "direct_calls": ["0x80001010"]},
            {"address": "0x80001010", "name": "FUN_80001010", "body_bytes": 16,
             "source": "DEFAULT", "direct_calls": ["0x80001000"]},
        ]}
        matches = {"candidates": [
            {"address": 0x80001000, "size": 16, "suggested_name": "First",
             "references": [{"project": "ref", "unit": "sdk/item.s"}]},
            {"address": 0x80001010, "size": 16, "suggested_name": "Second",
             "references": [{"project": "ref", "unit": "sdk/item.s"}]},
        ]}
        report = bootstrap.build(ghidra, matches, "TEST", preferred_projects=("ref",))
        self.assertEqual(report["summary"]["exact_symbol_proposals"], 2)
        self.assertEqual(report["summary"]["bounded_unit_regions"], 1)
        self.assertEqual(report["unit_regions"][0]["boundary_semantics"],
                         "anchor_envelope_not_exact_object_boundary")
        self.assertEqual(report["summary"]["recursive_call_clusters"], 1)

    def test_conflicting_owner_anchor_breaks_region(self):
        ghidra = {"functions": [{"address": 0x1000 + offset, "name": f"f{offset}",
                                  "body_bytes": 16, "direct_calls": []}
                                 for offset in (0, 16, 32)]}
        matches = {"candidates": [
            {"address": 0x1000, "suggested_name": "A", "references": [{"project": "ref", "unit": "a.s"}]},
            {"address": 0x1010, "suggested_name": "B", "references": [{"project": "ref", "unit": "b.s"}]},
            {"address": 0x1020, "suggested_name": "C", "references": [{"project": "ref", "unit": "a.s"}]},
        ]}
        self.assertEqual(bootstrap.build(ghidra, matches, "TEST", preferred_projects=("ref",))["unit_regions"], [])

    def test_only_non_default_signatures_become_type_hints(self):
        ghidra = {"functions": [
            {"address": 1, "name": "Known", "signature": "int Known(char *value)",
             "signature_source": "USER_DEFINED", "direct_calls": []},
            {"address": 2, "name": "FUN_2", "signature": "undefined FUN_2(undefined4 value)",
             "signature_source": "ANALYSIS", "direct_calls": []},
        ]}
        report = bootstrap.build(ghidra, {"candidates": []}, "TEST")
        self.assertEqual([row["name"] for row in report["type_hints"]], ["Known"])
        self.assertIn("hypothesis", report["type_hints"][0]["confidence"])

    def test_ledger_schema_selects_requested_version(self):
        payload = {"names": {"Known": {
            "A": {"address": 1, "references": []},
            "B": {"address": 2, "references": []},
        }}}
        self.assertEqual(bootstrap.match_rows(payload, "B")[0]["address"], 2)
        self.assertEqual(bootstrap.match_rows(payload, "B")[0]["suggested_name"], "Known")

    def test_only_exact_candidate_signatures_become_hints_and_are_hashed(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "candidate.c"
            source.write_text("typedef int s32;\ns32 fn_80001000(s32 value) {\n return value;\n}\n")
            attempts = {"items": {
                "exact": {"symbol": "fn_80001000", "address": 0x80001000,
                          "best_pct": 100.0, "best_source": str(source)},
                "near": {"symbol": "fn_80002000", "address": 0x80002000,
                         "best_pct": 99.9, "best_source": str(source)},
            }}
            hints = bootstrap.exact_candidate_types(attempts)
        self.assertEqual(len(hints), 1)
        self.assertEqual(hints[0]["signature"], "s32 fn_80001000(s32 value)")
        self.assertEqual(len(hints[0]["source_sha256"]), 64)

    def test_anchored_unique_size_sequence_expands_owner_hints(self):
        target = [{"address": 0x1000 + index * 0x10, "name": f"fn_{index}",
                   "size": size, "direct_calls": [], "current_owner": None}
                  for index, size in enumerate((20, 40, 60, 80, 100, 120))]
        catalog = {"functions": [
            {"project": "ref", "unit": "main/sdk/item", "address": index * 0x10,
             "symbol": f"Known{index}", "size": size}
            for index, size in enumerate((20, 40, 60, 80, 100, 120))
        ]}
        anchors = [{"address": 0x1000 + index * 0x10, "reference_project": "ref",
                    "reference_owner": "sdk/item.s"} for index in (1, 4)]
        regions, hints = bootstrap.reference_sequences(catalog, "ref", target, anchors)
        self.assertEqual(len(regions), 1)
        self.assertTrue(regions[0]["complete_reference_function_sequence"])
        self.assertEqual(regions[0]["exact_anchor_count"], 2)
        self.assertEqual(len(hints), 6)
        self.assertEqual(hints[0]["promotion"], "requires_independent_code_match")

    def test_branch_normalization_can_verify_sequence_names_and_split(self):
        self.assertEqual(bootstrap.normalized_code(bytes.fromhex("48000011")),
                         bootstrap.normalized_code(bytes.fromhex("4800ff11")))
        self.assertNotEqual(bootstrap.normalized_code(bytes.fromhex("7c632214")),
                            bootstrap.normalized_code(bytes.fromhex("7c642214")))

        class Image:
            def __init__(self, rows):
                self.rows = rows

            def read(self, address, size):
                return self.rows.get((address, size))

        sizes = (64, 68, 72, 76)
        target = [{"address": 0x1000 + index * 0x100, "name": f"fn_{index}",
                   "size": size, "direct_calls": [], "current_owner": None}
                  for index, size in enumerate(sizes)]
        references = [{"project": "ref", "unit": "main/sdk/item", "address": 0x2000 + index * 0x100,
                       "symbol": f"Known{index}", "size": size}
                      for index, size in enumerate(sizes)]
        target_bytes = {(row["address"], row["size"]): bytes.fromhex("48000011") * (row["size"] // 4)
                        for row in target}
        reference_bytes = {(row["address"], row["size"]): bytes.fromhex("4800ff11") * (row["size"] // 4)
                           for row in references}
        anchors = [{"address": target[0]["address"], "reference_project": "ref",
                    "reference_owner": "sdk/item.s"}]
        regions, hints = bootstrap.reference_sequences(
            {"functions": references}, "ref", target, anchors, Image(target_bytes), Image(reference_bytes)
        )
        self.assertEqual(regions[0]["code_comparison"]["normalized_exact"], 4)
        self.assertTrue(regions[0]["complete_reference_function_sequence"])
        self.assertTrue(all(row["promotion"] == "reviewable_code_match" for row in hints))
        splits = bootstrap.split_proposals(regions, target)
        self.assertEqual(splits[0]["confidence"], "high")
        self.assertIn("not_data", splits[0]["semantics"])

    def test_code_verified_reference_source_provides_hashed_type_hypothesis(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src/item.c"
            source.parent.mkdir()
            source.write_text("static int Known(unsigned value) {\n return value;\n}\n")
            hints = bootstrap.reference_type_hints([{
                "address": "0x80001000", "suggested_name": "Known", "reference_project": "ref",
                "reference_source_path": "src/item.c", "promotion": "reviewable_code_match",
            }], {"ref": root})
        self.assertEqual(hints[0]["signature"], "static int Known(unsigned value)")
        self.assertEqual(len(hints[0]["source_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
