#!/usr/bin/env python3
"""Synthesize reviewable decomp bootstrap evidence from a stripped binary.

This stage deliberately separates observations from conclusions. Exact
cross-project code matches may propose names and owner anchors. Ghidra signatures
and call clusters remain hints until compilation and object verification prove
them.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import struct
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import harness_paths


SYMBOL = re.compile(
    r"^(?P<name>\S+) = (?P<section>\S+):0x(?P<address>[0-9A-Fa-f]+);"
    r".*?\btype:(?P<type>\w+)(?:\s+size:0x(?P<size>[0-9A-Fa-f]+))?"
)
SPLIT_UNIT = re.compile(r"^(?P<name>\S.*):$")
SPLIT_RANGE = re.compile(
    r"^\s*(?P<section>\S+)\s+start:0x(?P<start>[0-9A-Fa-f]+)"
    r"\s+end:0x(?P<end>[0-9A-Fa-f]+)"
)
DEFAULT_NAMES = ("FUN_", "fn_", "sub_")


class DolImage:
    """Minimal bounds-checked DOL virtual-address reader."""

    def __init__(self, data: bytes):
        if len(data) < 0xE4:
            raise ValueError("DOL header is truncated")
        offsets = struct.unpack_from(">18I", data, 0)
        addresses = struct.unpack_from(">18I", data, 0x48)
        sizes = struct.unpack_from(">18I", data, 0x90)
        self.data = data
        self.sections = []
        for offset, address, size in zip(offsets, addresses, sizes, strict=True):
            if not size:
                continue
            if offset + size > len(data):
                raise ValueError("DOL section exceeds the input file")
            self.sections.append((address, address + size, offset))

    @classmethod
    def read_file(cls, path: Path) -> "DolImage":
        return cls(path.read_bytes())

    def read(self, address: int, size: int) -> bytes | None:
        for start, end, offset in self.sections:
            if start <= address and address + size <= end:
                return self.data[offset + address - start:offset + address - start + size]
        return None


def normalized_code(code: bytes) -> bytes:
    if len(code) % 4:
        raise ValueError("PowerPC code length is not word aligned")
    output = bytearray()
    for offset in range(0, len(code), 4):
        word = int.from_bytes(code[offset:offset + 4], "big")
        opcode = word >> 26
        if opcode == 18:
            word &= 0xFC000003
        elif opcode == 16:
            word &= 0xFFFF0003
        output.extend(word.to_bytes(4, "big"))
    return bytes(output)


def code_comparison(target_image: Any, reference_image: Any, target: dict[str, Any],
                    reference: dict[str, Any]) -> dict[str, Any]:
    size = int(target.get("size") or 0)
    reference_size = int(reference.get("size") or 0)
    reference_address = integer(reference.get("address"))
    if not size or size != reference_size or reference_address is None:
        return {"status": "unavailable"}
    target_code = target_image.read(target["address"], size) if target_image else None
    reference_code = reference_image.read(reference_address, size) if reference_image else None
    if target_code is None or reference_code is None:
        return {"status": "unavailable"}
    exact = target_code == reference_code
    normalized = exact or normalized_code(target_code) == normalized_code(reference_code)
    return {"status": "exact_bytes" if exact else "normalized_exact" if normalized else "different",
            "size": size, "target_sha256": hashlib.sha256(target_code).hexdigest(),
            "reference_sha256": hashlib.sha256(reference_code).hexdigest()}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def integer(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            return None
    return None


def read_symbols(path: Path | None) -> dict[int, dict[str, Any]]:
    result = {}
    if path is None:
        return result
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = SYMBOL.match(line)
        if not match or match["type"] != "function":
            continue
        address = int(match["address"], 16)
        result[address] = {"name": match["name"], "section": match["section"],
                           "size": int(match["size"], 16) if match["size"] else None}
    return result


def read_splits(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    owner = None
    result = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if match := SPLIT_UNIT.match(line):
            owner = match["name"]
            continue
        if owner and (match := SPLIT_RANGE.match(line)):
            result.append({"owner": owner, "section": match["section"],
                           "start": int(match["start"], 16), "end": int(match["end"], 16)})
    return result


def current_owner(address: int, splits: list[dict[str, Any]]) -> str | None:
    owners = [row["owner"] for row in splits if row["section"] in (".text", ".init")
              and row["start"] <= address < row["end"]]
    return owners[0] if len(owners) == 1 else None


def match_rows(payload: dict[str, Any], version: str) -> list[dict[str, Any]]:
    """Normalize exact-match ledgers and one-version candidate reports."""
    if isinstance(payload.get("names"), dict):
        rows = []
        for name, versions in payload["names"].items():
            row = versions.get(version)
            if row:
                rows.append({**row, "suggested_name": name})
        return rows
    return list(payload.get("candidates") or [])


def preferred_owner(references: list[dict[str, Any]], projects: tuple[str, ...]) -> tuple[str, str] | None:
    for project in projects:
        choices = sorted({str(row["unit"]) for row in references
                          if row.get("project") == project and row.get("unit")})
        if len(choices) == 1:
            return project, choices[0]
    return None


def canonical_owner(value: str) -> str:
    value = value.removeprefix("main/")
    return re.sub(r"\.(?:c|s)$", "", value)


def non_default_signature(row: dict[str, Any]) -> bool:
    signature = str(row.get("signature") or "").strip()
    source = str(row.get("signature_source") or row.get("source") or "DEFAULT")
    lowered = signature.lower()
    return bool(signature and source != "DEFAULT" and "undefined" not in lowered
                and not lowered.startswith("void fun_"))


def definition_signature(path: Path, symbol: str) -> str | None:
    pattern = re.compile(r"^\s*(?P<signature>[^;{}]*\b" + re.escape(symbol) +
                         r"\s*\([^;{}]*\))\s*(?P<brace>\{)?\s*$")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for index, line in enumerate(lines):
        if match := pattern.match(line):
            following = next((value.strip() for value in lines[index + 1:] if value.strip()), "")
            if match["brace"] or following.startswith("{"):
                return " ".join(match["signature"].split())
    return None


def exact_candidate_types(attempts: dict[str, Any] | None) -> list[dict[str, Any]]:
    result = []
    for row in (attempts or {}).get("items", {}).values():
        if float(row.get("best_pct") or 0) != 100.0 or not row.get("best_source"):
            continue
        path = Path(row["best_source"])
        if not path.is_file() or not (signature := definition_signature(path, str(row["symbol"]))):
            continue
        result.append({"address": f"0x{int(row['address']):08X}", "name": row["symbol"],
                       "signature": signature, "source": "instruction_exact_m2c_candidate",
                       "source_path": str(path.resolve()), "source_sha256": sha256(path),
                       "confidence": "instruction_exact_abi_hypothesis_requires_unit_validation"})
    return sorted(result, key=lambda row: row["address"])


def strongly_connected(functions: list[dict[str, Any]]) -> list[list[int]]:
    addresses = {row["address"] for row in functions}
    graph = {row["address"]: [target for target in row["direct_calls"] if target in addresses]
             for row in functions}
    index = 0
    indices: dict[int, int] = {}
    low: dict[int, int] = {}
    stack: list[int] = []
    active: set[int] = set()
    output: list[list[int]] = []

    def visit(node: int) -> None:
        nonlocal index
        indices[node] = low[node] = index
        index += 1
        stack.append(node)
        active.add(node)
        for target in graph[node]:
            if target not in indices:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in active:
                low[node] = min(low[node], indices[target])
        if low[node] == indices[node]:
            component = []
            while True:
                member = stack.pop()
                active.remove(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1:
                output.append(sorted(component))

    for address in sorted(graph):
        if address not in indices:
            visit(address)
    return sorted(output, key=lambda row: (row[0], len(row)))


def anchor_regions(anchors: list[dict[str, Any]], functions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return bounded owner regions; these are proposals, not exact splits."""
    ordered = sorted(anchors, key=lambda row: row["address"])
    runs: list[list[dict[str, Any]]] = []
    for anchor in ordered:
        if runs and runs[-1][-1]["reference_owner"] == anchor["reference_owner"]:
            runs[-1].append(anchor)
        else:
            runs.append([anchor])
    result = []
    for run in runs:
        if len(run) < 2:
            continue
        start = run[0]["address"]
        end = run[-1]["address"] + (run[-1].get("size") or 0)
        members = [row for row in functions if start <= row["address"] < end]
        member_addresses = {row["address"] for row in members}
        internal = sum(target in member_addresses for row in members for target in row["direct_calls"])
        outgoing = sum(target not in member_addresses for row in members for target in row["direct_calls"])
        current_owners = sorted({row["current_owner"] for row in members if row.get("current_owner")})
        confidence = "high" if len(run) >= 3 and internal >= outgoing else "medium"
        result.append({
            "reference_owner": run[0]["reference_owner"],
            "reference_project": run[0]["reference_project"],
            "first_anchor": run[0]["suggested_name"],
            "last_anchor": run[-1]["suggested_name"],
            "anchor_count": len(run),
            "observed_start": f"0x{start:08X}",
            "observed_end": f"0x{end:08X}",
            "function_count_in_envelope": len(members),
            "unanchored_functions_in_envelope": max(0, len(members) - len(run)),
            "internal_calls": internal,
            "outgoing_calls": outgoing,
            "current_owners": current_owners,
            "confidence": confidence,
            "boundary_semantics": "anchor_envelope_not_exact_object_boundary",
        })
    return result


def reference_sequences(catalog: dict[str, Any] | None, project: str,
                        functions: list[dict[str, Any]], anchors: list[dict[str, Any]],
                        target_image: Any = None, reference_image: Any = None) -> tuple[list, list]:
    """Align anchored function-size runs without claiming byte identity."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in (catalog or {}).get("functions") or []:
        if row.get("project") == project and row.get("unit") and integer(row.get("size")):
            grouped[str(row["unit"])].append(row)
    target_sizes = [int(row.get("size") or 0) for row in functions]
    anchor_owners = defaultdict(set)
    for anchor in anchors:
        if anchor["reference_project"] == project:
            anchor_owners[anchor["address"]].add(canonical_owner(anchor["reference_owner"]))
    regions = []
    hints = []
    for owner, rows in grouped.items():
        rows.sort(key=lambda row: integer(row.get("address")) or 0)
        sizes = [int(row["size"]) for row in rows]
        match = difflib.SequenceMatcher(None, target_sizes, sizes, autojunk=False).find_longest_match()
        if match.size < 4:
            continue
        target_rows = functions[match.a:match.a + match.size]
        reference_rows = rows[match.b:match.b + match.size]
        byte_count = sum(sizes[match.b:match.b + match.size])
        if byte_count < 256:
            continue
        canonical = canonical_owner(owner)
        matching_anchors = [row for row in target_rows if canonical in anchor_owners[row["address"]]]
        if not matching_anchors:
            continue
        sequence = target_sizes[match.a:match.a + match.size]
        occurrences = sum(target_sizes[index:index + match.size] == sequence
                          for index in range(len(target_sizes) - match.size + 1))
        if occurrences != 1:
            continue
        complete = match.b == 0 and match.size == len(rows)
        region_id = hashlib.sha256(
            f"{project}\0{owner}\0{target_rows[0]['address']:08X}".encode()
        ).hexdigest()[:16]
        comparisons = [code_comparison(target_image, reference_image, target, reference)
                       for target, reference in zip(target_rows, reference_rows, strict=True)]
        comparison_counts = {status: sum(row["status"] == status for row in comparisons)
                             for status in ("exact_bytes", "normalized_exact", "different", "unavailable")}
        regions.append({
            "id": region_id,
            "reference_owner": owner,
            "reference_project": project,
            "target_start": f"0x{target_rows[0]['address']:08X}",
            "target_end": f"0x{target_rows[-1]['address'] + int(target_rows[-1].get('size') or 0):08X}",
            "reference_start_index": match.b,
            "reference_function_count": len(rows),
            "aligned_function_count": match.size,
            "aligned_bytes": byte_count,
            "exact_anchor_count": len(matching_anchors),
            "complete_reference_function_sequence": complete,
            "unique_target_size_sequence": True,
            "code_comparison": comparison_counts,
            "confidence": "high" if len(matching_anchors) >= 2 and match.size >= 6 else "medium",
            "boundary_semantics": "anchored_size_sequence_not_byte_or_data_boundary_proof",
        })
        for target, reference, comparison in zip(target_rows, reference_rows, comparisons, strict=True):
            if target["name"] == reference["symbol"]:
                continue
            verified = comparison["status"] in ("exact_bytes", "normalized_exact")
            hints.append({"address": f"0x{target['address']:08X}", "current_name": target["name"],
                          "suggested_name": reference["symbol"], "reference_owner": owner,
                          "reference_project": project, "region_id": region_id,
                          "reference_source_path": reference.get("source_path"),
                          "code_comparison": comparison,
                          "confidence": comparison["status"] if verified else
                                        "anchored_unique_function_size_sequence",
                          "promotion": "reviewable_code_match" if verified else
                                       "requires_independent_code_match"})
    return regions, hints


def split_proposals(regions: list[dict[str, Any]], functions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    proposals = []
    for row in regions:
        if not row["complete_reference_function_sequence"]:
            continue
        start, end = integer(row["target_start"]), integer(row["target_end"])
        if start is None or end is None:
            continue
        current = sorted({function["current_owner"] for function in functions
                          if start <= function["address"] < end and function.get("current_owner")})
        comparisons = row["code_comparison"]
        verified = comparisons["exact_bytes"] + comparisons["normalized_exact"]
        proposals.append({"id": row["id"],
                          "suggested_owner": canonical_owner(row["reference_owner"]) + ".c",
                          "section": ".text", "start": row["target_start"], "end": row["target_end"],
                          "function_count": row["aligned_function_count"],
                          "code_verified_functions": verified,
                          "current_owners": current,
                          "confidence": "high" if verified == row["aligned_function_count"] else "medium",
                          "status": "review_required",
                          "semantics": "text_split_only_not_data_or_whole_object_ownership"})
    return proposals


def reference_type_hints(hints: list[dict[str, Any]], roots: dict[str, Path]) -> list[dict[str, Any]]:
    result = []
    for row in hints:
        if row["promotion"] != "reviewable_code_match" or not row.get("reference_source_path"):
            continue
        root = roots.get(row["reference_project"])
        path = (root / row["reference_source_path"]).resolve() if root else None
        if not path or not path.is_file():
            continue
        signature = definition_signature(path, row["suggested_name"])
        if not signature:
            continue
        target_name = row.get("current_name") or row["suggested_name"]
        result.append({"address": row["address"], "name": row["suggested_name"],
                       "signature": signature, "source": "code_verified_reference_source",
                       "target_name": target_name,
                       "target_signature": signature.replace(row["suggested_name"],
                                                             target_name, 1),
                       "source_path": str(path), "source_sha256": sha256(path),
                       "reference_project": row["reference_project"],
                       "confidence": "code_verified_reference_abi_hypothesis_requires_unit_validation"})
    return sorted(result, key=lambda row: (row["address"], row["name"]))


def default_preferred_projects() -> tuple[str, ...]:
    """Reference project ids, in configured order, for the selected project."""
    return tuple(project.id for project in harness_paths.reference_projects())


def build(ghidra: dict[str, Any], matches: dict[str, Any], version: str,
          symbols: dict[int, dict[str, Any]] | None = None,
          splits: list[dict[str, Any]] | None = None,
          attempts: dict[str, Any] | None = None,
          reference_catalog: dict[str, Any] | None = None,
          target_image: Any = None,
          reference_images: dict[str, Any] | None = None,
          reference_roots: dict[str, Path] | None = None,
          preferred_projects: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Assemble the bootstrap report.

    ``preferred_projects`` orders the reference projects whose owners anchor units; it
    defaults to the configured reference projects of the selected project.
    """
    if preferred_projects is None:
        preferred_projects = default_preferred_projects()
    symbols = symbols or {}
    splits = splits or []
    functions = []
    for source in ghidra.get("functions") or []:
        address = integer(source.get("address"))
        if address is None:
            continue
        calls = sorted({target for value in source.get("direct_calls") or []
                        if (target := integer(value)) is not None})
        known = symbols.get(address, {})
        functions.append({**source, "address": address, "direct_calls": calls,
                          "name": known.get("name") or source.get("name") or f"fn_{address:08X}",
                          "size": known.get("size") or source.get("body_bytes"),
                          "current_owner": current_owner(address, splits)})
    functions.sort(key=lambda row: row["address"])
    by_address = {row["address"]: row for row in functions}

    proposals = []
    anchors = []
    conflicts = []
    for row in match_rows(matches, version):
        address = integer(row.get("address"))
        name = row.get("suggested_name")
        if address is None or not name:
            continue
        target = by_address.get(address)
        if target is None:
            conflicts.append({"kind": "match_without_analyzed_function",
                              "address": f"0x{address:08X}", "suggested_name": name})
            continue
        references = list(row.get("references") or [])
        owner = preferred_owner(references, preferred_projects)
        proposal = {"address": f"0x{address:08X}", "current_name": target["name"],
                    "suggested_name": name, "size": row.get("size") or target.get("size"),
                    "current_owner": target.get("current_owner"),
                    "confidence": "exact", "evidence": "unique_normalized_code_match",
                    "references": references, "already_named": target["name"] == name}
        proposals.append(proposal)
        if owner:
            anchors.append({**proposal, "address": address,
                            "reference_project": owner[0], "reference_owner": owner[1]})

    ghidra_type_hints = [{"address": f"0x{row['address']:08X}", "name": row["name"],
                   "signature": row["signature"],
                   "source": row.get("signature_source") or row.get("source"),
                   "confidence": "hypothesis_requires_compile_validation"}
                  for row in functions if non_default_signature(row)]
    candidate_type_hints = exact_candidate_types(attempts)
    type_hints = ghidra_type_hints + candidate_type_hints
    type_hints.sort(key=lambda row: (row["address"], row["source"]))
    components = strongly_connected(functions)
    call_clusters = [{"addresses": [f"0x{address:08X}" for address in component],
                      "names": [by_address[address]["name"] for address in component],
                      "span": f"0x{component[-1] - component[0]:X}",
                      "evidence": "mutually_reachable_direct_call_component",
                      "unit_semantics": "neighborhood_hint_not_object_boundary"}
                     for component in components]
    regions = anchor_regions(anchors, functions)
    sequence_regions = []
    sequence_hints = []
    reference_images = reference_images or {}
    for project in preferred_projects:
        found_regions, found_hints = reference_sequences(reference_catalog, project, functions, anchors,
                                                         target_image, reference_images.get(project))
        sequence_regions.extend(found_regions)
        sequence_hints.extend(found_hints)
    proposed_splits = split_proposals(sequence_regions, functions)
    verified_hints = [row for row in sequence_hints if row["promotion"] == "reviewable_code_match"]
    source_type_hints = reference_type_hints(verified_hints, reference_roots or {})
    type_hints.extend(source_type_hints)
    type_hints.sort(key=lambda row: (row["address"], row["source"]))
    called = {target for row in functions for target in row["direct_calls"]}
    return {
        "schema_version": 1,
        "scope": "bootstrap_evidence_not_verified_symbols_types_or_unit_boundaries",
        "generated_at": datetime.now(UTC).isoformat(),
        "version": version,
        "binary": {key: ghidra.get(key) for key in
                   ("program", "input_sha256", "language", "compiler_spec")},
        "summary": {
            "analyzed_functions": len(functions),
            "direct_call_edges": sum(len(row["direct_calls"]) for row in functions),
            "call_targets_inside_inventory": sum(target in by_address for row in functions
                                                   for target in row["direct_calls"]),
            "call_roots": sum(row["address"] not in called for row in functions),
            "exact_symbol_proposals": len(proposals),
            "unit_anchors": len(anchors),
            "bounded_unit_regions": len(regions),
            "type_hints": len(type_hints),
            "ghidra_type_hints": len(ghidra_type_hints),
            "exact_candidate_type_hints": len(candidate_type_hints),
            "code_verified_reference_type_hints": len(source_type_hints),
            "recursive_call_clusters": len(call_clusters),
            "reference_sequence_regions": len(sequence_regions),
            "sequence_symbol_hints": len(sequence_hints),
            "code_verified_sequence_symbols": len(verified_hints),
            "text_split_proposals": len(proposed_splits),
            "conflicts": len(conflicts),
        },
        "symbol_proposals": sorted(proposals, key=lambda row: row["address"]),
        "type_hints": type_hints,
        "unit_anchors": [{**row, "address": f"0x{row['address']:08X}"} for row in anchors],
        "unit_regions": regions,
        "call_clusters": call_clusters,
        "reference_sequence_regions": sequence_regions,
        "sequence_symbol_hints": sequence_hints,
        "code_verified_sequence_symbols": verified_hints,
        "text_split_proposals": proposed_splits,
        "conflicts": conflicts,
        "policy": {
            "symbol_promotion": "exact unique match may be reviewed for promotion",
            "type_promotion": "never automatic; compile and object evidence required",
            "unit_promotion": "never automatic; anchor regions do not establish exact boundaries",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ghidra", type=Path, required=True)
    parser.add_argument("--matches", type=Path, required=True)
    parser.add_argument("--version", default=harness_paths.VERSION,
                        help="target build version (default: the selected project's version)")
    parser.add_argument("--symbols", type=Path)
    parser.add_argument("--splits", type=Path)
    parser.add_argument("--attempts", type=Path,
                        help="optional refinement state; only instruction-exact signatures become hints")
    parser.add_argument("--reference-catalog", type=Path,
                        help="optional matched corpus catalog for anchored owner-sequence alignment")
    parser.add_argument("--target-binary", type=Path,
                        help="optional DOL used to independently verify aligned target code")
    parser.add_argument("--reference-binary", action="append", default=[], metavar="PROJECT=PATH",
                        help="reference DOL for sequence code checks; may be repeated")
    parser.add_argument("--reference-root", action="append", default=[], metavar="PROJECT=PATH",
                        help="reference source root used for code-verified prototype hypotheses")
    parser.add_argument("--prefer-project", action="append", default=[],
                        help="reference project id to anchor owners (default: configured reference projects)")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.version:
        parser.error("no project version: configure a project (harness.local.json) or pass --version")
    reference_paths = {}
    for value in args.reference_binary:
        if "=" not in value:
            parser.error("--reference-binary must be PROJECT=PATH")
        project, path = value.split("=", 1)
        if not project or project in reference_paths:
            parser.error("reference binary projects must be unique and non-empty")
        reference_paths[project] = Path(path)
    if reference_paths and not args.target_binary:
        parser.error("--target-binary is required with --reference-binary")
    reference_roots = {}
    for value in args.reference_root:
        if "=" not in value:
            parser.error("--reference-root must be PROJECT=PATH")
        project, path = value.split("=", 1)
        if not project or project in reference_roots:
            parser.error("reference root projects must be unique and non-empty")
        reference_roots[project] = Path(path)
    inputs = [args.ghidra, args.matches,
              *(path for path in (args.symbols, args.splits, args.attempts, args.reference_catalog,
                                  args.target_binary) if path), *reference_paths.values()]
    def load(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    report = build(load(args.ghidra), load(args.matches),
                   args.version, read_symbols(args.symbols), read_splits(args.splits),
                   load(args.attempts) if args.attempts else None,
                   load(args.reference_catalog) if args.reference_catalog else None,
                   DolImage.read_file(args.target_binary) if args.target_binary else None,
                   {project: DolImage.read_file(path) for project, path in reference_paths.items()},
                   reference_roots,
                   tuple(args.prefer_project) or default_preferred_projects())
    bound_sources = sorted({Path(row["source_path"]) for row in report["type_hints"]
                            if row.get("source_path") and Path(row["source_path"]).is_file()})
    inputs.extend(bound_sources)
    report["inputs"] = [{"path": str(path.resolve()), "sha256": sha256(path)} for path in inputs]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**report["summary"], "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
