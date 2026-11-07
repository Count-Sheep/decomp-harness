"""Bounded full-owner refinement of the checked-in source candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import local_campaign_decompile_batch as batch_tools
import local_campaign_decompile_validate as validator
import m2c_current_source
import m2c_deterministic_transforms as transforms
import m2c_object_evidence
import m2c_context


def candidates(source: str, limit: int, classification: dict | None = None) -> list[dict]:
    if not 0 <= limit <= 64:
        raise ValueError("Source variant budget must be between zero and 64")
    return transforms.variants(source, classification, limit)


def rank(record: dict) -> tuple:
    audit = record.get("object_audit") or {}
    feedback = audit.get("byte_feedback") or {}
    return (bool(audit.get("scratch_object_equal")), bool(record.get("compile_ok")),
            float(record.get("pct") or 0), -int(feedback.get("different_words") or 10**9))


def mismatch(record: dict) -> dict:
    return record.get("mismatch_classification") or {}


def register_allocation_only(classification: dict | None) -> bool:
    counts = (classification or {}).get("counts") or {}
    return bool((classification or {}).get("total")) and set(counts) == {"register"}


def _called_names(source: str) -> list[str]:
    tokens = transforms.rewrite.tokenize(source)
    return list(dict.fromkeys(token.text for index, token in enumerate(tokens[:-1])
                              if token.kind == "ident" and tokens[index + 1].text == "("
                              and token.text not in transforms.rewrite.KEYWORDS))


def _prioritized_calls(source: str, target_lines: set[int]) -> list[str]:
    tokens = transforms.rewrite.tokenize(source)
    rows = []
    for index, token in enumerate(tokens[:-1]):
        if (token.kind == "ident" and tokens[index + 1].text == "("
                and token.text not in transforms.rewrite.KEYWORDS):
            line = source.count("\n", 0, token.start) + 1
            rows.append((not any(abs(line - target) <= 1 for target in target_lines), line, token.text))
    return list(dict.fromkeys(row[2] for row in sorted(rows)))


def inline_helpers(files: dict[str, str], target_symbol: str, target_source: str,
                   target_lines: set[int] | None = None) -> list[dict]:
    """Find transitive static-inline callees inside the captured owner files."""
    queue = _prioritized_calls(target_source, target_lines or set())
    found, seen = [], {target_symbol}
    while queue:
        symbol = queue.pop(0)
        if symbol in seen:
            continue
        seen.add(symbol)
        matches = []
        for path, text in files.items():
            try:
                start, end = validator.campaign.function_span(text, symbol)
            except ValueError:
                continue
            signature = text[start:text.find("{", start, end)]
            if re.search(r"\bstatic\b", signature) and re.search(r"\binline\b", signature):
                matches.append((path, text[start:end]))
        if len(matches) != 1:
            continue
        path, source = matches[0]
        found.append({"path": path, "symbol": symbol, "source": source})
        queue[0:0] = [name for name in _called_names(source) if name not in seen]
    return found


def owner_candidates(files: dict[str, str], target_symbol: str, target_source: str,
                     current: list[dict], classification: dict | None, limit: int) -> list[dict]:
    """Generate bounded helper-body variants for pure register-allocation residuals."""
    if limit <= 0 or not register_allocation_only(classification):
        return []
    prepared, _ = validator.apply_owner_rewrites(files, target_symbol, current)
    target_lines = {int(row["source_line"]) for row in (classification or {}).get("mismatches") or []
                    if isinstance(row.get("source_line"), int) and row["source_line"] > 0}
    helpers = inline_helpers(prepared, target_symbol, target_source, target_lines)
    groups, seen = [], set()
    for helper in helpers:
        variants = transforms.variants(helper["source"], {"register_only": True}, min(64, max(8, limit * 2)))
        group = []
        for variant in variants:
            rewrites = [row for row in current
                        if (row["path"], row["symbol"]) != (helper["path"], helper["symbol"])]
            rewrites.append({"path": helper["path"], "symbol": helper["symbol"],
                             "source": variant["code"]})
            key = hashlib.sha256(json.dumps({"target": target_source, "rewrites": rewrites},
                                            sort_keys=True).encode()).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            group.append({"code": target_source, "kind": "inline_helper_" + variant["kind"],
                          "detail": f"{helper['symbol']}: {variant['detail']}",
                          "helper_symbol": helper["symbol"], "targeted": False,
                          "target_distance": None, "owner_rewrites": rewrites, "sha256": key})
        if group:
            groups.append(group)
    rows = []
    while len(rows) < limit and any(groups):
        for group in groups:
            if group:
                rows.append(group.pop(0))
                if len(rows) >= limit:
                    break
    return rows


def materialize_owner_rewrites(artifact: Path, rewrites: list[dict]) -> list[dict]:
    evidence = []
    for index, rewrite in enumerate(rewrites):
        path = artifact / "owner-rewrites" / f"{index:02d}-{rewrite['symbol']}.c"
        path.parent.mkdir(parents=True, exist_ok=True)
        source = rewrite["source"].rstrip() + "\n"
        path.write_text(source, encoding="utf-8", newline="\n")
        evidence.append({"path": rewrite["path"], "symbol": rewrite["symbol"],
                         "source_path": str(path),
                         "sha256": hashlib.sha256(source.encode()).hexdigest()})
    return evidence


def run(root: Path, version: str, row: dict, output: Path, max_variants: int = 48, *,
        seed_path: Path | None = None, seed_origin: str = "current_source",
        beam_width: int = 3, max_depth: int = 3) -> dict:
    if not 0 <= max_variants <= 64:
        raise ValueError("Source variant budget must be between zero and 64")
    if not 1 <= beam_width <= 8:
        raise ValueError("Beam width must be between one and eight")
    if not 1 <= max_depth <= 6:
        raise ValueError("Search depth must be between one and six")
    root, output = root.resolve(), output.absolute()
    if output.exists():
        raise ValueError("Use a fresh source-search output")
    index = m2c_current_source.Index(root, version)
    baseline = m2c_current_source.measure(root, version, row, index)
    if baseline is None:
        raise ValueError(f"No checked-in source candidate for {row.get('symbol')}")
    found = index.locate(row)
    owner = found["owner"]
    source = baseline["source_path"]
    listing = subprocess.run(["ninja", "-t", "commands", owner["base_path"]], cwd=root,
                             capture_output=True, text=True, timeout=60, check=True)
    commands = batch_tools.command_map(listing.stdout)
    command = commands.get(source)
    if not command:
        raise ValueError(f"No native compile command for {source}")
    output.mkdir(parents=True)
    seed = baseline["candidate"]
    if seed_path is not None:
        seed = seed_path.read_text(encoding="utf-8")
        _, definition = m2c_context._source_function(seed, row["symbol"], len(seed) + 1)
        if definition is None:
            raise ValueError("Refinement seed must define its assigned symbol")
    seed_folder = output / "seed"
    artifact = seed_folder / "input"
    artifact.mkdir(parents=True)
    (artifact / "draft.c").write_text(seed, encoding="utf-8", newline="\n")
    attempt_row = {"id": f"seed-{row['symbol']}", "symbol": row["symbol"],
                   "unit": baseline["unit"], "status": "ok", "artifacts": str(artifact),
                   "source": source}
    measured = validator.validate_one(root, attempt_row, command, owner, "", seed_folder / "score",
                                      baseline_root=output / "baselines", source_lines=True)
    audit = {"status": "unavailable", "scratch_object_equal": False}
    compiled = Path(measured.get("compile_output") or "")
    if measured.get("compile_ok") and compiled.is_file():
        audit = m2c_object_evidence.compare(found["paths"]["target"], compiled, row["symbol"])
    baseline_record = {**measured, "origin": seed_origin if seed_path is not None else "current_source",
                       "validation_pct": baseline.get("pct"), "candidate": seed, "object_audit": audit}
    records = [baseline_record]
    captured_files = {}
    if register_allocation_only(mismatch(baseline_record)):
        _, captured_files = validator.snapshot_owner(root, source, row["symbol"])
    report = {"schema_version": 2, "symbol": row["symbol"], "unit": baseline["unit"],
              "source_path": source, "started_at": datetime.now(UTC).isoformat(),
              "max_variants": max_variants, "beam_width": beam_width, "max_depth": max_depth,
              "engine": "deterministic_compiler_guided", "model_inputs": [],
              "attempts": records, "best_attempt": 0,
              "finished": False, "promoted": False, "linked": False}
    (output / "results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    seen = {hashlib.sha256(seed.encode()).hexdigest()}
    frontier = [{"record_index": 0, "code": seed, "depth": 0, "lineage": [],
                 "owner_rewrites": []}]
    index_number = 0
    while frontier and index_number < max_variants:
        depth = frontier[0]["depth"] + 1
        if depth > max_depth:
            break
        remaining = max_variants - index_number
        round_budget = max(1, remaining // max(1, max_depth - depth + 1))
        proposals = []
        for parent in frontier:
            allowance = max(1, round_budget // len(frontier))
            pool_size = min(64, allowance * 3)
            classification = mismatch(records[parent["record_index"]])
            helper_pool = owner_candidates(captured_files, row["symbol"], parent["code"],
                                           parent["owner_rewrites"], classification, pool_size)
            source_pool = candidates(parent["code"], pool_size, classification)
            for variant in [*helper_pool, *source_pool]:
                if variant["sha256"] in seen:
                    continue
                seen.add(variant["sha256"])
                proposals.append((parent, variant))
                if len(proposals) >= round_budget:
                    break
            if len(proposals) >= round_budget:
                break
        if not proposals:
            break
        next_frontier = []
        for parent, variant in proposals:
            index_number += 1
            folder = output / f"{index_number:03d}"
            artifact = folder / "input"
            artifact.mkdir(parents=True)
            (artifact / "draft.c").write_text(variant["code"], encoding="utf-8", newline="\n")
            rewrites = variant.get("owner_rewrites", parent["owner_rewrites"])
            rewrite_artifacts = materialize_owner_rewrites(artifact, rewrites)
            attempt_row = {"id": f"source-{row['symbol']}-{index_number}", "symbol": row["symbol"],
                           "unit": baseline["unit"], "status": "ok", "artifacts": str(artifact),
                           "source": source}
            measured = validator.validate_one(root, attempt_row, command, owner, "", folder / "score",
                                              baseline_root=output / "baselines", source_lines=True,
                                              owner_rewrites=rewrites)
            audit = {"status": "unavailable", "scratch_object_equal": False}
            compiled = Path(measured.get("compile_output") or "")
            if measured.get("compile_ok") and compiled.is_file():
                audit = m2c_object_evidence.compare(found["paths"]["target"], compiled, row["symbol"])
            step = {key: value for key, value in variant.items()
                    if key not in {"code", "owner_rewrites"}}
            lineage = [*parent["lineage"], step]
            record = {**measured, "origin": "deterministic_variant", "search_depth": depth,
                      "parent_attempt": parent["record_index"], "variant": step,
                      "lineage": lineage, "candidate": variant["code"], "object_audit": audit}
            record["artifact"] = str(artifact)
            record["owner_rewrite_artifacts"] = rewrite_artifacts
            records.append(record)
            record_index = len(records) - 1
            next_frontier.append({"record_index": record_index, "code": variant["code"],
                                  "depth": depth, "lineage": lineage,
                                  "owner_rewrites": rewrites})
            report["best_attempt"] = max(range(len(records)), key=lambda item: rank(records[item]))
            (output / "results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
            if audit.get("scratch_object_equal"):
                frontier = []
                break
        else:
            next_frontier.sort(key=lambda item: rank(records[item["record_index"]]), reverse=True)
            frontier = next_frontier[:beam_width]
            continue
        break
    report.update(finished=True, finished_at=datetime.now(UTC).isoformat(),
                  improved=rank(records[report["best_attempt"]]) > rank(records[0]))
    (output / "results.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--address", required=True, type=lambda value: int(value, 0))
    parser.add_argument("--size", required=True, type=lambda value: int(value, 0))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-variants", type=int, default=48)
    parser.add_argument("--beam-width", type=int, default=3)
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--seed", type=Path, help="winning m2c/reference C to refine in the real owner")
    parser.add_argument("--seed-origin", default="external_winner")
    args = parser.parse_args()
    result = run(args.root, args.version, {"symbol": args.symbol, "address": args.address,
                 "size": args.size}, args.output, args.max_variants,
                 seed_path=args.seed, seed_origin=args.seed_origin,
                 beam_width=args.beam_width, max_depth=args.max_depth)
    best = result["attempts"][result["best_attempt"]]
    print(json.dumps({"symbol": result["symbol"], "attempts": len(result["attempts"]),
                      "best_pct": best.get("pct"), "object_exact":
                      bool((best.get("object_audit") or {}).get("scratch_object_equal"))}, indent=2))


if __name__ == "__main__":
    main()
