"""Parsing checks for the register-allocation replay."""

import tempfile
import unittest
from pathlib import Path

import local_campaign_colouring as colouring

ASSIGNED = """r40 -> r31 count
  flags:
  cost: 20
  adjusted cost: 1.50
  previous neighbors: 2 (r0 r3)
  neighbors: 4 (r0 r3 r41 r42)
r41 -> r30 index
  flags:
  cost: 41
  adjusted cost: 1.86
  neighbors: 3 (r0 r40 r42)
r42 -> r29 @12
  flags:
  cost: 41
  adjusted cost: 2.41
  neighbors: 3 (r0 r40 r41)
"""

BEFORE = """B1: Successors = { B2 }  Predecessors = { B0 }  Labels = { L1 }
    10  li       r40,0
    11  li       r41,0
        mr       r42,r41
    12  addi     r41,r41,1
"""


class ColouringTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.out = Path(temporary.name)
        (self.out / "regalloc-gpr-pass-1-assigned.txt").write_text(ASSIGNED)
        (self.out / "backend-12-before-regalloc.txt").write_text(BEFORE)

    def test_assignments_keep_order_names_and_costs(self):
        rows = colouring.assignments(self.out)
        self.assertEqual([(r["virtual"], r["physical"], r["name"], r["order"]) for r in rows],
                         [("r40", "r31", "count", 1), ("r41", "r30", "index", 2), ("r42", "r29", "@12", 3)])
        self.assertEqual(rows[1]["cost"], 41)
        self.assertEqual(rows[1]["adjusted"], 1.86)
        self.assertEqual(rows[0]["degree"], 4)

    def test_virtual_lines_record_where_each_value_is_written(self):
        self.assertEqual(colouring.virtual_lines(self.out), {"r40": [10], "r41": [11, 12]})

    def test_register_differences_only_on_same_instruction(self):
        rows = [("addi r29, r29, 0x1", "addi r30, r30, 0x1", 12), ("li r3, 0x0", "bl fn", 13),
                ("li r31, 0x0", "li r31, 0x0", 10)]
        self.assertEqual(colouring.register_differences(rows), [("r30", "r29", 12)])

    def test_explain_names_the_values_and_the_colouring_order(self):
        rows = [("addi r29, r29, 0x1", "addi r30, r30, 0x1", 12), ("mr r30, r29", "mr r29, r30", 11)]
        source = "\n" * 9 + "count = 0;\nindex = 0;\nindex++;\n"
        text = colouring.explain(self.out, rows, source, True)
        self.assertIn("identical to the unit's compiler", text)
        self.assertIn("we put r30 where retail has r29", text)
        self.assertIn("`index` (r41, written at line 11, 12 `index = 0;`", text)
        self.assertIn("compiler temporary @12", text)  # r29's holder has no line: an induction temp
        self.assertIn("2. index→r30 *", text)
        self.assertIn("NOT reproduce", colouring.explain(self.out, rows, source, False))

    def test_scheduling_noise_is_not_explained_as_colouring(self):
        rows = [("fadds f1, f2, f3", "fadds f0, f2, f3", 5), ("fmuls f4, f2, f3", "fmuls f0, f2, f3", 6)]
        self.assertIn("scheduling", colouring.explain(self.out, rows, "", True))


if __name__ == "__main__":
    unittest.main()
