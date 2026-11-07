#!/usr/bin/env python3
"""Export retained local-worker gains as review-only m2c improvement evidence."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import local_campaign as campaign
from harness_paths import DECOMP_ROOT


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def project_path(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Worker artifact escapes project root: " + value)
    return path


def function_source(text: str, symbol: str) -> str:
    start, end = campaign.function_span(text, symbol)
    return text[start:end].strip() + "\n"


def collect(root: Path, state_path: Path | None = None) -> dict:
    root = root.resolve()
    state_path = (state_path or root / "build/local_llm_campaign/state.json").resolve()
    state_bytes = state_path.read_bytes()
    state = json.loads(state_bytes)
    rows = []
    for item in state.get("items", {}).values():
        best = item.get("best") or {}
        baseline = float(item.get("base_pct") or 0)
        score = float(best.get("pct") or 0)
        candidate_name = best.get("candidate")
        if score <= baseline or not candidate_name:
            continue
        candidate_path = project_path(root, candidate_name)
        source_path = project_path(root, item.get("owner_source") or item["source"])
        if not candidate_path.is_file() or not source_path.is_file():
            continue
        candidate_bytes, source_bytes = candidate_path.read_bytes(), source_path.read_bytes()
        try:
            candidate_function = function_source(candidate_bytes.decode("utf-8", errors="replace"), item["symbol"])
            current_function = function_source(source_bytes.decode("utf-8", errors="replace"), item["symbol"])
        except ValueError:
            continue
        source_sha = digest(source_bytes)
        focused_diff = "".join(difflib.unified_diff(
            current_function.splitlines(keepends=True), candidate_function.splitlines(keepends=True),
            fromfile="current/" + item["symbol"], tofile="candidate/" + item["symbol"], n=3))
        rows.append({
            "symbol": item["symbol"], "unit": item.get("unit"),
            "owner_source": item.get("owner_source") or item.get("source"),
            "baseline_pct": baseline, "best_pct": score, "delta_pct": round(score - baseline, 6),
            "instruction_exact_claim": score == 100.0,
            "candidate_path": str(candidate_path), "candidate_sha256": digest(candidate_bytes),
            "candidate_function_sha256": digest(candidate_function.encode()),
            "current_source_sha256": source_sha, "queued_source_sha256": item.get("source_sha256"),
            "source_current": source_sha == item.get("source_sha256"),
            "worker": item.get("worker"), "attempt": best.get("attempt"), "variant": best.get("variant"),
            "measured_at": best.get("at"), "remaining_diff_feedback": best.get("diff_feedback") or [],
            "focused_source_diff": focused_diff,
            "admission": "review_only", "auto_consumed_by_m2c": False,
        })
    rows.sort(key=lambda row: (-row["best_pct"], -row["delta_pct"], row["symbol"]))
    return {
        "schema_version": 1, "generated_at": datetime.now(UTC).isoformat(),
        "project_root": str(root), "state_path": str(state_path),
        "state_sha256": digest(state_bytes), "admission": "review_only",
        "auto_consumed_by_m2c": False,
        "summary": {"retained_improvements": len(rows),
                    "instruction_exact_claims": sum(row["instruction_exact_claim"] for row in rows),
                    "current_source_bindings": sum(row["source_current"] for row in rows)},
        "improvements": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DECOMP_ROOT,
                        help="decomp checkout (default: the selected configured project)")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = collect(args.root, args.state)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
