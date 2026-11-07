import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import m2c_unit_pipeline as pipeline
from m2c_capture_inputs import Inputs
from m2c_unit_source import m2c_available

M2C = m2c_available()


class NativeRegenerationTests(unittest.TestCase):
    def regenerate(self, root, *, code=0, mutation=None, fingerprints=None):
        tool = root / "engine"
        tool.mkdir()
        (tool / "m2c.py").write_text("# engine")
        original = root / "draft.c"
        original.write_text("retained draft")
        (root / "target.s").write_text("assembly")
        inputs = Inputs()
        inputs.read(original)
        folder = root / "generated"

        def generate(engine, row, output, **options):
            self.assertEqual(engine, tool)
            self.assertEqual(row, {"symbol": "test", "assembly": "assembly", "context": "types"})
            self.assertEqual(options, {"context_decls": True, "infer_unknowns": True,
                                       "deterministic_vars": True})
            output.mkdir()
            for name, value in {"target.s": row["assembly"], "context.c": row["context"],
                                "draft.c": "void test(void) {}", "generation.json": "{}"}.items():
                (output / name).write_text(value)
            if mutation:
                mutation(root)
            return {"draft": "void test(void) {}", "returncode": code,
                    "command": ["m2c"], "error": "failed"}

        with patch("local_campaign_decompile.tool", return_value=tool / "m2c.py"), \
             patch("m2c_generation_experiment.generate", side_effect=generate), \
             patch("m2c_generation_experiment.tool_fingerprint", side_effect=fingerprints or ["abc", "abc"]):
            raw, evidence = pipeline.regenerate_draft("test", original, "types", folder, inputs)
        self.assertEqual(original.read_text(), "retained draft")
        self.assertEqual(raw, b"void test(void) {}")
        self.assertEqual(evidence["tool_fingerprint"], "abc")
        return inputs

    def test_binds_engine_and_generated_evidence_without_editing_retained_draft(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = self.regenerate(root)
            for name in ("engine/m2c.py", "target.s", "generated/draft.c", "generated/generation.json",
                         "generated/target.s", "generated/context.c"):
                self.assertIn(str(root / name), inputs.files)
            (root / "generated/draft.c").write_text("changed")
            with self.assertRaisesRegex(ValueError, "Input changed"):
                inputs.verify()

    def test_failure_is_not_a_draft(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "Native regeneration failed"):
                self.regenerate(Path(temporary), code=1)

    def test_engine_mutation_invalidates_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "Input changed"):
                self.regenerate(Path(temporary), mutation=lambda root:
                                (root / "engine/m2c.py").write_text("changed"))

    def test_engine_file_set_change_invalidates_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "Native generator changed"):
                self.regenerate(Path(temporary), fingerprints=["before", "after"])

    def test_output_mismatch_invalidates_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "differs from recorded output"):
                self.regenerate(Path(temporary), mutation=lambda root:
                                (root / "generated/draft.c").write_text("different"))


class UnitPipelineTests(unittest.TestCase):
    def test_link_context_defaults_to_native_and_allows_explicit_retail(self):
        for options, expected in (({}, "native"), ({"link_context": "retail-reference"}, "retail-reference")):
            with self.subTest(context=expected), \
                 patch.object(pipeline.m2c_unit_link, "run", return_value={"link_verified": False}) as link:
                pipeline.advance_verified_unit(Path("/project"), "TEST", Path("/run"), **options)
                link.assert_called_once_with(Path("/project"), Path("/run/compile"), Path("/run/link"),
                                             "build/TEST/main.elf", "config/TEST/build.sha1", mode=expected)

    def test_invalid_link_context_never_runs_linker(self):
        with patch.object(pipeline.m2c_unit_link, "run") as link:
            with self.assertRaises(ValueError):
                pipeline.advance_verified_unit(Path("/project"), "TEST", Path("/run"), link_context="unknown")
            link.assert_not_called()

    def test_link_failure_never_creates_proposal_or_native_build(self):
        with patch.object(pipeline.m2c_unit_link, "run", return_value={"link_verified": False}), \
             patch("m2c_unit_proposal.run") as propose, patch("m2c_unit_project_trial.run") as native:
            result = pipeline.advance_verified_unit(Path("/project"), "TEST", Path("/run"), verify_project=True)
        self.assertEqual(result["state"], "link_trial_complete")
        propose.assert_not_called()
        native.assert_not_called()

    def test_successful_link_can_advance_through_proposal_and_native_build(self):
        calls = []
        def link(*args, **kwargs):
            calls.append("link")
            return {"link_verified": True}
        def propose(*args, **kwargs):
            calls.append("proposal")
            return {}
        def native(*args, **kwargs):
            calls.append("native")
            return {"proposal_build_verified": True}
        with patch.object(pipeline.m2c_unit_link, "run", side_effect=link), \
             patch("m2c_unit_proposal.run", side_effect=propose) as proposal, \
             patch("m2c_unit_project_trial.run", side_effect=native):
            result = pipeline.advance_verified_unit(Path("/project"), "TEST", Path("/run"),
                verify_project=True, proposal_source="src/unit.c", configuration={"configure.py": Path("/config")})
        self.assertEqual(calls, ["link", "proposal", "native"])
        proposal.assert_called_once_with(Path("/project"), Path("/run/link/result.json"), Path("/run/proposal"),
                                         "src/unit.c", {"configure.py": Path("/config")})
        self.assertTrue(result["project_trial_passed"])
        self.assertTrue(result["link_verified"])
        self.assertNotIn("ready_to_merge", result)

    def test_proposal_failure_stops_before_native_build(self):
        with patch.object(pipeline.m2c_unit_link, "run", return_value={"link_verified": True}), \
             patch("m2c_unit_proposal.run", side_effect=ValueError("stale")), \
             patch("m2c_unit_project_trial.run") as native:
            with self.assertRaisesRegex(ValueError, "stale"):
                pipeline.advance_verified_unit(Path("/project"), "TEST", Path("/run"), verify_project=True)
            native.assert_not_called()

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_source_fills_missing_draft_but_still_requires_object_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "objdiff.json").write_text('{}')
            target = root / "build/TEST/target.o"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"object")
            source = root / "existing.c"
            source.write_text("int existing(void) { return 1; }")
            owner = {"unit": "main/test", "project_root": str(root), "target_path": "build/TEST/target.o",
                     "target_sha256": hashlib.sha256(b"object").hexdigest(), "objdiff_sha256": "unused",
                     "sections": [{"name": ".text", "definitions": [{"name": "existing", "type": 2,
                         "offset": 0}], "unnamed_ranges": [], "relocations": []}]}

            def compile_run(project, unit, assembled, output, provenance):
                self.assertIn(str(source), provenance.files)
                self.assertIn("return 1;", assembled.read_text())
                output.mkdir()
                (output / "result.json").write_text('{}')
                return {"compiled": True, "object_verified": False, "state": "draft", "inputs": provenance.files}

            with patch.object(pipeline, "capture", return_value=owner), \
                 patch("permuter.owner_extract.read_elf", return_value=None), \
                 patch.object(pipeline.m2c_unit_compile, "run", side_effect=compile_run) as compiler, \
                 patch.object(pipeline.m2c_unit_link, "run") as linker:
                result = pipeline.run(root, "main/test", [], root / "run", link=True, unit_source=source)
                compiler.assert_called_once()
                linker.assert_not_called()
                self.assertEqual(result["source_functions"], ["existing"])
                self.assertEqual(result["missing_implementations"], [])
                self.assertFalse(result["object_verified"])
                self.assertFalse(result["ready_to_merge"])
                result = pipeline.run(root, "main/test", [], root / "source-less", link=True,
                                      function_sources={"existing": source})
                self.assertTrue(result["compiled"])
                self.assertEqual(result["missing_implementations"], [])
                self.assertFalse(result["object_verified"])
                source.write_text("int outside(void) { return 2; }")
                compiler.reset_mock()
                result = pipeline.run(root, "main/test", [], root / "outside", unit_source=source)
                self.assertIn("outside target owner", result["error"])
                compiler.assert_not_called()

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_hash_bound_function_source_overrides_current_owner_body(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "objdiff.json").write_text('{}')
            target = root / "build/TEST/target.o"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"object")
            source = root / "existing.c"
            source.write_text("int existing(void) { return 1; }")
            replacement = root / "winner.c"
            replacement.write_text("int existing(void) { return 2; }")
            owner = {"unit": "main/test", "project_root": str(root), "target_path": "build/TEST/target.o",
                     "target_sha256": hashlib.sha256(b"object").hexdigest(), "objdiff_sha256": "unused",
                     "sections": [{"name": ".text", "definitions": [{"name": "existing", "type": 2,
                         "offset": 0}], "unnamed_ranges": [], "relocations": []}]}

            def compile_run(project, unit, assembled, output, provenance):
                self.assertIn("return 2;", assembled.read_text())
                self.assertIn(str(replacement), provenance.files)
                output.mkdir()
                (output / "result.json").write_text('{}')
                return {"compiled": True, "object_verified": False, "state": "draft", "inputs": provenance.files}

            with patch.object(pipeline, "capture", return_value=owner), \
                 patch("permuter.owner_extract.read_elf", return_value=None), \
                 patch.object(pipeline.m2c_unit_compile, "run", side_effect=compile_run):
                result = pipeline.run(root, "main/test", [], root / "run", unit_source=source,
                                      function_sources={"existing": replacement})
            self.assertEqual(result["function_overrides"]["existing"]["path"], str(replacement))
            self.assertFalse(result["object_verified"])

    @unittest.skipUnless(M2C, "m2c checkout is unavailable (configure tools.m2c or DECOMP_M2C)")
    def test_hash_bound_static_inline_helper_override_is_assembled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "objdiff.json").write_text('{}')
            target = root / "build/TEST/target.o"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"object")
            source = root / "existing.c"
            source.write_text("static inline int helper(void) { return 1; }\n"
                              "int existing(void) { return helper(); }\n")
            replacement = root / "helper.c"
            replacement.write_text("static inline int helper(void) { return 2; }\n")
            owner = {"unit": "main/test", "project_root": str(root), "target_path": "build/TEST/target.o",
                     "target_sha256": hashlib.sha256(b"object").hexdigest(), "objdiff_sha256": "unused",
                     "sections": [{"name": ".text", "definitions": [{"name": "existing", "type": 2,
                         "offset": 0}], "unnamed_ranges": [], "relocations": []}]}

            def compile_run(project, unit, assembled, output, provenance):
                self.assertIn("return 2;", assembled.read_text())
                self.assertIn(str(replacement), provenance.files)
                output.mkdir()
                (output / "result.json").write_text('{}')
                return {"compiled": True, "object_verified": False, "state": "draft",
                        "inputs": provenance.files}

            with patch.object(pipeline, "capture", return_value=owner), \
                 patch("permuter.owner_extract.read_elf", return_value=None), \
                 patch.object(pipeline.m2c_unit_compile, "run", side_effect=compile_run):
                result = pipeline.run(root, "main/test", [], root / "run", unit_source=source,
                                      helper_sources={"helper": replacement})
            self.assertEqual(result["helper_overrides"]["helper"]["path"], str(replacement))
            self.assertTrue(result["compiled"])
            self.assertFalse(result["object_verified"])

    def test_missing_drafts_never_reach_compiler_or_linker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "objdiff.json").write_text('{}')
            target = root / "build/TEST/target.o"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"object")
            batch = root / "batch.json"
            batch.write_text('{"results": []}')
            owner = {"unit": "main/test", "project_root": str(root), "target_path": "build/TEST/target.o",
                     "target_sha256": hashlib.sha256(b"object").hexdigest(), "objdiff_sha256": "unused",
                     "sections": [{"name": ".text", "definitions": [{"name": "missing", "type": 2,
                         "offset": 0}], "unnamed_ranges": [], "relocations": []}]}
            with patch.object(pipeline, "capture", return_value=owner), \
                 patch.object(pipeline.m2c_unit_compile, "run") as compile_run, \
                 patch.object(pipeline.m2c_unit_link, "run") as link_run:
                result = pipeline.run(root, "main/test", [batch], root / "run", link=True)
            compile_run.assert_not_called()
            link_run.assert_not_called()
            self.assertIn("Missing function drafts", result["error"])
            self.assertFalse(result["object_verified"])
            self.assertTrue((root / "run/result.json").is_file())

    def test_context_conflicts_and_capture_changes_stop_before_compilation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "objdiff.json").write_text('{}')
            target = root / "build/TEST/target.o"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"object")
            owner = {"unit": "main/test", "project_root": str(root), "target_path": "build/TEST/target.o",
                     "target_sha256": hashlib.sha256(b"object").hexdigest(), "objdiff_sha256": "unused",
                     "sections": [{"name": ".text", "definitions": [{"name": "f", "type": 2,
                         "offset": 0}], "unnamed_ranges": [], "relocations": []}]}
            rows = []
            for index, context in enumerate(("extern int value;", "extern short value;")):
                artifact = root / str(index)
                artifact.mkdir()
                (artifact / "draft.c").write_text("int f(void) { return value; }")
                (artifact / "context.c").write_text(context)
                rows.append({"symbol": "f", "unit": "test", "status": "ok", "artifacts": str(artifact)})
            batch = root / "batch.json"
            batch.write_text(json.dumps({"results": rows}))
            with patch.object(pipeline, "capture", return_value=owner), \
                 patch.object(pipeline.m2c_unit_compile, "run") as compiler, \
                 patch.object(pipeline.m2c_unit_link, "run") as linker:
                result = pipeline.run(root, "main/test", [batch], root / "conflict", link=True)
                self.assertIn("Ambiguous drafts for f", result["error"])
                batch.write_text(json.dumps({"results": rows[:1]}))
                track = pipeline.track_drafts

                def changed_context(*args):
                    captured = track(*args)
                    (root / "0/context.c").write_text("extern unsigned int value;")
                    return captured

                with patch.object(pipeline, "track_drafts", side_effect=changed_context):
                    result = pipeline.run(root, "main/test", [batch], root / "changed", link=True)
                self.assertIn("Draft context changed during selection", result["error"])
                compiler.assert_not_called()
                linker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
