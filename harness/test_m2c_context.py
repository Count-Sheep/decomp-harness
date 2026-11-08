import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import harness_paths
import m2c_context as context


ASM = """.text
.fn target_fn, global
/* 80001000 00000000  94 21 FF E0 */ stwu r1, -0x20(r1)
/* 80001004 00000004  80 63 00 18 */ lwz r3, 0x18(r3)
/* 80001008 00000008  88 8D 80 10 */ lbz r4, global_sda@sda21(r0)
/* 8000100C 0000000C  3C A0 80 20 */ lis r5, global_table@ha
/* 80001010 00000010  48 00 01 01 */ bl KnownCall
/* 80001014 00000014  2C 03 00 00 */ cmpwi r3, 0
/* 80001018 00000018  41 82 00 08 */ beq .L_80001020
/* 8000101C 0000001C  90 83 00 24 */ stw r4, 0x24(r3)
.L_80001020:
/* 80001020 00000020  38 21 00 20 */ addi r1, r1, 0x20
/* 80001024 00000024  4E 80 00 20 */ blr
.endfn target_fn
"""


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class AssemblyFeatureTests(unittest.TestCase):
    def test_reference_extraction_ignores_comment_mentions_and_braces(self):
        source = ('/* target() returns a record. */\ntypedef struct Record { int x; } Record;\n'
                  'void caller(void) { target(); if (1) {} }\n'
                  'int target(void);\nint target(void) { /* } misleading brace */ return 1; }\n')
        signature, snippet = context._source_function(source, "target")
        self.assertEqual(signature, "int target(void)")
        self.assertEqual(snippet, "int target(void) { /* } misleading brace */ return 1; }")

    def test_compiler_profile_reports_final_optimization_override(self):
        profile = context._compiler_profile("mwcc_242_53", "-O4,p -fp_contract on -O4,s -fp_contract off")
        self.assertEqual(profile["settings"]["optimization"], "-O4,s")
        self.assertEqual(profile["settings"]["fp_contract"], "off")

    @staticmethod
    def configured(root, *names):
        projects = []
        for name in names:
            (root / name).mkdir(parents=True, exist_ok=True)
            projects.append(harness_paths.Project(name, (root / name).resolve(), name.upper() + "01",
                                                  versions=(name.upper() + "01",)))
        return projects

    def test_default_projects_include_installed_registry_corpora(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            corpus_root = root / "sdk"
            write_json(corpus_root / "build/REFERENCE/report.json", {"units": []})
            registry = root / "registry.json"
            write_json(registry, {"projects": [
                {"name": "sdk", "status": "active", "root": str(corpus_root),
                 "version": "REFERENCE"},
                {"name": "waiting", "status": "source_only", "root": str(root / "waiting"),
                 "version": "REFERENCE"},
            ]})
            configured = self.configured(root, "target", "ref_a", "ref_b")
            with patch.object(harness_paths, "projects", return_value=configured):
                projects = context.default_reference_projects(registry, target_project="target")
            self.assertEqual([project.name for project in projects], ["ref_a", "ref_b", "sdk"])
            self.assertEqual(projects[0].version, "REF_A01")

    def test_reference_projects_exclude_the_target_project(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configured = self.configured(root, "ref_a", "target", "ref_b")
            with patch.object(harness_paths, "projects", return_value=configured):
                projects = context.default_reference_projects(root / "missing.json", target_project="ref_b")
            self.assertEqual([project.name for project in projects], ["ref_a", "target"])

    def test_target_identity_comes_from_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            configured = self.configured(root, "wii")
            with patch.object(harness_paths, "projects", return_value=configured):
                catalog = context.build_catalog(
                    root / "wii", "WII01", reference_projects=[], cache_path=root / "cache.json"
                )
                self.assertEqual(catalog["target_project"], "wii")
                self.assertEqual(catalog["target_version"], "WII01")
                unconfigured = context.build_catalog(
                    root / "other", "OTHER01", reference_projects=[], cache_path=root / "cache.json"
                )
                self.assertEqual(unconfigured["target_project"], "other")
        profile = context._compiler_profile("mwcc_41_60831", "-O4,p -fp_contract off")
        self.assertEqual(profile["mw_version"], "GC/3.0a5.2")

    def test_build_check_distinguishes_exact_mismatch_and_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(context.build_check(root, "TEST01"), {"status": "not_built"})
            manifest = root / "config/TEST01/build.sha1"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(hashlib.sha1(b"retail").hexdigest() + " main.dol\n")
            dol = root / "build/TEST01/main.dol"
            dol.parent.mkdir(parents=True)
            dol.write_bytes(b"retail")
            self.assertEqual(context.build_check(root, "TEST01")["status"], "exact")
            dol.write_bytes(b"modified")
            self.assertEqual(context.build_check(root, "TEST01")["status"], "mismatch")
            manifest.write_text(hashlib.sha1(b"other").hexdigest() + "  build/TEST01/other.dol\n")
            (root / "build/TEST01/other.dol").write_bytes(b"other")
            self.assertEqual(context.build_check(root, "TEST01")["status"], "exact")

    def test_parse_features_and_offset_metadata(self):
        features = context.parse_assembly_features(ASM, "target_fn")
        self.assertEqual(features["address"], 0x80001000)
        self.assertEqual(features["size"], 40)
        self.assertEqual(features["calls"], ["KnownCall"])
        self.assertEqual(features["globals"], ["global_sda", "global_table"])
        self.assertEqual(features["sda_refs"], ["global_sda"])
        self.assertEqual(features["stack_frame"], 0x20)
        self.assertEqual(features["branches"]["conditional"], 1)
        self.assertEqual(features["branches"]["returns"], 1)
        self.assertIn("r3", features["registers"]["gpr"])
        placeholder = next(row for row in features["placeholder_structs"] if row["base"] == "r3")
        self.assertEqual(placeholder["minimum_size"], 0x28)
        self.assertEqual([row["offset"] for row in placeholder["fields"]], [0x18, 0x24])

    def test_mini_header_preserves_known_declarations_without_inventing_returns(self):
        features = context.parse_assembly_features(ASM.replace("KnownCall", "GenericCall"), "target_fn")
        features["calls"].append("KnownCall")
        header = context.build_mini_header(features, {"KnownCall": "void KnownCall(u32 value)"})
        self.assertIn("void KnownCall(u32 value);", header)
        self.assertNotIn("GenericCall", header)
        self.assertIn("extern u8 global_sda[]; /* SDA */", header)
        self.assertIn("typedef struct M2C_Object_r3", header)
        self.assertIn("#define M2C_R3_OFFSET_0018 0x18", header)
        self.assertEqual(header, context.build_mini_header(features, {"KnownCall": "void KnownCall(u32 value)"}))

    def test_native_m2c_recovers_float_return_from_unknown_callee(self):
        tool = harness_paths.tool_path("m2c", context.REPO_ROOT / "build/tools/m2c")
        if not (tool / "m2c.py").is_file():
            self.skipTest("Native m2c checkout unavailable")
        assembly = ".fn target, global\nbl unknown\nfadd f1,f1,f1\nblr\n.endfn target\n"
        features = context.parse_assembly_features(assembly, "target")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, header = root / "target.s", root / "context.c"
            source.write_text(assembly)
            header.write_text(context.build_mini_header(features, target_signature="double target(void)"))
            result = subprocess.run([sys.executable, str(tool / "m2c.py"), "-t", "ppc-mwcc-c",
                                     "--no-cache", "--valid-syntax", "--context", str(header), str(source)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("f64 unknown()", result.stdout)
            self.assertNotIn("M2C_BITWISE", result.stdout)

    def test_mini_header_does_not_guess_custom_by_value_types(self):
        features = context.parse_assembly_features(ASM, "target_fn")
        header = context.build_mini_header(features, {
            "KnownCall": "GXBool KnownCall(OSContext *context)"
        })
        self.assertNotIn("typedef s32 GXBool;", header)
        self.assertNotIn("GXBool KnownCall", header)
        pointer_header = context.build_mini_header(features, {
            "KnownCall": "void KnownCall(OSContext *context)"
        })
        self.assertIn("typedef struct OSContext OSContext;", pointer_header)
        self.assertIn("void KnownCall(OSContext *context);", pointer_header)

    def test_unknown_array_alias_and_mixed_value_pointer_cannot_be_assumed_scalar(self):
        features = {"symbol": "target", "calls": ["matrix", "mixed"]}
        header = context.build_mini_header(features, {
            "matrix": "void matrix(register Mtx m)",
            "mixed": "void mixed(Custom *pointer, Custom value)"
        }, target_signature="GXBool target(void)")
        self.assertNotIn("matrix(", header)
        self.assertNotIn("mixed(", header)
        self.assertNotIn("GXBool", header)
        self.assertNotIn("typedef s32", header)

    def test_reference_retrieval_deduplicates_identical_sdk_copies(self):
        target = context.parse_assembly_features(ASM, "target_fn")
        duplicate = {"project": "one", "symbol": "SharedSdk", "address": 1, "size": 40,
                     "unit": "sdk", "source_path": "sdk.c", "compiler": None,
                     "features": target}
        unique = {**duplicate, "project": "two", "symbol": "UniqueSdk"}
        projects = (context.ProjectSpec("one", Path("one"), "v"),
                    context.ProjectSpec("two", Path("two"), "v"))
        index = {"functions": [duplicate, {**duplicate, "project": "two"}, unique]}
        references = context.find_similar_references(target, projects, 3, index=index)
        self.assertEqual([row["symbol"] for row in references].count("SharedSdk"), 1)

    def test_reference_retrieval_prefers_the_closest_projects_over_alphabetical_order(self):
        target = context.parse_assembly_features(ASM, "target_fn")
        distant = dict(target, instruction_count=999, opcodes={"nop": 999}, calls=[], stack_frame=0)
        names = ["a1", "a2", "a3", "a4", "a5", "z1", "z2"]
        projects = tuple(context.ProjectSpec(name, Path(name), "v") for name in names)
        index = {"functions": [
            {"project": name, "symbol": f"fn_{name}", "address": 1, "size": 40, "unit": "u",
             "source_path": "u.c", "compiler": None,
             "features": target if name.startswith("z") else distant}
            for name in names]}
        with patch.object(context, "_enrich_reference",
                          side_effect=lambda row, project, score, chars: {**row, "similarity": score}):
            references = context.find_similar_references(target, projects, 5, index=index)
        self.assertEqual([row["project"] for row in references[:2]], ["z1", "z2"])
        self.assertEqual(len(references), 5)

    def test_report_address_falls_back_to_generated_symbol_suffix(self):
        self.assertEqual(
            context._function_address({}, {"name": "fn_80001234", "address": "0"}),
            0x80001234,
        )

    def test_catalog_reuse_selects_external_reference_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "ref"
            source = reference / "src/shared.c"
            source.parent.mkdir(parents=True)
            source.write_text("int Shared(void) { return 1; }\n")
            catalog = {
                "target_project": "target",
                "reference_projects": [{"name": "ref", "root": str(reference), "version": "REF01"}],
                "reference_index": {"functions": []},
                "cross_project_exact": {"matches": [{
                    "name": "Shared", "projects": ["ref", "target"],
                    "addresses": {"ref": "0x80100000", "target": "0x80200000"},
                    "linked_sources": {"ref": "src/shared.c"}, "bytes": 8, "sha256": "abc",
                }]},
            }
            result = context._reusable_from_catalog("Shared", 0x80200000, catalog)
            self.assertEqual(result["project"], "ref")
            self.assertEqual(result["target_project"], "target")
            self.assertEqual(result["target_address"], 0x80200000)


class ContextFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.target = self.root / "target"
        self.ref_a = self.root / "ref_a"
        self.ref_b = self.root / "ref_b"
        self.projects = (
            context.ProjectSpec("ref_a", self.ref_a, "REFA01"),
            context.ProjectSpec("ref_b", self.ref_b, "REFB01"),
        )
        self._make_project(self.ref_a, "REFA01", "ref_a_similar", 0x80002000, similar=True)
        self._make_project(self.ref_b, "REFB01", "ref_b_similar", 0x80003000, similar=True)
        self._make_target()

    def tearDown(self):
        self.temporary.cleanup()

    def _make_project(self, root, version, symbol, address, similar):
        source = f"src/{symbol}.c"
        report = {
            "units": [{
                "name": f"main/{symbol}",
                "metadata": {"source_path": source, "complete": True},
                "sections": [{"name": ".text", "metadata": {"virtual_address": str(address)}}],
                "functions": [{"name": symbol, "size": "40", "address": "0",
                               "fuzzy_match_percent": 100.0}],
            }]
        }
        write_json(root / "build" / version / "report.json", report)
        body = ASM.replace("target_fn", symbol).replace("800010", "800020")
        if not similar:
            body = body.replace("lwz", "mulli").replace("stw", "divw")
        asm = root / "build" / version / "asm" / f"{symbol}.s"
        asm.parent.mkdir(parents=True, exist_ok=True)
        asm.write_text(body, encoding="utf-8")
        source_path = root / source
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(f"int {symbol}(int value)\n{{\n    return value + 1;\n}}\n", encoding="utf-8")
        write_json(root / "objdiff.json", {"units": [{
            "name": f"main/{symbol}",
            "scratch": {"compiler": "mwcc_233_163n", "c_flags": "-O4,p -inline auto -fp_contract on"},
            "metadata": {"source_path": source},
        }]})

    def _make_target(self):
        units = []
        objdiff = []
        for symbol, address, compiler, flags in (
            ("target_near_a", 0x80000F00, "mwcc_233_163n", "-O4,p -inline auto -fp_contract on -use_lmw_stmw on"),
            ("target_near_b", 0x80001100, "mwcc_233_163n", "-O4,p -inline auto -fp_contract on -use_lmw_stmw on"),
            ("target_far", 0x80100000, "mwcc_242_81", "-O3 -inline off -fp_contract off"),
        ):
            source = f"src/{symbol}.c"
            units.append({
                "name": f"main/{symbol}",
                "metadata": {"source_path": source, "complete": True},
                "sections": [{"name": ".text", "metadata": {"virtual_address": str(address)}}],
                "functions": [{"name": symbol, "size": "8", "address": "0",
                               "fuzzy_match_percent": 100.0}],
            })
            objdiff.append({"name": f"main/{symbol}", "scratch": {"compiler": compiler, "c_flags": flags},
                            "metadata": {"source_path": source}})
            path = self.target / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"int {symbol}(void) {{ return 1; }}\n", encoding="utf-8")
        write_json(self.target / "build/TEST01/report.json", {"units": units})
        write_json(self.target / "objdiff.json", {"units": objdiff})

    def test_reference_search_is_cross_project_and_cached(self):
        cache = self.root / "cache/index.json"
        target = context.parse_assembly_features(ASM, "target_fn")
        refs = context.find_similar_references(target, self.projects, 3, cache)
        self.assertEqual(len(refs), 2)
        self.assertEqual({row["project"] for row in refs}, {"ref_a", "ref_b"})
        self.assertTrue(all(row["similarity"] > 0.95 for row in refs))
        self.assertEqual(refs[0]["signature"], f"int {refs[0]['symbol']}(int value)")
        self.assertEqual(refs[0]["compiler"]["mw_version"], "GC/1.2.5n")
        self.assertTrue(cache.is_file())
        self.assertEqual(context.build_reference_index(self.projects, cache)["fingerprint"],
                         json.loads(cache.read_text())["fingerprint"])

    def test_reusable_implementation_uses_target_address_and_linked_source(self):
        snapshot = self.root / "cross.json"
        (self.ref_a / "src/SharedFn.c").write_text(
            "int SharedFn(int value) { return value + 1; }\n", encoding="utf-8"
        )
        write_json(snapshot, {"matches": [{
            "name": "SharedFn", "bytes": 12, "sha256": "abc", "projects": ["target", "ref_a"],
            "addresses": {"target": "0x80001000", "ref_a": "0x80002000"},
            "linked_sources": {"ref_a": "src/SharedFn.c"},
            "exact_sources": {"ref_a": "src/SharedFn.c"},
        }]})
        reusable = context.find_reusable_implementation(
            "fn_80001000", 0x80001000, snapshot, {"ref_a": self.ref_a, "ref_b": self.ref_b},
            "target", {"ref_a": "REFA01", "ref_b": "REFB01"},
        )
        self.assertEqual(reusable["name"], "SharedFn")
        self.assertEqual(reusable["source_kind"], "linked_exact")
        self.assertEqual(reusable["target_address"], 0x80001000)
        self.assertIn("return value + 1", reusable["source_snippet"])

    def test_compiler_inference_prefers_nearest_exact_configuration(self):
        result = context.infer_compiler_profiles(self.target, 0x80001000, "TEST01")
        self.assertEqual(result["method"], "nearest_source_backed_exact")
        self.assertFalse(result["fallback"])
        self.assertEqual(result["selected"]["mw_version"], "GC/1.2.5n")
        self.assertEqual(result["selected"]["confidence"], "high")
        self.assertEqual(result["selected"]["settings"]["lmw_stmw"], "on")
        fallback = context.infer_compiler_profiles(self.target, None, "TEST01")
        self.assertTrue(fallback["fallback"])
        self.assertEqual(fallback["confidence"], "fallback")

    def test_compiler_inference_recovers_missing_report_addresses_from_symbols(self):
        report_path = self.target / "build/TEST01/report.json"
        report = json.loads(report_path.read_text())
        for unit in report["units"]:
            unit.pop("sections", None)
        report_path.write_text(json.dumps(report), encoding="utf-8")
        symbols = self.target / "config/TEST01/symbols.txt"
        symbols.parent.mkdir(parents=True, exist_ok=True)
        symbols.write_text(
            "target_near_a = .text:0x80001000; // type:function size:0x8\n"
            "target_near_b = .text:0x80001100; // type:function size:0x8\n"
            "target_far = .text:0x80090000; // type:function size:0x8\n",
            encoding="utf-8",
        )
        result = context.infer_compiler_profiles(self.target, 0x80001008, "TEST01")
        self.assertFalse(result["fallback"])
        self.assertEqual(result["selected"]["evidence"][0]["address"], 0x80001000)

    def test_builder_returns_integration_ready_payload(self):
        snapshot = self.root / "cross.json"
        write_json(snapshot, {"matches": []})
        builder = context.ContextBuilder(
            self.target, "TEST01", reference_projects=self.projects, cross_project_path=snapshot,
            cache_path=self.root / "context-cache.json",
        )
        result = builder.build(ASM, "target_fn", 0x80001000, reference_limit=3)
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("extern s32 KnownCall();", result["context"])
        self.assertEqual(len(result["references"]), 2)
        self.assertEqual(result["compiler"]["selected"]["mw_version"], "GC/1.2.5n")
        json.dumps(result)

    def test_build_knowledge_uses_reusable_catalog_and_stable_contract(self):
        snapshot = self.root / "cross.json"
        write_json(snapshot, {"matches": []})
        catalog = context.build_catalog(
            self.target, "TEST01", self.projects, snapshot, self.root / "batch-cache.json"
        )
        knowledge = context.build_knowledge(
            self.target, "TEST01", {"symbol": "target_fn", "address": "0x80001000"},
            ASM, reference_limit=3, catalog=catalog,
        )
        self.assertEqual(
            set(knowledge),
            {"context", "features", "references", "compiler_profile", "exact_reuse", "bootstrap", "priority"},
        )
        self.assertEqual(knowledge["context"]["status"], "ok")
        self.assertIsNone(knowledge["context"]["error"])
        self.assertEqual(len(knowledge["references"]), 2)
        self.assertGreater(knowledge["priority"]["score"], 50)
        self.assertTrue(knowledge["priority"]["reasons"])
        json.dumps(catalog)
        json.dumps(knowledge)

    def test_bootstrap_evidence_is_selected_by_address_as_hypothesis(self):
        catalog = {"bootstrap_evidence": {
            "scope": "bootstrap_evidence_not_verified",
            "type_hints": [{"address": "0x80001000", "signature": "int target(void)"}],
            "sequence_symbol_hints": [{"address": "0x80001000", "suggested_name": "target"}],
            "reference_sequence_regions": [{"target_start": "0x80001000",
                                             "target_end": "0x80001100",
                                             "reference_owner": "sdk/target"}],
            "call_clusters": [{"addresses": ["0x80001000", "0x80002000"]}],
        }}
        result = context._bootstrap_for_address(catalog, 0x80001000)
        self.assertEqual(result["symbol_hints"][0]["suggested_name"], "target")
        self.assertEqual(result["unit_regions"][0]["reference_owner"], "sdk/target")
        self.assertEqual(len(result["call_clusters"]), 1)
        self.assertEqual(context._bootstrap_for_address(catalog, 0x90000000), {})

    def test_bootstrap_signature_requires_one_consistent_target_name(self):
        hint = {"target_name": "fn_80001000",
                "target_signature": "int fn_80001000(unsigned value)"}
        self.assertEqual(context._bootstrap_target_signature({"type_hints": [hint]}, "fn_80001000"),
                         "int fn_80001000(unsigned value)")
        conflicting = {"type_hints": [hint, {**hint, "target_signature": "void fn_80001000(void)"}]}
        self.assertIsNone(context._bootstrap_target_signature(conflicting, "fn_80001000"))

    def test_code_verified_bootstrap_reference_is_source_backed(self):
        source = self.root / "known.c"
        source.write_text("int Known(unsigned value) {\n HW[0] = value;\n return value;\n}\n")
        bootstrap = {
            "symbol_hints": [{"suggested_name": "Known", "reference_project": "ref_b",
                              "promotion": "reviewable_code_match",
                              "code_comparison": {"status": "exact_bytes"}}],
            "type_hints": [{"name": "Known", "reference_project": "ref_b",
                            "source": "code_verified_reference_source", "source_path": str(source)}],
        }
        project = self.root / "ref_b"
        header = project / "include/sdk/hw_regs.h"
        header.parent.mkdir(parents=True)
        header.write_text("volatile unsigned short HW[] AT_ADDRESS(0xCC000000);\n")
        compiler = {"compiler": "mwcc", "flags": ["-O4,p"]}
        catalog = {"reference_projects": [{"name": "ref_b", "root": str(project)}],
                   "reference_index": {"functions": [{"project": "ref_b", "symbol": "Known",
                                                        "source_path": "known.c", "compiler": compiler}]}}
        result = context._bootstrap_reference(bootstrap, catalog)
        self.assertEqual(result["symbol"], "Known")
        self.assertEqual(result["similarity"], 1.0)
        self.assertEqual(result["compiler"], compiler)
        self.assertEqual(result["required_declarations"],
                         ["volatile unsigned short HW[] : (0xCC000000);"])
        self.assertEqual(result["required_macros"], [])
        self.assertTrue(result["requires_target_compile_validation"])

    def test_reference_macro_context_includes_transitive_dependencies(self):
        compiler = {"flags": ["-DNDEBUG=1"]}
        macros = {"SET_FIELD": "#define SET_FIELD(x) CHECK(x)",
                  "CHECK": "#define CHECK(x) ((void)0)",
                  "UNUSED": "#define UNUSED 1"}
        with patch.object(context, "_preprocessor_macros", return_value=macros) as capture:
            result = context._required_macros(self.root, self.root / "source.c",
                                              "void f(void) { SET_FIELD(1); }", compiler)
        self.assertEqual(result, [macros["CHECK"], macros["SET_FIELD"]])
        self.assertEqual(capture.call_args.args[2], ("-DNDEBUG=1",))


if __name__ == "__main__":
    unittest.main()
