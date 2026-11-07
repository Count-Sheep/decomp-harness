import unittest

import m2c_failure_audit as audit
import m2c_template_index as templates
from m2c_repair_experiment import validate_owners


class FrozenEvidenceTests(unittest.TestCase):
    def test_replay_rejects_historical_target_unit_after_function_moves(self):
        report = {"units": [{"name": "main/game/new_leaf", "functions": [{"name": "leaf"}]}]}
        validate_owners(report, [{"symbol": "leaf", "unit": "game/new_leaf"}])
        with self.assertRaisesRegex(ValueError, "ownership is stale"):
            validate_owners(report, [{"symbol": "leaf", "unit": "old_combined_unit"}])

    def test_missing_disassembly_cannot_establish_exactness(self):
        outcome = {"compiled": True, "pct": 100, "diff": ""}
        for compiled in (None, "", ".text\n"):
            result = audit.assembly_evidence("/* 0 */ blr", compiled, outcome)
            self.assertIsNone(result["instruction_exact"])

    def test_fuzzy_100_with_different_instruction_is_flagged(self):
        result = audit.assembly_evidence("/* 0 */ li r3, 1", "/* 0 */ li r3, 0",
                                         {"compiled": True, "pct": 100})
        self.assertTrue(result["false_100"])
        self.assertFalse(result["instruction_exact"])

    def test_error_clustering_counts_categories_without_caret_noise(self):
        outcome = {"compiled": False, "error": "#   Error: ^\n#   pointer/array required\n"}
        self.assertEqual(audit.compiler_messages(outcome["error"]), ["pointer/array required"])
        self.assertIn("pointer_or_operand_type", audit.failure_categories("int f() {}", outcome, "ok"))

    def test_shape_abstracts_offsets_but_keeps_register_roles_and_branch_topology(self):
        a = "/* 0 */ lwz r3, 0x10(r4)\n/* 4 */ blr"
        b = "/* 0 */ lwz r3, 0x20(r4)\n/* 4 */ blr"
        self.assertEqual(templates.normalized_shape(a), templates.normalized_shape(b))
        self.assertNotEqual(templates.normalized_shape(a), templates.normalized_shape(b.replace('r4', 'r5')))
        branch = ".L_00000000:\n/* 0 */ bne .L_00000004\n.L_00000004:\n/* 4 */ blr"
        loop = branch.replace("bne .L_00000004", "bne .L_00000000")
        self.assertNotEqual(templates.normalized_shape(branch), templates.normalized_shape(loop))

    def test_direct_transfer_requires_operands_and_only_renames_code(self):
        assembly = "/* 0 */ li r3, 1\n/* 4 */ blr"
        row = templates.entry("old", assembly, '/* old */ int old(void) { return 1; }',
                              {"kind": "scratch_instruction_exact", "project": "ref"}, {})
        index = {"templates": [row], "groups": {row["shape_sha256"]: [0]}}
        draft = templates.direct_drafts(index, assembly, "new")[0]
        self.assertEqual(draft["code"], '/* old */ int new(void) { return 1; }')
        self.assertTrue(draft["requires_target_validation"])
        self.assertEqual(templates.direct_drafts(index, assembly.replace('r3, 1', 'r3, 2'), "new"), [])
        self.assertEqual(templates.direct_drafts(index, assembly, "old"), [])
        self.assertEqual(templates.direct_drafts(index, assembly, "old", include_same_symbol=True)[0]["code"],
                         row["source"])
        self.assertEqual(templates.direct_drafts(index, assembly, "new", limit=0), [])
        row["source"] += " /* changed */"
        with self.assertRaisesRegex(ValueError, "changed since indexing"):
            templates.direct_drafts(index, assembly, "new")


if __name__ == "__main__":
    unittest.main()
