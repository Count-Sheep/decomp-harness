import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_campaign_knowledge as knowledge
import local_campaign_knowledge_bench as bench
import local_campaign_knowledge_context as context
import local_campaign_knowledge_import as ingest


def function(symbol, address, owner, shape="a", eligible=True):
    core = ["li r3,0", "addi r3,r3,1", "cmpwi r3,2", "bne L0", "stw r3,0(r4)"]
    return {"module": "main", "address": address, "symbol": symbol, "unit": "main/" + symbol,
            "source_path": owner, "owner": owner, "source_text": f"int {symbol}(void) {{ return 1; }}",
            "assembly": "\n".join(core), "bytes_hash": "bytes-" + symbol,
            "strict_hash": "strict-" + symbol, "shape_hash": shape,
            "features": {"core": core, "calls": ["callee"], "opcode_counts": {"li": 1}, "normalized": core},
            "toolchain": {"command": ["mwcc", "-O4,p"]}, "insns": 12, "eligible": eligible,
            "pct": 100, "reported_complete": True, "eligibility": {"reasons": []}}


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = knowledge.KnowledgeStore(self.root / "evidence.sqlite3")
        self.addCleanup(self.store.close)
        self.snapshot = self.store.add_snapshot({"report_sha256": "report", "snapshot_sha256": "frozen"})

    def add(self, symbol, address, owner=None, **kwargs):
        row = function(symbol, address, owner or f"src/{symbol}.c", **kwargs)
        return self.store.add_function(self.snapshot, row)

    def test_artifacts_and_reimports_are_deduplicated_and_immutable(self):
        first = self.add("f", 1)
        self.assertEqual(self.add("f", 1), first)
        self.assertEqual(self.store.stats()["functions"], 1)
        self.assertEqual(self.store.stats()["observations"], 1)
        row = function("f", 1, "src/f.c")
        row["source_text"] = "int f(void) { return 2; }"
        with self.assertRaisesRegex(ValueError, "immutable"):
            self.store.add_function(self.snapshot, row)

    def test_scorer_match_does_not_become_verified_or_activate_a_candidate(self):
        target = self.add("f", 1)
        reference = self.add("g", 2)
        self.add("blocked", 3, eligible=False)
        variant = self.store.add_variant(target, "int f(void) { return 2; }", "m2c", {"context": "typed"})
        self.store.add_observation(variant, "scorer_exact", {"verification": knowledge.UNKNOWN_VERIFICATION})
        rows = self.store.retrieve(target)
        self.assertEqual([row["function_id"] for row in rows], [reference])
        self.assertEqual(rows[0]["evidence_class"], "REPORTED_SCORER_EXACT")
        self.assertTrue(all(value is None for value in rows[0]["verification"].values()))
        self.assertEqual(self.store.stats()["verified_exact"], 0)
        with self.assertRaisesRegex(ValueError, "certificates"):
            self.store.add_observation(variant, "scorer_exact", {"verification": {"linked_checksum": True}})
        with self.assertRaisesRegex(ValueError, "promotion"):
            self.store.add_observation(variant, "verified_exact", {"verification": knowledge.UNKNOWN_VERIFICATION})

    def test_parent_lineage_and_module_identity_are_checked(self):
        target, other = self.add("f", 1), self.add("g", 2)
        parent = self.store.add_variant(other, "g", "m2c", {})
        with self.assertRaisesRegex(ValueError, "lineage"):
            self.store.add_variant(target, "f", "agent", {}, parent)
        row = function("f", 1, "src/rel.c")
        row["module"] = "rel"
        rel = self.store.add_function(self.snapshot, row)
        self.assertEqual(self.store.resolve(self.snapshot, "f", "rel"), rel)
        self.assertEqual(self.store.resolve(self.snapshot, "f"), target)

    def test_derived_feature_index_is_idempotent(self):
        self.add("f", 1)
        self.add("g", 2)
        before = self.store.stats()["retrieval_terms"]
        self.assertEqual(self.store.index_features(self.snapshot), 2)
        self.assertEqual(self.store.stats()["retrieval_terms"], before)

    def test_index_recovers_a_reference_outside_the_size_only_pool(self):
        target = self.add("f", 1, shape="one")
        for index in range(800):
            distractor = function("unrelated" + str(index), index + 10, "src/other.c", shape="other")
            distractor["features"] = {"core": ["divw G0,G1,G2"] * 5,
                                      "calls": [], "opcode_counts": {"divw": 5}}
            self.store.add_function(self.snapshot, distractor)
        row = function("g", 2, "src/g.c", shape="two")
        row["insns"] = 21
        reference = self.store.add_function(self.snapshot, row)
        self.store.connection.execute("DELETE FROM retrieval_terms")
        self.assertEqual(self.store.retrieve(target), [])
        self.store.index_features(self.snapshot)
        self.assertEqual([item["function_id"] for item in self.store.retrieve(target)], [reference])
        self.assertEqual(self.store.retrieve(target, excluded={reference}), [])

    def test_body_local_register_ids_preserve_aliases(self):
        core = ["lwz G7,S(G9)", "addi G9,G7,M", "fmuls F4,F7,F4"]
        self.assertEqual(knowledge.body_shape(core),
                         ["lwz G0,S(G1)", "addi G1,G0,M", "fmuls F0,F1,F0"])
        target = self.add("f", 1, shape="one")
        reference = function("g", 2, "src/g.c", shape="two")
        reference["features"]["core"] = [line.replace("r3", "G7").replace("r4", "G9")
                                              for line in reference["features"]["core"]]
        row = function("f", 1, "src/f.c", shape="one")
        # Use a separate target: immutable observations cannot be rewritten.
        row.update(symbol="h", address=3, strict_hash="h")
        row["features"]["core"] = [line.replace("r3", "G2").replace("r4", "G5")
                                         for line in row["features"]["core"]]
        target = self.store.add_function(self.snapshot, row)
        reference_id = self.store.add_function(self.snapshot, reference)
        result = self.store.retrieve(target)
        self.assertEqual(result[0]["function_id"], reference_id)
        self.assertEqual(result[0]["score_components"]["body_rebased_shape"], 1)
        self.assertLess(result[0]["score_components"]["original_instruction_shape"], .65)
        self.assertNotIn(reference_id, [r["function_id"] for r in self.store.retrieve(target, excluded={target})])

    def test_budget_selection_skips_oversize_candidates_and_honors_zero_limit(self):
        target = self.add("f", 1)
        reference = self.add("g", 2)
        rows = self.store.retrieve(target)
        large = {**rows[0], "source_text": "x" * 200}
        with patch.object(self.store, "retrieve", return_value=[large, rows[0]]) as retrieve:
            pack = knowledge.retrieval_pack(self.store, target, budget=100, limit=1)
            self.assertEqual([r["function_id"] for r in pack["references"]], [reference])
            self.assertEqual(retrieve.call_args.kwargs["limit"], 12)
        self.assertEqual(knowledge.retrieval_pack(self.store, target, limit=0)["references"], [])

    def test_production_retrieval_keeps_matched_clones_but_never_excluded_sources(self):
        target = self.add("target", 1, eligible=False)
        reference = self.add("reference", 2)
        blocked = self.add("blocked", 3)
        excluded = {target, blocked}
        self.assertEqual(self.store.retrieve(target, excluded=excluded), [])
        refs = self.store.retrieve(target, excluded=excluded, exclude_clone_shapes=False)
        self.assertEqual([row["function_id"] for row in refs], [reference])
        pack = knowledge.retrieval_pack(self.store, target, excluded=excluded, exclude_clone_shapes=False)
        self.assertEqual([row["function_id"] for row in pack["references"]], [reference])
        self.assertFalse(pack["exclude_clone_shapes"])
        self.assertEqual(self.store.retrieve(target, excluded={reference, blocked}, exclude_clone_shapes=False), [])

    def test_complete_production_pack_does_not_drop_large_matched_bodies(self):
        target = self.add("target", 1, eligible=False)
        row = function("reference", 2, "src/reference.c")
        row["source_text"] = "int reference(void) { /*" + "x" * 7000 + "*/ return 1; }"
        reference = self.store.add_function(self.snapshot, row)
        self.assertEqual(knowledge.retrieval_pack(self.store, target, excluded={target},
                                                exclude_clone_shapes=False)["references"], [])
        pack = knowledge.full_source_pack(self.store, target, {target})
        self.assertIsNone(pack["budget"])
        self.assertEqual(pack["references"][0]["function_id"], reference)
        self.assertEqual(pack["references"][0]["source_text"], row["source_text"])
        self.assertEqual(pack["references"][0]["assembly"], row["assembly"])
        self.assertEqual(knowledge.full_source_pack(self.store, target, {target, reference})["references"], [])

    def test_shared_call_context_is_separate_from_a_structural_analogue(self):
        target = function("f", 1, "src/f.c", shape="one")
        other = function("g", 2, "src/g.c", shape="two")
        for row, bucket in ((target, "S"), (other, "M")):
            row["features"] = {"core": [f"lwz G0,{bucket}(G1)"] * 10 + ["bl SYM"] * 2,
                               "calls": ["specific_a", "specific_b"],
                               "opcode_counts": {"lwz": 10, "bl": 2}}
        key = self.store.add_function(self.snapshot, target)
        reference = self.store.add_function(self.snapshot, other)
        result = self.store.retrieve(key)
        self.assertEqual(result[0]["function_id"], reference)
        self.assertEqual(result[0]["reason"], "SHARED_CALLEE_REFERENCE")
        self.assertLess(result[0]["score_components"]["instruction_shape"], .65)
        self.assertEqual(self.store.retrieve(key, excluded={reference}), [])
        unrelated = function("h", 3, "src/h.c", shape="three")
        unrelated["features"] = {**other["features"], "calls": ["specific_a", "unrelated"]}
        unrelated_key = self.store.add_function(self.snapshot, unrelated)
        self.assertNotIn(unrelated_key, [r["function_id"] for r in self.store.retrieve(key)])

    def test_schema_upgrade_preserves_evidence_and_rebuilds_derived_terms(self):
        path = self.root / "upgrade.sqlite3"
        with knowledge.KnowledgeStore(path) as store:
            snapshot = store.add_snapshot({"report_sha256": "old"})
            store.add_function(snapshot, function("f", 1, "src/f.c"))
            store.connection.execute("DROP TABLE retrieval_terms")
            store.connection.execute("PRAGMA user_version=1")
            store.connection.commit()
        with self.assertRaisesRegex(ValueError, "schema"):
            knowledge.KnowledgeStore(path, readonly=True)
        with knowledge.KnowledgeStore(path) as store:
            self.assertEqual(store.stats()["functions"], 1)
            self.assertEqual(store.stats()["observations"], 1)
            self.assertEqual(store.index_features(snapshot), 1)
            self.assertGreater(store.stats()["retrieval_terms"], 0)
        with knowledge.KnowledgeStore(path, readonly=True) as store:
            self.assertEqual(store.stats()["functions"], 1)

    def test_local_idiom_keeps_aliases_and_rejects_bookkeeping(self):
        core = ["lwz G8,M(G9)", "lbz G7,S(G8)", "clrlwi G7,G7,M", "cmpwi G7,0",
                "beq FWD", "lha G6,M(G9)", "mullw G6,G6,G7", "stw G6,M(G9)"]
        renamed = [line.replace("G8", "G2").replace("G9", "G4").replace("G7", "G3").replace("G6", "G1")
                   for line in core]
        self.assertEqual(set(knowledge.idiom_windows(core)), set(knowledge.idiom_windows(renamed)))
        broken = renamed[:-1] + ["stw G2,M(G4)"]
        self.assertFalse(set(knowledge.idiom_windows(core)) & set(knowledge.idiom_windows(broken)))
        self.assertFalse(knowledge.idiom_windows(["mr G0,G1", "li G1,S", "bl SYM", "cmpwi G0,0"] * 2))
        self.assertFalse(knowledge.idiom_windows(["stw G0,M(G1)"] * 8))
        displaced = [line.replace("M(", "S(") for line in renamed]
        self.assertEqual(set(knowledge.idiom_windows(core)), set(knowledge.idiom_windows(displaced)))
        constant_changed = [line.replace("cmpwi G3,0", "cmpwi G3,S") for line in displaced]
        self.assertFalse(set(knowledge.idiom_windows(core)) & set(knowledge.idiom_windows(constant_changed)))

    def test_displacement_normalized_whole_body_holdout_clone_is_excluded(self):
        row = function("f", 1, "src/f.c", shape="one")
        row["features"]["core"] = [line.replace("0(r4)", "M(G2)") for line in row["features"]["core"]]
        key = self.store.add_function(self.snapshot, row)
        row.update(symbol="g", address=2, owner="src/g.c", source_path="src/g.c", shape_hash="two")
        row["features"]["core"] = [line.replace("M(G2)", "S(G2)") for line in row["features"]["core"]]
        self.store.add_function(self.snapshot, row)
        self.assertTrue(self.store.retrieve(key))
        self.assertEqual(self.store.retrieve(key, excluded={key}), [])

    def test_local_idiom_is_distinct_and_excerpt_matches_its_location(self):
        core = ["lwz G8,M(G9)", "lbz G7,S(G8)", "clrlwi G7,G7,M", "cmpwi G7,0",
                "beq FWD", "lha G6,M(G9)", "mullw G6,G6,G7", "stw G6,M(G9)"]
        keys = []
        for symbol, addr, prefix in (("f", 1, ["divw G1,G2,G3"] * 12),
                                     ("g", 2, ["fadds F1,F2,F3"] * 12)):
            row = function(symbol, addr, "src/" + symbol + ".c", shape=symbol)
            row["features"] = {"core": prefix + core, "calls": [], "normalized": prefix + core,
                               "opcode_counts": {"divw" if symbol == "f" else "fadds": 12, "lwz": 1}}
            row["assembly"] = "\n".join(prefix + core)
            row["insns"] = 20
            keys.append(self.store.add_function(self.snapshot, row))
        pack = knowledge.retrieval_pack(self.store, keys[0])
        ref = pack["references"][0]
        self.assertEqual(ref["reason"], "COMPILER_IDIOM_REFERENCE")
        self.assertLess(ref["score_components"]["instruction_shape"], .65)
        self.assertEqual(ref["assembly"], "\n".join(core))
        self.assertEqual(ref["assembly_excerpt"]["first_instruction"], 12)
        self.assertFalse(ref["assembly_excerpt"]["complete_function"])
        self.assertEqual(knowledge.retrieval_pack(self.store, keys[0], excluded={keys[1]})["references"], [])
        source_size = len(ref["source_text"])
        self.assertEqual(knowledge.retrieval_pack(self.store, keys[0], budget=source_size + 1)["references"], [])

    def test_assembly_excerpt_never_slices_an_instruction(self):
        key = self.add("g", 2)
        row = self.store.retrieve(self.add("f", 1))[0]
        row["assembly"] = "lwz r3,4(r4)\nstw r3,8(r5)"
        text, _ = knowledge.reference_assembly(self.store, row, 16)
        self.assertEqual(text, "lwz r3,4(r4)")
        row.update(reason="COMPILER_IDIOM_REFERENCE", idiom_match={"reference_core_offset": 0,
                                                                  "instruction_count": 8})
        self.assertEqual(knowledge.reference_assembly(self.store, row, 100), ("", None))

    def test_local_idiom_search_is_not_limited_by_whole_function_size(self):
        core = ["lwz G8,M(G9)", "lbz G7,S(G8)", "clrlwi G7,G7,M", "cmpwi G7,0",
                "beq FWD", "lha G6,M(G9)", "mullw G6,G6,G7", "stw G6,M(G9)"]
        keys = []
        for symbol, address, prefix in (("small", 1, []),
                                        ("large", 2, ["fadds F1,F2,F3"] * 80)):
            row = function(symbol, address, "src/" + symbol + ".c", shape=symbol)
            instructions = prefix + core
            row["features"] = {"core": instructions, "normalized": instructions,
                               "calls": [], "opcode_counts": {"fadds": len(prefix), "lwz": 1}}
            row.update(assembly="\n".join(instructions), insns=len(instructions))
            keys.append(self.store.add_function(self.snapshot, row))
        for target, reference in (keys, keys[::-1]):
            pack = knowledge.retrieval_pack(self.store, target)
            self.assertEqual(pack["references"][0]["function_id"], reference)
            self.assertEqual(pack["references"][0]["reason"], "COMPILER_IDIOM_REFERENCE")
            self.assertEqual(pack["references"][0]["assembly"], "\n".join(core))
            self.assertEqual(knowledge.retrieval_pack(self.store, target, excluded={reference})["references"], [])

    def test_local_idiom_search_checks_windows_beyond_target_term_cap(self):
        core = ["lwz G8,M(G9)", "lbz G7,S(G8)", "clrlwi G7,G7,M", "cmpwi G7,0",
                "beq FWD", "lha G6,M(G9)", "mullw G6,G6,G7", "stw G6,M(G9)"]
        keys = []
        for symbol, address, prefix in (("target", 1, ["fadds F1,F2,F3"] * 80), ("ref", 2, [])):
            row = function(symbol, address, "src/" + symbol + ".c", shape=symbol)
            row["features"] = {"core": prefix + core, "normalized": prefix + core,
                               "calls": [], "opcode_counts": {"fadds": len(prefix), "lwz": 1}}
            row.update(assembly="\n".join(prefix + core), insns=len(prefix + core))
            keys.append(self.store.add_function(self.snapshot, row))
        with patch.object(knowledge, "retrieval_terms", return_value=set()):
            self.assertEqual(self.store.retrieve(keys[0])[0]["function_id"], keys[1])

    def test_readonly_queries_and_artifact_integrity(self):
        self.add("f", 1)
        self.store.connection.commit()
        with knowledge.KnowledgeStore(self.store.path, readonly=True) as reader:
            self.assertEqual(reader.stats()["functions"], 1)
            with self.assertRaises(sqlite3.OperationalError):
                reader.artifact("new")
        key = self.store.artifact("body")
        self.store.connection.execute("UPDATE artifacts SET content=? WHERE sha256=?", (knowledge.zlib.compress(b"changed"), key))
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.store.text(key)

    def test_reader_keeps_working_during_a_writer_transaction(self):
        self.add("f", 1)
        self.store.connection.commit()
        with knowledge.KnowledgeStore(self.store.path, readonly=True) as reader:
            self.add("g", 2)
            self.assertEqual(reader.stats()["functions"], 1)
            self.store.connection.commit()
            self.assertEqual(reader.stats()["functions"], 2)

    def test_owner_clone_groups_close_transitively(self):
        rows = [dict(id="a", owner="first.c", shape_hash="one"),
                dict(id="b", owner="first.c", shape_hash="two"),
                dict(id="c", owner="second.c", shape_hash="two"),
                dict(id="d", owner="second.c", shape_hash="three"),
                dict(id="e", owner="third.c", shape_hash="four")]
        groups = knowledge.clone_groups(rows)
        self.assertEqual(len({groups[key] for key in "abcd"}), 1)
        self.assertNotEqual(groups["a"], groups["e"])

    def test_holdout_audit_is_frozen_excludes_owners_and_makes_no_gain_claim(self):
        for i in range(8):
            self.add("f" + str(i), i, owner="src/group" + str(i // 2) + ".c", shape=str(i))
        split = bench.make_split(self.store, self.snapshot, limit=2)
        self.assertEqual(split, bench.make_split(self.store, self.snapshot, limit=2))
        audit = bench.run_audit(self.store, split)
        self.assertEqual(audit["source_leaks"], 0)
        self.assertFalse(audit["inference_run"])
        self.assertIsNone(audit["verified_gain"])
        for row in audit["results"]:
            self.assertFalse(set(split["excluded"]) & set(row["reference_ids"]))
        split["excluded"].pop()
        with self.assertRaisesRegex(ValueError, "manifest changed"):
            bench.validate_split(self.store, split)

    def test_audit_cli_preserves_existing_result_and_split_history(self):
        for suffix in (".json", ".split.json"):
            with self.subTest(suffix=suffix):
                output = self.root / ("audit" + suffix.replace(".split", ""))
                existing = output.with_suffix(suffix)
                existing.write_text("historical evidence", encoding="utf-8", newline="\n")
                with patch.object(sys, "argv", ["bench", "audit", "snapshot", "--output", str(output)]):
                    with self.assertRaisesRegex(ValueError, "preserve history"):
                        bench.main()
                self.assertEqual(existing.read_text(encoding="utf-8"), "historical evidence")
                existing.unlink()

    def test_split_with_a_valid_hash_but_incomplete_exclusions_is_rejected(self):
        self.add("f", 1, "src/owner.c", shape="one")
        self.add("g", 2, "src/owner.c", shape="two")
        split = bench.make_split(self.store, self.snapshot, limit=1)
        split["excluded"] = split["targets"]
        split["split_sha256"] = knowledge.digest(knowledge.encoded({k: v for k, v in split.items() if k != "split_sha256"}))
        with self.assertRaisesRegex(ValueError, "complete owner/clone"):
            bench.validate_split(self.store, split)

    def test_pack_budget_never_truncates_source_or_contains_target_source(self):
        target, reference = self.add("f", 1, shape="one"), self.add("g", 2, shape="two")
        pack = knowledge.retrieval_pack(self.store, target, budget=20)
        self.assertEqual(pack["references"], [])
        pack = knowledge.retrieval_pack(self.store, target, budget=100)
        self.assertEqual(pack["references"][0]["function_id"], reference)
        self.assertLessEqual(pack["reference_content_chars"], 100)
        self.assertNotIn("int f(void)", knowledge.encoded(pack))

    def test_optional_prompt_checks_snapshot_report_and_live_source_hash(self):
        report = self.root / "report.json"
        report.write_text("{}", encoding="utf-8", newline="\n"); snapshot = self.store.add_snapshot({
            "report_sha256": knowledge.digest(report.read_bytes()), "source_root": str(self.root)})
        target = self.store.add_function(snapshot, function("f", 1, "src/f.c", shape="one"))
        reference = function("g", 2, "src/g.c", shape="two")
        self.store.add_function(snapshot, reference)
        self.store.connection.commit()
        with patch.dict("os.environ", {"DECOMP_KNOWLEDGE_DB": str(self.store.path)}):
            body = lambda *args: reference["source_text"]
            text = context.prompt_context(self.root, report, {"symbol": "f", "address": 1}, body)
            self.assertIn("not independent verification", text)
            self.assertIn("int g(void)", text)
            self.assertNotIn("int f(void)", text)
            self.assertEqual(context.prompt_context(self.root, report, {"symbol": "f"}, lambda *args: "changed"), "")
            self.assertEqual(context.prompt_context(self.root, report, {"symbol": "f", "address": 9}, body), "")
            report.write_text("changed", encoding="utf-8", newline="\n")
            self.assertEqual(context.prompt_context(self.root, report, {"symbol": "f"}, body), "")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()
        (self.root / "src/sample.c").write_text("int f(void) { return 1; }\nint g(void) { return 2; }\n", encoding="utf-8", newline="\n")
        report = self.root / "build/TEST01/report.json"
        report.parent.mkdir(parents=True)
        report.write_text(json.dumps({"units": [{"name": "main/sample", "metadata": {
            "source_path": "src/sample.c", "complete": True}, "functions": [
            {"name": "f", "fuzzy_match_percent": 100, "metadata": {"virtual_address": "1"}},
            {"name": "g", "fuzzy_match_percent": 90, "metadata": {"virtual_address": "2"}}]}]}), encoding="utf-8", newline="\n")
        (self.root / "snapshot.json").write_text('{"stable":true,"sha256":"stable","version":"TEST01"}', encoding="utf-8", newline="\n")
        (self.root / "objdiff.json").write_text("{}", encoding="utf-8", newline="\n")
        self.store = knowledge.KnowledgeStore(self.root / "store.sqlite3")
        self.addCleanup(self.store.close)
        self.assembly = [dict(name=name, addr=addr, asm_path="sample.s", insns=[("li", "r3,1"), ("blr", "")],
                              normalized=["li G0,S", "blr "], bytes_hash=name) for name, addr in (("f", 1), ("g", 2))]

    def import_(self, check=lambda root: "stable"):
        return ingest.import_snapshot(self.store, self.root, "unused", assembly=self.assembly,
                                      commands={"src/sample.c": "mwcc -c src/sample.c -o build/sample.o"},
                                      check_fingerprint=check)

    def test_import_is_idempotent_and_does_not_trust_unmatched_source(self):
        before = (self.root / "src/sample.c").read_bytes()
        result = self.import_()
        self.assertEqual(result["counts"]["eligible"], 1)
        self.assertEqual(self.import_()["stats"]["functions"], 2)
        self.assertEqual(self.store.stats()["observations"], 2)
        self.assertEqual((self.root / "src/sample.c").read_bytes(), before)

    def test_snapshot_provenance_names_the_configured_project_and_version(self):
        snapshot_id = self.import_()["snapshot_id"]
        provenance = json.loads(self.store.connection.execute(
            "SELECT provenance_json FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()[0])
        self.assertEqual(provenance["project"], ingest.PROJECT_ID)
        self.assertEqual(provenance["version"], "TEST01")

    def test_workspace_change_rolls_back_all_evidence(self):
        with self.assertRaisesRegex(ValueError, "rolled back"):
            self.import_(check=unittest.mock.Mock(side_effect=["stable", "changed"]))
        self.assertEqual(self.store.stats()["snapshots"], 0)
        self.assertEqual(self.store.stats()["functions"], 0)

    def test_unstable_snapshot_and_path_escape_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "stable frozen"):
            self.import_(check=lambda root: "wrong")
        with self.assertRaisesRegex(ValueError, "escapes"):
            ingest.safe_path(self.root, "../elsewhere.c")

    def test_owner_resolution_uses_explicit_root_not_campaign_global(self):
        import local_campaign as campaign
        (self.root / "src/wrapper.c").write_text('#include "src/sample.c"\n', encoding="utf-8", newline="\n")
        with patch.object(campaign, "ROOT", self.root / "nonexistent"):
            self.assertEqual(campaign.owner_source("src/wrapper.c", root=self.root), "src/sample.c")

    def test_experiment_binds_inputs_and_preserves_diagnostics_without_verification(self):
        imported = self.import_()
        snapshot_id = imported["snapshot_id"]
        batch, validation = self.root / "batch", self.root / "validation"
        batch.mkdir(); validation.mkdir()
        draft = batch / "draft.c"
        draft.write_text("int f(void) { return 1; }", encoding="utf-8", newline="\n")
        row = {"id": "task", "symbol": "f", "module": "main", "artifacts": str(batch), "status": "ok"}
        report_hash = knowledge.digest((self.root / "build/TEST01/report.json").read_bytes())
        summary = {"finished": True, "report_sha256": report_hash, "workspace_sha256": "stable",
                   "workspace": str(self.root), "run_id": "run", "tool": {"fingerprint": "tool"}}
        generated = {"summary": summary, "results": [row]}
        compiled = {"summary": {"finished": True, "workspace": str(self.root), "batch": str(batch),
                    "validator_version": 2, "environment_unchanged": True}, "results": [
                    {"id": "task", "draft_sha256": knowledge.digest(draft.read_bytes()),
                     "status": "scorer_exact", "pct": 100, "compile_ok": True}]}
        (batch / "results.json").write_text(json.dumps(generated), encoding="utf-8", newline="\n")
        (validation / "results.json").write_text(json.dumps(compiled), encoding="utf-8", newline="\n")
        result = ingest.import_experiment(self.store, snapshot_id, batch, validation)
        self.assertEqual(result["counts"]["scorer_exact"], 1)
        self.assertEqual(self.store.stats()["verified_exact"], 0)
        self.assertEqual(ingest.import_experiment(self.store, snapshot_id, batch, validation)["stats"]["observations"], 3)
        row["address"] = "99"
        (batch / "results.json").write_text(json.dumps(generated), encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(ValueError, "target address"):
            ingest.import_experiment(self.store, snapshot_id, batch, validation)
        del row["address"]
        generated["results"].append(row.copy())
        (batch / "results.json").write_text(json.dumps(generated), encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(ValueError, "duplicate task"):
            ingest.import_experiment(self.store, snapshot_id, batch, validation)
        generated["results"].pop()
        (batch / "results.json").write_text(json.dumps(generated), encoding="utf-8", newline="\n")
        draft.write_text("changed", encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(ValueError, "Draft changed"):
            ingest.import_experiment(self.store, snapshot_id, batch, validation)
        generated["summary"]["workspace_sha256"] = "other"
        (batch / "results.json").write_text(json.dumps(generated), encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(ValueError, "different frozen"):
            ingest.import_experiment(self.store, snapshot_id, batch, validation)

    def test_paired_import_binds_prompt_and_draft_and_does_not_activate_it(self):
        snapshot = self.import_()["snapshot_id"]
        key = self.store.resolve(snapshot, "f")
        target = self.store.function(key)
        folder = self.root / "paired"
        directory = folder / key / "off"
        directory.mkdir(parents=True)
        prompt = "Retail assembly and declarations only"
        (directory.parent / "off.prompt.txt").write_text(prompt, encoding="utf-8", newline="\n")
        draft = "int f(void) { return 1; }"
        (directory / "draft.c").write_text(draft, encoding="utf-8", newline="\n")
        configuration = {"snapshot_id": snapshot, "workspace_sha256": "stable", "model": "test"}
        captured = {"target_id": key, "symbol": "f", "unit": target["unit"], "module": "main", "address": 1,
                    "reference_ids": [], "prompt_sha256": {"off": knowledge.digest(prompt)}}
        measured = {"status": "scorer_exact", "pct": 100, "compile_ok": True,
                    "draft_sha256": knowledge.digest(draft), "prompt_sha256": knowledge.digest(prompt)}
        summary = {**configuration, "configuration_sha256": knowledge.digest(knowledge.encoded(configuration)),
                   "finished": True, "environment_unchanged": True, "final_workspace_sha256": "stable"}
        (folder / "results.json").write_text(json.dumps({"summary": summary, "results": [{**captured, "off": measured}]}), encoding="utf-8", newline="\n")
        (folder / "inputs.json").write_text(json.dumps({"configuration": configuration, "inputs": [captured]}), encoding="utf-8", newline="\n")
        (folder / "selection.json").write_text(json.dumps({"configuration": configuration, "targets": [key], "excluded": [key]}), encoding="utf-8", newline="\n")
        result = ingest.import_paired_experiment(self.store, snapshot, folder)
        self.assertEqual(result["counts"]["scorer_exact"], 1)
        self.assertEqual(ingest.import_paired_experiment(self.store, snapshot, folder)["stats"]["observations"], 3)
        self.assertEqual(self.store.stats()["verified_exact"], 0)
        (directory.parent / "off.prompt.txt").write_text("changed", encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(ValueError, "prompt changed"):
            ingest.import_paired_experiment(self.store, snapshot, folder)


if __name__ == "__main__":
    unittest.main()
