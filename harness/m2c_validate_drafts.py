#!/usr/bin/env python3
"""Scratch MWCC/objdiff validation for a complete m2c batch of the selected project."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import m2c_draft_attempts as attempts
import m2c_context as m2c_context
import local_campaign_m2c_report as m2c_report
import m2c_artifact_retention
import m2c_current_source
import m2c_refinement_pipeline
from harness_paths import DECOMP_ROOT, VERSION, bind_project_tools, build_tool, compiler_wrapper
from m2c_context import build_check


def linked_symbols(root: Path, version: str) -> set[str]:
    spec = m2c_context.ProjectSpec(m2c_context.target_project_id(root, version), root, version)
    return {row["symbol"] for row in m2c_context._report_exact_rows(spec)}


def fingerprint(root: Path, version: str, batch: Path, rows: list[dict]) -> str:
    wrapper = [Path(part) for part in compiler_wrapper(root)[:1] if Path(part).is_file()]
    paths = {batch / "results.json", root / "config" / version / "build.sha1",
             root / "build" / version / "main.dol", *wrapper,
             build_tool(root, "objdiff-cli"),
             Path(__file__), Path(attempts.__file__), Path(attempts.compile_repair.__file__),
             Path(m2c_current_source.__file__), Path(m2c_refinement_pipeline.__file__),
             Path(m2c_refinement_pipeline.m2c_source_search.__file__),
             Path(m2c_refinement_pipeline.m2c_source_search.transforms.__file__),
             Path(m2c_refinement_pipeline.m2c_source_search.transforms.permute.__file__),
             Path(m2c_refinement_pipeline.m2c_source_search.transforms.rewrite.__file__),
             Path(m2c_refinement_pipeline.m2c_source_search.validator.__file__),
             Path(m2c_refinement_pipeline.m2c_source_search.validator.campaign.__file__),
             Path(m2c_refinement_pipeline.m2c_unit_autogate.__file__),
             Path(m2c_refinement_pipeline.m2c_unit_autogate.m2c_unit_pipeline.__file__)}
    paths.update(root / "build/compilers/GC" / profile / "mwcceppc.exe" for profile in attempts.PROFILES)
    paths.update(attempts.target_object(root, version, row) for row in rows)
    paths.update(m2c_current_source.input_paths(root, version, rows))
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.resolve()).encode())
        with path.open("rb") as handle:
            digest.update(hashlib.file_digest(handle, "sha256").digest())
    return digest.hexdigest()


def validate_one(root: Path, version: str, row: dict, folder: Path, environment: str,
                 linked: set[str], current_index: m2c_current_source.Index | None = None) -> dict:
    record = {key: row.get(key) for key in ("id", "symbol", "unit")}
    scope = ("standalone_inferred_profile" if (row.get("compiler_profile") or {}).get("selected")
             else "standalone_fallback_flags")
    record.update(draft_status=row.get("status"), scope=scope,
                  review_eligible=False, promoted=False, tested_at=datetime.now(UTC).isoformat())
    if row.get("symbol") in linked:
        return {**record, "status": "linked_elsewhere", "linked": True, "compile_ok": False,
                "instruction_exact": False, "pct": None, "profile": None, "diff": "",
                "error": "Function is now source-backed; historical draft scoring skipped"}
    if row.get("status") == "exact_reuse" or row.get("exact_reuse"):
        return {**record, "status": "exact_reuse_available", "linked": False, "compile_ok": False,
                "instruction_exact": False, "pct": None, "profile": None, "diff": "",
                "scope": "retail_byte_identity",
                "exact_reuse": row.get("exact_reuse"),
                "error": "Byte-identical verified source exists in another project; m2c bypassed pending import"}
    draft_path = Path(row.get("artifacts") or "") / "draft.c"
    candidates = []
    draft_hash = None
    if row.get("status") == "ok" and draft_path.is_file():
        draft_bytes = draft_path.read_bytes()
        draft_hash = hashlib.sha256(draft_bytes).hexdigest()
        record["draft_sha256"] = draft_hash
        candidates.append(("m2c", row, draft_bytes.decode("utf-8"), True))
    record["input_sha256"] = hashlib.sha256((environment + (draft_hash or "")).encode()).hexdigest()
    for index, reference in enumerate(row.get("references") or []):
        comparison = reference.get("code_comparison") or {}
        snippet = reference.get("source_snippet")
        compiler = reference.get("compiler")
        reference_symbol = reference.get("symbol")
        if (comparison.get("status") != "exact_bytes" or not snippet or not compiler
                or not reference_symbol):
            continue
        candidate = re.sub(rf"\b{re.escape(str(reference_symbol))}\b", row["symbol"], snippet)
        macros = "\n".join(reference.get("required_macros") or [])
        declarations = "\n".join(reference.get("required_declarations") or [])
        if declarations:
            candidate = declarations + "\n" + candidate
        if macros:
            candidate = macros + "\n" + candidate
        candidate_row = {**row, "compiler_profile": {
            "method": "code_verified_reference", "confidence": "code_verified_reference",
            "fallback": False, "selected": compiler, "profiles": [compiler],
        }}
        candidates.append((f"code_verified_reference_{index}", candidate_row, candidate, False))
    outcomes = []
    candidate_sources = {}
    try:
        current = m2c_current_source.measure(root, version, row, current_index)
        if current is not None:
            outcomes.append(("current_source", current))
            candidate_sources["current_source"] = current["candidate"]
    except (OSError, ValueError, RuntimeError) as error:
        outcomes.append(("current_source", {"compiled": False, "profile": None, "pct": 0.0,
                                             "error": str(error), "diff": "", "validation_error": True}))
    if not candidates and not outcomes:
        return {**record, "status": "missing_draft", "compile_ok": False,
                "error": row.get("error") or "No m2c, reference, or current-source candidate"}
    for origin, candidate_row, code, apply_repair in candidates:
        candidate_sources[origin] = code
        candidate_folder = folder / origin if len(candidates) > 1 else folder
        try:
            outcomes.append((origin, attempts.score(root, version, candidate_row, code,
                                                    candidate_folder, apply_repair=apply_repair)))
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            outcomes.append((origin, {"compiled": False, "profile": None, "pct": 0.0,
                                      "error": str(error), "diff": "", "validation_error": True}))
    origin, outcome = max(outcomes, key=lambda item: (
        float(item[1].get("pct") or 0), bool(item[1].get("compiled")),
        not bool(item[1].get("validation_error")),
        item[0] == "current_source", item[0] == "m2c"
    ))
    status = ("validation_error" if outcome.get("validation_error") else
              "compile_error" if not outcome["compiled"] else
              "objdiff_error" if outcome["profile"] is None else
              "scorer_exact" if outcome["pct"] == 100 else "non_exact")
    feedback = outcome.get("feedback") or {}
    outcome["instruction_exact"] = bool(
        status == "scorer_exact" and not outcome["diff"] and feedback.get("exact")
        and feedback.get("target_count", 0) > 0 and feedback.get("compiled_count", 0) > 0)
    if status not in {"scorer_exact", "non_exact"}:
        outcome["pct"] = None
    winner = outcome.pop("candidate", None) or candidate_sources.get(origin)
    if winner is not None:
        folder.mkdir(parents=True, exist_ok=True)
        winner_path = folder / "winner.c"
        winner_path.write_text(winner.rstrip() + "\n", encoding="utf-8")
        outcome["winner_path"] = str(winner_path)
        outcome["winner_sha256"] = hashlib.sha256(winner_path.read_bytes()).hexdigest()
    variants = [{"origin": candidate_origin, "compiled": candidate_outcome.get("compiled", False),
                 "pct": candidate_outcome.get("pct"), "profile": candidate_outcome.get("profile"),
                 "scoring_scope": candidate_outcome.get("scoring_scope", "standalone_candidate"),
                 "scratch_object_equal": bool((candidate_outcome.get("object_audit") or {}).get("scratch_object_equal")),
                 "instruction_exact": bool((candidate_outcome.get("feedback") or {}).get("exact")
                                           and not candidate_outcome.get("diff"))}
                for candidate_origin, candidate_outcome in outcomes]
    return {**record, **outcome, "compile_ok": outcome["compiled"], "status": status,
            "candidate_origin": origin, "candidate_variants": variants}


def draft_sha256(row: dict) -> str | None:
    draft = Path(row.get("artifacts") or "") / "draft.c"
    return hashlib.sha256(draft.read_bytes()).hexdigest() if draft.is_file() else None


def compact_validation_item(item: Path) -> int:
    """Retain the hashed winner/result while dropping reproducible compiler scratch."""
    if not (item / "result.json").is_file():
        return 0
    removed = 0
    for child in item.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
            removed += 1
    return removed


def cached_result(item: Path, row: dict, environment: str, force: bool,
                  seed_validation: Path | None = None) -> dict | None:
    """Load a current result, or explicitly migrate one from a compatible interrupted pass."""
    draft_hash = draft_sha256(row)
    expected = hashlib.sha256((environment + (draft_hash or "")).encode()).hexdigest()
    saved = attempts.read_json(item / "result.json")
    if saved.get("input_sha256") == expected and not force:
        compact_validation_item(item)
        return saved
    if force or seed_validation is None:
        return None
    source_item = seed_validation / f"{row['address']:08X}"
    seeded = attempts.read_json(source_item / "result.json")
    identity = {key: row.get(key) for key in ("id", "symbol", "unit")}
    if (not seeded or any(seeded.get(key) != value for key, value in identity.items())
            or seeded.get("draft_sha256") != draft_hash):
        return None
    if item.exists():
        shutil.rmtree(item)
    shutil.copytree(source_item, item)
    winner = seeded.get("winner_path")
    if winner:
        try:
            relative = Path(winner).resolve().relative_to(source_item.resolve())
            seeded["winner_path"] = str(item / relative)
        except ValueError:
            return None
    seeded["input_sha256"] = expected
    seeded["reused_from_validation"] = str(seed_validation.resolve())
    attempts.write_json(item / "result.json", seeded)
    compact_validation_item(item)
    return seeded


def run(root: Path, version: str, batch: Path, force: bool = False,
        prune_superseded: bool = True, refinement_limit: int = 12,
        source_variants: int = 48, native_variants: int = 3, jobs: int = 1,
        seed_validation: Path | None = None, auto_promote: bool = False) -> dict:
    root, batch = root.resolve(), batch.resolve()
    bind_project_tools(root, prefer=True)
    if build_check(root, version)["status"] != "exact":
        raise ValueError(f"{version}: byte-exact DOL build required")
    data = attempts.read_json(batch / "results.json")
    if not (data.get("summary") or {}).get("finished"):
        raise ValueError("m2c batch is not complete")
    rows = sorted(data["results"], key=lambda row: (row["unit"], row["address"]))
    environment = fingerprint(root, version, batch, rows)
    support = root / "build/decomp-support" / version
    linked = linked_symbols(root, version)
    current_index = m2c_current_source.Index(root, version)
    folder = support / "validation" / f"{batch.name}-{environment[:12]}"
    folder.mkdir(parents=True, exist_ok=True)
    jobs = max(1, min(jobs, 8))
    seed_validation = seed_validation.resolve() if seed_validation else None
    if seed_validation:
        seed_summary = attempts.read_json(seed_validation / "manifest.json")
        if (Path(seed_summary.get("batch") or "").resolve() != batch
                or Path(seed_summary.get("workspace") or "").resolve() != root):
            raise ValueError("Seed validation must use the same batch and workspace")
    results: dict[int, dict] = {}

    def checkpoint(finished: bool, final_environment: str | None = None) -> dict:
        ordered = [results[index] for index in sorted(results)]
        summary = {"batch": str(batch), "workspace": str(root), "validator_version": m2c_report.VALIDATOR_VERSION,
                   "environment_sha256": environment, "final_environment_sha256": final_environment,
                   "environment_unchanged": final_environment == environment if finished else None,
                   "total": len(rows), "processed": len(ordered), "jobs": jobs,
                   "seed_validation": str(seed_validation) if seed_validation else None,
                   "seeded": sum(bool(row.get("reused_from_validation")) for row in ordered),
                   "finished": finished,
                   "updated_at": datetime.now(UTC).isoformat(),
                   "counts": dict(Counter(row["status"] for row in ordered)),
                   "compiler_succeeded": sum(row.get("compile_ok", False) for row in ordered),
                   "compiled_functions": sum(row.get("compile_ok", False) for row in ordered),
                   "instruction_exact": sum(row.get("instruction_exact", False) for row in ordered),
                   "promoted": 0, "scope": "standalone_inferred_or_fallback_profile"}
        attempts.write_json(folder / "manifest.json", summary)
        if finished:
            attempts.write_json(folder / "results.json", {"summary": summary, "results": ordered})
            m2c_report.refresh_report(support)
        return summary

    checkpoint(False)
    pending = []
    for index, row in enumerate(rows):
        item = folder / f"{row['address']:08X}"
        result = cached_result(item, row, environment, force, seed_validation)
        if result is None:
            pending.append((index, row, item))
            continue
        results[index] = result
        summary = checkpoint(False)
        print(f"{summary['processed']}/{summary['total']} {row['symbol']}: {result['status']} (reused)", flush=True)

    def validate(index: int, row: dict, item: Path) -> tuple[int, dict, dict]:
        result = validate_one(root, version, row, item, environment, linked, current_index)
        attempts.write_json(item / "result.json", result)
        compact_validation_item(item)
        return index, row, result

    if jobs == 1:
        completed = (validate(*entry) for entry in pending)
        for index, row, result in completed:
            results[index] = result
            summary = checkpoint(False)
            print(f"{summary['processed']}/{summary['total']} {row['symbol']}: {result['status']}", flush=True)
    else:
        with ThreadPoolExecutor(max_workers=jobs, thread_name_prefix="m2c-validation") as pool:
            futures = [pool.submit(validate, *entry) for entry in pending]
            for future in as_completed(futures):
                index, row, result = future.result()
                results[index] = result
                summary = checkpoint(False)
                print(f"{summary['processed']}/{summary['total']} {row['symbol']}: {result['status']}", flush=True)
    summary = checkpoint(True, fingerprint(root, version, batch, rows))
    ordered_results = [results[index] for index in sorted(results)]
    if (root / "objdiff.json").is_file() and (root / "build" / version / "report.json").is_file():
        try:
            refined = m2c_refinement_pipeline.run(root, version, folder / "results.json",
                support / "refinement-runs" / folder.name, limit=refinement_limit,
                source_variants=source_variants, native_variants=native_variants,
                auto_promote=auto_promote)
            summary["refinement"] = {"selected": len(refined["selection"]),
                                     "exact": refined["exact"], **refined["unit_gate"]}
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            summary["refinement_error"] = str(error)
        attempts.write_json(folder / "manifest.json", summary)
        attempts.write_json(folder / "results.json", {"summary": summary, "results": ordered_results})
    if prune_superseded:
        retention = m2c_artifact_retention.prune(support, apply=True)
        summary["retention"] = {"removed_batches": len(retention["remove_batches"]),
                                "removed_validations": len(retention["remove_validations"]),
                                "removed_drafts": len(retention["remove_drafts"]),
                                "reclaimed_bytes": retention["estimated_bytes"]}
        attempts.write_json(folder / "manifest.json", summary)
        attempts.write_json(folder / "results.json", {"summary": summary, "results": ordered_results})
        m2c_report.refresh_report(support)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DECOMP_ROOT,
                        help="decomp checkout (default: the selected configured project)")
    parser.add_argument("--version", default=VERSION,
                        help="build version (default: the selected project's version)")
    parser.add_argument("--batch", type=Path, help="completed batch directory; defaults to latest completed batch")
    parser.add_argument("--force", action="store_true", help="revalidate cached results")
    parser.add_argument("--keep-superseded", action="store_true",
                        help="do not prune superseded batches and orphaned draft cache entries")
    parser.add_argument("--refinement-limit", type=int, default=12)
    parser.add_argument("--source-variants", type=int, default=48)
    parser.add_argument("--native-variants", type=int, default=3)
    parser.add_argument("--jobs", type=int, default=1,
                        help="concurrent isolated MWCC/objdiff validations (default: 1)")
    parser.add_argument("--seed-validation", type=Path,
                        help="explicit compatible interrupted validation whose completed rows may be reused")
    parser.add_argument("--auto-promote", action="store_true",
                        help="commit units only after link and full retail-SHA verification")
    args = parser.parse_args()
    if not args.version:
        parser.error("no project version: configure a project (harness.local.json) or pass --version")
    support = args.root / "build/decomp-support" / args.version
    batch = args.batch or max((path for path in (support / "batches").glob("*")
                               if attempts.read_json(path / "results.json").get("summary", {}).get("finished")),
                              key=lambda path: path.stat().st_mtime)
    summary = run(args.root, args.version, batch, args.force, not args.keep_superseded,
                  args.refinement_limit, args.source_variants, args.native_variants,
                  args.jobs, args.seed_validation, args.auto_promote)
    print(json.dumps({key: summary[key] for key in ("total", "counts", "compiler_succeeded",
                                                     "environment_unchanged")}, indent=2))


if __name__ == "__main__":
    main()
