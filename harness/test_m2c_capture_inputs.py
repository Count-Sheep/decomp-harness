from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as Record
import unittest
from unittest.mock import patch

import m2c_capture_inputs as capture
import m2c_context as context


def assembly(symbol="target", address=0x80001000, words=("38600001", "4e800020")):
    lines = [".text", f".fn {symbol}, global"]
    for index, word in enumerate(words):
        code = "blr" if word == "4e800020" else "li r3, 1"
        lines.append(f"/* {address + index * 4:08X} 00000000  {bytes.fromhex(word).hex(' ')} */ {code}")
    return "\n".join([*lines, f".endfn {symbol}", ""])


def owner(path=Path("owner.o"), symbol="target", payload=bytes.fromhex("386000014e800020")):
    return Record(path=path, data=b"owner", sections=[Record(), Record(data=payload, type=1, flags=6)],
                  symbols=[Record(name=symbol, type=2, shndx=1, value=0, size=len(payload))],
                  relocations=[])


class CaptureInputsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.version = "TEST01"
        self.batch = self.root / "batch"
        self.artifact = self.root / "artifacts"
        self.report = {"units": [{"name": "main/new", "metadata": {"module_name": "main"},
                                  "functions": [{"name": "target", "size": "8", "address": "0",
                                                 "metadata": {"virtual_address": "2147487744"}}]}]}
        self.objdiff = {"units": [{"name": "main/new", "target_path": "build/TEST01/obj/new.o"},
                                  {"name": "main/old", "scratch": {"compiler": "mwcc_233_159p1",
                                                                   "c_flags": "-O4,p -lang=c"}}]}
        self.historical = {"symbol": "target", "module": "main", "address": "2147487744",
                           "unit": "main/old", "status": "ok", "pct": 100,
                           "assembly": "/deliberately/not/the/artifact.s", "artifacts": str(self.artifact)}
        self.data = {"summary": {"workspace": str(self.root), "version": self.version, "finished": True,
                                 "total": 1, "processed": 1, "report_sha256": "historical-symbols-hash"},
                     "results": [self.historical]}
        self.write("build/TEST01/obj/new.o", b"owner")
        self.write("build/TEST01/asm/new.s", assembly())
        self.write("build/TEST01/config.json", "{}")
        for name in ("config.yml", "symbols.txt", "splits.txt"):
            self.write("config/TEST01/" + name, "config")
        self.write("artifacts/original_target.s", assembly())
        self.write("artifacts/target.s", assembly() + "# prepared input\n")
        self.write("artifacts/context.c", "typedef int ContextType;\n")
        self.save_metadata()
        self.reader = patch.object(capture, "read_owner", side_effect=self.read_owner).start()
        self.addCleanup(patch.stopall)

    def write(self, name, contents):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents if isinstance(contents, bytes) else contents.encode())
        return path

    def save_metadata(self):
        self.write("build/TEST01/report.json", json.dumps(self.report))
        self.write("objdiff.json", json.dumps(self.objdiff))
        self.write("batch/results.json", json.dumps(self.data))

    def read_owner(self, path, inputs):
        obj = owner(path)
        obj.data = inputs.read(path)
        return obj

    def run_capture(self, batches=None, symbols=None):
        return capture.capture(self.root, self.version, batches or [self.batch], symbols or ["target"])

    def test_rebinds_and_keeps_actual_inputs_without_inventing_outcomes(self):
        snapshot = self.run_capture()
        row = snapshot["rows"][0]
        self.assertEqual(row["unit"], "new")
        self.assertEqual(row["original_unit"], "main/old")
        self.assertTrue(row["owner_rebound"])
        self.assertEqual(row["size"], 8)  # Campaign rows do not store size.
        self.assertEqual(row["assembly"], (self.artifact / "target.s").read_text())
        self.assertEqual(row["original_assembly"], assembly())
        self.assertIn("ContextType", row["context"])
        self.assertEqual(row["historical_pct"], 100)
        self.assertEqual(row["outcome"], {})
        self.assertEqual(row["categories"], [])
        self.assertIsNone(row["evidence"]["instruction_exact"])
        self.assertEqual(snapshot["report_sha256"], capture.digest((self.root / "build/TEST01/report.json").read_bytes()))
        self.assertNotEqual(snapshot["report_sha256"], self.data["summary"]["report_sha256"])
        self.assertEqual(row["compiler_profile"]["selected"]["compiler"], "mwcc_233_159p1")
        self.assertFalse(row["compiler_profile"]["supported"])
        self.assertEqual(row["compiler_profile"]["profiles"], [])
        capture.verify_snapshot(snapshot)

    def test_report_offset_without_address_uses_current_owner_assembly(self):
        self.report["units"][0]["functions"][0]["metadata"] = {}
        self.historical.update(size=8, unit="old")
        del self.historical["module"]
        self.save_metadata()
        self.assertEqual(self.run_capture()["rows"][0]["unit"], "new")
        self.write("build/TEST01/asm/new.s", assembly(address=0x80002000))
        with self.assertRaisesRegex(ValueError, "unique module/symbol/address/size"):
            self.run_capture()

    def test_exact_original_owner_profile_and_binary_hash(self):
        self.objdiff["units"][1]["scratch"] = {"compiler": "mwcc_242_53", "c_flags": '-O4,p -pragma "cats off"'}
        binary = self.write("build/compilers/GC/1.3/mwcceppc.exe", b"compiler fixture")
        self.save_metadata()
        snapshot = self.run_capture()
        profile = snapshot["rows"][0]["compiler_profile"]
        self.assertTrue(profile["supported"])
        self.assertEqual(profile["profiles"][0]["flags"], ["-O4,p", "-pragma", "cats off"])
        self.assertEqual(profile["selected"]["mw_version"], "GC/1.3")
        binary.write_bytes(b"changed compiler")
        with self.assertRaisesRegex(ValueError, "Input changed"):
            capture.verify_snapshot(snapshot)

    def test_missing_original_profile_can_use_strict_exact_current_owner(self):
        import m2c_object_evidence
        self.objdiff["units"][1].pop("scratch")
        self.objdiff["units"][0].update(
            scratch={"compiler": "mwcc_242_53", "c_flags": "-O4,p"},
            base_path="build/TEST01/src/new.o",
            metadata={"complete": True, "source_path": "src/new.c"})
        self.write("build/compilers/GC/1.3/mwcceppc.exe", b"compiler fixture")
        base = self.write("build/TEST01/src/new.o", b"compiled fixture")
        self.write("src/new.c", "int target(void) { return 1; }")
        self.save_metadata()
        with patch.object(m2c_object_evidence, "compare", return_value=
                          {"status": "checked", "scratch_object_equal": True}) as compare:
            snapshot = self.run_capture()
        profile = snapshot["rows"][0]["compiler_profile"]
        self.assertEqual(profile["method"], "current_exact_owner_configuration")
        self.assertEqual(profile["profiles"][0]["confidence"], "current_exact_owner_configuration")
        self.assertEqual(profile["selected"]["unit"], "new")
        self.assertEqual(profile["original_profile"]["status"], "unsupported_or_unknown")
        self.assertEqual(compare.call_args.args[2], "target")
        capture.verify_snapshot(snapshot)
        base.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "Input changed"):
            capture.verify_snapshot(snapshot)

    def test_current_profile_rejects_unverified_or_nonexact_objects(self):
        import m2c_object_evidence
        self.objdiff["units"][0].update(
            scratch={"compiler": "mwcc_242_53", "c_flags": "-O4,p"},
            base_path="build/TEST01/src/new.o",
            metadata={"complete": True, "source_path": "src/new.c"})
        self.write("build/compilers/GC/1.3/mwcceppc.exe", b"compiler fixture")
        self.write("build/TEST01/src/new.o", b"compiled fixture")
        self.write("src/new.c", "int target(void) { return 1; }")
        original = {"status": "unsupported_or_unknown"}
        for evidence in ({"status": "checked", "scratch_object_equal": False},
                         {"status": "unavailable", "scratch_object_equal": True}):
            with patch.object(m2c_object_evidence, "compare", return_value=evidence):
                profile = capture.current_exact_owner_profile(
                    self.root, "new", "target", self.objdiff, capture.Inputs(), original)
                self.assertEqual(profile["status"], "unsupported_or_unknown")
        with patch.object(m2c_object_evidence, "compare") as compare:
            supported = {"status": "supported"}
            self.assertIs(capture.current_exact_owner_profile(
                self.root, "new", "target", self.objdiff, capture.Inputs(), supported), supported)
            self.objdiff["units"][0]["metadata"]["complete"] = False
            self.assertIs(capture.current_exact_owner_profile(
                self.root, "new", "target", self.objdiff, capture.Inputs(), original), original)
            compare.assert_not_called()

    def test_fresh_context_preserves_original_and_hashes_source_evidence(self):
        source = self.write("src/target.c", "double target(void) { return 1.0; }\n")
        rows = [{"symbol": "target", "source_path": "src/target.c", "unit": "main/new"}]
        with patch.object(context, "_report_exact_rows", return_value=rows):
            snapshot = capture.capture(self.root, self.version, [self.batch], ["target"], fresh_context=True)
        row = snapshot["rows"][0]
        self.assertEqual(row["original_context"], "typedef int ContextType;\n")
        self.assertIn("double target(void);", row["context"])
        self.assertEqual(row["context_evidence"]["target_signature"], "double target(void)")
        capture.verify_snapshot(snapshot)
        source.write_text("double target(void) { return 2.0; }\n")
        with self.assertRaisesRegex(ValueError, "Input changed"):
            capture.verify_snapshot(snapshot)

    def test_fresh_context_omits_unknown_and_conflicting_callee_returns(self):
        self.write("src/first.c", "double called(void) { return 1.0; }\n")
        self.write("src/second.c", "int called(void) { return 1; }\n")
        rows = [{"symbol": "called", "source_path": f"src/{name}.c", "unit": name}
                for name in ("first", "second")]
        asm = ".fn target, global\nbl called\nbl unknown\nblr\n.endfn target\n"
        with patch.object(context, "_report_exact_rows", return_value=rows):
            header, evidence = capture.refresh_context(self.root, self.version, "target", asm, capture.Inputs())
        self.assertNotIn("called(", header)
        self.assertNotIn("unknown(", header)
        self.assertEqual(evidence["conflicts"], ["called"])
        self.assertEqual(evidence["unknown_callees"], ["called", "unknown"])

    def test_fresh_context_records_unavailable_custom_type_dependencies(self):
        self.write("src/matrix.c", "void matrix(register Mtx m) { }\n")
        self.write("src/target.c", "GXBool target(void) { return 0; }\n")
        rows = [{"symbol": name, "source_path": f"src/{name}.c", "unit": name}
                for name in ("matrix", "target")]
        asm = ".fn target, global\nbl matrix\nblr\n.endfn target\n"
        inputs = capture.Inputs()
        with patch.object(context, "_report_exact_rows", return_value=rows):
            header, evidence = capture.refresh_context(self.root, self.version, "target", asm, inputs)
        self.assertNotIn("Mtx", header)
        self.assertNotIn("GXBool", header)
        self.assertEqual(evidence["unresolved_by_value_types"], {"matrix": ["Mtx"], "target": ["GXBool"]})
        self.assertEqual(evidence["known_callees"], [])
        self.assertEqual(evidence["unknown_callees"], ["matrix"])
        self.assertIsNone(evidence["target_signature"])
        self.assertEqual(len(evidence["sources"]), 2)
        self.assertEqual(len(inputs.files), 2)

    def test_ambiguous_symbols_and_incomplete_batches_fail(self):
        other = self.root / "other"
        self.write("other/results.json", json.dumps(self.data))
        with self.assertRaisesRegex(ValueError, "ambiguous selected symbol"):
            self.run_capture(batches=[self.batch, other])
        with self.assertRaisesRegex(ValueError, "Missing or ambiguous"):
            self.run_capture(symbols=["absent"])
        self.data["summary"]["finished"] = False
        self.save_metadata()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.run_capture()

    def test_multiple_batches_form_explicit_union(self):
        other = deepcopy(self.data)
        other["results"][0].update(symbol="second", address=0x80002000, artifacts=str(self.root / "second"))
        self.write("other/results.json", json.dumps(other))
        self.report["units"].append({"name": "main/second", "functions": [
            {"name": "second", "size": 8, "address": 0, "metadata": {"virtual_address": 0x80002000}}]})
        self.objdiff["units"].append({"name": "main/second", "target_path": "build/TEST01/obj/second.o"})
        self.write("build/TEST01/obj/second.o", b"second owner")
        for name in ("target.s", "original_target.s"):
            self.write("second/" + name, assembly("second", 0x80002000))
        self.write("second/context.c", "")
        self.save_metadata()
        def reader(path, inputs):
            obj = owner(path, "second" if path.stem == "second" else "target")
            obj.data = inputs.read(path)
            return obj
        self.reader.side_effect = reader
        snapshot = self.run_capture([self.batch, self.root / "other"], ["second", "target"])
        self.assertEqual([r["symbol"] for r in snapshot["rows"]], ["second", "target"])
        self.assertEqual(len(snapshot["target_objects"]), 2)

    def test_wrong_identity_ambiguous_owner_and_objdiff_mismatch_fail(self):
        for field, value in (("size", 12), ("address", 0x80002000), ("module", "other")):
            with self.subTest(field=field):
                saved = deepcopy(self.historical)
                self.historical[field] = value
                self.save_metadata()
                with self.assertRaises(ValueError):
                    self.run_capture()
                self.historical.clear()
                self.historical.update(saved)
        self.report["units"].append(deepcopy(self.report["units"][0]))
        self.save_metadata()
        with self.assertRaisesRegex(ValueError, "unique module/symbol/address/size"):
            self.run_capture()
        self.report["units"].pop()
        self.objdiff["units"][0]["target_path"] = "wrong.o"
        self.save_metadata()
        with self.assertRaisesRegex(ValueError, "Objdiff/report"):
            self.run_capture()

    def test_stale_observations_fail_even_when_identity_matches(self):
        for name in ("target.s", "original_target.s"):
            self.write("artifacts/" + name, assembly(words=("38600002", "4e800020")))
        with self.assertRaisesRegex(ValueError, "Stale assembly"):
            self.run_capture()

    def test_missing_artifact_never_falls_back_to_original_batch_assembly(self):
        (self.artifact / "context.c").unlink()
        with self.assertRaises(FileNotFoundError):
            self.run_capture()

    def test_report_mutation_during_capture_and_after_export_is_rejected(self):
        original_reader = self.read_owner
        def mutate(path, inputs):
            obj = original_reader(path, inputs)
            self.write("build/TEST01/report.json", "{}")
            return obj
        self.reader.side_effect = mutate
        with self.assertRaisesRegex(ValueError, "Input changed"):
            self.run_capture()
        self.reader.side_effect = original_reader
        self.save_metadata()
        output = self.root / "snapshot.json"
        snapshot = capture.export(self.root, self.version, [self.batch], ["target"], output)
        with self.assertRaises(FileExistsError):
            capture.export(self.root, self.version, [self.batch], ["target"], output)
        self.write("config/TEST01/splits.txt", "changed")
        with self.assertRaisesRegex(ValueError, "Input changed"):
            capture.verify_snapshot(snapshot)

    def test_symlink_retargeting_with_identical_bytes_is_rejected(self):
        first = self.write("first", "same")
        second = self.write("second", "same")
        link = self.root / "link"
        try:
            link.symlink_to(first)
        except OSError as error:  # Windows without Developer Mode or the symlink privilege
            self.skipTest(f"symbolic links unavailable: {error}")
        inputs = capture.Inputs()
        inputs.read(link)
        link.unlink()
        link.symlink_to(second)
        with self.assertRaisesRegex(ValueError, "Input changed"):
            inputs.verify()

    def test_preflight_rejects_embedded_mutation_and_new_tool(self):
        snapshot = self.run_capture()
        snapshot["rows"][0]["context"] += "changed"
        with self.assertRaisesRegex(ValueError, "Embedded input changed"):
            capture.verify_snapshot(snapshot)
        snapshot = self.run_capture()
        self.write("build/tools/dtk", b"new tool")
        with self.assertRaisesRegex(ValueError, "Previously unavailable tool"):
            capture.verify_snapshot(snapshot)


class InstructionEvidenceTests(unittest.TestCase):
    def test_relocation_masks_preserve_branch_opcode_and_target_identity(self):
        obj = owner(payload=bytes.fromhex("480000014e800020"))
        obj.symbols.append(Record(name="callee"))
        obj.relocations = [Record(target_section_index=1, offset=0, type=10, symbol_index=1, addend=0)]
        rows = [(0x80001000, bytes.fromhex("48000101"), "bl callee"),
                (0x80001004, bytes.fromhex("4e800020"), "blr")]
        evidence = capture.compare_owner(obj, "target", rows, {"address": 0})
        self.assertEqual(evidence["relocations"][0]["mask"], "03fffffc")
        for word, operand in (("48000100", "b callee"), ("48000101", "bl other"),
                              ("48000101", "bl callee+4")):
            with self.subTest(operand=operand), self.assertRaises(ValueError):
                capture.compare_owner(obj, "target", [(rows[0][0], bytes.fromhex(word), operand), rows[1]], {})
        for kind, addend in ((99, 0), (10, 4), (10, None)):
            obj.relocations[0].type, obj.relocations[0].addend = kind, addend
            with self.assertRaises(ValueError):
                capture.compare_owner(obj, "target", rows, {})

    def test_halfword_and_sda_masks_keep_other_instruction_bits(self):
        for kind, offset, raw, linked, operand in (
                (6, 2, "3c600000", "3c608040", "lis r3, global@ha"),
                (109, 0, "80600000", "806d8120", "lwz r3, global@sda21(r0)")):
            with self.subTest(kind=kind):
                obj = owner(payload=bytes.fromhex(raw))
                obj.symbols.append(Record(name="global"))
                obj.relocations = [Record(target_section_index=1, offset=offset, type=kind, symbol_index=1, addend=0)]
                capture.compare_owner(obj, "target", [(0, bytes.fromhex(linked), operand)], {})
                changed = (int(linked, 16) ^ 0x00200000).to_bytes(4, "big")
                with self.assertRaisesRegex(ValueError, "Stale assembly"):
                    capture.compare_owner(obj, "target", [(0, changed, operand)], {})

    def test_quoted_compiler_generated_relocation_symbols(self):
        obj = owner(payload=bytes.fromhex("c8000000"))
        obj.symbols.append(Record(name="@301"))
        obj.relocations = [Record(target_section_index=1, offset=0, type=109, symbol_index=1, addend=0)]
        rows = [(0, bytes.fromhex("c8028fa8"), 'lfd f0, "@301"@sda21(r0)')]
        capture.compare_owner(obj, "target", rows, {})
        for operand in ('lfd f0, "other@301"@sda21(r0)', 'lfd f0, "@301"+4@sda21(r0)',
                        'lfd f0, "@301"@ha(r0)'):
            with self.subTest(operand=operand), self.assertRaises(ValueError):
                capture.compare_owner(obj, "target", [(0, rows[0][1], operand)], {})

    def test_function_scope_contiguity_and_offset_are_required(self):
        text = assembly("sibling", 0x80000000) + assembly()
        self.assertEqual(len(capture.instructions(text, "target")), 2)
        for bad in (assembly() + assembly(), assembly().replace("80001004", "80001008"),
                    assembly().replace("4e 80 00 20", "XX XX XX XX")):
            with self.assertRaises(ValueError):
                capture.instructions(bad, "target")
        with self.assertRaisesRegex(ValueError, "offset mismatch"):
            capture.compare_owner(owner(), "target", capture.instructions(assembly(), "target"), {"address": 4})


if __name__ == "__main__":
    unittest.main()
