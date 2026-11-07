import json
from pathlib import Path
import shutil
import tempfile
import unittest

import local_campaign_source_package as package


class PackageTests(unittest.TestCase):
    def test_replace_preserves_everything_outside_assigned_definition_and_crlf(self):
        original = "/* before */\r\nint f(int x) { return x; }\r\nint sibling(void) { return 99; }\r\n"
        candidate = "int f(int x)\n{\n    return x + 1;\n}\n"
        actual = package.replace_function(original, candidate, "f", "int f(int x)")
        self.assertTrue(actual.startswith("/* before */\r\nint f"))
        self.assertTrue(actual.endswith("\r\nint sibling(void) { return 99; }\r\n"))
        self.assertNotIn("\n", actual.replace("\r\n", ""))

    def test_source_contract_rejects_helpers_signature_changes_and_extra_definitions(self):
        original = "int f(int x) { return x; }\n"
        for candidate in ("int f(int x) { return M2C_UNK(x); }", "int f(void) { return 1; }",
                          "int f(int x) { return x; } int g(void) { return 1; }",
                          "#define X 1\nint f(int x) { return X; }"):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                package.replace_function(original, candidate, "f", "int f(int x)")

    def test_paths_cannot_export_inc_config_cpp_or_escape(self):
        for value in ("../src/f.c", "src/../f.c", "/src/f.c", "config/f.c", "src/f.cpp", "src/f.inc", "src/f c.c"):
            with self.subTest(path=value), self.assertRaises(ValueError):
                package.owner_path(value)

    def test_real_private_git_apply_round_trips_lf_crlf_and_no_final_newline(self):
        for newline, tail in (("\n", "\n"), ("\r\n", "\r\n"), ("\n", "")):
            with self.subTest(newline=newline, tail=tail), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                before = {"src/f.c": "int f(void) {" + newline + "    return 1;" + newline + "}" + tail}
                after = {"src/f.c": before["src/f.c"].replace("return 1", "return 2")}
                patch = root / "owner.patch"
                patch.write_bytes(package.source_diff(before, after).encode())
                result = package.apply_patch_private(before, after, patch, root / "applied")
                self.assertTrue(result["passed"])
                self.assertEqual((root / "applied/src/f.c").read_bytes(), after["src/f.c"].encode())

    def test_empty_patch_is_explicitly_no_change_and_tampered_patch_is_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            owners = {"src/f.c": "int f(void) { return 1; }\n"}
            path = root / "owner.patch"
            path.write_bytes(b"")
            self.assertTrue(package.apply_patch_private(owners, owners, path, root / "applied")["no_change"])
            path.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "no longer matches"):
                package.apply_patch_private(owners, owners, path, root / "tampered")

    def test_ast_gate_rejects_unprototyped_calls_not_typed_void_calls(self):
        for spelling, failed in (("unsigned int (*)()", True), ("int (*)(int)", False), ("void (*)(void)", False)):
            node = {"kind": "CallExpr", "inner": [{"type": {"qualType": spelling}}]}
            self.assertEqual(bool(package.target_ast_errors(node)), failed)
        self.assertTrue(package.target_ast_errors({"kind": "ForStmt", "inner": [{"kind": "DeclStmt"}]}))
        self.assertFalse(package.target_ast_errors({"kind": "ForStmt", "inner": []}))

    def test_ast_gate_rejects_assembly_hidden_inside_expanded_macros(self):
        for kind in ("GCCAsmStmt", "MSAsmStmt"):
            with self.subTest(kind=kind):
                node = {"kind": "CompoundStmt", "inner": [{"kind": kind}]}
                self.assertIn("not recovered C", package.target_ast_errors(node)[0])

    @unittest.skipUnless(shutil.which("clang"), "Clang diagnostics require a real parser")
    def test_real_clang_rejects_expanded_assembly_even_when_syntax_is_valid(self):
        with tempfile.TemporaryDirectory() as name:
            source = 'int f(int x) { __asm__ volatile ("nop"); return x; }'
            result = package.analyze_source(source, "f", Path(name))
            self.assertEqual(result["returncode"], 0, result)
            self.assertFalse(result["passed"])
            self.assertTrue(any("Assembly in the expanded target" in error for error in result["errors"]))

    @unittest.skipUnless(shutil.which("clang"), "Clang diagnostics require a real parser")
    def test_real_clang_rejects_uninitialized_late_declarations_and_unprototyped_calls(self):
        cases = [("int f(int x) { int value; if (x) value = 1; return value; }", False),
                 ("int f(int x) { x++; int value = x; return value; }", False),
                 ("unsigned int callback; unsigned int f(unsigned int x) { return ((unsigned int (*)()) callback)(); }", False),
                 ("int f(int x) { for (int i = 0; i < x; i++) x--; return x; }", False),
                 ("int f(int x) { int zero[0]; return sizeof(zero) + x; }", False),
                 ("int f(int x) { int value; value = x + 1; return value; }", True)]
        for source, passed in cases:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as name:
                result = package.analyze_source(source, "f", Path(name))
                self.assertEqual(result["passed"], passed, result)

    @unittest.skipUnless(shutil.which("clang"), "Clang diagnostics require a real parser")
    def test_existing_header_extensions_do_not_weaken_the_assigned_body_gate(self):
        with tempfile.TemporaryDirectory() as name:
            source = "struct Header { int value; char padding[0]; }; extern int unused(); int f(int x) { return x; }"
            result = package.analyze_source(source, "f", Path(name))
            self.assertTrue(result["passed"], result)

    def test_candidate_reader_requires_source_bound_to_measured_hash(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            folder = root / "target"
            folder.mkdir()
            source = "int f(void) { return 1; }\n"
            (folder / "draft.c").write_text(source, encoding="utf-8", newline="\n")
            data = {"summary": {"finished": True, "environment_unchanged": True, "workspace_sha256": "workspace"},
                    "results": [{"target_id": "target", "symbol": "f", "source": "src/f.c", "unit": "main/f",
                                 "signature": "int f(void)", "measurement": {"compile_ok": True, "pct": 100,
                                                                            "draft_sha256": package.knowledge.digest(source)}}]}
            (root / "results.json").write_text(json.dumps(data), encoding="utf-8", newline="\n")
            rows, _ = package.candidates([root])
            self.assertEqual(rows[0]["candidate"], source)
            (folder / "draft.c").write_text("changed", encoding="utf-8", newline="\n")
            with self.assertRaisesRegex(ValueError, "candidate changed"):
                package.candidates([root])

    def test_source_policy_hooks_come_from_config_and_are_optional(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "src").mkdir()
            (root / "src/f.c").write_text("int f(void) { return 1; }\n", encoding="utf-8")
            with patch.object(package, "source_policy", return_value={}):
                result = package.ci_quality(root, root, {}, root, "")
                self.assertTrue(result["passed"])
                self.assertFalse(result["policy_configured"])
                self.assertEqual(package.policy_files(root), [])
            scripts = root / "policy"
            scripts.mkdir()
            (scripts / "quality.py").write_text(
                "def added_lines_from_diff(patch):\n    return {'src/f.c': [1]}\n"
                "def scan_source(name, text, lines):\n    return 'BAD' not in text\n", encoding="utf-8")
            (scripts / "wrappers.py").write_text("def scan(path):\n    return []\n", encoding="utf-8")
            policy = {"quality_scan": "policy/quality.py", "wrapper_scan": "policy/wrappers.py",
                      "attest": ["src/f.c"]}
            patch_text = "diff --git a/src/f.c b/src/f.c\n+++ b/src/f.c\n"
            with patch.object(package, "source_policy", return_value=policy):
                good = package.ci_quality(root, root, {"src/f.c": "int f;"}, root, patch_text)
                bad = package.ci_quality(root, root, {"src/f.c": "BAD"}, root, patch_text)
                files = package.policy_files(root)
        self.assertTrue(good["passed"] and good["policy_configured"])
        self.assertEqual(good["full_tree_wrapper_scan_count"], 1)
        self.assertFalse(bad["passed"])
        self.assertEqual([path.name for path in files], ["quality.py", "wrappers.py", "f.c"])

    def test_capsule_requires_explicit_project_checksum_and_unchanged_inputs(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            prior, project = root / "prior", root / "project"
            project.mkdir()
            prior.mkdir()
            prefix = ["tools/wibo", "tools/mwldeppc.exe", "-lcf", "build/main.lcf", "-o", "build/main.elf"]
            plan = {"link_command": prefix + ["@build/main.elf.rsp"], "objects": ["obj/a.o"],
                    "elf": "build/main.elf", "dol": "build/main.dol", "response_file": "build/main.elf.rsp",
                    "conversion_command": ["tools/dtk", "elf2dol", "build/main.elf", "build/main.dol"],
                    "tools": ["tools/wibo", "tools/mwldeppc.exe", "tools/dtk"], "linker_script": "build/main.lcf",
                    "checksum_file": "build.sha1", "expected_sha1": "a" * 40}
            checksum = plan["expected_sha1"] + "  build/main.dol\n"
            (project / "build.sha1").write_text(checksum, encoding="utf-8", newline="\n")
            plan["checksum_file_sha256"] = package.knowledge.digest(checksum)
            plan["files"] = []
            for slot in [*plan["objects"], *plan["tools"], plan["linker_script"]]:
                path = prior / "workspace" / slot
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(slot.encode())
                plan["files"].append({"slot": slot, "sha256": package.knowledge.digest(slot)})
            (prior / "plan.json").write_text(json.dumps(plan), encoding="utf-8", newline="\n")
            (prior / "manifest.json").write_text(json.dumps({"finished": True, "phase": "complete",
                "baseline": {"passed": True}, "project": str(project),
                "plan_sha256": package.knowledge.digest((prior / "plan.json").read_bytes())}), encoding="utf-8", newline="\n")
            package.capture_capsule(prior, root / "copied", project)
            with self.assertRaisesRegex(ValueError, "differs from the recorded"):
                package.capture_capsule(prior, root / "wrong-project", root)
            (prior / "workspace/obj/a.o").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "input changed"):
                package.capture_capsule(prior, root / "tampered", project)


if __name__ == "__main__":
    unittest.main()
