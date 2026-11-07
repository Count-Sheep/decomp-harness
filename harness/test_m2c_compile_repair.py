"""Tests for conservative m2c compile repair and assembly feedback."""

import json
import unittest

import m2c_compile_repair as repair


class CompileRepairTests(unittest.TestCase):
    def test_pointer_assignment_refines_generic_return_stub(self):
        result = repair.repair_draft(
            "void caller(void) {\n    void *p;\n    p = Find();\n}\n", "caller", "extern s32 Find();")
        self.assertIn("extern void * Find(void);", result["code"])

    def test_conflicting_return_uses_do_not_infer_pointer(self):
        result = repair.repair_draft(
            "void caller(void) {\n    void *p;\n    s32 x;\n    p = Find();\n    x = Find();\n}\n",
            "caller", "extern s32 Find();")
        self.assertIn("extern s32 Find(void);", result["code"])

    def test_known_library_return_type_overrides_generic_stub(self):
        result = repair.repair_draft(
            "void caller(void *p) { memset(p, 0, 4); }\n", "caller", "extern s32 memset();")
        self.assertIn("extern void * memset(void *, s32, u32);", result["code"])

    def test_preserves_context_return_type_for_nested_comparison(self):
        result = repair.repair_draft(
            "u8 isStoryMode(void) { return (u8) (GSflagGet(0x30D) == 0); }\n",
            "isStoryMode", "extern s32 GSflagGet();")
        self.assertIn("extern s32 GSflagGet(s32);", result["code"])
        self.assertNotIn("extern void GSflagGet", result["code"])

    def test_context_prototype_uses_array_argument_decay(self):
        result = repair.repair_draft(
            "void caller(void) { Known(buffer); }\n", "caller",
            "extern s32 Known();\nextern u8 buffer[];")
        self.assertIn("extern s32 Known(u8 *);", result["code"])

    def test_explicit_prototype_wins_over_context_stub(self):
        result = repair.repair_draft(
            "extern f32 Known(f32);\nf32 caller(f32 x) { return Known(x); }\n",
            "caller", "extern s32 Known();")
        self.assertNotIn("extern s32 Known", result["code"])
        self.assertIn("extern f32 Known(f32);", result["code"])

    def test_comment_delimiters_are_not_orphan_expressions(self):
        result = repair.repair_draft("/*\n * prose = value\n */\nvoid caller(void) {}\n", "caller")
        self.assertTrue(result["ready_for_compile"])
        self.assertIn(" * prose = value\n */", result["code"])

    def test_semicolon_is_inserted_before_trailing_comment(self):
        result = repair.repair_draft("void caller(s32 x) {\n    x = 1 // reason\n}\n", "caller")
        self.assertIn("x = 1; // reason", result["code"])

    def test_public_api_accepts_context_and_is_json_serializable(self):
        result = repair.repair_draft(
            "void fn_80001000(void) { KnownCall(1); }\n",
            "fn_80001000",
            "extern void KnownCall(s32);",
        )
        self.assertTrue(result["code"].startswith("extern void KnownCall(s32);"))
        self.assertEqual(result["unresolved"], [])
        json.dumps(result)

        missing = repair.repair_draft("void other(void) {}\n", "fn_80001000")
        self.assertIn("assigned function fn_80001000 is not defined", missing["unresolved"])

    def test_repairs_real_unknown_type_and_field_patterns(self):
        source = """M2C_UNK fn_800889B8(u32);
M2C_UNK fn_800889D8(M2C_UNK, M2C_UNK *);
extern M2C_UNK lbl_801E82C0;

void fn_80088CB4(void *arg0, M2C_UNK arg1) {
    M2C_UNK *temp_r3;
    M2C_FIELD(arg0, M2C_UNK **, 0xF8) = temp_r3;
    fn_800889B8(arg1);
}
"""
        result = repair.repair_m2c_draft(source)
        self.assertNotIn("M2C_UNK", result["code"])
        self.assertIn("s32 fn_800889B8(u32);", result["code"])
        self.assertIn("s32 fn_800889D8(s32, void *);", result["code"])
        self.assertIn("extern s32 lbl_801E82C0;", result["code"])
        self.assertIn("void *temp_r3;", result["code"])
        self.assertIn("M2C_FIELD(arg0, void **, 0xF8)", result["code"])
        self.assertIn("#define M2C_FIELD(base, type, offset)", result["code"])
        self.assertTrue(result["ready_for_compile"])
        self.assertTrue(any(item["kind"] == "unknown_type" for item in result["transformations"]))

    def test_preserves_unknown_markers_in_comments_and_strings(self):
        source = """/* M2C_UNK is documented here. */
M2C_UNK fn_1(void) { /* M2C_UNK remains commentary. */
    const char *name = "M2C_UNK";
    return 0;
}
"""
        result = repair.repair_draft(source, "fn_1")
        self.assertIn("/* M2C_UNK is documented here. */", result["code"])
        self.assertIn("/* M2C_UNK remains commentary. */", result["code"])
        self.assertIn('"M2C_UNK"', result["code"])
        self.assertIn("s32 fn_1(void)", result["code"])

    def test_preserves_existing_field_definition(self):
        source = """#define M2C_FIELD(base, type, offset) (*(type)((char *)(base) + (offset)))
void fn_1(void *arg0) { M2C_FIELD(arg0, s32 *, 4) = 1; }
"""
        result = repair.repair_draft(source)
        self.assertEqual(result["code"].count("#define M2C_FIELD"), 1)
        self.assertFalse(any(item["kind"] == "field_macro" for item in result["transformations"]))

    def test_converts_only_direct_void_pointer_arithmetic(self):
        source = """void fn_80029040(void *arg0, void **table) {
    memset(arg0 + 0x98, 0, 0x20);
    table += 1;
}
"""
        result = repair.repair_m2c_draft(source)
        self.assertIn("memset((char *)arg0 + 0x98, 0, 0x20);", result["code"])
        self.assertIn("table += 1;", result["code"])
        self.assertNotIn("(char *)table", result["code"])
        self.assertIn("extern void * memset(void *, s32, u32);", result["code"])

    def test_flags_void_pointer_subtraction_as_risky(self):
        result = repair.repair_draft("s32 fn_1(void *arg0) { return arg0 - 4; }\n", "fn_1")
        self.assertIn("arg0 - 4", result["code"])
        self.assertTrue(any("unsupported void-pointer arithmetic" in item
                            for item in result["unresolved"]))

    def test_adds_inferred_function_and_data_externs(self):
        source = """s32 fn_80001000(void *arg0) {
    GXLoadPosMtxImm(arg0, 0);
    fn_80002000(&lbl_801E5220);
    return lbl_8022A268[0];
}
"""
        result = repair.repair_m2c_draft(source)
        self.assertIn("extern void GXLoadPosMtxImm(void *, s32);", result["code"])
        self.assertIn("extern void fn_80002000(void *);", result["code"])
        self.assertIn("extern u8 lbl_801E5220;", result["code"])
        self.assertIn("extern u8 lbl_8022A268[];", result["code"])
        kinds = [item["kind"] for item in result["transformations"]]
        self.assertIn("function_extern", kinds)
        self.assertIn("data_extern", kinds)

    def test_uses_mwcc_intrinsic_abi_instead_of_guessing_void(self):
        result = repair.repair_draft(
            "f32 fn_1(f32 value) {\n    return __frsqrte(value);\n}\n", "fn_1"
        )
        self.assertIn("extern f64 __frsqrte(f64);", result["code"])
        self.assertNotIn("extern void __frsqrte", result["code"])

    def test_repairs_only_side_effecting_orphan_statements(self):
        source = """void fn_1(void *arg0) {
    fn_80002000(arg0)
    arg0 = 0
}
"""
        result = repair.repair_m2c_draft(source)
        self.assertIn("fn_80002000(arg0);", result["code"])
        self.assertIn("arg0 = 0;", result["code"])
        self.assertEqual(sum(item["kind"] == "orphan_statement" for item in result["transformations"]), 2)

    def test_refuses_bare_dereference_and_unbalanced_rewrites(self):
        source = """void fn_1(void *temp_r4_2) {
    (*(s32 *)((char *)(temp_r4_2) + (0x10)))
    fn_80002000((temp_r4_2)
}
"""
        result = repair.repair_m2c_draft(source)
        self.assertIn("(*(s32 *)((char *)(temp_r4_2) + (0x10)))\n", result["code"])
        self.assertFalse(result["ready_for_compile"])
        self.assertTrue(any("orphan expression" in item for item in result["unresolved"]))
        self.assertTrue(any("unbalanced delimiter" in item for item in result["unresolved"]))

    def test_refuses_inconsistent_call_arity(self):
        source = """void fn_1(void) {
    fn_80002000(1);
    fn_80002000(1, 2);
}
"""
        result = repair.repair_m2c_draft(source)
        self.assertNotIn("extern void fn_80002000", result["code"])
        self.assertTrue(any("inconsistent call arity" in item for item in result["unresolved"]))


class AssemblyComparisonTests(unittest.TestCase):
    def test_reports_first_difference_frame_shape_registers_and_opcode(self):
        target = """.L_80001000:
/* 80001000 00000000  94 21 FF E0 */ stwu r1, -0x20(r1)
/* 80001004 00000004  48 00 00 01 */ bl fn_80002000
/* 80001008 00000008  40 82 FF F8 */ bne .L_80001000
/* 8000100C 0000000C  4E 80 00 20 */ blr
"""
        compiled = """.L_00000000:
/* 00000000 00000000  94 21 FF F0 */ stwu r1, -0x10(r1)
/* 00000004 00000004  38 60 00 00 */ li r3, 0
/* 00000008 00000008  48 00 00 01 */ bl fn_80002000
/* 0000000C 0000000C  4E 80 00 20 */ blr
"""
        result = repair.compare_assembly(target, compiled)
        self.assertEqual(result["first_divergent_index"], 0)
        self.assertEqual(result["target_opcode"], "stwu")
        self.assertEqual(result["compiled_opcode"], "stwu")
        self.assertFalse(result["opcode_mismatch"])
        self.assertEqual(result["target_stack_frame"], 0x20)
        self.assertEqual(result["compiled_stack_frame"], 0x10)
        self.assertEqual(result["stack_frame_delta"], -0x10)
        self.assertEqual(result["target_shape"]["conditional"], 1)
        self.assertEqual(result["compiled_shape"]["conditional"], 0)
        self.assertEqual(result["compiled_only_registers"], ("r3",))
        prompt = repair.format_feedback(result)
        self.assertIn("First divergence at instruction 0", prompt)
        self.assertIn("delta=-0x10", prompt)
        self.assertLess(len(prompt), 700)
        json.dumps(result)

    def test_normalizes_dtk_local_labels_and_detects_exact_text(self):
        target = """.L_80001234:
/* 80001230 00000000  40 82 00 04 */ bne .L_80001234
/* 80001234 00000004  4E 80 00 20 */ blr
"""
        compiled = """.L_00000004:
/* 00000000 00000000  40 82 00 04 */ bne .L_00000004
/* 00000004 00000004  4E 80 00 20 */ blr
"""
        result = repair.structured_assembly_comparison(target, compiled)
        self.assertTrue(result["exact"])
        self.assertIsNone(result["first_divergent_index"])
        self.assertIn("instruction-exact", repair.compact_prompt_text(target, compiled))

    def test_reports_opcode_mismatch_and_truncated_output(self):
        target = "/* 0 0 00 */ lwz r3, 0(r4)\n/* 4 4 00 */ blr\n"
        compiled = "/* 0 0 00 */ li r3, 0\n"
        result = repair.compare_assembly(target, compiled)
        self.assertEqual(result["first_divergent_index"], 0)
        self.assertTrue(result["opcode_mismatch"])
        self.assertEqual((result["target_opcode"], result["compiled_opcode"]), ("lwz", "li"))
        self.assertEqual((result["target_count"], result["compiled_count"]), (2, 1))


if __name__ == "__main__":
    unittest.main()
