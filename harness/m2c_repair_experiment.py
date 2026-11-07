"""Replay frozen draft checks into an isolated directory, without model requests."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path

import m2c_draft_attempts as attempts
from m2c_failure_audit import assembly_evidence, digest, failure_categories, compiler_messages, summarize
from m2c_template_index import direct_drafts


def validate_owners(report: dict, rows: list[dict]) -> None:
    owners = {(unit["name"].removeprefix("main/"), function["name"])
              for unit in report.get("units", []) for function in unit.get("functions", [])}
    missing = [row["symbol"] for row in rows
               if (row["unit"].removeprefix("main/"), row["symbol"]) not in owners]
    if missing:
        raise ValueError("Frozen target ownership is stale; rebind and rebaseline before scoring: "
                         + ", ".join(missing[:10]))


def run(snapshot: Path, output: Path, categories: list[str], limit: int, exact_controls: int,
        templates: Path | None = None) -> dict:
    raw = snapshot.read_bytes()
    frozen = json.loads(raw)
    root = Path(frozen["root"])
    for filename, expected in frozen["target_objects"].items():
        if digest(Path(filename).read_bytes()) != expected:
            raise ValueError(f"Frozen target object changed: {filename}")
    selected = [row for row in frozen["rows"] if not categories or set(categories) & set(row["categories"])]
    template_data = templates.read_bytes() if templates else None
    index_data = json.loads(template_data) if template_data else None
    proposals = {}
    if index_data:
        for row in selected:
            if row["evidence"].get("instruction_exact") is not True:
                proposals[row["symbol"]] = direct_drafts(index_data, row["assembly"], row["symbol"])
        selected = [row for row in selected if proposals.get(row["symbol"])]
    selected = selected[:limit] if limit else selected
    selected_symbols = {row["symbol"] for row in selected}
    controls = [row for row in frozen["rows"] if row["evidence"].get("instruction_exact") is True
                and row["symbol"] not in selected_symbols][:exact_controls]
    selected += controls
    report_path = root / "build" / frozen["version"] / "report.json"
    report_raw = report_path.read_bytes()
    validate_owners(json.loads(report_raw), selected)
    output.mkdir(parents=True, exist_ok=False)
    configuration = {"snapshot": str(snapshot.resolve()), "snapshot_sha256": digest(raw),
                     "started_at": datetime.now(UTC).isoformat(), "categories": categories,
                     "symbols": [row["symbol"] for row in selected],
                     "controls": [row["symbol"] for row in controls],
                     "templates_sha256": digest(template_data) if template_data else None,
                     "experiment_sha256": digest(Path(__file__).read_bytes()),
                     "report_sha256": digest(report_raw),
                     "repair_sha256": digest(Path(attempts.compile_repair.__file__).read_bytes()),
                     "scorer_sha256": digest(Path(attempts.__file__).read_bytes())}
    attempts.write_json(output / "manifest.json", configuration)
    results = []
    for index, row in enumerate(selected):
        folder = output / f"{index:05d}"
        folder.mkdir()
        (folder / "target.s").write_text(row["assembly"], encoding="utf-8", newline="\n")
        attempts.write_json(folder / "knowledge.json", {"context": {"context": row["context"]}})
        target = {**row, "assembly": str(folder / "target.s"), "artifacts": str(folder)}
        variants = [*(proposals.get(row["symbol"]) or []), {"code": row["draft"], "method": "repaired_m2c"}]
        measurements = []
        for variant_number, variant in enumerate(variants):
            variant_folder = folder / f"candidate-{variant_number}"
            measured = attempts.score(root, frozen["version"], target, variant["code"], variant_folder)
            profile = measured.get("profile_artifact") or measured.get("profile")
            compiled_path = variant_folder / f"{profile}.s"
            compiled = compiled_path.read_text(encoding="utf-8") if compiled_path.is_file() else None
            evidence = assembly_evidence(row["assembly"], compiled, measured)
            measurements.append({"outcome": measured, "evidence": evidence,
                                 "method": variant["method"], "reference": variant.get("reference"),
                                 "folder": str(variant_folder)})
            if evidence.get("instruction_exact") is True:
                break
        best = max(measurements, key=lambda value: (value["evidence"].get("instruction_exact") is True,
                                                   value["outcome"].get("pct", 0),
                                                   bool(value["outcome"].get("compiled"))))
        measured = best["outcome"]
        result = {"symbol": row["symbol"], "size": row["size"], "outcome": measured,
                  "evidence": best["evidence"], "variants": measurements,
                  "categories": failure_categories(row["draft"], measured, row["draft_status"]),
                  "compiler_messages": compiler_messages(measured.get("error") or "")}
        results.append(result)
        summary = {"processed": len(results), "total": len(selected),
                   "baseline": summarize(selected[:len(results)]), "candidate": summarize(results),
                   "compile_gains": sum(not old["outcome"].get("compiled") and new["outcome"].get("compiled", False)
                                        for old, new in zip(selected, results)),
                   "compile_losses": [old["symbol"] for old, new in zip(selected, results)
                                      if old["outcome"].get("compiled") and not new["outcome"].get("compiled")],
                   "exact_losses": [old["symbol"] for old, new in zip(selected, results)
                                    if old["evidence"].get("instruction_exact") is True and
                                    new["evidence"].get("instruction_exact") is not True]}
        attempts.write_json(output / "results.json", {"summary": summary, "results": results})
        print(f"{index + 1}/{len(selected)} {row['symbol']}: {measured['pct']:.3f}% compiled={measured['compiled']}", flush=True)
    for filename, expected in frozen["target_objects"].items():
        if digest(Path(filename).read_bytes()) != expected:
            raise ValueError(f"Target changed during experiment: {filename}")
    if digest(report_path.read_bytes()) != digest(report_raw):
        raise ValueError("Project report changed during experiment")
    configuration["finished_at"] = datetime.now(UTC).isoformat()
    attempts.write_json(output / "manifest.json", configuration)
    return summary if results else {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--exact-controls", type=int, default=30)
    parser.add_argument("--templates", type=Path, help="validate direct template transfers from this index")
    args = parser.parse_args()
    if args.limit < 0 or args.exact_controls < 0:
        parser.error("limits must be nonnegative")
    print(json.dumps(run(args.snapshot, args.output.resolve(), args.category, args.limit,
                         args.exact_controls, args.templates), indent=2))


if __name__ == "__main__":
    main()
