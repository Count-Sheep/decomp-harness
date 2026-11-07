"""Declaration-only m2c baselines for frozen holdouts or actual unmatched project functions."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys

import local_campaign as campaign
import local_campaign_c89 as c89
import local_campaign_corpus as corpus
import local_campaign_decompile as decompile
import local_campaign_decompile_validate as validator
import local_campaign_knowledge as knowledge
from harness_paths import VERSION
from local_campaign_coordination import Coordinator
from local_campaign_decompile_batch import command_map, targets as report_targets
from local_campaign_decompile_snapshot import fingerprint
from local_campaign_knowledge_ablation import declaration_context, candidate_declarations, extract_assembly
from local_campaign_knowledge_bench import validate_split
from local_campaign_recovery import object_evidence


def unmatched_selection(store: knowledge.KnowledgeStore, snapshot_id: str) -> dict:
    snapshot = store.connection.execute("SELECT provenance_json FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if snapshot is None:
        raise ValueError("Unknown knowledge snapshot")
    provenance = json.loads(snapshot[0])
    version = provenance.get("version") or VERSION
    report = (Path(provenance["workspace"]) / "build" / version / "report.json").read_bytes()
    if knowledge.digest(report) != provenance["report_sha256"]:
        raise ValueError("Snapshot report changed")
    selection, missing, scores = [], [], {}
    for row in report_targets(json.loads(report), "unmatched"):
        try:
            key = store.resolve(snapshot_id, row["symbol"], row["module"])
        except ValueError:
            count = store.connection.execute("SELECT COUNT(*) FROM functions WHERE snapshot_id=? AND symbol=? AND module=?",
                                              (snapshot_id, row["symbol"], row["module"])).fetchone()[0]
            if count:
                raise
            missing.append(row)
            continue
        target = store.function(key)
        if (target["address"] != int(str(row["address"]), 0) or target["unit"] != row["unit"]
                or target["source_path"] != row["source"] or target["eligible"]):
            raise ValueError("Unmatched target differs from its immutable warehouse identity")
        selection.append(key)
        scores[key] = row["pct"]
    if not selection and not missing:
        raise ValueError("Snapshot has no unmatched functions")
    corpus_ids = [row[0] for row in store.connection.execute(
        "SELECT id FROM functions WHERE snapshot_id=? ORDER BY id", (snapshot_id,))]
    payload = {"schema_version": 1, "mode": "actual_unmatched_project_baseline", "snapshot_id": snapshot_id,
               "report_sha256": provenance["report_sha256"], "targets": selection,
               "excluded": sorted(selection), "missing_warehouse_targets": missing, "reported_scores": scores,
               "corpus_function_ids_sha256": knowledge.digest(knowledge.encoded(corpus_ids)),
               "notice": "Production-style unmatched baseline. Matched siblings/clones are allowed references; not a blind holdout."}
    return {**payload, "split_sha256": knowledge.digest(knowledge.encoded(payload))}


def validate_selection(store: knowledge.KnowledgeStore, selection: dict) -> None:
    if selection.get("mode") == "actual_unmatched_project_baseline":
        if unmatched_selection(store, selection["snapshot_id"]) != selection:
            raise ValueError("Frozen unmatched target selection or corpus changed")
    else:
        validate_split(store, selection)


def untested_targets(split: dict, selections: list[dict]) -> list[str]:
    tested = set()
    for selection in selections:
        targets = selection.get("targets", [])
        if (selection.get("split_sha256") != split["split_sha256"] or not targets
                or len(set(targets)) != len(targets) or not set(targets) <= set(split["targets"])):
            raise ValueError("Development selection belongs to a different or invalid holdout")
        tested.update(targets)
    remaining = [key for key in split["targets"] if key not in tested]
    if not remaining:
        raise ValueError("No untouched targets remain")
    return remaining


def summarize(results: list[dict]) -> dict:
    rows = [row.get("measurement", {}) for row in results]
    return {"processed": len(results), "outcomes": dict(Counter(r.get("status", "no_candidate") for r in rows)),
            "compiled": sum(bool(r.get("compile_ok")) for r in rows),
            "scorer_exact": sum(r.get("pct") == 100 for r in rows),
            "object_verified": sum(all(r.get("verification", {}).get(axis) is True
                                       for axis in ("raw_candidate_bytes", "relocations", "owner_object")) for r in rows),
            "policy_screen_passes": sum(r.get("verification", {}).get("policy_screen_passed") is True for r in rows),
            "covered_targets": sum(bool(r.get("reference_ids")) for r in results),
            "baseline_compiled": sum(bool(r.get("baseline_measurement", {}).get("compile_ok")) for r in results),
            "baseline_already_exact": sum(r.get("baseline_measurement", {}).get("pct") == 100 for r in results),
            "actual_unmatched_scorer_gains": sum(r.get("baseline_measurement", {}).get("compile_ok") is True
                and isinstance(r.get("baseline_measurement", {}).get("pct"), (int, float))
                and r["baseline_measurement"]["pct"] < 100 and r.get("measurement", {}).get("pct") == 100 for r in results),
            "model_requests": 0, "ready": 0, "promoted": 0, "retrieval_effect": None}


def run(store: knowledge.KnowledgeStore, split: dict, excluded_runs: list[Path], output: Path,
        *, baseline_cache: Path | None = None, native_c: bool = False) -> dict:
    validate_selection(store, split)
    actual_unmatched = split.get("mode") == "actual_unmatched_project_baseline"
    if actual_unmatched and excluded_runs:
        raise ValueError("Actual unmatched baseline must include the full cohort, not excluded development selections")
    selections = []
    for folder in excluded_runs:
        selection = json.loads((folder / "selection.json").read_text(encoding="utf-8"))
        prior = json.loads((folder / "results.json").read_text(encoding="utf-8"))["summary"]
        if not prior.get("finished") or not prior.get("environment_unchanged"):
            raise ValueError("Excluded development runs must be finished and unchanged")
        if knowledge.digest(knowledge.encoded(selection["configuration"])) != prior["configuration_sha256"]:
            raise ValueError("Excluded development configuration changed")
        selections.append(selection)
    targets = untested_targets(split, selections)
    provenance = json.loads(store.connection.execute("SELECT provenance_json FROM snapshots WHERE id=?",
                                                     (split["snapshot_id"],)).fetchone()[0])
    root = Path(provenance["workspace"])
    version = provenance.get("version") or VERSION
    if root.resolve() != campaign.ROOT.resolve() or fingerprint(root, version) != provenance["snapshot_sha256"]:
        raise ValueError("Use DECOMP_ROOT pointing to the unchanged frozen workspace")
    if output.exists():
        raise ValueError("Sweep output already exists; use a new directory to preserve history")
    coordinator = Coordinator(Path(provenance["source_root"]) / "build/local_llm_campaign")
    commands = subprocess.run(["ninja", "-t", "commands", "all_source"], cwd=root,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              check=True, timeout=60)
    lookup = command_map(commands.stdout)
    units = {row["name"]: row for row in json.loads((root / "objdiff.json").read_text(encoding="utf-8"))["units"]}
    environment = validator.environment_fingerprint(root, lookup, version)
    metadata = decompile.tool_metadata()
    baseline_cache = (baseline_cache or output / "baselines").resolve()
    configuration = {"schema_version": 1, "mode": "actual_unmatched_project_m2c_baseline" if actual_unmatched
                     else "untouched_remainder_type_informed_m2c_baseline",
                     "snapshot_id": split["snapshot_id"], "split_sha256": split["split_sha256"],
                     "workspace_sha256": provenance["snapshot_sha256"], "targets": targets,
                     "selection": "all_reported_unmatched_targets_no_size_or_coverage_filter" if actual_unmatched
                     else "all_remaining_frozen_targets_no_size_or_coverage_filter",
                     "excluded_runs": [{"path": str(folder.resolve()),
                                        "selection_sha256": knowledge.digest((folder / "selection.json").read_bytes())}
                                       for folder in excluded_runs],
                     "m2c_tool": metadata, "retrieval_version": knowledge.RETRIEVAL_VERSION,
                     "reference_exclude_clone_shapes": not actual_unmatched,
                     "baseline_cache_root": str(baseline_cache),
                     "native_c": native_c,
                     "native_c_operations": ["explicit_byte_access", "real_candidate_declaration_closure", "primitive_alias_binding"]
                         if native_c else [],
                     "producer_sha256": knowledge.digest(Path(__file__).read_bytes()),
                     "retriever_sha256": knowledge.digest(Path(knowledge.__file__).read_bytes()),
                     "repair_sha256": knowledge.digest(Path(c89.__file__).read_bytes()),
                     "validator_sha256": knowledge.digest(Path(validator.__file__).read_bytes()),
                     "attested_tools": {str(Path(module.__file__).resolve()): knowledge.digest(Path(module.__file__).read_bytes())
                         for module in (campaign, corpus, decompile, sys.modules[declaration_context.__module__],
                                        sys.modules[object_evidence.__module__])},
                     "environment_sha256": environment,
                     "notice": split["notice"] if actual_unmatched else
                     "Known matched C bodies held out. Not an unmatched-project baseline or retrieval ablation."}
    output.mkdir(parents=True, mode=0o700)
    decompile.write_json(output / "selection.json", {"configuration": configuration, "targets": targets,
                         "excluded": split["excluded"], "split_sha256": split["split_sha256"]})
    decompile.write_json(output / "cohort.json", split)
    config_hash = knowledge.digest(knowledge.encoded(configuration))
    inputs, results = [], []

    def checkpoint(phase, finished=False, **detail):
        preparation = {"preparation_errors": sum(bool(row.get("preparation_error")) for row in inputs)}
        if phase == "preparing":
            preparation.update(baseline_compiled=sum(bool(row.get("baseline_measurement", {}).get("compile_ok")) for row in inputs),
                               baseline_already_exact=sum(row.get("baseline_measurement", {}).get("pct") == 100 for row in inputs),
                               covered_targets=sum(bool(row.get("reference_ids")) for row in inputs))
        summary = {**configuration, "configuration_sha256": config_hash, "phase": phase,
                   "finished": finished, "total": len(targets) + len(split.get("missing_warehouse_targets", [])), "prepared": len(inputs),
                   "updated_at": datetime.now(UTC).isoformat(), **summarize(results), **preparation, **detail}
        decompile.write_json(output / "manifest.json", summary)
        decompile.write_json(output / "results.json", {"summary": summary, "results": results})
        print(json.dumps({key: summary[key] for key in ("phase", "prepared", "processed", "total", "compiled",
                                                       "scorer_exact", "object_verified")}), flush=True)
        return summary

    try:
        sys.path.insert(0, str(decompile.tool().parent))
        for row in split.get("missing_warehouse_targets", []):
            results.append({**row, "measurement": {"status": "missing_warehouse_target", "compile_ok": False}})
        # Capture every input and failure before generation, without adapting selection to outcomes.
        for key in targets:
            target = store.function(key)
            item = {"target_id": key, "symbol": target["symbol"], "source": target["source_path"],
                    "unit": target["unit"], "address": target["address"], "module": target["module"]}
            folder = output / key
            folder.mkdir()
            try:
                if not (item["source"] or "").endswith(".c"):
                    raise ValueError("C++ owner unsupported by the declaration-only C baseline")
                assembly = extract_assembly(root, target, version)
                owner = (root / target["owner"]).read_text(encoding="utf-8")
                start, end = campaign.function_span(owner, item["symbol"])
                signature = campaign.function_signature(owner, start, end)
                item.update(signature=signature, reported_pct=split.get("reported_scores", {}).get(key, 100))
                if actual_unmatched:
                    with coordinator.build("Unmatched owner baseline", "MWCC/objdiff: " + item["symbol"]):
                        owner_name, files = validator.snapshot_owner(root, item["source"], item["symbol"])
                        baseline = validator.compile_baseline(root, item["source"], owner_name, files, item["symbol"],
                                                              lookup[item["source"]], environment, baseline_cache)
                        score_folder = folder / "baseline-score"
                        score_folder.mkdir()
                        score = validator.score_object(root, root / units[item["unit"]]["target_path"],
                            Path(baseline["object"]), item["symbol"], score_folder) if baseline["compile_ok"] else {}
                        item["baseline_measurement"] = {**baseline, **score}
                context = decompile.preprocess(root, lookup[item["source"]], item["source"])
                if context["status"] != "ok":
                    raise ValueError("Preprocessing failed: " + context.get("error", "")[-1000:])
                declarations = declaration_context(context["context"],
                                                   {item["symbol"], *corpus.CALL.findall(assembly), *corpus.DATA.findall(assembly)})
                pack = knowledge.retrieval_pack(store, key, excluded=set(split["excluded"]),
                                               exclude_clone_shapes=not actual_unmatched)
                if set(split["excluded"]) & {ref["function_id"] for ref in pack["references"]}:
                    raise ValueError("Held-out reference leaked into the sweep")
                (folder / "target.s").write_text(assembly, encoding="utf-8", newline="\n")
                (folder / "declarations.c").write_text(declarations, encoding="utf-8", newline="\n")
                decompile.write_json(folder / "references.json", pack)
                item.update(signature=signature, assembly_sha256=knowledge.digest(assembly),
                            declarations_sha256=knowledge.digest(declarations),
                            references_sha256=knowledge.digest(knowledge.encoded(pack)),
                            reference_ids=[r["function_id"] for r in pack["references"]])
            except Exception as error:
                item["preparation_error"] = f"{type(error).__name__}: {error}"
            inputs.append(item)
            decompile.write_json(folder / "input.json", item)
            checkpoint("preparing", current=item["symbol"])
        decompile.write_json(output / "inputs.json", {"configuration": configuration, "inputs": inputs})
        for item in inputs:
            folder = output / item["target_id"]
            result = dict(item)
            checkpoint("generating", current=item["symbol"])
            try:
                if item.get("preparation_error"):
                    result["measurement"] = {"status": "preparation_error", "error": item["preparation_error"]}
                else:
                    declarations = (folder / "declarations.c").read_text(encoding="utf-8")
                    assembly = (folder / "target.s").read_bytes()
                    if (knowledge.digest(declarations) != item["declarations_sha256"]
                            or knowledge.digest(assembly) != item["assembly_sha256"]):
                        raise ValueError("Frozen generation inputs changed")
                    translated = decompile.translate(root, item["symbol"], assembly, str(folder / "target.s"),
                                                      {"status": "ok", "context": declarations}, output / "m2c-cache", metadata,
                                                      explicit_fields=native_c)
                    result["m2c"] = {key: translated.get(key) for key in ("status", "diagnostics", "artifacts", "error")}
                    candidate = campaign.extract_function(translated.get("draft", ""), item["symbol"])
                    (folder / "original.c").write_text(candidate, encoding="utf-8", newline="\n")
                    result["original_candidate_sha256"] = knowledge.digest(candidate)
                    if native_c:
                        context = decompile.preprocess(root, lookup[item["source"]], item["source"])
                        if context["status"] != "ok":
                            raise ValueError("Native C declaration preprocessing failed")
                        declarations = candidate_declarations(context["context"], candidate, item["symbol"])
                        (folder / "native-declarations.c").write_text(declarations, encoding="utf-8", newline="\n")
                        result["native_declarations_sha256"] = knowledge.digest(declarations)
                    for operation in ("signature_binding", "c89_declarations"):
                        try:
                            if operation == "signature_binding":
                                candidate = c89.bind_signature(candidate, declarations, item["signature"], item["symbol"],
                                                               primitive_aliases=native_c)
                            else:
                                candidate, count = c89.repair_declarations(candidate, declarations, item["symbol"])
                                result["c89_repairs"] = count
                        except Exception as error:
                            result[operation + "_error"] = str(error)
                    (folder / "draft.c").write_text(candidate, encoding="utf-8", newline="\n")
                    with coordinator.build("Untouched m2c baseline", "MWCC/objdiff: " + item["symbol"]):
                        measured = validator.validate_one(root, {**item, "id": item["target_id"], "status": "ok",
                                                                  "artifacts": str(folder)}, lookup[item["source"]],
                                                          units[item["unit"]], "", folder / "validation",
                                                          environment=environment, baseline_root=baseline_cache)
                    measured["verification"] = object_evidence(root, item, candidate, declarations, measured, units[item["unit"]])
                    result["measurement"] = measured
            except Exception as error:
                result["measurement"] = {"status": "generation_or_validation_error", "compile_ok": False,
                                         "error": f"{type(error).__name__}: {error}"}
            results.append(result)
            decompile.write_json(folder / "result.json", result)
            checkpoint("target_complete", current=item["symbol"])
        unchanged = (fingerprint(root, version) == provenance["snapshot_sha256"]
                     and decompile.tool_metadata()["fingerprint"] == metadata["fingerprint"]
                     and knowledge.digest(Path(validator.__file__).read_bytes()) == configuration["validator_sha256"]
                     and knowledge.digest(Path(__file__).read_bytes()) == configuration["producer_sha256"]
                     and knowledge.digest(Path(knowledge.__file__).read_bytes()) == configuration["retriever_sha256"]
                     and knowledge.digest(Path(c89.__file__).read_bytes()) == configuration["repair_sha256"]
                     and all(knowledge.digest(Path(path).read_bytes()) == sha for path, sha in configuration["attested_tools"].items()))
        if not unchanged:
            raise ValueError("Frozen workspace or m2c engine changed during the sweep")
        return checkpoint("complete", finished=True, environment_unchanged=True)
    except Exception as error:
        checkpoint("failed", finished=True, environment_unchanged=False, error=f"{type(error).__name__}: {error}")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=knowledge.DEFAULT_DATABASE)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--split", type=Path)
    selection.add_argument("--unmatched-snapshot", help="baseline every currently unmatched function in this frozen snapshot")
    parser.add_argument("--exclude-run", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--baseline-cache", type=Path,
                        help="reuse only hash-verified unchanged owner compilations; candidates always compile separately")
    parser.add_argument("--native-c", action="store_true",
                        help="explicit byte accesses and real draft declaration dependencies; never resolves unknown operations")
    args = parser.parse_args()
    with knowledge.KnowledgeStore(args.database, readonly=True) as store:
        split = unmatched_selection(store, args.unmatched_snapshot) if args.unmatched_snapshot else json.loads(args.split.read_text(encoding="utf-8"))
        summary = run(store, split, args.exclude_run, args.output.resolve(), baseline_cache=args.baseline_cache,
                      native_c=args.native_c)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
