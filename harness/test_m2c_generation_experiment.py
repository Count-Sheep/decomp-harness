import subprocess
import hashlib
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import m2c_generation_experiment as experiment


class RawGenerationTests(unittest.TestCase):
    def test_string_variant_is_native_and_preserves_raw_output(self):
        row = {"symbol": "test", "assembly": ".text", "context": ""}
        with tempfile.TemporaryDirectory() as directory:
            for enabled in (False, True):
                with patch.object(experiment.subprocess, "run", return_value=
                                  subprocess.CompletedProcess([], 0, 'char *test = "raw";', "")) as run:
                    result = experiment.generate(Path("/m2c"), row, Path(directory) / str(enabled),
                                                 context_decls=True, heuristic_strings=enabled)
                    self.assertEqual("--heuristic-strings" in run.call_args.args[0], enabled)
                    self.assertEqual(result["draft"], 'char *test = "raw";')

    def test_stack_storage_variant_is_native_and_opt_in(self):
        row = {"symbol": "test", "assembly": ".text", "context": ""}
        with tempfile.TemporaryDirectory() as directory:
            for enabled in (False, True):
                with patch.object(experiment.subprocess, "run", return_value=
                                  subprocess.CompletedProcess([], 0, "void test(void);", "")) as run:
                    result = experiment.generate(Path("/m2c"), row, Path(directory) / str(enabled),
                                                 context_decls=True, ppc_stack_storage=enabled)
                    self.assertEqual("--ppc-stack-storage" in run.call_args.args[0], enabled)
                    self.assertEqual(result["draft"], "void test(void);")

    def test_register_variants_are_native_and_validate_register_names(self):
        row = {"symbol": "test", "assembly": ".text", "context": ""}
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.object(experiment.subprocess, "run", return_value=
                              subprocess.CompletedProcess([], 0, "void test(void);", "")) as run:
                experiment.generate(Path("/m2c"), row, folder / "valid", context_decls=True,
                                    reg_vars=("f3", "r31"))
                command = run.call_args.args[0]
                self.assertEqual(command[command.index("--reg-vars") + 1], "f3,r31")
                for invalid in ("f32", "r-1", "f3,--void", "lr"):
                    with self.assertRaisesRegex(ValueError, "Invalid PPC register"):
                        experiment.generate(Path("/m2c"), row, folder / "invalid", context_decls=True,
                                            reg_vars=(invalid,))
                self.assertEqual(run.call_count, 1)

    def test_fused_subtract_variant_is_requested_inside_m2c(self):
        row = {"symbol": "test", "assembly": ".text", "context": ""}
        with tempfile.TemporaryDirectory() as directory:
            for enabled in (False, True):
                with patch.object(experiment.subprocess, "run", return_value=
                                  subprocess.CompletedProcess([], 0, "double test(void);", "")) as run:
                    result = experiment.generate(Path("/m2c"), row, Path(directory) / str(enabled),
                                                 context_decls=True, mwcc_fused_subtract=enabled)
                    self.assertEqual("--mwcc-fused-subtract" in run.call_args.args[0], enabled)
                    self.assertEqual(result["draft"], "double test(void);")

    def test_captured_inputs_fail_before_generation_when_hashes_or_profiles_are_invalid(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            report = root / "build/TEST/report.json"
            report.parent.mkdir(parents=True)
            report.write_text("{}")
            row = {"symbol": "leaf", "categories": [], "evidence": {}, "compiler_profile": {}}
            frozen = {"root": str(root), "version": "TEST", "rows": [row],
                      "capture_kind": "native_m2c_batch_inputs", "target_objects": {}}
            snapshot = root / "snapshot.json"
            snapshot.write_text(json.dumps(frozen))
            with patch.object(experiment, "generate") as generate:
                with self.assertRaisesRegex(ValueError, "Requested symbol"):
                    experiment.run(snapshot, root, root, root / "out", [], 0, 0, symbols=["missing"])
                with patch.object(experiment.m2c_capture_inputs, "verify_snapshot",
                                  side_effect=ValueError("Changed input")):
                    with self.assertRaisesRegex(ValueError, "Changed input"):
                        experiment.run(snapshot, root, root, root / "out", [], 0, 0)
                with patch.object(experiment.m2c_capture_inputs, "verify_snapshot"):
                    with self.assertRaisesRegex(ValueError, "strict compiler selection"):
                        experiment.run(snapshot, root, root, root / "out", [], 0, 0)
                    row["compiler_profile"] = {"strict_selection_required": True, "supported": False}
                    snapshot.write_text(json.dumps(frozen))
                    with self.assertRaisesRegex(ValueError, "Strict compiler profile unavailable"):
                        experiment.run(snapshot, root, root, root / "out", [], 0, 0)
                generate.assert_not_called()
            self.assertFalse((root / "out").exists())

    def test_generation_uses_frozen_inputs_and_native_declaration_flag(self):
        row = {"symbol": "leaf", "assembly": ".fn leaf, global\nblr\n.endfn leaf\n", "context": "int leaf(void);"}
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name) / "candidate"
            with patch.object(experiment.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], 0, "int leaf(void) { return 0; }", "")) as run:
                result = experiment.generate(Path("/m2c"), row, folder, context_decls=True)
            self.assertEqual((folder / "target.s").read_text(), row["assembly"])
            self.assertEqual((folder / "context.c").read_text(), row["context"])
            self.assertEqual((folder / "draft.c").read_text(), result["draft"])
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--globals") + 1], "used-context")
            self.assertIn("--no-unk-inference", command)

    def test_failed_generation_never_becomes_a_compiled_candidate(self):
        row = {"symbol": "leaf", "size": 4, "assembly": "/* 0 */ blr"}
        with tempfile.TemporaryDirectory() as name, patch.object(experiment.attempts, "score") as score:
            result = experiment.measure(Path(name), "TEST", row,
                                        {"returncode": 1, "draft": "/* failed */", "error": "unsupported"}, Path(name))
        score.assert_not_called()
        self.assertFalse(result["outcome"]["compiled"])
        self.assertIn("generation_failure", result["categories"])

    def test_successful_generation_is_scored_without_repairs(self):
        row = {"symbol": "leaf", "size": 4, "assembly": "/* 0 */ blr"}
        outcome = {"compiled": True, "pct": 100, "profile": "test"}
        with tempfile.TemporaryDirectory() as name, patch.object(experiment.attempts, "score", return_value=outcome) as score:
            result = experiment.measure(Path(name), "TEST", row,
                                        {"returncode": 0, "draft": "void leaf(void) {}", "error": ""}, Path(name))
        self.assertFalse(score.call_args.kwargs["apply_repair"])
        self.assertIsNone(result["evidence"]["instruction_exact"])
        self.assertEqual(experiment.summarize([result])["compiled"], 1)

    def test_compiled_object_is_bound_to_its_measurement(self):
        row = {"symbol": "leaf", "size": 4, "assembly": "/* 0 */ blr"}
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            obj = root / "score/test.o"
            obj.parent.mkdir()
            obj.write_bytes(b"compiled object evidence")
            with patch.object(experiment.attempts, "score", return_value={
                "compiled": True, "pct": 100, "profile": "test"}):
                result = experiment.measure(root, "TEST", row,
                    {"returncode": 0, "draft": "void leaf(void) {}", "error": ""}, root)
            self.assertEqual(result["compiled_object_sha256"], hashlib.sha256(obj.read_bytes()).hexdigest())

    def test_readonly_inputs_can_be_identical_on_both_sides(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            report = root / "build/TEST/report.json"
            report.parent.mkdir(parents=True)
            report.write_text("{}")
            owner = report.parent / "obj/unit.o"
            owner.parent.mkdir()
            owner.write_bytes(b"owner")
            row = {"symbol": "leaf", "size": 4, "assembly": ".text\n", "context": "fresh header",
                   "original_context": "original header",
                   "compiler_profile": {"strict_selection_required": True},
                   "unit": "unit", "categories": [], "evidence": {"instruction_exact": False}}
            frozen = root / "snapshot.json"
            frozen.write_text(json.dumps({"root": str(root), "version": "TEST", "rows": [row, {**row, "symbol": "ignored"}],
                                          "target_objects": {}, "capture_kind": "native_m2c_batch_inputs"}))
            measured = {"symbol": "leaf", "size": 4, "outcome": {"compiled": True, "pct": 100},
                        "categories": [], "compiler_messages": [],
                        "evidence": {"instruction_exact": True, "status": "checked"}}
            with patch.object(experiment, "validate_owners"), \
                 patch.object(experiment.m2c_capture_inputs, "verify_snapshot") as verify, \
                 patch.object(experiment.attempts, "compiler_choices", return_value=[]), \
                 patch.object(experiment, "tool_fingerprint", return_value="tool"), \
                 patch.object(experiment.m2c_readonly_data, "capture", return_value=(b"enriched", {})) as capture, \
                 patch.object(experiment, "generate", return_value={}) as generate, \
                 patch.object(experiment, "measure", return_value=measured):
                experiment.run(frozen, root, root, root / "out", [], 0, 0,
                               readonly_data=True, baseline_readonly_data=True,
                               baseline_context_declarations=True, baseline_original_context=True,
                               candidate_mwcc_fused_subtract=True, symbols=["leaf"])
            self.assertEqual(capture.call_count, 2)
            self.assertEqual([call.kwargs["mwcc_fused_subtract"] for call in generate.call_args_list], [False, True])
            self.assertEqual([call.args[1]["context"] for call in generate.call_args_list],
                             ["original header", "fresh header"])
            self.assertGreaterEqual(verify.call_count, 3)
            for call in generate.call_args_list:
                self.assertEqual(call.args[1]["assembly"], "enriched")
                self.assertTrue(call.kwargs["context_decls"])
            manifest = json.loads((root / "out/manifest.json").read_text())
            self.assertTrue(manifest["baseline_readonly_data"])
            self.assertTrue(manifest["baseline_original_context"])
            self.assertEqual(manifest["symbols"], ["leaf"])


if __name__ == "__main__":
    unittest.main()
