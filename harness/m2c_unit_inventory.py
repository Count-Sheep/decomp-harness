"""Inventory complete owner objects without claiming source or link readiness."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import harness_paths  # noqa: F401


def uncovered(size: int, spans: list[tuple[int, int]]) -> list[list[int]]:
    cursor = 0
    gaps = []
    for start, end in sorted(spans):
        if start > cursor:
            gaps.append([cursor, start])
        cursor = max(cursor, end)
    if cursor < size:
        gaps.append([cursor, size])
    return gaps


def inventory(owner) -> dict:
    sections = []
    for section in owner.sections:
        if not section.flags & 2:
            continue
        definitions = []
        spans = []
        for symbol in owner.symbols:
            if symbol.shndx != section.index or symbol.type in (3, 4):
                continue
            if not 0 <= symbol.value <= symbol.value + symbol.size <= section.size:
                raise ValueError(f"Symbol outside section: {symbol.name}")
            definitions.append({"index": symbol.index, "name": symbol.name,
                                "offset": symbol.value, "size": symbol.size,
                                "type": symbol.type, "binding": symbol.bind, "other": symbol.other})
            if symbol.size:
                spans.append((symbol.value, symbol.value + symbol.size))
        relocations = []
        for relocation in owner.relocations:
            if relocation.target_section_index != section.index:
                continue
            if not 0 <= relocation.offset < section.size:
                raise ValueError("Relocation outside section")
            if not 0 <= relocation.symbol_index < len(owner.symbols):
                raise ValueError("Invalid relocation symbol")
            symbol = owner.symbols[relocation.symbol_index]
            relocations.append({"offset": relocation.offset, "type": relocation.type,
                                "addend": relocation.addend, "symbol_index": symbol.index,
                                "symbol": symbol.name, "symbol_section": symbol.shndx,
                                "symbol_value": symbol.value})
        sections.append({"index": section.index, "name": section.name, "type": section.type,
                         "flags": section.flags, "size": section.size,
                         "alignment": section.addralign, "entry_size": section.entsize,
                         "bytes_sha256": None if section.type == 8 else hashlib.sha256(section.data).hexdigest(),
                         "definitions": definitions, "relocations": relocations,
                         "unnamed_ranges": uncovered(section.size, spans)})
    return {"schema_version": 1, "scope": "owner_inventory_not_completeness_or_link_proof",
            "target_sha256": hashlib.sha256(owner.data).hexdigest(), "elf_flags": owner.flags,
            "sections": sections,
            "special_symbols": [{"index": s.index, "name": s.name, "section": s.shndx,
                                 "value": s.value, "size": s.size, "info": s.info, "other": s.other}
                                for s in owner.symbols if s.name and (s.shndx == 0 or s.shndx >= 0xFF00)],
            "function_count": sum(s.type == 2 and any(s.shndx == sec["index"] for sec in sections)
                                  for s in owner.symbols)}


def capture(root: Path, unit_name: str) -> dict:
    from permuter import owner_extract
    root = root.resolve()
    raw = (root / "objdiff.json").read_bytes()
    matches = [unit for unit in json.loads(raw)["units"] if unit["name"] == unit_name]
    if len(matches) != 1:
        raise ValueError("Expected one exact objdiff unit name")
    unit = matches[0]
    target = (root / unit["target_path"]).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Target object escapes project root")
    return {**inventory(owner_extract.read_elf(target)), "unit": unit_name,
            "project_root": str(root), "target_path": target.relative_to(root).as_posix(),
            "objdiff_sha256": hashlib.sha256(raw).hexdigest(),
            "configuration": unit}


def track_drafts(report: dict, batches: list[Path]) -> dict:
    """Draft availability is a work queue, not evidence of function exactness."""
    functions = [symbol for section in report["sections"] for symbol in section["definitions"]
                 if symbol["type"] == 2]
    by_name = {}
    inputs = []
    for path in batches:
        raw = path.read_bytes()
        batch = json.loads(raw)
        inputs.append({"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest()})
        for row in batch["results"]:
            unit = row.get("unit", "")
            if report["unit"].startswith("main/") and row.get("module", "main") == "main":
                unit = unit if unit.startswith("main/") else "main/" + unit
            if unit != report["unit"] or row.get("status") != "ok":
                continue
            artifact = row.get("artifacts")
            if not artifact:
                continue
            draft = Path(artifact) / "draft.c"
            if not draft.is_file():
                continue
            source = draft.read_bytes()
            context = draft.parent / "context.c"
            declarations = context.read_bytes() if context.is_file() else b""
            by_name.setdefault(row["symbol"], []).append({"path": str(draft.resolve()),
                "sha256": hashlib.sha256(source).hexdigest(), "batch": str(path.resolve()),
                "context_sha256": hashlib.sha256(declarations).hexdigest(),
                "ownership": "recorded_unit_and_symbol_requires_object_validation"})
    tracked = [{**symbol, "drafts": by_name.get(symbol["name"], [])} for symbol in functions]
    data = [{"section": section["name"], **symbol} for section in report["sections"]
            for symbol in section["definitions"] if symbol["type"] != 2]
    gaps = [{"section": section["name"], "ranges": section["unnamed_ranges"]}
            for section in report["sections"] if section["unnamed_ranges"]]
    return {"unit": report["unit"], "state": "draft", "unit_complete": False,
            "object_verified": False, "link_verified": False,
            "functions": tracked, "drafted_functions": sum(bool(s["drafts"]) for s in tracked),
            "missing_functions": [s["name"] for s in tracked if not s["drafts"]],
            "ambiguous_functions": [s["name"] for s in tracked
                                    if len({(d["sha256"], d["context_sha256"]) for d in s["drafts"]}) > 1],
            "data_definitions_requiring_source": data, "unnamed_ranges_requiring_review": gaps,
            "relocations_requiring_validation": sum(len(s["relocations"]) for s in report["sections"]),
            "inputs": inputs, "target_sha256": report["target_sha256"],
            "objdiff_sha256": report["objdiff_sha256"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-results", type=Path, action="append", default=[])
    args = parser.parse_args()
    report = capture(args.root, args.unit)
    if args.batch_results:
        report["draft_tracker"] = track_drafts(report, args.batch_results)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"unit": report["unit"], "functions": report["function_count"],
                      "sections": len(report["sections"]), "output": str(args.output)}))


if __name__ == "__main__":
    main()
