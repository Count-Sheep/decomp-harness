"""Bounded, review-only native m2c variants driven by compiled object feedback."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import re
import shutil

import m2c_draft_attempts as attempts
import m2c_capture_inputs as capture
import m2c_generation_experiment as generation
import m2c_object_evidence as objects
import m2c_readonly_data as readonly
import m2c_template_index as templates
from m2c_failure_audit import digest
from m2c_repair_experiment import validate_owners


def variant_key(options: dict) -> tuple:
    return (bool(options.get("mwcc_fused_subtract")), tuple(options.get("reg_vars") or []),
            bool(options.get("ppc_stack_storage")), bool(options.get("heuristic_strings")))


def proposals(record: dict) -> list[dict]:
    options = record["options"]
    measurement = record.get("measurement") or {}
    if (record.get("stage", "native") == "native"
            and measurement.get("generation_returncode") == 0
            and not (measurement.get("outcome") or {}).get("compiled")):
        missing = [message for message in measurement.get("compiler_messages", [])
                   if re.fullmatch(r"undefined identifier 'sp[0-9A-Fa-f]+'", message)]
        unknown_stack = (
            "undefined identifier 'M2C_UNK'" in measurement.get("compiler_messages", [])
            and re.search(r"^#\s+\d+:\s+M2C_UNK\s+sp[0-9A-Fa-f]+\s*;\s*$",
                          (measurement.get("outcome") or {}).get("error") or "", re.MULTILINE)
        )
        variants = []
        if missing and not options.get("ppc_stack_storage"):
            variants.append({"options": {**options, "ppc_stack_storage": True},
                             "reason": {"kind": "missing_stack_declaration", "compiler_messages": missing}})
        elif unknown_stack and not options.get("ppc_stack_storage"):
            variants.append({"options": {**options, "ppc_stack_storage": True},
                             "reason": {"kind": "unknown_stack_storage",
                                        "compiler_messages": ["undefined identifier 'M2C_UNK'"]}})
        if "invalid_symbol_name" in measurement.get("categories", []) and not options.get("heuristic_strings"):
            variants.append({"options": {**options, "heuristic_strings": True},
                             "reason": {"kind": "anonymous_readonly_string_hypothesis"}})
        if variants:
            return variants
    outcome = measurement.get("outcome") or {}
    comparison = outcome.get("feedback") or {}
    if (outcome.get("compiled") and not options.get("ppc_stack_storage")
            and int(comparison.get("target_stack_frame") or 0) > 0
            and int(comparison.get("compiled_stack_frame") or 0) > 0
            and int(comparison.get("target_stack_frame")) > int(comparison.get("compiled_stack_frame"))):
        return [{"options": {**options, "ppc_stack_storage": True},
                 "reason": {"kind": "collapsed_stack_storage",
                            "target_stack_frame": comparison["target_stack_frame"],
                            "compiled_stack_frame": comparison["compiled_stack_frame"]}}]
    audit = record.get("object_audit") or {}
    feedback = audit.get("byte_feedback") or {}
    difference = feedback.get("first_difference") or {}
    before, after = difference.get("target_word", ""), difference.get("candidate_word", "")
    if audit.get("status") != "checked" or len(before) != 8 or len(after) != 8:
        return []
    try:
        target, candidate = int(before, 16), int(after, 16)
    except ValueError:
        return []
    destination_mask = 31 << 21
    target_reg, candidate_reg = (target >> 21) & 31, (candidate >> 21) & 31
    if (target >> 26 in (32, 34, 40, 42) and target & ~destination_mask == candidate & ~destination_mask
            and target_reg != candidate_reg and min(target_reg, candidate_reg) >= 14):
        return [{"options": {**options, "reg_vars": sorted(set(options.get("reg_vars") or []) | {f"r{reg}"})},
                 "reason": {"kind": "preserved_load_register", "offset": difference["offset"],
                            "target_word": before, "candidate_word": after}}
                for reg in (target_reg, candidate_reg) if f"r{reg}" not in (options.get("reg_vars") or [])]
    # Near-exact leaf functions often differ only because m2c folds two loads
    # into one expression.  Preserve a bounded set of implicated temporaries;
    # r0 is excluded because m2c also models LR saves through that register.
    differences = feedback.get("differences") or []
    if (outcome.get("compiled") and float(outcome.get("pct") or 0) >= 98.0
            and feedback.get("target_bytes") == feedback.get("candidate_bytes")
            and 1 <= int(feedback.get("different_words") or 0) <= 4):
        registers = []
        for difference_row in differences:
            try:
                wanted = int(difference_row["target_word"], 16)
                actual = int(difference_row["candidate_word"], 16)
            except (KeyError, TypeError, ValueError):
                continue
            if (wanted >> 26 not in (32, 34, 40, 42)
                    or wanted & ~destination_mask != actual & ~destination_mask):
                continue
            for register in ((wanted >> 21) & 31, (actual >> 21) & 31):
                name = f"r{register}"
                if register != 0 and name not in registers and name not in (options.get("reg_vars") or []):
                    registers.append(name)
        if registers:
            return [{"options": {**options, "reg_vars": sorted(set(options.get("reg_vars") or []) | {name})},
                     "reason": {"kind": "near_exact_load_temporary", "different_words": len(differences),
                                "register": name}}
                    for name in registers]
    if target >> 26 not in (59, 63) or target >> 26 != candidate >> 26:
        return []
    target_op, candidate_op = (target >> 1) & 31, (candidate >> 1) & 31
    reason = {"offset": difference["offset"], "target_word": before, "candidate_word": after}
    if target_op == 30 and candidate_op == 28 and target ^ candidate == 4:
        if not options.get("mwcc_fused_subtract"):
            return [{"options": {**options, "mwcc_fused_subtract": True},
                     "reason": {**reason, "kind": "negative_fused_subtraction"}}]
    operand_mask = (31 << 16) | (31 << 6)
    if (target_op == candidate_op and target_op in (28, 29, 30, 31)
            and target & ~operand_mask == candidate & ~operand_mask
            and (target >> 16) & 31 == (candidate >> 6) & 31
            and (target >> 6) & 31 == (candidate >> 16) & 31):
        result = []
        for register in ((target >> 16) & 31, (target >> 6) & 31):
            registers = sorted(set(options.get("reg_vars") or []) | {f"f{register}"})
            if tuple(registers) != tuple(options.get("reg_vars") or []):
                result.append({"options": {**options, "reg_vars": registers},
                               "reason": {**reason, "kind": "fused_multiply_operand_order"}})
        return result
    return []


def rank(record: dict) -> tuple:
    outcome = record["measurement"]["outcome"]
    audit = record.get("object_audit") or {}
    feedback = audit.get("byte_feedback") or {}
    return (bool(audit.get("scratch_object_equal")),
            bool(outcome.get("compiled") and outcome.get("profile") is not None),
            float(outcome.get("pct") or 0),
            -abs(feedback.get("target_bytes", 0) - feedback.get("candidate_bytes", 0)),
            -feedback.get("different_words", 10**9))


def search(evaluate, max_variants: int) -> dict:
    if not 0 <= max_variants <= 8:
        raise ValueError("Variant budget must be between zero and eight")
    baseline = evaluate({"mwcc_fused_subtract": False, "reg_vars": [], "ppc_stack_storage": False,
                         "heuristic_strings": False},
                        {"kind": "baseline"}, 0)
    records, best = [baseline], baseline
    seen = {variant_key(baseline["options"])}
    while len(records) <= max_variants and not best.get("object_audit", {}).get("scratch_object_equal"):
        # A failed variant may expose the next repair even when its score ties.
        proposal = next((item for origin in [best, *reversed(records)] for item in proposals(origin)
                         if variant_key(item["options"]) not in seen), None)
        if proposal is None:
            break
        seen.add(variant_key(proposal["options"]))
        current = evaluate(proposal["options"], proposal["reason"], len(records))
        records.append(current)
        if rank(current) > rank(best):
            best = current
    return {"attempts": records, "best_attempt": records.index(best),
            "improved": rank(best) > rank(baseline), "model_calls": 0, "promoted": False,
            "linked": False, "variant_budget": max_variants}


def route(candidates: list[dict], evaluate_template, evaluate_native, max_variants: int) -> dict:
    if not 0 <= max_variants <= 8:
        raise ValueError("Variant budget must be between zero and eight")
    records = []
    for index, candidate in enumerate(candidates):
        current = evaluate_template(candidate, index)
        records.append(current)
        if current.get("object_audit", {}).get("scratch_object_equal"):
            return {"attempts": records, "best_attempt": index, "improved": None,
                    "model_calls": 0, "promoted": False, "linked": False,
                    "variant_budget": max_variants, "route": "template",
                    "template_attempts": len(records), "native_generations": 0,
                    "native_generation_skipped": True}
    native = search(evaluate_native, max_variants)
    baseline = len(records)
    records.extend(native["attempts"])
    best = max(range(len(records)), key=lambda index: rank(records[index]))
    return {**native, "attempts": records, "best_attempt": best,
            "improved": rank(records[best]) > rank(records[baseline]),
            "route": "template" if best < baseline else "native",
            "template_attempts": baseline, "native_generations": len(native["attempts"]),
            "native_generation_skipped": False}


def run(snapshot: Path, engine: Path, output: Path, symbols: list[str], max_variants: int = 3, *,
        template_index: Path | None = None, max_templates: int = 3) -> dict:
    if not 0 <= max_variants <= 8 or not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("Select distinct symbols and a variant budget between zero and eight")
    if not 0 <= max_templates <= 5:
        raise ValueError("Template budget must be between zero and five")
    raw = snapshot.read_bytes()
    frozen = json.loads(raw)
    if frozen.get("capture_kind") != "native_m2c_batch_inputs":
        raise ValueError("Native search requires hash-bound captured inputs")
    root, version = Path(frozen["root"]), frozen["version"]
    rows = [row for row in frozen["rows"] if row["symbol"] in symbols]
    if len(rows) != len(symbols) or {row["symbol"] for row in rows} != set(symbols):
        raise ValueError("Missing or ambiguous selected symbol")
    template_raw = template_index.read_bytes() if template_index else None
    template_data = json.loads(template_raw) if template_raw is not None else None
    modules = [Path(module.__file__) for module in (attempts, capture, generation, objects, readonly, templates)]
    modules.append(Path(__file__))
    harness_hashes = {str(path): digest(path.read_bytes()) for path in modules}
    engine_hash = generation.tool_fingerprint(engine)

    def verify():
        if snapshot.read_bytes() != raw:
            raise ValueError("Captured snapshot changed")
        capture.verify_snapshot(frozen)
        if template_index and template_index.read_bytes() != template_raw:
            raise ValueError("Template index changed during search")
        if engine_hash != generation.tool_fingerprint(engine):
            raise ValueError("Native m2c changed during search")
        if any(digest(Path(path).read_bytes()) != expected for path, expected in harness_hashes.items()):
            raise ValueError("Harness changed during search")

    verify()
    validate_owners(attempts.read_json(root / "build" / version / "report.json"), rows)
    for row in rows:
        if not (row.get("compiler_profile") or {}).get("strict_selection_required"):
            raise ValueError("Native search requires strict compiler profiles")
        attempts.compiler_choices(root, row)
    expected = {"snapshot_sha256": digest(raw), "engine_sha256": engine_hash,
                "harness_sha256": harness_hashes,
                "template_index_sha256": digest(template_raw) if template_raw is not None else None,
                "max_templates": max_templates, "symbols": symbols, "max_variants": max_variants}
    saved = output / "results.json"
    if saved.is_file():
        report = json.loads(saved.read_text(encoding="utf-8"))
        if any(report.get(key) != value for key, value in expected.items()):
            raise ValueError("Existing native search belongs to different inputs")
        if report.get("finished_at"):
            return {"functions": len(rows), "improved": sum(bool(row["improved"]) for row in report["results"]),
                    "native_generation_skipped": sum(row["native_generation_skipped"] for row in report["results"]),
                    "scratch_exact": sum(row["attempts"][row["best_attempt"]]["object_audit"].get("scratch_object_equal", False)
                                         for row in report["results"]), "model_calls": 0}
    elif output.exists():
        raise ValueError("Native search output exists without a recovery record")
    else:
        output.mkdir(parents=True)
        report = {"started_at": datetime.now(UTC).isoformat(), **expected,
                  "model_calls": 0, "repair_enabled": False, "promoted": False, "results": []}
        attempts.write_json(saved, report)
    completed = {row["symbol"] for row in report["results"]}
    for index, row in enumerate(rows):
        if row["symbol"] in completed:
            continue
        interrupted = output / f"{index:05d}"
        if interrupted.exists():
            shutil.rmtree(interrupted)
        target = attempts.target_object(root, version, row)
        assembly, readonly_info = readonly.capture(target, row["symbol"], row["assembly"].encode())
        inputs = {**row, "assembly": assembly.decode()}

        def finish(folder, generated, options, reason, stage):
            measured = generation.measure(root, version, row, generated, folder)
            outcome = measured["outcome"]
            audit = {"status": "unavailable", "scratch_object_equal": False}
            if outcome.get("compiled") and outcome.get("profile") is not None:
                profile = outcome.get("profile_artifact") or outcome["profile"]
                obj = folder / "score" / f"{profile}.o"
                if digest(obj.read_bytes()) != measured["compiled_object_sha256"]:
                    raise ValueError("Compiled object changed before audit")
                audit = objects.compare(target, obj, row["symbol"])
            verify()
            record = {"options": options, "reason": reason, "measurement": measured,
                      "object_audit": audit, "artifact": str(folder), "stage": stage}
            attempts.write_json(folder / "result.json", record)
            return record

        def evaluate(options, reason, attempt_index):
            verify()
            folder = output / f"{index:05d}" / f"{attempt_index:02d}"
            generated = generation.generate(engine, inputs, folder, context_decls=True,
                infer_unknowns=True, deterministic_vars=True,
                mwcc_fused_subtract=options["mwcc_fused_subtract"], reg_vars=tuple(options["reg_vars"]),
                ppc_stack_storage=bool(options.get("ppc_stack_storage")),
                heuristic_strings=bool(options.get("heuristic_strings")))
            return finish(folder, generated, options, reason, "native")

        def evaluate_template(candidate, attempt_index):
            verify()
            folder = output / f"{index:05d}" / f"template-{attempt_index:02d}"
            folder.mkdir(parents=True, exist_ok=False)
            (folder / "target.s").write_text(inputs["assembly"], encoding="utf-8", newline="\n")
            (folder / "context.c").write_text(inputs["context"], encoding="utf-8", newline="\n")
            (folder / "draft.c").write_text(candidate["code"], encoding="utf-8", newline="\n")
            attempts.write_json(folder / "template.json", {key: value for key, value in candidate.items() if key != "code"})
            generated = {"returncode": 0, "draft": candidate["code"], "error": ""}
            return finish(folder, generated, {}, {"kind": "identical_instruction_template",
                          "reference": candidate["reference"], "provenance": candidate["provenance"]}, "template")

        candidates = templates.direct_drafts(template_data, row["assembly"], row["symbol"], max_templates,
                                             include_same_symbol=True) if template_data is not None else []
        result = route(candidates, evaluate_template, evaluate, max_variants)
        result.update(symbol=row["symbol"], readonly_capture=readonly_info)
        report["results"].append(result)
        attempts.write_json(output / "results.json", report)
        print(f"{row['symbol']}: {len(result['attempts'])} attempts, best {result['best_attempt']} ({result['route']})", flush=True)
    verify()
    report["finished_at"] = datetime.now(UTC).isoformat()
    attempts.write_json(output / "results.json", report)
    return {"functions": len(rows), "improved": sum(bool(row["improved"]) for row in report["results"]),
            "native_generation_skipped": sum(row["native_generation_skipped"] for row in report["results"]),
            "scratch_exact": sum(row["attempts"][row["best_attempt"]]["object_audit"].get("scratch_object_equal", False)
                                 for row in report["results"]), "model_calls": 0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--engine", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--symbol", required=True, action="append")
    parser.add_argument("--max-variants", type=int, default=3)
    parser.add_argument("--templates", type=Path)
    parser.add_argument("--max-templates", type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(run(args.snapshot.resolve(), args.engine.resolve(), args.output.resolve(),
                         args.symbol, args.max_variants, template_index=args.templates.resolve() if args.templates else None,
                         max_templates=args.max_templates), indent=2))


if __name__ == "__main__":
    main()
