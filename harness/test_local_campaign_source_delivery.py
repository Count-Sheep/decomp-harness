import json
from pathlib import Path
import tempfile
import unittest

import local_campaign_source_delivery as delivery


class DeliveryTests(unittest.TestCase):
    def test_combined_patch_keeps_unassigned_source_and_rejects_duplicate_targets(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = root / "src/owner.c"
            path.parent.mkdir()
            source = "int global = 99;\nint f(void) { return 1; }\nint g(void) { return 2; }\nint other(void) { return 77; }\n"
            path.write_text(source, encoding="utf-8", newline="\n")
            rows = [{"owner": "src/owner.c", "symbol": symbol, "signature": f"int {symbol}(void)",
                     "candidate": f"int {symbol}(void) {{ return {value}; }}"} for symbol, value in (("f", 3), ("g", 4))]
            before, after = delivery.combine_sources(root, rows)
            self.assertEqual(before["src/owner.c"], source)
            self.assertEqual(after["src/owner.c"], source.replace("return 1", "return 3").replace("return 2", "return 4"))
            with self.assertRaisesRegex(ValueError, "duplicate"):
                delivery.combine_sources(root, rows + rows[:1])

    def test_delivery_rejects_terminal_failure_or_unsupported_ready_claim(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = {"root": "root"}
            (root / "configuration.json").write_text(json.dumps(config), encoding="utf-8", newline="\n")
            summary = {"finished": True, "phase": "failed", "environment_unchanged": True,
                       "configuration_sha256": delivery.knowledge.digest(delivery.knowledge.encoded(config))}
            (root / "results.json").write_text(json.dumps({"summary": summary, "results": []}), encoding="utf-8", newline="\n")
            with self.assertRaisesRegex(ValueError, "incomplete"):
                delivery.selected_packages(root)
            summary["phase"] = "complete"
            (root / "results.json").write_text(json.dumps({"summary": summary, "results": [{"machine_verified_C": True}]}), encoding="utf-8", newline="\n")
            with self.assertRaisesRegex(ValueError, "without all actual gates"):
                delivery.selected_packages(root)

    def test_delivery_binds_source_patch_applied_owner_object_and_ast(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            folder = root / "packages/id"
            (folder / "applied/src").mkdir(parents=True)
            payloads = {"function.c": b"int f(void) { return 1; }\n", "owner.patch": b"patch",
                        "applied/src/f.c": b"int f(void) { return 1; }\n", "object.o": b"object", "analysis.c": b"analysis"}
            for filename, content in payloads.items():
                (folder / filename).write_bytes(content)
            ast = {"kind": "FunctionDecl", "name": "f"}
            (folder / "target.ast.json").write_text(json.dumps(ast), encoding="utf-8", newline="\n")
            digest = delivery.knowledge.digest
            row = {"machine_verified_C": True, "package_id": "id", "owner": "src/f.c", "symbol": "f",
                   "candidate_sha256": digest(payloads["function.c"]), "ci_quality": {"passed": True},
                   "linked_checksum": True, "patch_application": {"passed": True,
                       "patch_sha256": digest(payloads["owner.patch"]), "owners": {"src/f.c": {"after_sha256": digest(payloads["applied/src/f.c"])}}},
                   "source_diagnostics": {"passed": True, "analysis_sha256": digest(payloads["analysis.c"]),
                       "target_ast_sha256": digest(delivery.knowledge.encoded(ast))},
                   "object_evidence": {**{axis: True for axis in (*delivery.package.OBJECT_AXES, "policy_screen_passed")},
                       "objects": {"candidate": {"sha256": digest(payloads["object.o"])}}},
                   "measurement": {"compile_output": str(folder / "object.o")}}
            config = {"root": "root"}
            (root / "configuration.json").write_text(json.dumps(config), encoding="utf-8", newline="\n")
            summary = {"finished": True, "phase": "complete", "environment_unchanged": True,
                       "configuration_sha256": digest(delivery.knowledge.encoded(config))}
            (folder / "package.json").write_text(json.dumps(row), encoding="utf-8", newline="\n")
            (root / "results.json").write_text(json.dumps({"summary": summary, "results": [row]}), encoding="utf-8", newline="\n")
            rows, _, _ = delivery.selected_packages(root)
            self.assertEqual(rows[0]["candidate"], payloads["function.c"].decode())
            (folder / "function.c").write_text("changed", encoding="utf-8", newline="\n")
            with self.assertRaisesRegex(ValueError, "changed"):
                delivery.selected_packages(root)


if __name__ == "__main__":
    unittest.main()
