from types import SimpleNamespace as Record
import json
from pathlib import Path
import tempfile
import unittest

from m2c_unit_inventory import inventory, uncovered, track_drafts


class UnitInventoryTests(unittest.TestCase):
    def example(self):
        section = Record(index=1, name=".text", type=1, flags=6, size=12,
                         addralign=8, entsize=0, data=b"x" * 12)
        symbol = Record(index=0, name="function", shndx=1, value=0, size=4,
                        type=2, bind=1, info=18, other=0)
        return Record(data=b"object", flags=0, sections=[section], symbols=[symbol], relocations=[])

    def test_gaps_handle_aliases_overlaps_and_empty_sections(self):
        self.assertEqual(uncovered(16, [(0, 4), (0, 4), (2, 8), (12, 16)]), [[8, 12]])
        self.assertEqual(uncovered(8, []), [[0, 8]])
        self.assertEqual(uncovered(0, []), [])

    def test_function_exactness_cannot_hide_unnamed_data(self):
        result = inventory(self.example())
        self.assertEqual(result["function_count"], 1)
        self.assertEqual(result["sections"][0]["unnamed_ranges"], [[4, 12]])
        self.assertEqual(result["sections"][0]["alignment"], 8)
        self.assertNotIn("unit_complete", result)

    def test_bss_keeps_size_without_claiming_file_bytes(self):
        owner = self.example()
        owner.sections[0].type = 8
        owner.sections[0].data = b""
        result = inventory(owner)["sections"][0]
        self.assertEqual(result["size"], 12)
        self.assertIsNone(result["bytes_sha256"])

    def test_relocation_retains_identity_and_addend(self):
        owner = self.example()
        owner.relocations = [Record(target_section_index=1, offset=4, type=109,
                                    addend=8, symbol_index=0)]
        relocation = inventory(owner)["sections"][0]["relocations"][0]
        self.assertEqual((relocation["symbol"], relocation["addend"]), ("function", 8))
        owner.relocations[0].offset = 12
        with self.assertRaisesRegex(ValueError, "outside section"):
            inventory(owner)

    def test_out_of_bounds_definition_rejected(self):
        owner = self.example()
        owner.symbols[0].size = 13
        with self.assertRaisesRegex(ValueError, "outside section"):
            inventory(owner)

    def test_tracker_binds_owner_and_preserves_missing_data_work(self):
        report = inventory(self.example())
        report.update(unit="main/game/unit", objdiff_sha256="configuration")
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            (folder / "draft.c").write_text("void function(void) {}")
            batch = folder / "results.json"
            rows = [{"symbol": "function", "unit": "game/unit", "status": "ok",
                     "artifacts": str(folder), "instruction_exact": True}]
            batch.write_text(json.dumps({"results": rows}))
            tracked = track_drafts(report, [batch])
            self.assertEqual(tracked["drafted_functions"], 1)
            self.assertEqual(tracked["missing_functions"], [])
            self.assertTrue(tracked["unnamed_ranges_requiring_review"])
            self.assertFalse(tracked["unit_complete"])
            self.assertFalse(tracked["object_verified"])
            digest = tracked["functions"][0]["drafts"][0]["sha256"]
            (folder / "draft.c").write_text("void function(void) { return; }")
            self.assertNotEqual(digest, track_drafts(report, [batch])["functions"][0]["drafts"][0]["sha256"])
            for field, value in (("unit", "other"), ("module", "rel"), ("status", "compile_error")):
                changed = dict(rows[0], **{field: value})
                batch.write_text(json.dumps({"results": [changed]}))
                self.assertEqual(track_drafts(report, [batch])["missing_functions"], ["function"])

    def test_identical_source_with_different_context_is_ambiguous(self):
        report = inventory(self.example())
        report.update(unit="main/game/unit", objdiff_sha256="configuration")
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            rows = []
            for index, context in enumerate(("extern int value;", "extern short value;")):
                artifact = folder / str(index)
                artifact.mkdir()
                (artifact / "draft.c").write_text("int function(void) { return value; }")
                (artifact / "context.c").write_text(context)
                rows.append({"symbol": "function", "unit": "game/unit", "status": "ok",
                             "artifacts": str(artifact)})
            batch = folder / "results.json"
            batch.write_text(json.dumps({"results": rows}))
            tracked = track_drafts(report, [batch])
            self.assertEqual(tracked["ambiguous_functions"], ["function"])
            self.assertFalse(tracked["unit_complete"])
            (folder / "1/context.c").write_text("extern int value;")
            self.assertEqual(track_drafts(report, [batch])["ambiguous_functions"], [])


if __name__ == "__main__":
    unittest.main()
