import unittest

import m2c_deterministic_transforms as transforms
import local_campaign_permute as permute


class DeterministicTransformTests(unittest.TestCase):
    def test_declaration_block_skips_leading_externs(self):
        source = """int f(void) {
    extern int global;
    extern void call(void);
    int first;
    int second;

    return global;
}
"""
        block = permute.declaration_block(source)
        self.assertIsNotNone(block)
        self.assertEqual([line.strip() for line in block[2]], ["int first;", "int second;"])

    def test_splits_and_merges_scalar_initialization(self):
        split = transforms.local_shapes("int f(void) {\n    int value = call();\n    return value;\n}\n")
        self.assertEqual(len(split), 1)
        self.assertIn("int value;\n    value = call();", split[0]["code"])
        merged = transforms.local_shapes("int f(void) {\n    int value;\n    value = call();\n    return value;\n}\n")
        self.assertEqual(len(merged), 1)
        self.assertIn("int value = call();", merged[0]["code"])

    def test_toggles_register_storage_class_on_simple_locals(self):
        source = "int f(void) {\n    int value = call();\n    char* cursor;\n    return value;\n}\n"
        variants = transforms.storage_class_shapes(source)
        self.assertEqual(len(variants), 2)
        self.assertIn("register int value = call();", variants[0]["code"])
        self.assertIn("register char* cursor;", variants[1]["code"])
        removed = transforms.storage_class_shapes(
            "int f(void) {\n    register int value = call();\n    return value;\n}\n")
        self.assertEqual(len(removed), 1)
        self.assertIn("    int value = call();", removed[0]["code"])

    def test_storage_class_skips_nonlocal_or_qualified_declarations(self):
        source = "int global;\nint f(void) {\n    static int saved;\n    const int fixed = 1;\n    int values[2];\n}\n"
        self.assertEqual(transforms.storage_class_shapes(source), [])

    def test_register_classification_prioritizes_declaration_changes(self):
        source = "int f(int a, int b) {\n    int first;\n    int second;\n    first = a + b;\n    return first;\n}\n"
        variants = transforms.variants(source, {"register_only": True}, 3)
        self.assertEqual(variants[0]["kind"], "storage_class_shape")
        self.assertTrue(all(row["code"] != source for row in variants))

    def test_register_source_lines_prioritize_local_rewrites(self):
        source = """int f(int a, int b) {
    int first;
    int second;
    first = a + b;
    second = a - b;
    return first + second;
}
"""
        classification = {"register_only": True, "mismatches": [
            {"kind": "register", "source_line": 4}]}
        variants = transforms.variants(source, classification, 8)
        self.assertTrue(any(row["targeted"] for row in variants))
        first_targeted = next(index for index, row in enumerate(variants) if row["targeted"])
        first_general = next((index for index, row in enumerate(variants) if not row["targeted"]), len(variants))
        self.assertLessEqual(first_targeted, first_general)
        self.assertTrue(all(row["targeted"] for row in variants[:first_general]))

    def test_nearest_rewrites_are_ranked_first_within_a_family(self):
        source = """int f(int a, int b) {
    int first;
    int second;
    first = a + b;
    second = a + b;
    return first + second;
}
"""
        classification = {"register_only": True, "mismatches": [
            {"kind": "register", "source_line": 5}]}
        expressions = [row for row in transforms.variants(source, classification, 12)
                       if row["kind"] == "expression_shape"]
        self.assertEqual(expressions[0]["target_distance"], 0)
        self.assertIn("second = b + a", expressions[0]["code"])


if __name__ == "__main__":
    unittest.main()
