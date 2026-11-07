"""Compile contract-compatible matched C seeds; similarity is never a match certificate."""

from __future__ import annotations

import argparse
from collections import Counter
import copy
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys

import local_campaign as campaign
import local_campaign_c89 as c89
import local_campaign_decompile as decompile
import local_campaign_decompile_validate as validator
import local_campaign_knowledge as knowledge
from local_campaign_coordination import Coordinator
from local_campaign_decompile_batch import command_map
from local_campaign_decompile_snapshot import fingerprint
from local_campaign_knowledge_ablation import declaration_context, candidate_declarations as reference_declarations
from local_campaign_recovery import object_evidence, rank

WHOLE_FUNCTION_REASONS = {"IDENTICAL_TARGET_BYTES", "IDENTICAL_INSTRUCTION_TEXT", "STRUCTURAL_ANALOGUE"}


def adapt_reference(source: str, reference_symbol: str, symbol: str, signature: str, declarations: str) -> str:
    """Alpha-rename a body only after identical parsed return/parameter contracts."""
    parser_root = str(decompile.tool().parent)
    if parser_root not in sys.path:
        sys.path.insert(0, parser_root)
    from m2c import c_types
    from m2c.error import DecompFailure
    from m2c_pycparser import c_ast as ca
    from m2c_pycparser.c_generator import CGenerator

    try:
        ast, _ = c_types.parse_c(c_types.strip_comments(declarations + "\n" + source), None)
        contract, _ = c_types.parse_c(c_types.strip_comments(declarations + "\n" + signature + ";"), None)
    except DecompFailure as error:
        raise ValueError("Reference context cannot be parsed: " + str(error)) from error
    definitions = [node for node in ast.ext if isinstance(node, ca.FuncDef)]
    required = [node for node in contract.ext if isinstance(node, ca.Decl) and node.name == symbol]
    if len(definitions) != 1 or definitions[0].decl.name != reference_symbol or not required:
        raise ValueError("Expected exactly one reference definition and the target contract")
    reference, target = definitions[0], required[-1]
    generator = CGenerator()

    def canonical_type(declaration):
        node = copy.deepcopy(declaration.type)

        def unbind(current):
            if isinstance(current, ca.TypeDecl):
                current.declname = None
                return
            if isinstance(current, ca.FuncDecl) and current.args:
                for parameter in current.args.params:
                    if isinstance(parameter, (ca.Decl, ca.Typename)):
                        if isinstance(parameter, ca.Decl):
                            parameter.name = None
                        unbind(parameter.type)
            if hasattr(current, "type"):
                unbind(current.type)

        unbind(node)
        return generator.visit(node)

    if canonical_type(reference.decl) != canonical_type(target):
        raise ValueError("Reference return/parameter types differ from the target contract")
    left = reference.decl.type.args.params if reference.decl.type.args else []
    right = target.type.args.params if target.type.args else []
    renames = {reference_symbol: symbol}
    for old, new in zip(left, right):
        if isinstance(old, ca.Decl) and isinstance(new, ca.Decl):
            if not old.name or not new.name:
                raise ValueError("Unnamed formal parameters cannot be safely adapted")
            if old.name == reference_symbol:
                raise ValueError("Reference function name is shadowed by a formal parameter")
            renames[old.name] = new.name
    if len(set(renames.values())) != len(renames):
        raise ValueError("Renaming creates duplicate identifier destinations")
    local_names, identifiers = set(), set()

    class Inspect(ca.NodeVisitor):
        def visit_Decl(self, node):
            if node.name:
                local_names.add(node.name)
            self.generic_visit(node)

        def visit_ID(self, node):
            identifiers.add(node.name)

        def visit_StructRef(self, node):
            self.visit(node.name)

    Inspect().visit(reference.body)
    if local_names & set(renames):
        raise ValueError("Reference shadows a renamed identifier")
    destinations = {new for old, new in renames.items() if old != new}
    if destinations & (local_names | (identifiers - set(renames))):
        raise ValueError("Renamed identifiers would capture a local or global name")

    class Rename(ca.NodeVisitor):
        def visit_ID(self, node):
            node.name = renames.get(node.name, node.name)

        def visit_StructRef(self, node):
            self.visit(node.name)

    Rename().visit(reference.body)
    return signature + "\n" + generator.visit(reference.body) + "\n"


def run(prior: Path, output: Path, *, full_references: bool = False) -> dict:
    data = json.loads((prior / "results.json").read_text(encoding="utf-8"))
    inputs = json.loads((prior / "inputs.json").read_text(encoding="utf-8"))
    summary, config = data["summary"], inputs["configuration"]
    if (not summary.get("finished") or not summary.get("environment_unchanged")
            or config.get("mode") != "actual_unmatched_project_m2c_baseline"
            or knowledge.digest(knowledge.encoded(config)) != summary.get("configuration_sha256")):
        raise ValueError("Reference reuse requires a complete, unchanged actual unmatched baseline")
    root = campaign.ROOT
    if fingerprint(root) != config["workspace_sha256"]:
        raise ValueError("Use the unchanged baseline workspace")
    if output.exists():
        raise ValueError("Use a new reference-draft output directory to preserve history")
    with knowledge.KnowledgeStore(knowledge.DEFAULT_DATABASE, readonly=True) as store:
        provenance = json.loads(store.connection.execute("SELECT provenance_json FROM snapshots WHERE id=?",
                                                        (config["snapshot_id"],)).fetchone()[0])
    if Path(provenance["workspace"]).resolve() != root.resolve():
        raise ValueError("Knowledge snapshot belongs to a different workspace")
    commands = command_map(subprocess.run(["ninja", "-t", "commands", "all_source"], cwd=root,
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            check=True, timeout=60).stdout)
    units = {unit["name"]: unit for unit in json.loads((root / "objdiff.json").read_text(encoding="utf-8"))["units"]}
    environment = validator.environment_fingerprint(root, commands)
    if environment != config["environment_sha256"]:
        raise ValueError("Baseline compiler environment changed")
    selection = json.loads((prior / "selection.json").read_text(encoding="utf-8"))
    if ([row["target_id"] for row in inputs["inputs"]] != config["targets"]
            or selection["targets"] != config["targets"] or selection["configuration"] != config):
        raise ValueError("Baseline target selection changed")
    work = list(inputs["inputs"])
    work.extend({**row, "target_id": row["id"], "preparation_error": "No immutable warehouse target"}
                for row in data["results"] if row.get("measurement", {}).get("status") == "missing_warehouse_target")
    if len(work) != summary["total"]:
        raise ValueError("Reference reuse must retain the entire unmatched cohort")
    coordinator = Coordinator(Path(provenance["source_root"]) / "build/local_llm_campaign")
    baseline_cache = Path(config.get("baseline_cache_root", prior / "baselines"))
    attested = {str(Path(module.__file__).resolve()): knowledge.digest(Path(module.__file__).read_bytes())
                for module in (campaign, c89, decompile, validator, knowledge, sys.modules[object_evidence.__module__],
                               sys.modules[declaration_context.__module__])}
    attested[str(Path(__file__).resolve())] = knowledge.digest(Path(__file__).read_bytes())
    bindings = {str(prior / name): knowledge.digest((prior / name).read_bytes())
                for name in ("results.json", "inputs.json", "selection.json", "cohort.json")}
    packs = {}
    if full_references:
        with knowledge.KnowledgeStore(knowledge.DEFAULT_DATABASE, readonly=True) as store:
            for item in inputs["inputs"]:
                packs[item["target_id"]] = knowledge.full_source_pack(store, item["target_id"], set(selection["excluded"]))
                print(json.dumps({"phase": "capturing_references", "captured": len(packs),
                                  "total": len(inputs["inputs"])}), flush=True)
    configuration = {"schema_version": 1, "mode": "actual_unmatched_reference_C_seeds",
                     "prior": str(prior.resolve()), "snapshot_id": config["snapshot_id"],
                     "workspace_sha256": config["workspace_sha256"], "environment_sha256": environment,
                     "prior_configuration_sha256": summary["configuration_sha256"],
                     "parser_tool": decompile.tool_metadata(),
                     "attested_tools": attested, "bound_inputs": bindings,
                     "reference_source": "complete_production_top_12" if full_references else "frozen_baseline_prompt_packs",
                     "retrieval_version": knowledge.RETRIEVAL_VERSION,
                     "captured_reference_sha256": {key: knowledge.digest(knowledge.encoded(pack)) for key, pack in packs.items()},
                     "captured_reference_coverage": sum(bool(pack["references"]) for pack in packs.values()),
                     "captured_whole_function_coverage": sum(any(ref["reason"] in WHOLE_FUNCTION_REASONS
                         for ref in pack["references"]) for pack in packs.values()),
                     "selection": "all_baseline_targets_strong_whole_function_references_only",
                     "model_requests": 0, "ready": 0, "promoted": 0}
    output.mkdir(parents=True, mode=0o700)
    for key, pack in packs.items():
        (output / key).mkdir()
        decompile.write_json(output / key / "references.json", pack)
    decompile.write_json(output / "configuration.json", configuration)
    config_hash = knowledge.digest(knowledge.encoded(configuration))
    results = []

    def checkpoint(phase, finished=False, **detail):
        bests = [row.get("best", {}) for row in results]
        state = {**configuration, "configuration_sha256": config_hash, "phase": phase, "finished": finished,
                 "processed": len(results), "total": len(work), "updated_at": datetime.now(UTC).isoformat(),
                 "counts": dict(Counter(row.get("status", "missing") for row in bests)),
                 "compiled": sum(bool(row.get("compile_ok")) for row in bests),
                 "scorer_exact": sum(row.get("pct") == 100 for row in bests),
                 "object_verified": sum(all(row.get("verification", {}).get(axis) is True for axis in
                      ("raw_candidate_bytes", "relocations", "owner_object")) for row in bests),
                 "actual_unmatched_scorer_gains": sum(row.get("best", {}).get("pct") == 100
                     and row.get("baseline_measurement", {}).get("compile_ok") is True
                     and isinstance(row["baseline_measurement"].get("pct"), (int, float))
                     and row["baseline_measurement"]["pct"] < 100 for row in results), **detail}
        decompile.write_json(output / "manifest.json", state)
        decompile.write_json(output / "results.json", {"summary": state, "results": results})
        print(json.dumps({key: state[key] for key in ("phase", "processed", "total", "compiled", "scorer_exact")}), flush=True)
        return state

    checkpoint("preparing")
    try:
        for item in work:
            result = {**item, "attempts": []}
            best = {"status": "no_whole_function_reference", "compile_ok": False}
            folder = output / item["target_id"]
            folder.mkdir(exist_ok=True)
            if item.get("preparation_error"):
                best["status"] = "preparation_error"
            else:
                source = prior / item["target_id"]
                declarations = (source / "declarations.c").read_text(encoding="utf-8")
                pack = (source / "references.json").read_bytes()
                pack = json.loads(pack)
                if (knowledge.digest(declarations) != item["declarations_sha256"]
                        or knowledge.digest(knowledge.encoded(pack)) != item["references_sha256"]
                        or pack["target_id"] != item["target_id"]
                        or [reference["function_id"] for reference in pack["references"]] != item["reference_ids"]):
                    raise ValueError("Frozen reference inputs changed")
                if full_references:
                    pack = json.loads((folder / "references.json").read_text(encoding="utf-8"))
                    if (knowledge.digest(knowledge.encoded(pack)) != configuration["captured_reference_sha256"][item["target_id"]]
                            or pack["target_id"] != item["target_id"]):
                        raise ValueError("Captured production references changed")
                    result.update(prior_reference_ids=item["reference_ids"],
                                  reference_ids=[ref["function_id"] for ref in pack["references"]])
                for reference in pack["references"]:
                    if reference["reason"] not in WHOLE_FUNCTION_REASONS:
                        continue
                    if (reference["function_id"] == item["target_id"]
                            or reference["function_id"] in selection["excluded"]
                            or knowledge.digest(reference["source_text"]) != reference["source_sha256"]):
                        raise ValueError("Unbound reference source or target source in the pack")
                    attempt = folder / ("reference-" + str(len(result["attempts"])))
                    attempt.mkdir()
                    measured = {"status": "reference_contract_error", "compile_ok": False,
                                "reference_id": reference["function_id"], "reference_source_sha256": reference["source_sha256"]}
                    try:
                        context = decompile.preprocess(root, commands[item["source"]], item["source"])
                        if context["status"] != "ok":
                            raise ValueError("Cannot preprocess reference contract dependencies")
                        enriched = reference_declarations(context["context"], reference["source_text"], item["symbol"])
                        (attempt / "declarations.c").write_text(enriched, encoding="utf-8", newline="\n")
                        measured["reference_declarations_sha256"] = knowledge.digest(enriched)
                        candidate = adapt_reference(reference["source_text"], reference["symbol"], item["symbol"],
                                                    item["signature"], enriched)
                        candidate, count = c89.repair_declarations(candidate, enriched, item["symbol"])
                        (attempt / "draft.c").write_text(candidate, encoding="utf-8", newline="\n")
                        with coordinator.build("Matched C seed", "MWCC/objdiff: " + item["symbol"]):
                            measured.update(validator.validate_one(root, {**item, "id": item["target_id"], "status": "ok",
                                "artifacts": str(attempt.resolve())}, commands[item["source"]], units[item["unit"]], "",
                                attempt / "validation", environment=environment, baseline_root=baseline_cache))
                        measured.update(c89_repairs=count, candidate_path=str((attempt / "draft.c").resolve()),
                                        verification=object_evidence(root, item, candidate, enriched, measured, units[item["unit"]]))
                    except Exception as error:
                        measured["error"] = f"{type(error).__name__}: {error}"
                    decompile.write_json(attempt / "result.json", measured)
                    result["attempts"].append(measured)
                    if len(result["attempts"]) == 1 or rank(measured) > rank(best):
                        best = measured
                    if best.get("pct") == 100:
                        break
            result["best"] = best
            results.append(result)
            decompile.write_json(folder / "result.json", result)
            checkpoint("target_complete", current=item["symbol"])
        if (fingerprint(root) != config["workspace_sha256"]
                or decompile.tool_metadata()["fingerprint"] != configuration["parser_tool"]["fingerprint"]
                or any(knowledge.digest(knowledge.encoded(json.loads((output / key / "references.json").read_text(encoding="utf-8")))) != sha
                       for key, sha in configuration["captured_reference_sha256"].items())
                or any(knowledge.digest(Path(path).read_bytes()) != sha for path, sha in {**attested, **bindings}.items())):
            raise ValueError("Reference inputs, compiler workspace or producer changed")
        return checkpoint("complete", finished=True, environment_unchanged=True)
    except Exception as error:
        checkpoint("failed", finished=True, environment_unchanged=False, error=f"{type(error).__name__}: {error}")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--full-references", action="store_true",
                        help="capture complete top-12 production references, allowing matched clones; not a blind holdout")
    args = parser.parse_args()
    print(json.dumps(run(args.baseline.resolve(), args.output.resolve(), full_references=args.full_references), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
