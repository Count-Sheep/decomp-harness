"""Index frozen exact drafts and source-backed references by PowerPC structure."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re

import m2c_context as context
import m2c_compile_repair as repair
from m2c_failure_audit import digest


def normalized_shape(assembly: str) -> list[str]:
    """Retain register roles, opcode order and local branch targets; abstract literals."""
    literals: dict[str, str] = {}
    symbols: dict[str, str] = {}
    result = []
    token = re.compile(r"\.L\d+\b|\b(?:r\d+|f\d+|cr\d+)\b|@\d+|"
                       r"(?<![\w.])-?(?:0x[0-9A-Fa-f]+|\d+)\b|[A-Za-z_$][\w.$]*")

    def normalize(match: re.Match) -> str:
        value = match[0]
        if re.fullmatch(r"\.L\d+|(?:r|f|cr)\d+", value) or value in {"sda21", "sda2", "ha", "h", "l"}:
            return value
        try:
            key = str(int(value, 0))
        except ValueError:
            return symbols.setdefault(value, f"S{len(symbols)}")
        return literals.setdefault(key, f"N{len(literals)}")

    for instruction in repair._instructions(assembly):
        opcode, _, operands = instruction.partition(" ")
        result.append(opcode + (" " + token.sub(normalize, operands).replace(" ", "") if operands else ""))
    return result


def entry(symbol: str, assembly: str, source: str, provenance: dict, compiler: dict | None) -> dict | None:
    shape = normalized_shape(assembly)
    if not shape or not source:
        return None
    features = context.parse_assembly_features(assembly, symbol)
    return {"symbol": symbol, "shape": shape, "shape_sha256": digest("\n".join(shape).encode()),
            "assembly": assembly, "assembly_sha256": digest(assembly.encode()),
            "source": source, "source_sha256": digest(source.encode()),
            "features": features, "compiler": compiler, "provenance": provenance,
            "requires_target_validation": True}


def retrieve(index: dict, assembly: str, symbol: str, limit: int = 5, *, include_same_symbol: bool = False) -> list[dict]:
    shape = normalized_shape(assembly)
    if not shape:
        return []
    key = digest("\n".join(shape).encode())
    candidates = [index["templates"][number] for number in index["groups"].get(key, [])]
    return sorted((row for row in candidates if include_same_symbol or row["symbol"] != symbol),
                  key=lambda row: (row["provenance"]["kind"] != "scratch_instruction_exact",
                                   row["provenance"]["project"], row["symbol"]))[:limit]


def direct_drafts(index: dict, assembly: str, symbol: str, limit: int = 3, *,
                  include_same_symbol: bool = False) -> list[dict]:
    """Copy C only when every instruction operand also agrees with the target."""
    if limit <= 0 or not context.C_IDENTIFIER.fullmatch(symbol):
        return []
    target = repair._instructions(assembly)
    result = []
    sources = set()
    for candidate in retrieve(index, assembly, symbol, limit=len(index["templates"]),
                              include_same_symbol=include_same_symbol):
        if (digest(candidate["assembly"].encode()) != candidate["assembly_sha256"]
                or digest(candidate["source"].encode()) != candidate["source_sha256"]):
            raise ValueError("Template source or assembly changed since indexing")
        if repair._instructions(candidate["assembly"]) != target:
            continue
        source = candidate["source"]
        clean = repair._mask_comments_and_strings(source)
        old_name = candidate["symbol"]
        if not re.search(rf"\b{re.escape(old_name)}\s*\([^;{{}}]*\)\s*{{", clean):
            continue
        # Identifier positions from the masked C leave comments and literals intact.
        for match in reversed(list(re.finditer(rf"\b{re.escape(old_name)}\b", clean))):
            source = source[:match.start()] + symbol + source[match.end():]
        key = digest(source.encode())
        if key in sources:
            continue
        sources.add(key)
        result.append({"code": source, "source_sha256": key, "compiler": candidate["compiler"],
                       "reference": candidate["symbol"], "provenance": candidate["provenance"],
                       "method": "identical_instruction_template", "requires_target_validation": True})
        if len(result) >= limit:
            break
    return result


def build(snapshot: Path, output: Path, include_references: bool = True) -> dict:
    raw = snapshot.read_bytes()
    frozen = json.loads(raw)
    rows = []
    for row in frozen["rows"]:
        if row["evidence"].get("instruction_exact") is not True:
            continue
        candidate = entry(row["symbol"], row["assembly"], row["candidate"],
                          {"kind": "scratch_instruction_exact", "project": frozen["version"],
                           "snapshot_sha256": digest(raw), "unit": row["unit"]},
                          {"mw_version": row["outcome"].get("profile"),
                           "flags": row["outcome"].get("compiler_flags")})
        if candidate:
            rows.append(candidate)
    reports = {}
    if include_references:
        # The snapshot's own project contributes its source-backed exact functions as well.
        root = Path(frozen.get("root") or context.harness_paths.DECOMP_ROOT).resolve()
        target = context.target_project_id(root, frozen["version"])
        specs = [context.ProjectSpec(target, root, frozen["version"]),
                 *context.default_reference_projects(target_project=target, target_root=root)]
        for spec in specs:
            report_path = spec.root / "build" / spec.version / "report.json"
            if not report_path.is_file():
                continue
            report_hash = digest(report_path.read_bytes())
            reports[spec.name] = report_hash
            matched = context._report_exact_rows(spec)
            blocks = context._assembly_blocks(spec, {row["symbol"] for row in matched})
            sources = {}
            for row in matched:
                choices = blocks.get(row["symbol"], [])
                choices = [block for address, block in choices if address == row["address"]]
                if len(choices) != 1:
                    continue
                path = spec.root / row["source_path"]
                if path not in sources:
                    sources[path] = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
                signature, snippet = context._source_function(sources[path], row["symbol"], maximum=1000000)
                candidate = entry(row["symbol"], choices[0], snippet or "",
                                  {"kind": "source_report_100", "project": spec.name,
                                   "report_sha256": report_hash, "source_path": str(path),
                                   "source_file_sha256": digest(sources[path].encode()),
                                   "signature": signature, "unit": row["unit"]}, row.get("compiler"))
                if candidate:
                    rows.append(candidate)
            if digest(report_path.read_bytes()) != report_hash:
                raise ValueError(f"Reference report changed during capture: {report_path}")
    groups = defaultdict(list)
    for index, row in enumerate(rows):
        groups[row["shape_sha256"]].append(index)
    result = {"schema_version": 1, "snapshot_sha256": digest(raw), "reports": reports,
              "summary": {"templates": len(rows), "shapes": len(groups),
                          "shared_shapes": sum(len(group) > 1 for group in groups.values()),
                          "projects": dict(Counter(row["provenance"]["project"] for row in rows))},
              "groups": dict(groups), "templates": rows}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    return result["summary"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--drafts-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build(args.snapshot, args.output, not args.drafts_only), indent=2))


if __name__ == "__main__":
    main()
