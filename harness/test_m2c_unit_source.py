import unittest

from m2c_unit_source import assemble, m2c_available, source_drafts, overlay_native_source, validate_directives

M2C = m2c_available()


class UnitSourceTests(unittest.TestCase):
    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_native_overlay_preserves_cpp_context_and_only_replaces_target(self):
        original = ('#pragma cats off\nclass Widget { public: int value; };\n'
                    'extern "C" { int target(void) { return 0; } }\n'
                    'int other(Widget *w) { return w->value; }\n')
        result = overlay_native_source(original, {"target": "int target(void) { return 1; }"},
                                       {"target"}, cpp=True)
        self.assertIn('extern "C" { int target(void)', result)
        self.assertIn('class Widget { public: int value; };', result)
        self.assertIn('int other(Widget *w) { return w->value; }', result)
        self.assertIn('return 1;', result)
        self.assertNotIn('return 0;', result)

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_native_overlay_can_fill_missing_unmangled_owner_function(self):
        original = 'class Widget;\nextern "C" int target(void);\n'
        result = overlay_native_source(original, {"target": "int target(void) { return 1; }"},
                                       {"target"}, cpp=True)
        self.assertTrue(result.startswith(original))
        self.assertIn('extern "C" {', result)
        for candidates, required in (({"outside": "void outside(void) {}"}, {"target"}),
                                     ({"target": "void target(void) {} void extra(void) {}"}, {"target"}),
                                     ({"target__Fv": "void target__Fv(void) {}"}, {"target__Fv"})):
            with self.subTest(candidates=candidates), self.assertRaises(ValueError):
                overlay_native_source(original, candidates, required, cpp=True)

    def test_only_captured_pragmas_may_pass_native_directive_validation(self):
        validate_directives('#pragma cats off\n', {'#pragma cats off'})
        for value in ('#pragma cats on\n', '#include "untracked.h"\n', '#define NEW 1\n'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_directives(value, {'#pragma cats off'})

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_static_inline_helpers_are_preserved_but_do_not_cover_owner_functions(self):
        rows = source_drafts("static inline int helper(int x) { return x + 1; }\n"
                             "int target(int x) { return helper(x); }")
        self.assertTrue(rows[0]["static_inline"])
        output = assemble([rows[1]], ["target"], helper_drafts=[rows[0]])
        self.assertIn("return x + 1;", output)
        self.assertLess(output.index("return x + 1;"), output.index("return helper(x);"))
        with self.assertRaisesRegex(ValueError, "cover"):
            assemble([], ["target"], helper_drafts=[rows[0]])
        with self.assertRaisesRegex(ValueError, "conflict"):
            assemble([rows[1]], ["target", "helper"], helper_drafts=[rows[0]])

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_helper_qualifications_are_checked_in_ast(self):
        for qualifier in ("", "static ", "inline "):
            helper = {"symbol": "helper", "static_inline": True,
                      "source": qualifier + "int helper(void) { return 1; }"}
            with self.subTest(qualifier=qualifier), self.assertRaisesRegex(ValueError, "static inline"):
                assemble([], [], helper_drafts=[helper])

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_source_bodies_and_drafts_share_storage_and_target_order(self):
        imported = source_drafts("static int count = 2; static int get(void) { return count; }")
        source = assemble(imported + [{"symbol": "set", "context": imported[0]["context"],
                                       "source": "void set(int value) { count = value; }"}], ["set", "get"])
        self.assertEqual(source.count("static int count = 2;"), 1)
        self.assertIn("static int get(void)", source)
        self.assertLess(source.index("void set(int value)\n{"), source.index("static int get(void)\n{"))

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_source_import_preserves_types_and_null(self):
        imported = source_drafts("#define NULL ((void*)0)\ntypedef signed long s32;\n"
                                 "s32 a(void) { return 1; } void *b(void) { return NULL; }")
        source = assemble(imported, ["b", "a"])
        self.assertEqual(source.count("typedef signed long s32;"), 1)
        self.assertEqual(source.count("#define NULL"), 1)
        self.assertNotIn("typedef int s32;", source)

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_source_import_rejects_uncaptured_preprocessing_and_scoped_pragmas(self):
        for source in ("#include <types.h>\nvoid a(void) {}", "void a(void) {}\n#pragma fp_contract off",
                       "void a(void) {} void a(void) {}", "extern int a;"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                source_drafts(source)
        with self.assertRaisesRegex(ValueError, "Shared context"):
            source_drafts("void a(void) {}", "void reference(void) {}")

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_shared_types_globals_and_target_order(self):
        context = "typedef unsigned int u32; extern u32 counter;"
        source = assemble([
            {"symbol": "get", "context": context, "source": "u32 get(void) { return counter; }"},
            {"symbol": "set", "context": context, "source": "void set(u32 x) { counter = x; }"},
        ], ["set", "get"])
        self.assertEqual(source.count("typedef unsigned int u32;"), 1)
        self.assertEqual(source.count("extern u32 counter;"), 1)
        self.assertLess(source.index("void set(u32 x)\n{"), source.index("u32 get(void)\n{"))

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_conflicts_are_not_silently_resolved(self):
        for context in ("extern float x;", "static int x;", "typedef int x;"):
            with self.subTest(context=context), self.assertRaisesRegex(ValueError, "Conflicting declarations"):
                assemble([{"symbol": "a", "context": "extern int x;", "source": "void a(void) {}"},
                          {"symbol": "b", "context": context, "source": "void b(void) {}"}], ["a", "b"])

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_shared_storage_emitted_once(self):
        source = assemble([{"symbol": "a", "source": "int a(void) { return values[0]; }"},
                           {"symbol": "b", "source": "int b(void) { return values[1]; }"}],
                          ["a", "b"], "static int values[2] = {1, 2};")
        self.assertEqual(source.count("static int values[2]"), 1)

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_late_prototype_conflict_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Definition conflicts"):
            assemble([{"symbol": "a", "source": "int a(int x) { return x; }"},
                      {"symbol": "b", "context": "float a(int y);", "source": "void b(void) {}"}], ["a", "b"])

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_parameter_names_do_not_change_contract(self):
        self.assertIn("return x;", assemble([{"symbol": "a", "context": "extern int a(int y);",
                                             "source": "int a(int x) { return x; }"}], ["a"]))

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_missing_functions_and_directives_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "cover"):
            assemble([], ["missing"])
        with self.assertRaisesRegex(ValueError, "Preprocessor"):
            assemble([], [], "#pragma pack(1)\n")

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_codegen_pragma_is_preserved_and_conflicts_rejected(self):
        source = assemble([{"symbol": "a", "context": "#pragma use_lmw_stmw on\n",
                            "source": "void a(void) {}"}], ["a"])
        self.assertIn("#pragma use_lmw_stmw on", source)
        with self.assertRaisesRegex(ValueError, "Conflicting declarations"):
            assemble([{"symbol": "a", "context": "#pragma use_lmw_stmw on\n",
                       "source": "void a(void) {}"},
                      {"symbol": "b", "context": "#pragma use_lmw_stmw off\n",
                       "source": "void b(void) {}"}], ["a", "b"])

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_context_bodies_are_not_imported(self):
        source = assemble([{"symbol": "a", "context": "int a(int old) { return 999; }",
                            "source": "int a(int x) { return x; }"}], ["a"])
        self.assertNotIn("999", source)
        self.assertIn("return x;", source)

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_null_macro_kept_as_context_not_body_repair(self):
        source = assemble([{"symbol": "a", "source": "void *a(void) { return NULL; }"}],
                          ["a"], "#define NULL ((void*)0)\n")
        self.assertEqual(source.count("#define NULL"), 1)
        self.assertIn("return NULL;", source)
        with self.assertRaisesRegex(ValueError, "Preprocessor"):
            assemble([], [], "#define NULL external_value\n")

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_shared_array_definition_satisfies_function_externs(self):
        source = assemble([
            {"symbol": "a", "context": "extern const unsigned char data[3];",
             "source": "int a(void) { return data[0]; }"},
            {"symbol": "b", "context": "extern const unsigned char data[3];",
             "source": "int b(void) { return data[2]; }"},
        ], ["a", "b"], "const unsigned char data[3] = {1, 2, 3};")
        self.assertEqual(source.count("const unsigned char data[3]"), 1)
        self.assertIn("{1, 2, 3}", source)

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_later_definition_replaces_extern_without_reordering(self):
        source = assemble([{"symbol": "a", "context": "int counter = 7;",
                            "source": "int a(void) { return counter; }"}],
                          ["a"], "extern int counter;")
        self.assertEqual(source.count("int counter"), 1)
        self.assertIn("int counter = 7;", source)

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_shared_data_conflicts_preserve_type_size_and_linkage(self):
        for declaration in ("extern char data[3];", "extern const unsigned char data[4];",
                            "static const unsigned char data[3];",
                            "const unsigned char data[3] = {3, 2, 1};"):
            with self.subTest(declaration=declaration), self.assertRaisesRegex(ValueError, "Conflicting"):
                assemble([{"symbol": "a", "context": declaration, "source": "void a(void) {}"}],
                         ["a"], "const unsigned char data[3] = {1, 2, 3};")

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_native_builtin_types_fill_empty_context_but_project_types_win(self):
        source = assemble([{"symbol": "a", "source": "s32 a(void) { return 0; }"}], ["a"])
        self.assertIn("typedef int s32;", source)
        source = assemble([{"symbol": "a", "context": "typedef signed long s32;",
                            "source": "s32 a(void) { return 0; }"}], ["a"])
        self.assertIn("typedef signed long s32;", source)
        self.assertNotIn("typedef int s32;", source)


if __name__ == "__main__":
    unittest.main()
