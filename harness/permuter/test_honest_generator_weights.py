import re
import unittest
from pathlib import Path


GENERATOR = Path(__file__).resolve().parent / "gen_workunits.py"
UNSAFE = {
    "perm_dummy_comma_expr",
    "perm_pad_var_decl",
    "perm_duplicate_assignment",
    "perm_add_self_assignment",
    "perm_refer_to_var",
}


class HonestGeneratorWeightsTest(unittest.TestCase):
    def test_generator_disables_source_invalid_mutations(self):
        text = GENERATOR.read_text(encoding="utf-8")
        for key in UNSAFE:
            with self.subTest(key=key):
                match = re.search(rf"{re.escape(key)}\s*=\s*(\d+)", text)
                self.assertIsNotNone(match, key)
                self.assertEqual(match.group(1), "0", key)


if __name__ == "__main__":
    unittest.main()
