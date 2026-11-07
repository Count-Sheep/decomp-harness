#!/usr/bin/env python3
"""Prune superseded m2c artifacts while retaining compact dashboard history."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

import local_campaign_m2c_report as m2c_report


ATTEMPT_FIELDS = ("index", "at", "model", "host", "pct", "profile", "compiled",
                  "delta", "instruction_exact", "source")


def read(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def size(path: Path) -> int:
    if path.is_file() and not path.is_symlink():
        return path.stat().st_size
    if not path.is_dir():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*")
               if item.is_file() and not item.is_symlink())


def compact_attempt_state(support: Path, apply: bool = False) -> dict[str, Any]:
    """Drop duplicated compiler diagnostics while preserving dashboard and resume fields."""
    path = support / "attempts" / "state.json"
    if not path.is_file():
        return {"path": str(path), "items": 0, "attempts": 0, "before_bytes": 0,
                "after_bytes": 0, "reclaimed_bytes": 0, "applied": apply}
    state = read(path)
    items = state.get("items") or {}
    attempts = 0
    for item in items.values():
        compact = []
        for attempt in item.get("attempts") or []:
            retained = {key: attempt.get(key) for key in ATTEMPT_FIELDS if key in attempt}
            source = retained.get("source")
            if source and not Path(str(source)).is_file():
                retained["source"] = None
            compact.append(retained)
        attempts += len(compact)
        item["attempts"] = compact
        best_source = item.get("best_source")
        if best_source and not Path(str(best_source)).is_file():
            item["best_source"] = None
    encoded = (json.dumps(state, indent=2, sort_keys=False) + "\n").encode()
    before = path.stat().st_size
    if apply and len(encoded) < before:
        temporary = path.with_suffix(".json.tmp")
        temporary.write_bytes(encoded)
        os.replace(temporary, path)
    return {"path": str(path), "items": len(items), "attempts": attempts,
            "before_bytes": before, "after_bytes": min(before, len(encoded)),
            "reclaimed_bytes": max(0, before - len(encoded)), "applied": apply}


def strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def external_references(support: Path) -> set[Path]:
    """Find managed artifacts named by unit/review records outside managed stores."""
    managed = {"batches", "validation", "drafts", "exact-reuse"}
    references = set()
    for path in support.rglob("*.json"):
        try:
            relative = path.relative_to(support)
        except ValueError:
            continue
        if relative.parts and relative.parts[0] in managed or path.name == m2c_report.REPORT_NAME:
            continue
        for value in strings(read(path)):
            candidate = Path(value)
            if not candidate.is_absolute():
                continue
            try:
                candidate.resolve(strict=False).relative_to(support)
            except ValueError:
                continue
            references.add(candidate.resolve(strict=False))
    return references


def validation_rows(support: Path) -> dict[str, dict[str, dict[str, Any]]]:
    choices: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for path in (support / "validation").glob("*/results.json"):
        data = read(path)
        summary = data.get("summary") or {}
        batch = Path(str(summary.get("batch") or "")).name
        if batch and summary.get("finished"):
            choices[batch].append((str(summary.get("updated_at") or ""), data))
    selected = {}
    for batch, values in choices.items():
        data = max(values, key=lambda value: value[0])[1]
        selected[batch] = {str(row.get("symbol")): row for row in data.get("results") or []
                           if row.get("symbol")}
    return selected


def quality(batch_row: dict[str, Any], measured: dict[str, Any] | None) -> tuple:
    measured = measured or {}
    pct = measured.get("pct")
    return (measured.get("instruction_exact") is True,
            measured.get("compile_ok") is True,
            float(pct) if isinstance(pct, (int, float)) else -1.0,
            batch_row.get("status") in {"ok", "exact_reuse"})


def plan(support_root: Path, keep_recent: int = 2) -> dict[str, Any]:
    support = support_root.resolve()
    batch_data = []
    for folder in sorted((support / "batches").glob("*")):
        data = read(folder / "results.json")
        summary = data.get("summary") or read(folder / "manifest.json")
        if folder.is_dir() and summary:
            batch_data.append((folder, summary, data.get("results") or []))
    measured = validation_rows(support)
    protected = external_references(support)
    keep = set()
    completed = [(folder, summary, rows) for folder, summary, rows in batch_data
                 if summary.get("finished")]
    active = [(folder, summary, rows) for folder, summary, rows in batch_data
              if not summary.get("finished")]
    if active:
        keep.add(max(active, key=lambda item: str(item[1].get("updated_at") or ""))[0].name)
    newest_order = sorted(completed, key=lambda item: str(item[1].get("updated_at") or ""), reverse=True)
    keep.update(folder.name for folder, _, _ in newest_order[:max(0, keep_recent)])

    by_symbol: dict[str, list[tuple]] = defaultdict(list)
    for folder, summary, rows in completed:
        updated = str(summary.get("updated_at") or "")
        scores = measured.get(folder.name, {})
        for row in rows:
            symbol = row.get("symbol")
            if symbol:
                by_symbol[str(symbol)].append((folder.name, updated, quality(row, scores.get(str(symbol)))))
    for choices in by_symbol.values():
        keep.add(max(choices, key=lambda item: item[1])[0])
        keep.add(max(choices, key=lambda item: (item[2], item[1]))[0])

    for reference in protected:
        parts = reference.parts
        for kind in ("batches", "validation", "drafts"):
            if kind in parts and parts.index(kind) + 1 < len(parts):
                if kind == "batches":
                    keep.add(parts[parts.index(kind) + 1])

    summaries = {folder.name: summary for folder, summary, _ in batch_data}
    pending = list(keep)
    while pending:
        name = pending.pop()
        baseline = str((summaries.get(name) or {}).get("baseline") or "")
        if baseline in summaries and baseline not in keep:
            keep.add(baseline)
            pending.append(baseline)

    remove_batches = [folder for folder, _, _ in batch_data if folder.name not in keep]
    remove_batch_names = {path.name for path in remove_batches}
    remove_validations = []
    for folder in (support / "validation").glob("*"):
        data = read(folder / "results.json") or read(folder / "manifest.json")
        batch = Path(str((data.get("summary") or data).get("batch") or "")).name
        if folder.is_dir() and batch in remove_batch_names:
            remove_validations.append(folder)
    unfinished_validations: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    for folder in (support / "validation").glob("*"):
        data = read(folder / "results.json") or read(folder / "manifest.json")
        summary = data.get("summary") or data
        batch = Path(str(summary.get("batch") or "")).name
        if folder.is_dir() and batch and not summary.get("finished") and folder not in remove_validations:
            unfinished_validations[batch].append((str(summary.get("updated_at") or ""), folder))
    for values in unfinished_validations.values():
        newest = max(values, key=lambda value: value[0])[1]
        remove_validations.extend(folder for _, folder in values if folder != newest)

    kept_artifacts = set(protected)
    for folder, _, rows in batch_data:
        if folder.name not in keep:
            continue
        for row in rows:
            artifact = row.get("artifacts")
            if artifact:
                kept_artifacts.add(Path(str(artifact)).resolve(strict=False))
    remove_drafts = []
    draft_root = support / "drafts"
    kept_draft_dirs = set()
    protected_draft_paths = []
    for path in kept_artifacts:
        try:
            relative = path.relative_to(draft_root)
        except ValueError:
            continue
        if relative.parts:
            kept_draft_dirs.add((draft_root / relative.parts[0]).resolve(strict=False))
            if path in protected:
                protected_draft_paths.append(path)
    for folder in draft_root.glob("*"):
        if folder.name == "inputs" or not folder.is_dir():
            continue
        resolved = folder.resolve(strict=False)
        if resolved not in kept_draft_dirs and not any(
            path == resolved or resolved in path.parents or path in resolved.parents
            for path in protected_draft_paths
        ):
            remove_drafts.append(folder)

    input_root = draft_root / "inputs"
    used_inputs = set()
    for folder in draft_root.glob("*"):
        if folder in remove_drafts or not folder.is_dir() or folder.name == "inputs":
            continue
        for path in folder.iterdir():
            if path.is_symlink():
                used_inputs.add(path.resolve(strict=False))
    remove_inputs = [path for path in input_root.glob("*")
                     if path.resolve(strict=False) not in used_inputs]
    removals = [*remove_batches, *remove_validations, *remove_drafts, *remove_inputs]
    return {"support_root": str(support), "keep_batches": sorted(keep),
            "remove_batches": [str(path) for path in remove_batches],
            "remove_validations": [str(path) for path in remove_validations],
            "remove_drafts": [str(path) for path in remove_drafts],
            "remove_inputs": [str(path) for path in remove_inputs],
            "estimated_bytes": sum(size(path) for path in removals),
            "external_references": len(protected)}


def prune(support_root: Path, keep_recent: int = 2, apply: bool = False) -> dict[str, Any]:
    support = support_root.resolve()
    m2c_report.refresh_report(support)
    result = plan(support, keep_recent)
    result["applied"] = apply
    result["attempt_state"] = compact_attempt_state(support, apply)
    result["estimated_bytes"] += result["attempt_state"]["reclaimed_bytes"]
    if apply:
        for key in ("remove_batches", "remove_validations", "remove_drafts"):
            for value in result[key]:
                shutil.rmtree(value)
        for value in result["remove_inputs"]:
            Path(value).unlink(missing_ok=True)
        m2c_report.refresh_report(support)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("support_root", type=Path)
    parser.add_argument("--keep-recent", type=int, default=2)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.keep_recent < 1:
        parser.error("--keep-recent must be positive")
    print(json.dumps(prune(args.support_root, args.keep_recent, args.apply), indent=2))


if __name__ == "__main__":
    main()
