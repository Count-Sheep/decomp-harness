"""Frozen, clone/owner-grouped retrieval audit. This does not measure model match gains."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter
from pathlib import Path

from local_campaign_decompile import write_json
import local_campaign_knowledge as knowledge
from local_campaign_knowledge import DEFAULT_DATABASE, KnowledgeStore, clone_groups, digest, encoded, retrieval_pack


def make_split(store: KnowledgeStore, snapshot_id: str, limit: int = 100, seed: str = "pilot-1") -> dict:
    if limit < 1:
        raise ValueError("Holdout target limit must be positive")
    rows = [dict(row) for row in store.connection.execute(
        "SELECT * FROM functions WHERE snapshot_id=? ORDER BY id", (snapshot_id,))]
    if not rows:
        raise ValueError("Snapshot contains no function observations")
    groups = clone_groups(rows)
    candidates = {}
    for row in rows:
        if row["eligible"] and 8 <= row["insns"] <= 400:
            candidates.setdefault(groups[row["id"]], []).append(row["id"])
    ordered = sorted(candidates, key=lambda group: digest(seed + group))
    targets, held_groups = [], set()
    for group in ordered:
        held_groups.add(group)
        targets.extend(sorted(candidates[group], key=lambda value: digest(seed + value))[:limit - len(targets)])
        if len(targets) >= limit:
            break
    if not targets:
        raise ValueError("No source-backed, policy-screened targets in the pilot size range")
    excluded = sorted(key for key, group in groups.items() if group in held_groups)
    payload = {"schema_version": 1, "mode": "retrieval_audit", "snapshot_id": snapshot_id,
               "seed": seed, "targets": targets, "excluded": excluded,
               "grouping": ["source_owner", "target_bytes", "instruction_text", "normalized_shape", "source_body"],
               "groups": len(set(groups.values())), "held_groups": len(held_groups),
               "largest_group": max(Counter(groups.values()).values()),
               "corpus_function_ids_sha256": digest(encoded(sorted(groups)))}
    return {**payload, "split_sha256": digest(encoded(payload))}


def validate_split(store: KnowledgeStore, split: dict) -> None:
    payload = {key: value for key, value in split.items() if key != "split_sha256"}
    if digest(encoded(payload)) != split.get("split_sha256"):
        raise ValueError("Frozen holdout manifest changed")
    rows = [dict(row) for row in store.connection.execute(
        "SELECT * FROM functions WHERE snapshot_id=? ORDER BY id", (split["snapshot_id"],))]
    groups = clone_groups(rows)
    if digest(encoded(sorted(groups))) != split["corpus_function_ids_sha256"]:
        raise ValueError("Corpus changed since the holdout was frozen")
    if len(set(split["targets"])) != len(split["targets"]) or not split["targets"]:
        raise ValueError("Holdout targets must be nonempty and unique")
    held = {groups[key] for key in split["targets"]}
    required = {key for key, group in groups.items() if group in held}
    if required != set(split["excluded"]):
        raise ValueError("Holdout does not exclude complete owner/clone groups")


def run_audit(store: KnowledgeStore, split: dict, budget: int = 6000) -> dict:
    validate_split(store, split)
    excluded = set(split["excluded"])
    results, times = [], []
    for target_id in split["targets"]:
        started = time.monotonic()
        pack = retrieval_pack(store, target_id, excluded=excluded, budget=budget)
        times.append(time.monotonic() - started)
        reference_ids = [row["function_id"] for row in pack["references"]]
        if excluded.intersection(reference_ids):
            raise ValueError("Held-out source leaked into a retrieval pack")
        results.append({"target_id": target_id, "reference_ids": reference_ids,
                        "pack_sha256": digest(encoded(pack)),
                        "reference_content_chars": pack["reference_content_chars"],
                        "reasons": [row["reason"] for row in pack["references"]]})
    hits = sum(bool(row["reference_ids"]) for row in results)
    return {"schema_version": 1, "mode": "retrieval_audit", "snapshot_id": split["snapshot_id"],
            "split_sha256": split["split_sha256"], "inference_run": False, "compiler_run": False,
            "retrieval_configuration": {"feature_version": knowledge.FEATURE_VERSION,
                "retrieval_version": knowledge.RETRIEVAL_VERSION,
                "reference_content_budget": budget, "reference_limit": 3,
                "retriever_sha256": digest(Path(knowledge.__file__).read_bytes()),
                "audit_producer_sha256": digest(Path(__file__).read_bytes())},
            "target_count": len(results), "excluded_functions": len(excluded), "source_leaks": 0,
            "retrieval_on": {"targets_with_references": hits, "reference_hit_rate": hits / len(results),
                             "median_query_seconds": statistics.median(times),
                             "median_reference_chars": statistics.median(row["reference_content_chars"] for row in results)},
            "retrieval_off": {"targets_with_references": 0, "reference_content_chars": 0},
            "exact_match_rate": None, "verified_gain": None,
            "notice": "Retrieval coverage/exclusion check only. A paired inference/compile benchmark is still required.",
            "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("stats")
    index = sub.add_parser("index")
    index.add_argument("snapshot_id")
    query = sub.add_parser("query")
    query.add_argument("snapshot_id")
    query.add_argument("symbol")
    query.add_argument("--module", default="main")
    query.add_argument("--split", type=Path)
    audit = sub.add_parser("audit")
    audit.add_argument("snapshot_id")
    audit.add_argument("--limit", type=int, default=100)
    audit.add_argument("--seed", default="pilot-1")
    audit.add_argument("--split", type=Path)
    audit.add_argument("--output", type=Path, required=True)
    paired = sub.add_parser("paired-report")
    paired.add_argument("runs", type=Path, nargs="+")
    paired.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "paired-report":
        from local_campaign_knowledge_ablation import report_paired_run
        if args.output.exists():
            raise ValueError("Paired report already exists; preserve history with a new output path")
        report = {"schema_version": 1, "mode": "paired_pilot_report",
                  "runs": [report_paired_run(folder) for folder in args.runs]}
        write_json(args.output, report)
        print(json.dumps({**report, "runs": [{key: value for key, value in row.items() if key != "pairs"}
                                            for row in report["runs"]], "output": str(args.output)}, indent=2))
        return 0
    if args.command == "audit" and (args.output.exists()
            or not args.split and args.output.with_suffix(".split.json").exists()):
        raise ValueError("Audit or split already exists; use a new output path to preserve history")
    with KnowledgeStore(args.database, readonly=args.command != "index") as store:
        if args.command == "index":
            result = {"indexed_functions": store.index_features(args.snapshot_id), "stats": store.stats()}
        elif args.command == "stats":
            result = store.stats()
        elif args.command == "query":
            split = json.loads(args.split.read_text(encoding="utf-8")) if args.split else None
            if split:
                validate_split(store, split)
                if split["snapshot_id"] != args.snapshot_id:
                    raise ValueError("Query and holdout must use the same snapshot")
            result = retrieval_pack(store, store.resolve(args.snapshot_id, args.symbol, args.module),
                                    excluded=set(split["excluded"]) if split else None)
        else:
            if args.split:
                split = json.loads(args.split.read_text(encoding="utf-8"))
                if split["snapshot_id"] != args.snapshot_id:
                    raise ValueError("Audit and holdout must use the same snapshot")
            else:
                split = make_split(store, args.snapshot_id, args.limit, args.seed)
                write_json(args.output.with_suffix(".split.json"), split)
            result = run_audit(store, split)
            write_json(args.output, result)
            result = {key: value for key, value in result.items() if key != "results"}
            result["output"] = str(args.output)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
