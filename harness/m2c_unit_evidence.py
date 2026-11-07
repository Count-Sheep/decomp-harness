"""Conservative whole-owner comparison; never a substitute for the link gate."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from itertools import islice
import json
from pathlib import Path

import harness_paths  # noqa: F401
from m2c_unit_inventory import inventory


def collection_difference(before: list, after: list, limit: int = 8) -> dict:
    left = Counter(json.dumps(row, sort_keys=True) for row in before)
    right = Counter(json.dumps(row, sort_keys=True) for row in after)
    removed, added = left - right, right - left
    return {"removed_count": sum(removed.values()), "added_count": sum(added.values()),
            "removed": [json.loads(row) for row in islice(removed.elements(), limit)],
            "added": [json.loads(row) for row in islice(added.elements(), limit)]}


def diagnostics(before: dict, after: dict) -> dict:
    left = {section["name"]: section for section in before["sections"]}
    right = {section["name"]: section for section in after["sections"]}
    details = {"section_order": {"target": list(left), "candidate": list(right)}, "sections": []}
    for name in dict.fromkeys([*left, *right]):
        old, new = left.get(name), right.get(name)
        if old == new:
            continue
        entry = {"name": name}
        if old is None or new is None:
            present = old if old is not None else new
            entry.update(state="missing" if new is None else "extra", size=present["size"],
                         symbols=len(present["symbols"]), relocations=len(present["relocations"]))
        else:
            entry["attributes"] = {key: {"target": old[key], "candidate": new[key]}
                                   for key in ("type", "flags", "size", "alignment", "entry_size")
                                   if old[key] != new[key]}
            if old["bytes"] != new["bytes"]:
                a, b = bytes.fromhex(old["bytes"] or ""), bytes.fromhex(new["bytes"] or "")
                offsets = [index for index, (x, y) in enumerate(zip(a, b)) if x != y]
                first = offsets[0] if offsets else min(len(a), len(b))
                start = first - first % 4
                entry["bytes"] = {"first_difference": first, "different_overlap_bytes": len(offsets),
                                  "target_only_bytes": max(0, len(a) - len(b)),
                                  "candidate_only_bytes": max(0, len(b) - len(a)),
                                  "window_offset": start, "target_window": a[start:start + 16].hex(),
                                  "candidate_window": b[start:start + 16].hex()}
            for key in ("symbols", "relocations"):
                if old[key] != new[key]:
                    entry[key] = collection_difference(old[key], new[key])
        details["sections"].append(entry)
    if before["special_symbols"] != after["special_symbols"]:
        details["special_symbols"] = collection_difference(before["special_symbols"], after["special_symbols"])
    if before["elf_flags"] != after["elf_flags"]:
        details["elf_flags"] = {"target": before["elf_flags"], "candidate": after["elf_flags"]}
    return details


def canonical(owner) -> dict:
    inventory(owner)  # Validate symbol and relocation bounds before comparing.
    allocated = [section for section in owner.sections if section.flags & 2]
    names = [section.name for section in allocated]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate allocated section names require an explicit adapter")

    def identity(symbol):
        index = symbol.shndx
        if index == 0 or index >= 0xFF00:
            section = ["special", index]
        elif index < len(owner.sections) and owner.sections[index].flags & 2:
            section = ["allocated", owner.sections[index].name]
        else:
            raise ValueError("Link-relevant symbol references a nonallocated section")
        return [symbol.name, section, symbol.value, symbol.size, symbol.info, symbol.other]

    sections = []
    for section in allocated:
        if section.type not in (1, 8) or section.flags & ~7:
            raise ValueError("Unsupported allocated section semantics")
        if section.type == 1 and len(section.data) != section.size:
            raise ValueError("Truncated section bytes")
        symbols = [identity(s) for s in owner.symbols
                   if s.shndx == section.index and s.type not in (3, 4)]
        relocations = []
        for rel in owner.relocations:
            if rel.target_section_index != section.index:
                continue
            if rel.addend is None:
                raise ValueError("Implicit relocation addends require an explicit adapter")
            relocations.append([rel.offset, rel.type, rel.addend,
                                identity(owner.symbols[rel.symbol_index])])
        sections.append({"name": section.name, "type": section.type, "flags": section.flags,
                         "size": section.size, "alignment": section.addralign,
                         "entry_size": section.entsize,
                         "bytes": section.data.hex() if section.type != 8 else None,
                         "symbols": symbols, "relocations": relocations})
    special = [identity(s) for s in owner.symbols if s.name and s.type != 4
               and (s.shndx == 0 or s.shndx >= 0xFF00)]
    return {"elf_flags": owner.flags, "sections": sections, "special_symbols": special}


def compare_objects(target, candidate) -> dict:
    result = {"status": "unavailable", "object_verified": False, "link_verified": False,
              "scope": "allocated_sections_symbols_and_relocations",
              "target_sha256": hashlib.sha256(target.data).hexdigest(),
              "candidate_sha256": hashlib.sha256(candidate.data).hexdigest()}
    try:
        before, after = canonical(target), canonical(candidate)
        differences = [key for key in before if before[key] != after[key]]
        result.update(status="checked", object_verified=not differences, differences=differences)
        if differences:
            result["diagnostics"] = diagnostics(before, after)
    except ValueError as error:
        result["error"] = str(error)
    return result


def main() -> None:
    from permuter import owner_extract
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare_objects(owner_extract.read_elf(args.target), owner_extract.read_elf(args.candidate))
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
