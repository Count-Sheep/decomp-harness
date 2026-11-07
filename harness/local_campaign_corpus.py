"""Corpus index for the local-model runner: similar matched code and callee declarations.

Built from dtk's target assembly (callees via `bl`, globals via @sda21/@ha/@l) joined with
the objdiff report (match %, owning source). It lets a prompt show the model code that this
project already matches for the same callees and data, and the exact prototypes of the
functions it calls, instead of leaving it to guess types or idioms.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

FN = re.compile(r"^\.fn\s+([^,\s]+)")
END = re.compile(r"^\.endfn\s+")
CALL = re.compile(r"\bbl\s+([A-Za-z_][\w@$.]*)")
DATA = re.compile(r"\b([A-Za-z_][\w$.]*)@(?:sda21|sda2|ha|l)\b")
INSN = re.compile(r"^/\* [0-9A-F]{8} ")
INDEX_VERSION = 2


def build_index(root: Path, report_file: Path, version: str | None = None) -> dict[str, Any]:
    """Index dtk's target assembly (``build/<VERSION>/asm``; default: the selected project's version)."""
    if version is None:
        from harness_paths import VERSION as version
    functions: dict[str, dict[str, Any]] = {}
    for path in sorted((root / "build" / version / "asm").rglob("*.s")):
        name, callees, data, size = None, set(), set(), 0
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            start = FN.match(line)
            if start:
                name, callees, data, size = start.group(1), set(), set(), 0
                continue
            if name is None:
                continue
            if END.match(line):
                functions[name] = {"callees": sorted(callees - {name}), "data": sorted(data), "insns": size,
                                   "asm_path": path.relative_to(root).as_posix()}
                name = None
                continue
            if INSN.match(line):
                size += 1
            callees.update(CALL.findall(line))
            data.update(d for d in DATA.findall(line) if not d.startswith(("jumptable_", "@")))
    report = json.loads(report_file.read_text(encoding="utf-8")) if report_file.exists() else {"units": []}
    for unit in report.get("units", []):
        source = (unit.get("metadata") or {}).get("source_path")
        linked = bool((unit.get("metadata") or {}).get("complete"))
        for function in unit.get("functions", []):
            row = functions.get(function["name"])
            if row is None:
                continue
            pct = function.get("fuzzy_match_percent") or 0
            if pct >= row.get("pct", -1):
                row.update(pct=pct, source=source, linked=linked, unit=unit["name"])
    return {"version": INDEX_VERSION,
            "report_mtime": report_file.stat().st_mtime if report_file.exists() else 0, "functions": functions}


def load_index(root: Path, report_file: Path, cache: Path) -> dict[str, Any]:
    mtime = report_file.stat().st_mtime if report_file.exists() else 0
    if cache.exists():
        try:
            index = json.loads(cache.read_text(encoding="utf-8"))
            if index.get("version") == INDEX_VERSION and index.get("report_mtime") == mtime:
                return index
        except (OSError, ValueError):
            pass
    index = build_index(root, report_file)
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(json.dumps(index), encoding="utf-8")
    tmp.replace(cache)
    return index


def similar_matched(index: dict[str, Any], symbol: str, owner_source: str, limit: int = 3) -> list[str]:
    """Exact functions sharing the most callees/globals with `symbol`, same source file first."""
    functions = index["functions"]
    target = functions.get(symbol)
    if not target:
        return []
    callees, data = set(target["callees"]), set(target["data"])
    scored = []
    for name, row in functions.items():
        if name == symbol or row.get("pct", 0) < 100 or not row.get("source"):
            continue
        shared_calls = len(callees & set(row["callees"]))
        shared_data = len(data & set(row["data"]))
        if not shared_calls and not shared_data:
            continue
        score = 3 * shared_calls + 2 * shared_data + (4 if row["source"] == owner_source else 0)
        score -= abs(row["insns"] - target["insns"]) / max(target["insns"], 1)
        scored.append((score, name))
    scored.sort(reverse=True)
    return [name for _, name in scored[:limit]]


def assembly_excerpt(root: Path, row: dict[str, Any], symbol: str, budget: int = 700) -> str:
    """A bounded target instruction prefix paired with a matched function's C."""
    path = row.get("asm_path")
    if not path:
        return ""
    lines, used, active = [], 0, False
    try:
        for line in (root / path).read_text(encoding="utf-8", errors="replace").splitlines():
            match = FN.match(line)
            if match:
                active = match.group(1) == symbol
                continue
            if not active:
                continue
            if END.match(line):
                break
            if not INSN.match(line):
                continue
            instruction = line.split("*/", 1)[-1].strip()
            if used + len(instruction) + 1 > budget:
                lines.append("# further instructions omitted")
                break
            lines.append(instruction)
            used += len(instruction) + 1
    except OSError:
        return ""
    return "\n".join(lines)


PROTOTYPE = r"(?m)^[ \t]*(?:extern\s+)?(?:static\s+)?(?:inline\s+)?[A-Za-z_][\w\s\*]*?\b{name}\s*\([^;{{}}]*\)\s*;"
DEFINITION = r"(?m)^[ \t]*(?:static\s+)?(?:inline\s+)?[A-Za-z_][\w\s\*]*?\b{name}\s*\([^;{{}}]*\)\s*\{{"


def callee_declarations(index: dict[str, Any], symbol: str, owner_text: str, headers_text: str,
                        root: Path, limit: int = 20) -> list[str]:
    """Prototypes for the functions `symbol` calls: owner file first, then headers, then the callee's definition."""
    target = index["functions"].get(symbol)
    if not target:
        return []
    found = []
    for callee in target["callees"][:limit * 2]:
        name = re.escape(callee)
        decl = None
        for text in (owner_text, headers_text):
            match = re.search(PROTOTYPE.format(name=name), text)
            if match:
                decl = " ".join(match.group(0).split())
                break
        if decl is None:
            row = index["functions"].get(callee) or {}
            source = row.get("source")
            if source and (root / source).is_file():
                match = re.search(DEFINITION.format(name=name), (root / source).read_text(encoding="utf-8", errors="replace"))
                if match:
                    decl = " ".join(match.group(0).rstrip("{").split()) + ";"
        if decl and len(decl) < 240:
            found.append(decl)
        if len(found) >= limit:
            break
    return found
