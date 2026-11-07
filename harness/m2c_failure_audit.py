"""Freeze draft evidence and classify failures without touching a running campaign."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re

import m2c_compile_repair as repair


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compiler_messages(error: str) -> list[str]:
    # MWCC prints the diagnostic after the caret, often on several lines.
    return sorted(set(re.findall(r"#\s+Error:[^\n]*\n#\s+([^\n]+)", error)))


def failure_categories(code: str, outcome: dict, draft_status: str) -> list[str]:
    if outcome.get("compiled"):
        return []
    clean = repair._mask_comments_and_strings(code)
    error = outcome.get("error") or ""
    categories = []
    if draft_status != "ok":
        categories.append("generation_failure")
    if re.search(r"@\d+", clean):
        categories.append("invalid_symbol_name")
    if "undefined identifier" in error:
        categories.append("missing_declaration")
    if "'void'" in error:
        categories.append("void_value_or_return")
    if "pointer/array required" in error or "illegal operand" in error:
        categories.append("pointer_or_operand_type")
    if "cannot convert" in error or "illegal implicit conversion" in error or "illegal explicit conversion" in error:
        categories.append("incompatible_type")
    if re.search(r"\bM2C_(?!FIELD\b|UNK\b)\w+", clean):
        categories.append("unresolved_helper")
    if outcome.get("repair", {}).get("unresolved"):
        categories.append("repair_blocker")
    if any("syntax" in message or "expected" in message for message in compiler_messages(error)):
        categories.append("syntax")
    return categories or ["unclassified"]


def assembly_evidence(target: str, compiled: str | None, outcome: dict) -> dict:
    if not compiled or not repair._instructions(target) or not repair._instructions(compiled):
        return {"status": "unavailable", "instruction_exact": None}
    comparison = repair.compare_assembly(target, compiled)
    exact = bool(outcome.get("compiled") and outcome.get("pct") == 100 and comparison["exact"])
    return {"status": "checked", "instruction_exact": exact,
            "false_100": outcome.get("pct") == 100 and not comparison["exact"],
            "comparison": comparison}


def summarize(rows: list[dict]) -> dict:
    categories = Counter(category for row in rows for category in row["categories"])
    messages = Counter(message for row in rows for message in row["compiler_messages"])
    return {"total": len(rows), "compiled": sum(bool(row["outcome"].get("compiled")) for row in rows),
            "scorer_100": sum(row["outcome"].get("pct") == 100 for row in rows),
            "instruction_exact": sum(row["evidence"].get("instruction_exact") is True for row in rows),
            "instruction_exact_bytes": sum(row["size"] for row in rows
                                           if row["evidence"].get("instruction_exact") is True),
            "false_100": sum(bool(row["evidence"].get("false_100")) for row in rows),
            "unverified_100": sum(row["outcome"].get("pct") == 100 and
                                  row["evidence"]["status"] != "checked" for row in rows),
            "failure_categories": dict(categories.most_common()),
            "compiler_messages": dict(messages.most_common())}


def freeze(root: Path, version: str, output: Path) -> dict:
    support = root / "build/decomp-support" / version
    raw = (support / "attempts/state.json").read_bytes()
    state = json.loads(raw)
    batch = support / "batches" / state["batch"]
    batch_raw = (batch / "results.json").read_bytes()
    targets = {row["symbol"]: row for row in json.loads(batch_raw)["results"]}
    target_hashes = {}
    rows = []
    for symbol, item in sorted(state["items"].items()):
        baseline = next((attempt for attempt in item.get("attempts", []) if attempt["index"] == 0), None)
        if baseline is None:
            continue
        target = targets[symbol]
        artifact = Path(target["artifacts"])
        code = (artifact / "draft.c").read_text(encoding="utf-8")
        assembly = Path(target["assembly"]).read_text(encoding="utf-8")
        folder = support / "attempts" / symbol / "000"
        candidate = folder / "candidate.c"
        profile = baseline.get("profile_artifact") or baseline.get("profile")
        compiled_path = folder / f"{profile}.s"
        compiled = compiled_path.read_text(encoding="utf-8") if compiled_path.is_file() else None
        obj = root / "build" / version / "obj" / (target["unit"] + ".o")
        if str(obj) not in target_hashes:
            target_hashes[str(obj)] = digest(obj.read_bytes())
        knowledge_path = artifact / "knowledge.json"
        knowledge = json.loads(knowledge_path.read_text(encoding="utf-8")) if knowledge_path.is_file() else {}
        rows.append({"symbol": symbol, "size": item["size"], "unit": target["unit"],
                     "address": item["address"], "draft_status": target["status"],
                     "draft": code, "draft_sha256": digest(code.encode()),
                     "assembly": assembly, "assembly_sha256": digest(assembly.encode()),
                     "candidate": candidate.read_text(encoding="utf-8") if candidate.is_file() else None,
                     "compiled_assembly": compiled, "outcome": baseline,
                     "context": knowledge.get("context", {}).get("context", ""),
                     "context_available": knowledge_path.is_file(),
                     "compiler_profile": target.get("compiler_profile"),
                     "evidence": assembly_evidence(assembly, compiled, baseline),
                     "categories": failure_categories(code, baseline, target["status"]),
                     "compiler_messages": compiler_messages(baseline.get("error") or "")})
    result = {"schema_version": 1, "root": str(root), "version": version,
              "captured_at": datetime.now(UTC).isoformat(), "ledger_updated_at": state.get("updated_at"),
              "ledger_sha256": digest(raw), "batch": state["batch"], "batch_sha256": digest(batch_raw),
              "target_objects": target_hashes, "summary": summarize(rows), "rows": rows}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    return result["summary"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(freeze(args.root.resolve(), args.version, args.output), indent=2))


if __name__ == "__main__":
    main()
