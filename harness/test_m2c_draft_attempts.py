"""Small guards for the scratch-attempt workflow."""

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import m2c_draft_attempts as attempts


class DraftAttemptTests(unittest.TestCase):
    def test_raw_generator_scoring_never_repairs_or_adds_context(self):
        code = "int f(void) { return Missing(); }\n"
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "score"
            with patch.object(attempts.compile_repair, "repair_draft") as repair, \
                 patch.object(attempts, "compile_context") as context, \
                 patch.object(attempts, "compiler_choices", return_value=[]):
                result = attempts.score(Path(temporary), "TEST", {"symbol": "f"}, code, folder,
                                        apply_repair=False)
            self.assertEqual((folder / "candidate.c").read_text(), attempts.PRELUDE + code)
            self.assertTrue(result["repair"]["disabled"])
            repair.assert_not_called()
            context.assert_not_called()

    def test_scoring_uses_target_project_tools_and_host_compiler_wrapper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "build/TEST/obj").mkdir(parents=True)
            (root / "build/TEST/obj/unit.o").write_bytes(b"target")
            calls = []

            def fake_run(command, **_kwargs):
                calls.append(command)
                compiled = len(calls) == 1  # compile succeeds; objdiff then stops the attempt
                return attempts.subprocess.CompletedProcess(command, 0 if compiled else 1, "", "stop")

            choice = [("1.3.2", ("-O4,p",), {"confidence": "test"})]
            for wrapper in ([], ["wrapper-tool"]):
                calls.clear()
                with patch.object(attempts, "compiler_choices", return_value=choice), \
                     patch.object(attempts, "compiler_wrapper", return_value=wrapper), \
                     patch.object(attempts.subprocess, "run", side_effect=fake_run):
                    attempts.score(root, "TEST", {"symbol": "f", "unit": "unit"},
                                   "int f(void) { return 0; }\n", root / f"score{len(wrapper)}",
                                   apply_repair=False)
                compiler = str(root / "build/compilers/GC/1.3.2/mwcceppc.exe")
                self.assertEqual(calls[0][:len(wrapper) + 1], [*wrapper, compiler])
                self.assertEqual(calls[1][0], str(attempts.build_tool(root, "objdiff-cli")))
                self.assertTrue(Path(calls[1][0]).is_relative_to(root))

    def test_profiles_cover_sdk_game_and_runtime_compilers(self):
        self.assertEqual(attempts.PROFILES,
                         ("1.2.5n", "1.3", "1.3.2", "2.0", "2.5", "2.6", "3.0a5.2"))
        self.assertEqual(attempts.PROFILE_FLAGS["2.5"],
                         ("-char", "signed", "-use_lmw_stmw", "on"))
        self.assertEqual(attempts.PROFILE_FLAGS["2.6"],
                         ("-char", "signed", "-use_lmw_stmw", "on"))
        self.assertEqual(attempts.PROFILE_FLAGS["3.0a5.2"],
                         ("-char", "signed", "-use_lmw_stmw", "on", "-enc", "SJIS"))

    def test_resume_keeps_batch_and_attempts_when_model_endpoint_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            support = root / "build/decomp-support/TEST01"
            batch = support / "batches/original"
            artifact = batch / "candidate"
            artifact.mkdir(parents=True)
            (artifact / "draft.c").write_text("int Shared(void) { return 0; }\n")
            (batch / "results.json").write_text(json.dumps({"results": [{"symbol": "Shared", "status": "ok",
                "size": 8, "address": 0x80001000, "artifacts": str(artifact), "unit": "main/test"}]}))
            newer = support / "batches/newer"
            newer.mkdir()
            (newer / "results.json").write_text(json.dumps({"results": []}))
            state_path = support / "attempts/state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"version": "TEST01", "batch": "original", "model": "old-model",
                "host": "http://127.0.0.1:18081/v1", "items": {"Shared": {"symbol": "Shared",
                    "attempts": [{"index": index} for index in range(9)], "best_pct": 0}}}))
            args = Namespace(root=root, version="TEST01", batch=None, limit=0, model="new-model",
                             host="http://127.0.0.1:18080/v1", max_attempts=8, loop=False, pause=0, timeout=1)
            with patch.object(attempts, "build_check", return_value={"status": "exact"}):
                state = attempts.run(args)
            self.assertEqual(state["batch"], "original")
            self.assertEqual(state["model"], "new-model")
            self.assertEqual(state["host"], "http://127.0.0.1:18080/v1")
            self.assertEqual(state["model_history"][0]["model"], "old-model")
            self.assertEqual(len(state["items"]["Shared"]["attempts"]), 9)

    def test_response_must_define_assigned_function_without_asm(self):
        self.assertEqual(attempts.clean_code("```c\nint fn_1(void) { return 1; }\n```", "fn_1"),
                         "int fn_1(void) { return 1; }\n")
        for text in ("int other(void) { return 1; }", "asm volatile(\"nop\"); int fn_1(void) { return 1; }",
                     "int fn_1(void) { return M2C_FIELD(x); }"):
            with self.assertRaises(ValueError):
                attempts.clean_code(text, "fn_1")

    def test_draft_rows_include_failed_m2c_as_model_fallbacks(self):
        with tempfile.TemporaryDirectory() as temporary:
            batch = Path(temporary)
            drafts = []
            for symbol, size, errors in (("big", 64, 0), ("small", 24, 0),
                                         ("unclear", 20, 2)):
                folder = batch / symbol
                folder.mkdir()
                (folder / "draft.c").write_text("int f(void) { return 0; }")
                drafts.append({"symbol": symbol, "status": "ok", "size": size,
                               "address": size, "artifacts": str(folder),
                               "diagnostics": {"unknown_fields": errors}})
            fallback = batch / "fallback"
            fallback.mkdir()
            (fallback / "draft.c").write_text("/* m2c failed; use target assembly */\n")
            drafts.append({"symbol": "fallback", "status": "decompile_error", "size": 32,
                           "address": 0x80002000, "artifacts": str(fallback), "diagnostics": {}})
            (batch / "results.json").write_text(json.dumps({"results": drafts}))
            self.assertEqual([row["symbol"] for row in attempts.draft_rows(batch)],
                             ["small", "fallback", "big", "unclear"])

    def test_draft_rows_reads_an_in_progress_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            batch = Path(temporary)
            artifact = batch / "draft"
            artifact.mkdir()
            (artifact / "draft.c").write_text("int partial(void) { return 0; }\n")
            results = batch / "results"
            results.mkdir()
            (results / "partial.json").write_text(json.dumps({
                "symbol": "partial", "status": "ok", "size": 8, "address": 0x80001000,
                "artifacts": str(artifact), "diagnostics": {},
            }))

            self.assertEqual([row["symbol"] for row in attempts.draft_rows(batch)], ["partial"])

    def test_instruction_diff_ignores_local_label_addresses(self):
        target = ".L_80001234:\n/* 80001230 00000000  40 82 00 04 */ bne .L_80001234\n"
        compiled = ".L_00000004:\n/* 00000000 00000000  40 82 00 04 */ bne .L_00000004\n"
        self.assertEqual(attempts.instructions(target), attempts.instructions(compiled))

    def test_compiler_choices_use_inferred_version_and_exact_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            compiler = root / "build/compilers/GC/1.3.2/mwcceppc.exe"
            compiler.parent.mkdir(parents=True)
            compiler.touch()
            row = {"compiler_profile": {"profiles": [{"mw_version": "GC/1.3.2",
                    "flags": ["-O4,p", "-fp_contract", "on", "-sdata", "0"],
                    "confidence": "high"}]}}
            choices = attempts.compiler_choices(root, row)
        self.assertEqual(choices[0][0], "1.3.2")
        self.assertEqual(choices[0][1], ("-O4,p", "-fp_contract", "on", "-sdata", "0"))

    def test_compile_context_keeps_return_evidence_for_prototype_specialization(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary)
            (artifact / "knowledge.json").write_text(json.dumps({"context": {"context":
                "typedef int s32;\nextern u8 lbl_80100000[];\nextern s32 Weak();\n"
                "extern void Strong(s32 value);\n"}}))
            header = attempts.compile_context({"artifacts": str(artifact)})
        self.assertIn("extern u8 lbl_80100000[];", header)
        self.assertIn("extern void Strong(s32 value);", header)
        self.assertNotIn("typedef", header)
        self.assertIn("extern s32 Weak();", header)

    def test_strict_profile_has_no_generic_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            compiler = root / "build/compilers/GC/1.3.2/mwcceppc.exe"
            compiler.parent.mkdir(parents=True)
            compiler.write_bytes(b"compiler")
            selected = {"mw_version": "GC/1.3.2", "flags": ["-O4,p", "-sdata", "0"],
                        "binary": str(compiler),
                        "binary_sha256": attempts.hashlib.sha256(b"compiler").hexdigest()}
            profile = {"selected": selected, "profiles": [selected], "supported": True,
                       "strict_selection_required": True}
            row = {"symbol": "test", "compiler_profile": profile}
            self.assertEqual(attempts.compiler_choices(root, row),
                             [("1.3.2", ("-O4,p", "-sdata", "0"), selected)])
            compiler.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "binary changed"):
                attempts.compiler_choices(root, row)
            compiler.write_bytes(b"compiler")
            profile["profiles"] = []
            with self.assertRaisesRegex(ValueError, "profile unavailable"):
                attempts.compiler_choices(root, row)
            profile["profiles"] = [selected]
            profile["supported"] = False
            with self.assertRaisesRegex(ValueError, "profile unavailable"):
                attempts.compiler_choices(root, row)

    def test_20p1_is_strict_only_and_uses_recorded_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            compiler = root / "build/compilers/GC/2.0p1/mwcceppc.exe"
            compiler.parent.mkdir(parents=True)
            compiler.write_bytes(b"compiler")
            selected = {"mw_version": "GC/2.0p1", "flags": ["-O4,s", "-sdata", "8"],
                        "binary": str(compiler),
                        "binary_sha256": attempts.hashlib.sha256(b"compiler").hexdigest()}
            profile = {"selected": selected, "profiles": [selected], "supported": True,
                       "strict_selection_required": True}
            self.assertEqual(attempts.compiler_choices(root, {"compiler_profile": profile}),
                             [("2.0p1", ("-O4,s", "-sdata", "8"), selected)])
            self.assertNotIn("2.0p1", [p[0] for p in attempts.compiler_choices(root, {})])
            self.assertNotIn("2.0p1", [p[0] for p in attempts.compiler_choices(
                root, {"compiler_profile": {"profiles": [selected]}})])
            compiler.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "binary changed"):
                attempts.compiler_choices(root, {"compiler_profile": profile})


if __name__ == "__main__":
    unittest.main()
