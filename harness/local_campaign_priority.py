"""Evidence-based scheduling hints; these never confer acceptance or progress."""

from bisect import bisect_right
from collections import defaultdict

import harness_paths  # noqa: F401  (loads the project configuration)
from permuter.owner_extract import ExtractError, STT_FUNC, read_elf


def reference_graph(root, units, functions):
    """Read function references from retail objects, preserving local scope.

    Relocations include address-taking as well as calls. Indirect calls and
    relocation-free branches are not inferred, so this is a lower bound.
    """
    by_name = defaultdict(list)
    for key in functions:
        by_name[key[1]].append(key)
    outgoing = defaultdict(set)
    covered, missing = set(), []
    for unit in units:
        name = unit["name"]
        keys = {key for key in functions if key[0] == name}
        if not keys:
            continue
        path = unit.get("target_path")
        try:
            if not path:
                raise ValueError("no target object")
            obj = read_elf(root / path)
        except (ExtractError, ValueError) as exc:
            missing.append({"unit": name, "error": str(exc)})
            continue
        ranges = defaultdict(list)
        for symbol in obj.symbols:
            if symbol.type == STT_FUNC and symbol.size and symbol.shndx:
                ranges[symbol.shndx].append(symbol)
                if (name, symbol.name) in keys:
                    covered.add((name, symbol.name))
        for symbols in ranges.values():
            symbols.sort(key=lambda symbol: symbol.value)
        starts = {section: [symbol.value for symbol in symbols] for section, symbols in ranges.items()}
        for relocation in obj.relocations:
            section = relocation.target_section_index
            index = bisect_right(starts.get(section, []), relocation.offset) - 1
            if index < 0:
                continue
            caller = ranges[section][index]
            if relocation.offset >= caller.value + caller.size:
                continue
            origin = (name, caller.name)
            if origin not in functions:
                continue
            target = obj.symbols[relocation.symbol_index]
            local = (name, target.name)
            if target.shndx and local in functions:
                destination = local
            elif not target.shndx and len(by_name[target.name]) == 1:
                destination = by_name[target.name][0]
            else:
                continue
            if destination != origin:
                outgoing[origin].add(destination)
    return outgoing, covered, missing


def rank(functions, outgoing, covered):
    incoming = defaultdict(set)
    owners = defaultdict(set)
    units = defaultdict(set)
    for key, row in functions.items():
        units[key[0]].add(key)
        if row.get("owner_source"):
            owners[row["owner_source"]].add(key)
    for origin, targets in outgoing.items():
        for target in targets:
            incoming[target].add(origin)
    unresolved = {key for key, row in functions.items() if row["base_pct"] < 100}
    profiles = {}
    for key in unresolved:
        row = functions[key]
        peers = (owners.get(row.get("owner_source"), set()) & unresolved) - {key}
        referrers = incoming[key] & unresolved
        beneficiaries = peers | referrers
        learning = min(80, 8 * len(referrers)) + min(20, 2 * len(peers))
        ease = round(min(100, row["base_pct"] * 0.7 + (30 if row["size"] <= 192 else 15 if row["size"] <= 512 else 0)))
        residual = len(units[key[0]] & unresolved)
        closure = 100 if residual == 1 else 50 if residual <= 4 else 0
        related = outgoing.get(key, set()) | incoming[key] | owners.get(row.get("owner_source"), set())
        references = [functions[other] for other in related - unresolved - {key} if functions[other].get("owner_source")]
        references.sort(key=lambda other: (other["owner_source"] != row.get("owner_source"), other["size"], other["symbol"]))
        profiles[key] = {
            "value_score": 6 * learning + 2 * ease + closure,
            "learning_score": learning, "ease_score": ease, "closure_score": closure,
            "potential_beneficiaries": len(beneficiaries),
            "unresolved_referrers": len(referrers), "unresolved_owner_peers": len(peers),
            "beneficiaries": [{"unit": other[0], "symbol": other[1]} for other in sorted(beneficiaries)],
            "reference_coverage": key in covered,
            "value_reasons": [f"{len(referrers)} unresolved functions reference this target",
                              f"{len(peers)} unresolved owner peers",
                              f"{residual} residual functions in scoring unit"],
            "context_references": [
                {field: other[field] for field in ("unit", "symbol", "owner_source", "source_sha256")}
                for other in references[:8]
            ],
        }
    return profiles
