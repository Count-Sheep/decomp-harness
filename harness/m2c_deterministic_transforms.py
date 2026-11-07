"""Semantics-preserving source variants for the model-free m2c finisher."""

from __future__ import annotations

import hashlib
import re

import local_campaign_permute as permute
import local_campaign_rewrite as rewrite


INITIALIZED = re.compile(
    r"^(?P<indent>[ \t]*)(?P<type>(?!(?:const|volatile)\b)(?:(?:unsigned|signed|struct|union|enum|register)\s+)*"
    r"[A-Za-z_]\w*(?:\s+[A-Za-z_]\w*)*[ \t*]+)(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<rhs>[^;]+);(?P<tail>[ \t]*(?://.*)?)$",
    re.MULTILINE,
)
PLAIN = re.compile(
    r"^(?P<indent>[ \t]*)(?P<type>(?!(?:const|volatile)\b)(?:(?:unsigned|signed|struct|union|enum|register)\s+)*"
    r"[A-Za-z_]\w*(?:\s+[A-Za-z_]\w*)*[ \t*]+)(?P<name>[A-Za-z_]\w*)\s*;(?P<tail>[ \t]*(?://.*)?)\n"
    r"(?P=indent)(?P=name)\s*=\s*(?P<rhs>[^;]+);",
    re.MULTILINE,
)
LOCAL_DECLARATION = re.compile(
    r"^(?P<indent>[ \t]+)(?P<type>(?!(?:extern|static|const|volatile|register|return|goto|case|break|continue)\b)"
    r"(?:(?:unsigned|signed|struct|union|enum)\s+)*"
    r"[A-Za-z_]\w*(?:\s+[A-Za-z_]\w*)*[ \t*]+)(?P<name>[A-Za-z_]\w*)"
    r"(?P<init>\s*=\s*[^;]+)?;(?P<tail>[ \t]*(?://.*)?)$",
    re.MULTILINE,
)
REGISTER_DECLARATION = re.compile(
    r"^(?P<indent>[ \t]+)register\s+(?P<declaration>[^;\n]+;(?P<tail>[ \t]*(?://.*)?))$",
    re.MULTILINE,
)


def _line(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _target_distance(source: str, start: int, end: int, target_lines: set[int]) -> int | None:
    first, last = _line(source, start), _line(source, end)
    if not target_lines:
        return None
    return min(0 if first <= line <= last else min(abs(line - first), abs(line - last))
               for line in target_lines)


def _location(source: str, start: int, end: int, target_lines: set[int]) -> dict:
    distance = _target_distance(source, start, end, target_lines)
    return {"targeted": distance is not None and distance <= 1, "target_distance": distance}


def local_shapes(source: str, target_lines: set[int] | None = None) -> list[dict]:
    variants = []
    targets = target_lines or set()
    for match in INITIALIZED.finditer(source):
        replacement = (f"{match['indent']}{match['type']}{match['name']};{match['tail']}\n"
                       f"{match['indent']}{match['name']} = {match['rhs']};")
        variants.append({"code": source[:match.start()] + replacement + source[match.end():],
                         "kind": "initialization_shape",
                         "detail": f"split initialization of {match['name']}",
                         **_location(source, match.start(), match.end(), targets)})
    for match in PLAIN.finditer(source):
        replacement = f"{match['indent']}{match['type']}{match['name']} = {match['rhs']};{match['tail']}"
        variants.append({"code": source[:match.start()] + replacement + source[match.end():],
                         "kind": "initialization_shape",
                         "detail": f"merge initialization of {match['name']}",
                         **_location(source, match.start(), match.end(), targets)})
    return variants


def storage_class_shapes(source: str, target_lines: set[int] | None = None) -> list[dict]:
    """Toggle the standard C register hint on simple local declarations."""
    variants = []
    targets = target_lines or set()
    for match in LOCAL_DECLARATION.finditer(source):
        replacement = (f"{match['indent']}register {match['type']}{match['name']}"
                       f"{match['init'] or ''};{match['tail']}")
        variants.append({"code": source[:match.start()] + replacement + source[match.end():],
                         "kind": "storage_class_shape",
                         "detail": f"qualify {match['name']} as register",
                         **_location(source, match.start(), match.end(), targets)})
    for match in REGISTER_DECLARATION.finditer(source):
        replacement = f"{match['indent']}{match['declaration']}"
        variants.append({"code": source[:match.start()] + replacement + source[match.end():],
                         "kind": "storage_class_shape",
                         "detail": "remove register qualifier",
                         **_location(source, match.start(), match.end(), targets)})
    return variants


def _category(option: rewrite.Rewrite) -> str:
    if option.label.startswith(("swap declarations", "move ")):
        return "declaration_scope_order"
    if option.label.startswith("swap statements"):
        return "statement_order"
    if "loop" in option.label or "branches" in option.label:
        return "control_flow"
    return "expression_shape"


def variants(source: str, classification: dict | None, limit: int) -> list[dict]:
    """Generate a balanced bounded set, prioritised by measured mismatch class."""
    register_only = bool((classification or {}).get("register_only"))
    target_lines = {int(row["source_line"]) for row in (classification or {}).get("mismatches") or []
                    if row.get("kind") == "register" and isinstance(row.get("source_line"), int)
                    and int(row["source_line"]) > 0}
    groups: dict[str, list[dict]] = {key: [] for key in (
        "declaration_order", "declaration_scope_order", "initialization_shape",
        "storage_class_shape", "statement_order", "expression_shape", "control_flow")}
    block = permute.declaration_block(source)
    if block:
        start, end, lines = block
        for order in permute.orderings(lines, max(limit * 2, 16)):
            groups["declaration_order"].append({
                "code": source[:start] + "".join(order) + source[end:],
                "kind": "declaration_order", "detail": "reorder leading local declarations",
                **_location(source, start, end, target_lines)})
    groups["initialization_shape"].extend(local_shapes(source, target_lines))
    groups["storage_class_shape"].extend(storage_class_shapes(source, target_lines))
    for option in rewrite.rewrites(source):
        kind = _category(option)
        groups[kind].append({"code": option.apply(source), "kind": kind, "detail": option.label,
                             **_location(source, option.start, option.end, target_lines)})
    for rows in groups.values():
        rows.sort(key=lambda row: (row.get("target_distance") is None,
                                  row.get("target_distance") or 0))
    order = (["storage_class_shape", "statement_order", "expression_shape", "initialization_shape", "declaration_scope_order",
              "declaration_order", "control_flow"] if register_only and target_lines else
             ["storage_class_shape", "declaration_order", "declaration_scope_order", "initialization_shape", "statement_order",
              "expression_shape", "control_flow"] if register_only else
             ["expression_shape", "control_flow", "initialization_shape", "statement_order",
              "declaration_order", "declaration_scope_order", "storage_class_shape"])
    result, seen = [], {hashlib.sha256(source.encode()).hexdigest()}
    # Spend the bounded compile budget on rewrites at the measured source line first.
    # Category round-robin still keeps one prolific generator from consuming the search.
    phases = (True, False) if target_lines else (False,)
    for targeted in phases:
        while len(result) < limit and any(any(bool(row.get("targeted")) == targeted for row in groups[name])
                                          for name in order):
            for name in order:
                rows = groups[name]
                index = next((i for i, row in enumerate(rows)
                              if bool(row.get("targeted")) == targeted), None)
                if index is None:
                    continue
                row = rows.pop(index)
                key = hashlib.sha256(row["code"].encode()).hexdigest()
                if key in seen:
                    continue
                seen.add(key)
                result.append({**row, "sha256": key})
                if len(result) >= limit:
                    break
    return result
