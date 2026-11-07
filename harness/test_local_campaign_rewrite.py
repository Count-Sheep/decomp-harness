"""Safety checks for the policy-safe rewrite search."""

import unittest

import local_campaign_rewrite as rw


def labels(function_text):
    return [r.label for r in rw.rewrites(function_text)]


def applied(function_text, label_start):
    return [r.apply(function_text) for r in rw.rewrites(function_text) if r.label.startswith(label_start)]


class RewriteSafetyTests(unittest.TestCase):
    def test_operand_swaps_respect_precedence(self):
        text = "int f(int a, int b, int c) {\n    return a - b + c;\n}\n"
        self.assertFalse(any("b + c" in l for l in labels(text)))  # (a - b) + c: b + c is not a subexpression
        text = "int f(int a, int b, int c) {\n    return a + b * c;\n}\n"
        self.assertFalse(any("a + b" in l for l in labels(text)))
        self.assertTrue(any("b * c" in l for l in labels(text)))
        text = "int f(int a, int b) {\n    return (a + b) * 2;\n}\n"
        self.assertIn("int f(int a, int b) {\n    return (b + a) * 2;\n}\n", applied(text, "swap operands of a + b"))

    def test_no_swaps_through_short_circuit_unary_or_calls(self):
        text = "int f(int *p, int a) {\n    return p && *p == a;\n}\n"
        self.assertFalse(any("&&" in l for l in labels(text)))
        text = "int f(int a, int b) {\n    return &a == b;\n}\n"
        self.assertFalse(any("swap operands" in l for l in labels(text)))
        text = "int f(int a) {\n    return g(a) + a;\n}\n"
        self.assertFalse(any("swap operands" in l for l in labels(text)))
        text = "int f(int a, int b) {\n    return a++ + b;\n}\n"
        self.assertFalse(any("swap operands" in l for l in labels(text)))

    def test_relational_comparisons_are_mirrored_not_negated(self):
        text = "int f(float a, float b) {\n    return a < b;\n}\n"
        self.assertIn("int f(float a, float b) {\n    return b > a;\n}\n", applied(text, "swap operands"))

    def test_declarations_and_prototypes_are_not_products(self):
        text = "void f(void) {\n    extern void g(u8 * p);\n    u8 * q;\n    q = 0;\n}\n"
        self.assertFalse(any("swap operands" in l for l in labels(text)))

    def test_increment_and_loop_forms(self):
        text = "void f(int n) {\n    int i;\n    for (i = 0; i < n; i++) {\n        g(i);\n    }\n}\n"
        self.assertTrue(any(l == "write i++ as i += 1" for l in labels(text)))
        loop = applied(text, "write the for")
        self.assertEqual(len(loop), 1)
        self.assertIn("i = 0;\n    while (i < n) {\n        g(i);\n        i++;\n    }", loop[0])
        text = "void f(int n) {\n    int i;\n    for (i = 0; i < n; i++) {\n        if (i) continue;\n    }\n}\n"
        self.assertEqual(applied(text, "write the for"), [])

    def test_declarations_move_only_with_literal_initializers(self):
        text = "void f(void) {\n    int a;\n    int b = 0;\n    a = b;\n}\n"
        self.assertIn("void f(void) {\n    int b = 0;\n    int a;\n    a = b;\n}\n", applied(text, "swap declarations"))
        text = "void f(void) {\n    int a = g();\n    int b;\n    b = a;\n}\n"
        self.assertEqual(applied(text, "swap declarations"), [])

    def test_statement_swaps_only_when_independent(self):
        text = "void f(int *p, int k) {\n    int a;\n    int b;\n    a = k + 1;\n    b = k * 2;\n    use(a, b);\n}\n"
        self.assertTrue(applied(text, "swap statements a = k + 1;"))
        # b reads a: the order matters.
        text = "void f(int k) {\n    int a;\n    int b;\n    a = k + 1;\n    b = a * 2;\n    use(a, b);\n}\n"
        self.assertEqual(applied(text, "swap statements a = k + 1;"), [])
        # A call could write memory the simple statement reads.
        text = "void f(int *p) {\n    int a;\n    a = p[0];\n    g();\n    use(a);\n}\n"
        self.assertEqual(applied(text, "swap statements"), [])
        # A local whose address escapes is not treated as private.
        text = "void f(void) {\n    int a;\n    int b;\n    h(&a);\n    b = 1;\n    a = 2;\n    g();\n}\n"
        self.assertEqual(applied(text, "swap statements a = 2;"), [])
        # `return x;` does not make a global x look like a local.
        text = "void f(void) {\n    x = 1;\n    g();\n    return x;\n}\n"
        self.assertEqual(applied(text, "swap statements"), [])


if __name__ == "__main__":
    unittest.main()
