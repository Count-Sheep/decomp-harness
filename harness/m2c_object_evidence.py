"""Strict scratch function bytes/relocations, with proven local float-pool identity."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

import harness_paths  # noqa: F401  (shared harness configuration)


def float_pool_identity(owner, target, relocation, *, candidate: bool = False):
    """Only a direct SDA float load can ignore a compiler-local label's spelling."""
    symbol = owner.symbols[relocation.symbol_index]
    if (relocation.type != 109 or relocation.addend != 0 or symbol.bind != 0 or symbol.type != 1
            or not re.fullmatch(r"@\d+", symbol.name) or not 0 < symbol.shndx < len(owner.sections)):
        return None
    offset = relocation.offset
    if offset % 4 or offset < target.start or offset + 4 > target.end:
        return None
    instruction = int.from_bytes(target.section.data[offset:offset + 4], "big")
    width = {48: 4, 50: 8}.get(instruction >> 26)
    section = owner.sections[symbol.shndx]
    if (width != symbol.size or section.type != 1 or not section.flags & 2 or section.flags & 4
            or symbol.value < 0 or symbol.value + symbol.size > len(section.data)):
        return None
    if section.flags & 1:
        # MWCC marks generated .sdata2 as WA. Only a candidate pool consumed
        # exclusively by direct float loads may match target readonly bytes.
        if not candidate or section.name != ".sdata2":
            return None
        for use in owner.relocations:
            if use.symbol_index != relocation.symbol_index:
                continue
            used_section = owner.sections[use.target_section_index]
            if not used_section.flags & 2:
                continue
            if (used_section is not target.section or use.type != 109 or use.addend != 0
                    or use.offset % 4 or not target.start <= use.offset <= target.end - 4):
                return None
            opcode = int.from_bytes(used_section.data[use.offset:use.offset + 4], "big") >> 26
            if {48: 4, 50: 8}.get(opcode) != symbol.size:
                return None
    if any(rel.target_section_index == symbol.shndx and symbol.value <= rel.offset < symbol.value + symbol.size
           for rel in owner.relocations):
        return None
    payload = section.data[symbol.value:symbol.value + symbol.size]
    return ("READONLY_FLOAT", section.name, symbol.type, symbol.bind, symbol.other, symbol.size, payload.hex())


def record(owner, symbol: str, *, candidate: bool = False) -> dict:
    from permuter import owner_extract
    target = owner_extract.find_target(owner, symbol)
    value = owner_extract.target_record(owner, target)
    relocations = owner_extract.target_relocations(owner, target)
    identities = {}
    unresolved_pools = []
    seen_offsets = set()
    for relocation in relocations:
        if relocation.offset in seen_offsets:
            raise ValueError("Overlapping target relocations require a separate audit")
        seen_offsets.add(relocation.offset)
        identity = float_pool_identity(owner, target, relocation, candidate=candidate)
        if identity is not None:
            identities[(relocation.offset - target.start, relocation.type, relocation.addend)] = identity
        elif re.fullmatch(r"@\d+", owner.symbols[relocation.symbol_index].name):
            unresolved_pools.append(relocation.offset - target.start)
    normalized = []
    for location, offset, kind, addend, identity in value["relocations"]:
        normalized.append((location, offset, kind, addend, identities.get((offset, kind, addend), identity)))
    return {"size": value["size"], "bytes_sha256": value["bytes_sha256"],
            "relocations": normalized, "float_pool_loads": len(identities),
            "unresolved_constant_relocations": unresolved_pools,
            "symbol_info": target.symbol.info, "symbol_other": target.symbol.other}


def byte_feedback(target: bytes, candidate: bytes) -> dict:
    """Locate raw object differences without equating unresolved relocations."""
    differences = []
    for offset in range(0, max(len(target), len(candidate)), 4):
        before, after = target[offset:offset + 4], candidate[offset:offset + 4]
        if before != after:
            differences.append({"offset": offset, "instruction_index": offset // 4,
                                "target_word": before.hex(), "candidate_word": after.hex()})
    return {"scope": "raw_unlinked_function_bytes", "target_bytes": len(target),
            "candidate_bytes": len(candidate), "different_words": len(differences),
            "first_difference": differences[0] if differences else None,
            "differences": differences,
            "relocation_identity_checked_separately": True}


def compare(target: Path, candidate: Path, symbol: str) -> dict:
    from permuter import owner_extract
    result = {"status": "unavailable", "scratch_object_equal": False,
              "scope": "function_bytes_and_relocations_not_owner_or_link"}
    try:
        objects = {"target": owner_extract.read_elf(target), "candidate": owner_extract.read_elf(candidate)}
        records = {kind: record(obj, symbol, candidate=kind == "candidate") for kind, obj in objects.items()}
        result.update(status="checked", records=records,
                      object_sha256={kind: hashlib.sha256(obj.data).hexdigest() for kind, obj in objects.items()})
        a, b = records["target"], records["candidate"]
        functions = {kind: owner_extract.find_target(obj, symbol) for kind, obj in objects.items()}
        payloads = {kind: fn.section.data[fn.start:fn.end] for kind, fn in functions.items()}
        result["byte_feedback"] = byte_feedback(payloads["target"], payloads["candidate"])
        result["scratch_object_equal"] = (not a["unresolved_constant_relocations"]
            and not b["unresolved_constant_relocations"] and all(a[key] == b[key] for key in
                ("size", "bytes_sha256", "relocations", "symbol_info", "symbol_other")))
    except (OSError, ValueError, RuntimeError) as error:
        result["error"] = str(error)
    return result


def audit(snapshot: Path, experiment: Path, output: Path, *, all_compiled: bool = False) -> dict:
    from m2c_draft_attempts import target_object
    frozen_raw = snapshot.read_bytes()
    frozen = json.loads(frozen_raw)
    manifest = json.loads((experiment / "manifest.json").read_bytes())
    if not manifest.get("finished_at") or manifest.get("snapshot_sha256") != hashlib.sha256(frozen_raw).hexdigest():
        raise ValueError("Object audit requires a completed experiment bound to this snapshot")
    raw = (experiment / "results.json").read_bytes()
    data = json.loads(raw)
    rows = {row["symbol"]: row for row in frozen["rows"]}
    records = []
    for index, pair in enumerate(data["results"]):
        row = rows[pair["symbol"]]
        item = {"symbol": row["symbol"]}
        target = target_object(Path(frozen["root"]), frozen["version"], row)
        expected = frozen["target_objects"].get(str(target))
        if expected is None or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Target is not bound to frozen snapshot: {target}")
        for side in ("baseline", "candidate"):
            measured = pair[side]
            outcome = measured["outcome"]
            if outcome.get("compiled") and (all_compiled or outcome.get("pct") == 100):
                profile = outcome.get("profile_artifact") or outcome.get("profile")
                path = experiment / f"{index:05d}" / side / "score" / f"{profile}.o"
                bound_hash = measured.get("compiled_object_sha256")
                if bound_hash and hashlib.sha256(path.read_bytes()).hexdigest() != bound_hash:
                    raise ValueError(f"Compiled object changed since measurement: {path}")
                item[side] = compare(target, path, row["symbol"])
                item[side]["bound_to_compile_measurement"] = bool(bound_hash)
        records.append(item)
    result = {"snapshot_sha256": hashlib.sha256(frozen_raw).hexdigest(),
              "all_compiled": all_compiled,
              "experiment_sha256": hashlib.sha256(raw).hexdigest(),
              "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "summary": {side: sum(item.get(side, {}).get("scratch_object_equal", False) for item in records)
                          for side in ("baseline", "candidate")}, "results": records}
    with output.open("x", encoding="utf-8") as file:
        json.dump(result, file, indent=2)
    return result["summary"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("experiment", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--all-compiled", action="store_true", help="also audit non-100%% compiled candidates")
    args = parser.parse_args()
    print(json.dumps(audit(args.snapshot, args.experiment, args.output, all_compiled=args.all_compiled), indent=2))


if __name__ == "__main__":
    main()
