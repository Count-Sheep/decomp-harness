from types import SimpleNamespace as Record
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from m2c_object_evidence import audit, byte_feedback, float_pool_identity


class ObjectEvidenceTests(unittest.TestCase):
    def test_byte_feedback_locates_real_word_changes_and_length_mismatches(self):
        target = bytes.fromhex("c8200000fc0100bcfc010028")
        candidate = bytes.fromhex("c8200000fc0100b8fc01002a")
        feedback = byte_feedback(target, candidate)
        self.assertEqual(feedback["different_words"], 2)
        self.assertEqual(feedback["first_difference"], {"offset": 4, "instruction_index": 1,
                         "target_word": "fc0100bc", "candidate_word": "fc0100b8"})
        self.assertIsNone(byte_feedback(target, target)["first_difference"])
        self.assertEqual(byte_feedback(target, target[:4])["first_difference"]["candidate_word"], "")
        self.assertEqual(byte_feedback(target[:4], target)["first_difference"]["target_word"], "")
        self.assertEqual(byte_feedback(b"", b"x")["first_difference"]["candidate_word"], "78")

    def example(self, name="@1", payload=b"\x00\x00\x00\x00", opcode=48):
        text = Record(data=(opcode << 26).to_bytes(4, "big"), flags=6)
        section = Record(name=".sdata2", data=payload, flags=2, type=1)
        symbol = Record(name=name, bind=0, type=1, other=0, size=4, value=0, shndx=1)
        relocation = Record(type=109, addend=0, offset=0, symbol_index=0, target_section_index=0)
        return (Record(symbols=[symbol], sections=[text, section], relocations=[relocation]),
                Record(section=text, start=0, end=4), relocation)

    def test_renumbered_constant_requires_identical_payload(self):
        one = float_pool_identity(*self.example("@1"))
        self.assertEqual(one, float_pool_identity(*self.example("@999")))
        self.assertNotEqual(one, float_pool_identity(*self.example("@999", bytes.fromhex("80000000"))))

    def test_named_global_writable_and_non_float_references_keep_identity(self):
        self.assertIsNone(float_pool_identity(*self.example("named")))
        self.assertIsNone(float_pool_identity(*self.example(opcode=32)))
        for attribute, value in (("bind", 1), ("type", 0), ("size", 8), ("shndx", 0)):
            owner, target, relocation = self.example()
            setattr(owner.symbols[0], attribute, value)
            self.assertIsNone(float_pool_identity(owner, target, relocation))
        owner, target, relocation = self.example()
        owner.sections[1].flags = 3
        self.assertIsNone(float_pool_identity(owner, target, relocation))

    def test_addends_missing_bounds_and_embedded_relocations_are_rejected(self):
        for field, value in (("addend", 4), ("type", 1), ("offset", 2)):
            owner, target, relocation = self.example()
            setattr(relocation, field, value)
            self.assertIsNone(float_pool_identity(owner, target, relocation))
        owner, target, relocation = self.example()
        owner.relocations.append(Record(target_section_index=1, offset=0))
        self.assertIsNone(float_pool_identity(owner, target, relocation))

    def test_mwcc_candidate_sdata2_requires_only_direct_target_float_loads(self):
        owner, target, relocation = self.example()
        owner.sections[1].flags = 3
        self.assertIsNone(float_pool_identity(owner, target, relocation))
        self.assertIsNotNone(float_pool_identity(owner, target, relocation, candidate=True))
        owner.sections[1].name = ".data"
        self.assertIsNone(float_pool_identity(owner, target, relocation, candidate=True))
        owner.sections[1].name = ".sdata2"
        owner.relocations.append(Record(target_section_index=0, symbol_index=0, type=1, offset=0, addend=0))
        self.assertIsNone(float_pool_identity(owner, target, relocation, candidate=True))

    def test_audit_requires_completed_snapshot_and_unchanged_bound_candidate(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            target, snapshot = root / "target.o", root / "snapshot.json"
            target.write_bytes(b"original target")
            snapshot.write_text(json.dumps({"root": str(root), "version": "TEST",
                "rows": [{"symbol": "fn"}],
                "target_objects": {str(target): hashlib.sha256(target.read_bytes()).hexdigest()}}))
            experiment = root / "experiment"
            obj = experiment / "00000/candidate/score/profile.o"
            obj.parent.mkdir(parents=True)
            obj.write_bytes(b"original candidate")
            manifest = {"snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest()}
            manifest_path = experiment / "manifest.json"
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "completed experiment"):
                audit(snapshot, experiment, root / "audit.json")
            manifest["finished_at"] = "2026-10-08T06:00:00Z"
            manifest_path.write_text(json.dumps(manifest))
            data = {"results": [{"symbol": "fn", "baseline": {"outcome": {}}, "candidate": {
                "compiled_object_sha256": hashlib.sha256(obj.read_bytes()).hexdigest(),
                "outcome": {"compiled": True, "pct": 100, "profile": "profile"}}}]}
            results_path = experiment / "results.json"
            results_path.write_text(json.dumps(data))
            with patch("m2c_draft_attempts.target_object", return_value=target), \
                 patch("m2c_object_evidence.compare", return_value={"scratch_object_equal": True}):
                audit(snapshot, experiment, root / "audit.json")
                result = json.loads((root / "audit.json").read_text())["results"][0]["candidate"]
                self.assertTrue(result["bound_to_compile_measurement"])
                data["results"][0]["candidate"]["outcome"]["pct"] = 98.0
                results_path.write_text(json.dumps(data))
                audit(snapshot, experiment, root / "near-default.json")
                self.assertNotIn("candidate", json.loads((root / "near-default.json").read_text())["results"][0])
                audit(snapshot, experiment, root / "near-all.json", all_compiled=True)
                self.assertTrue(json.loads((root / "near-all.json").read_text())["results"][0]
                                ["candidate"]["bound_to_compile_measurement"])
                data["results"][0]["candidate"]["outcome"]["pct"] = 100
                results_path.write_text(json.dumps(data))
                obj.write_bytes(b"changed candidate")
                with self.assertRaisesRegex(ValueError, "changed since measurement"):
                    audit(snapshot, experiment, root / "changed.json")
                del data["results"][0]["candidate"]["compiled_object_sha256"]
                results_path.write_text(json.dumps(data))
                audit(snapshot, experiment, root / "legacy.json")
                legacy = json.loads((root / "legacy.json").read_text())["results"][0]["candidate"]
                self.assertFalse(legacy["bound_to_compile_measurement"])
if __name__ == "__main__":
    unittest.main()
