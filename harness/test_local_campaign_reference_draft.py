from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from local_campaign_decompile import tool
from local_campaign_reference_draft import adapt_reference, reference_declarations


@unittest.skipUnless((tool().parent / "m2c_pycparser").is_dir(), "optional m2c parser is unavailable")
class ReferenceDraftTests(unittest.TestCase):
    def test_reuses_body_with_exact_return_and_argument_contracts(self):
        result = adapt_reference("int ref(int x) { return x + 1; }", "ref", "target", "int target(int count)", "")
        self.assertIn("int target(int count)", result)
        self.assertIn("return count + 1;", result)
        self.assertNotIn("ref", result)

    def test_preserves_member_names_and_performs_simultaneous_parameter_swaps(self):
        context = "typedef struct S { int x; } S;"
        result = adapt_reference("int ref(S *x) { return x->x; }", "ref", "target", "int target(S *ptr)", context)
        self.assertIn("return ptr->x;", result)
        result = adapt_reference("int ref(int x, int y) { return x - y; }", "ref", "target",
                                 "int target(int y, int x)", "")
        self.assertIn("return y - x;", result)

    def test_recursive_calls_are_renamed_not_left_bound_to_the_reference(self):
        result = adapt_reference("int ref(int x) { return x ? ref(x - 1) : 0; }", "ref", "target",
                                 "int target(int depth)", "")
        self.assertIn("target(depth - 1)", result)

    def test_argument_signedness_qualifiers_and_arity_are_not_guessed(self):
        for signature in ("unsigned int target(int x)", "int target(unsigned int x)",
                          "int target(int x, int y)", "int target(void)"):
            with self.subTest(signature=signature), self.assertRaisesRegex(ValueError, "types differ"):
                adapt_reference("int ref(int x) { return x; }", "ref", "target", signature, "")
        with self.assertRaisesRegex(ValueError, "types differ"):
            adapt_reference("int ref(int *x) { return *x; }", "ref", "target", "int target(const int *ptr)", "")

    def test_rejects_local_global_and_shadowing_capture(self):
        for source in ("int ref(int x) { int count = 1; return x + count; }",
                       "int ref(int x) { return x + count; }",
                       "int ref(int x) { { int x = 1; return x; } }"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                adapt_reference(source, "ref", "target", "int target(int count)", "")

    def test_void_contract_and_named_function_pointer_parameters_are_supported(self):
        result = adapt_reference("void ref(void) { return; }", "ref", "target", "void target(void)", "")
        self.assertIn("void target(void)", result)
        result = adapt_reference("int ref(int (*cb)(int x), int v) { return cb(v); }", "ref", "target",
                                 "int target(int (*callback)(int arg), int value)", "")
        self.assertIn("return callback(value);", result)

    def test_missing_reference_types_are_recordable_value_errors(self):
        with self.assertRaisesRegex(ValueError, "cannot be parsed"):
            adapt_reference("int ref(int x) { u16 value; value = x; return value; }",
                            "ref", "target", "int target(int count)", "")

    def test_reference_dependencies_come_from_real_declarations_without_bodies(self):
        context = ("typedef unsigned short u16; int secret = 12345; int target(int count) { return 88888; } "
                   "void helper(u16 value) { } int unused(void) { return 77777; }")
        source = "int ref(int x) { helper((u16)x); return x; }"
        declarations = reference_declarations(context, source, "target")
        self.assertIn("typedef unsigned short u16;", declarations)
        self.assertIn("void helper(u16 value);", declarations)
        for secret in ("12345", "88888", "77777", "return", "secret", "unused"):
            self.assertNotIn(secret, declarations)
        adapted = adapt_reference(source, "ref", "target", "int target(int count)", declarations)
        self.assertIn("helper((u16) count)", adapted)

    def test_mwcc_placement_keeps_target_signature_and_omits_initializer_evidence(self):
        context = ('__declspec(section ".sdata2") const int value = 77777; '
                   '__declspec(section ".init") int target(int count) { return 88888; }')
        declarations = reference_declarations(context, "int ref(int x) { return value + x; }", "target")
        self.assertIn("const int value;", declarations)
        self.assertNotIn("77777", declarations)
        self.assertNotIn("88888", declarations)
        signature = '__declspec(section ".init") int target(int count)'
        adapted = adapt_reference("int ref(int x) { return value + x; }", "ref", "target", signature, declarations)
        self.assertTrue(adapted.startswith(signature + "\n"))
        self.assertIn("value + count", adapted)

    def test_candidate_only_types_are_closed_from_real_context_without_bodies(self):
        context = ("typedef signed char s8; typedef double f64; typedef unsigned int u32; "
                   "f64 table[3] = {1, 2, 3}; int unused_secret = 44444; "
                   "int target(void* ptr) { return 88888; }")
        source = "int target(void* ptr) { s8 x; f64 y; x = *(s8*)ptr; y = table[0]; return x + y; }"
        declarations = reference_declarations(context, source, "target")
        for expected in ("typedef signed char s8;", "typedef double f64;", "f64 table[3];"):
            self.assertIn(expected, declarations)
        for secret in ("44444", "88888", "unused_secret", "{1", "u32"):
            self.assertNotIn(secret, declarations)

    @unittest.skipUnless(shutil.which("clang"), "Clang is required for a compiled rename check")
    def test_adapted_source_compiles_as_c89(self):
        context = "typedef struct S { int x; } S;"
        result = adapt_reference("int ref(S *x) { return x->x + 1; }", "ref", "target", "int target(S *ptr)", context)
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "candidate.c"
            path.write_text(context + "\n" + result, encoding="utf-8", newline="\n")
            checked = subprocess.run([shutil.which("clang"), "-std=c89", "-pedantic-errors", "-Wall", "-Werror",
                                      "-fsyntax-only", str(path)], capture_output=True, text=True, timeout=10)
            self.assertEqual(checked.returncode, 0, checked.stderr)


if __name__ == "__main__":
    unittest.main()
