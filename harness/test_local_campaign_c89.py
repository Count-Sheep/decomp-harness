import unittest

import local_campaign_c89 as c89
import local_campaign_decompile as decompile


@unittest.skipUnless((decompile.tool().parent / "m2c_pycparser").is_dir(), "m2c parser unavailable")
class C89Tests(unittest.TestCase):
    def test_declaration_order_block_preserves_offsets_comments_and_typedefs(self):
        source = "/* prefix */\nint f(void) /* { */ {\n    Count a; // keep\n    int *b;\n\n    a = 1;\n    return a;\n}\n"
        first, last, lines = c89.declaration_order_block(source, "typedef unsigned int Count;", "f")
        self.assertEqual(source[first:last], "".join(lines))
        self.assertEqual(lines, ["    Count a; // keep\n", "    int *b;\n"])
        self.assertEqual(source[:first] + "".join(reversed(lines)) + source[last:],
                         source.replace("".join(lines), "".join(reversed(lines))))

    def test_declaration_order_block_rejects_initializers_storage_qualifiers_and_typedef_arrays(self):
        for declaration, context in (
            ("int a = 0;", ""), ("int a = NULL;", ""),
            ("static int a;", ""), ("volatile int a;", ""), ("const int a;", ""),
            ("int a[4];", ""), ("Array a;", "typedef int Array[4];"),
            ("ReadOnly a;", "typedef volatile int ReadOnly;"),
            ("Aligned a;", "typedef int Aligned __attribute__((aligned(16)));"),
            ("S a;", "typedef struct S { int field; } S;"),
            ("Pointer a;", "typedef int (*Pointer)[4];"),
        ):
            with self.subTest(declaration=declaration), self.assertRaises(c89.RepairError):
                c89.declaration_order_block("int f(void) {\n    " + declaration + "\n    int b;\n    return 1;\n}", context, "f")

    def test_declaration_order_block_supports_void_and_aggregate_pointers_not_aggregate_objects(self):
        context = "typedef struct S { int field; } S; typedef S* Pointer; enum Mode { ON, OFF };"
        source = "int f(void) {\n    void *a;\n    S *b;\n    Pointer c;\n    enum Mode mode;\n    int d;\n    return 1;\n}"
        start, end, lines = c89.declaration_order_block(source, context, "f")
        self.assertEqual(len(lines), 5)
        self.assertEqual(source[start:end], "".join(lines))

    def test_declaration_order_block_skips_single_local_and_side_effect_initializer(self):
        self.assertIsNone(c89.declaration_order_block("int f(void) {\n    int a;\n    return 1;\n}", "", "f"))
        self.assertIsNone(c89.declaration_order_block("int f(void) {\n    int a = tick();\n    int b;\n    return 1;\n}",
                                                     "int tick(void);", "f"))
        with self.assertRaisesRegex(c89.RepairError, "Cannot screen"):
                c89.declaration_order_block("int f(void) {\n    int a;\n    int b;\n    return +;\n}", "", "f")

    def repair(self, source, context=""):
        return c89.repair_declarations(source, context, "f")

    def test_initializer_stays_after_side_effect(self):
        result, count = self.repair("int f(void) { tick(); int x = value(); return x; }",
                                    "void tick(void); int value(void);")
        self.assertEqual(count, 1)
        self.assertLess(result.index("int x;"), result.index("tick();"))
        self.assertLess(result.index("tick();"), result.index("x = value();"))

    def test_signature_and_noop_spelling_are_preserved(self):
        source = "void* f(void) { /* keep */ int x; return 0; }"
        self.assertEqual(self.repair(source), (source, 0))
        source = "void* f(void) { tick(); void *x = 0; return x; }"
        result, count = self.repair(source, "void tick(void);")
        self.assertTrue(result.startswith("void* f(void)"))
        self.assertEqual(count, 1)

    def test_nested_block_and_typedef_scalar(self):
        result, count = self.repair("int f(int a) { if(a) { a++; u32 x=a; return x; } return 0; }",
                                    "typedef unsigned int u32;")
        self.assertEqual(count, 1)
        self.assertLess(result.index("u32 x;"), result.index("a++;"))
        self.assertLess(result.index("a++;"), result.index("x = a;"))

    def test_bind_only_parser_identical_signatures(self):
        source = "void *f(int *x) { return x; }"
        result = c89.bind_signature(source, "", "void* f(int* x)", "f")
        self.assertTrue(result.startswith("void* f(int* x)"))
        self.assertIn("{ return x; }", result)
        for signature in ("int* f(int* x)", "void* f(unsigned int* x)", "void* f(int* y)",
                          "static void* f(int* x)"):
            with self.subTest(signature=signature), self.assertRaises(c89.RepairError):
                c89.bind_signature(source, "", signature, "f")

    def test_fail_closed_on_shadowing_and_lifetime_constructs(self):
        cases = [
            ("int f(int x) { x++; int x=2; return x; }", ""),
            ("int f(void) { tick(); const int x=2; return x; }", "void tick(void);"),
            ("int f(void) { tick(); static int x=2; return x; }", "void tick(void);"),
            ("int f(void) { tick(); int x[2]={1,2}; return x[0]; }", "void tick(void);"),
            ("int f(void) { goto end; int x=2; end: return x; }", ""),
            ("int f(void) { tick(); S x; return 0; }", "typedef struct S { int x; } S; void tick(void);"),
            ("int f(void) { tick(); A x; return 0; }", "typedef int A[4]; void tick(void);"),
            ("int f(void) { tick(); int x=y; int y=2; return x+y; }", "int y; void tick(void);"),
        ]
        for source, context in cases:
            with self.subTest(source=source), self.assertRaises(c89.RepairError):
                self.repair(source, context)

    def test_primitive_alias_binding_uses_actual_declarations(self):
        context = "typedef double f64; typedef f64 Sample;"
        source = "f64 f(Sample *x) { return *x; }"
        result = c89.bind_signature(source, context, "double f(double* x)", "f", primitive_aliases=True)
        self.assertTrue(result.startswith("double f(double* x)"))
        with self.assertRaises(c89.RepairError):
            c89.bind_signature(source, context, "double f(double* x)", "f")

    def test_alias_binding_preserves_qualifiers_names_storage_and_scalar_identity(self):
        context = "typedef double f64; typedef const f64 ReadOnly;"
        source = "f64 f(ReadOnly *x) { return *x; }"
        result = c89.bind_signature(source, context, "double f(const double* x)", "f", primitive_aliases=True)
        self.assertIn("double f(const double* x)", result)
        for signature in ("double f(double* x)", "double f(const double* y)",
                          "static double f(const double* x)", "float f(const double* x)"):
            with self.subTest(signature=signature), self.assertRaises(c89.RepairError):
                c89.bind_signature(source, context, signature, "f", primitive_aliases=True)
        with self.assertRaises(c89.RepairError):
            c89.bind_signature("Count f(Count x) { return x; }", "typedef unsigned int Count;",
                               "int f(int x)", "f", primitive_aliases=True)

    def test_pointer_aggregate_and_attributed_aliases_are_not_flattened(self):
        for context, source, signature in (
            ("typedef double *Pointer;", "Pointer f(Pointer x) { return x; }", "double* f(double* x)"),
            ("struct S { int x; }; typedef struct S S;", "S* f(S* x) { return x; }", "struct S* f(struct S* x)"),
            ("typedef int Aligned __attribute__((aligned(16)));", "Aligned f(Aligned x) { return x; }", "int f(int x)"),
        ):
            with self.subTest(context=context), self.assertRaises(c89.RepairError):
                c89.bind_signature(source, context, signature, "f", primitive_aliases=True)


if __name__ == "__main__":
    unittest.main()
