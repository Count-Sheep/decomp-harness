#!/usr/bin/env python3
"""Deterministic context and reference evidence for m2c drafts of the selected project.

The target project is the configured one (``harness_paths.SELECTED``) unless a root and
version are passed explicitly; cross-project evidence comes from the configured
reference projects (``harness_paths.reference_projects()``) plus any active installed
reference corpora. Public functions return JSON-serializable dictionaries so callers can
adopt feature parsing, reference retrieval, compiler inference, or the complete context
independently.

The batch-oriented API is::

    catalog = build_catalog(root, version)
    knowledge = build_knowledge(root, version, row, assembly_text, catalog=catalog)

Building the catalog once avoids rescanning reports and assembly for every target.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import harness_paths


CACHE_VERSION = 1
MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = harness_paths.REPO_ROOT
DEFAULT_CROSS_PROJECT = REPO_ROOT / "build/decomp-support/cross-project-exact.json"
DEFAULT_REFERENCE_REGISTRY = REPO_ROOT / "build/decomp-support/reference-corpora/registry.json"

FUNCTION_START = re.compile(r"^\s*\.fn\s+([^,\s]+)", re.MULTILINE)
FUNCTION_END = re.compile(r"^\s*\.endfn\s+([^,\s]+)", re.MULTILINE)
INSTRUCTION = re.compile(
    r"^\s*/\*\s*([0-9A-Fa-f]{8})\s+[0-9A-Fa-f]{8}\s+"
    r"(?:[0-9A-Fa-f]{2}\s+){4}\*/\s*([.A-Za-z0-9_+-]+)(?:\s+(.*?))?\s*$"
)
PLAIN_INSTRUCTION = re.compile(r"^\s*([.A-Za-z][.A-Za-z0-9_+-]*)(?:\s+(.*?))?\s*$")
SYMBOL_REFERENCE = re.compile(
    r"(?<![\w$.])([A-Za-z_][\w$.]*)@(?:sda21|sda2|ha|h|l)\b"
)
SDA_REFERENCE = re.compile(r"(?<![\w$.])([A-Za-z_][\w$.]*)@(?:sda21|sda2)\b")
DIRECT_CALL = re.compile(r"^([A-Za-z_][\w$.]*)(?:[+-]0x[0-9A-Fa-f]+)?$")
REGISTER = re.compile(r"\b(r(?:[12]?\d|3[01])|f(?:[12]?\d|3[01])|cr[0-7]|lr|ctr)\b")
MEMORY_OFFSET = re.compile(r"(?<![\w])(-?(?:0x[0-9A-Fa-f]+|\d+))\((r(?:[12]?\d|3[01]))\)")
C_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TEXT_SYMBOL = re.compile(
    r"^(\S+) = \.text:0x([0-9A-Fa-f]+); // type:function size:0x([0-9A-Fa-f]+)\b"
)
ADDRESS_SUFFIX = re.compile(r"(?:^|_)([0-9A-Fa-f]{8})$")

CONDITIONAL_BRANCHES = {
    "beq", "beq+", "beq-", "bge", "bge+", "bge-", "bgt", "bgt+", "bgt-",
    "ble", "ble+", "ble-", "blt", "blt+", "blt-", "bne", "bne+", "bne-",
    "bdnz", "bdnz+", "bdnz-", "bdz", "bdz+", "bdz-", "bc", "bcl",
}
UNCONDITIONAL_BRANCHES = {"b", "ba", "bctr"}
CALL_OPCODES = {"bl", "bla", "bctrl"}
RETURN_OPCODES = {"blr", "bclr"}
MEMORY_WIDTHS = {
    "lbz": 1, "lbzu": 1, "stb": 1, "stbu": 1,
    "lha": 2, "lhau": 2, "lhz": 2, "lhzu": 2, "sth": 2, "sthu": 2,
    "lwz": 4, "lwzu": 4, "stw": 4, "stwu": 4, "lfs": 4, "lfsu": 4,
    "stfs": 4, "stfsu": 4, "lfd": 8, "lfdu": 8, "stfd": 8, "stfdu": 8,
    "lmw": 4, "stmw": 4, "psq_l": 8, "psq_lu": 8, "psq_st": 8, "psq_stu": 8,
}
COMPILER_NAMES = {
    "mwcc_233_163": "GC/1.2.5",
    "mwcc_233_163e": "GC/1.2.5e",
    "mwcc_233_163n": "GC/1.2.5n",
    "mwcc_242_53": "GC/1.3",
    "mwcc_242_81": "GC/1.3.2",
    "mwcc_247_92": "GC/2.0",
    "mwcc_247_92p1": "GC/2.0p1",
    "mwcc_247_105": "GC/2.5",
    "mwcc_247_107": "GC/2.6",
    "mwcc_247_108": "GC/2.7",
    "mwcc_41_60831": "GC/3.0a5.2",
}


@dataclass(frozen=True)
class ProjectSpec:
    """A decompilation checkout and the report version to index."""

    name: str
    root: Path
    version: str


def target_project_id(root: Path, version: str) -> str:
    """The configured project id for a checkout, else for its version, else the root name."""
    root = Path(root).resolve()
    for project in (harness_paths.SELECTED, *harness_paths.projects()):
        if project.root == root and (not version or not project.version or project.version == version
                                     or version in project.versions):
            return project.id
    return harness_paths.project_id_for_version(version, root.name)


def _target_project(target_project: str | None, root: Path | None = None) -> harness_paths.Project:
    selected = harness_paths.SELECTED
    if target_project is None or target_project == selected.id:
        return selected
    found = harness_paths.project(target_project)
    if found is not None:
        return found
    return harness_paths.Project(target_project, Path(root).resolve() if root else Path(target_project), "")


def default_reference_projects(
    registry_path: Path = DEFAULT_REFERENCE_REGISTRY,
    target_project: str | None = None,
    target_root: Path | None = None,
) -> tuple[ProjectSpec, ...]:
    """Configured reference projects for the target, then active installed reference corpora."""
    target = _target_project(target_project, target_root)
    projects = [ProjectSpec(project.id, project.root.resolve(), project.version)
                for project in harness_paths.reference_projects(target) if project.version]
    names = {project.name for project in projects} | {target.id}
    registry = _json(registry_path)
    for row in registry.get("projects") or []:
        if row.get("status") != "active" or not row.get("root") or not row.get("version"):
            continue
        project = ProjectSpec(str(row["name"]), Path(str(row["root"])).resolve(), str(row["version"]))
        if project.name in names:
            continue
        if (project.root / "build" / project.version / "report.json").is_file():
            projects.append(project)
            names.add(project.name)
    return tuple(projects)


def build_check(root: Path, version: str) -> dict[str, Any]:
    """Compare the built main binary against the project's ``config/<VERSION>/build.sha1``.

    The first manifest line names the expected SHA-1 and, in dtk projects, the built file
    relative to the root; ``build/<VERSION>/main.dol`` is used when that path is absent.
    """
    root = Path(root)
    manifest = root / "config" / version / "build.sha1"
    try:
        fields = manifest.read_text(encoding="ascii").split("\n", 1)[0].split()
        expected = fields[0].lower()
        if len(expected) != 40 or not all(char in "0123456789abcdef" for char in expected):
            raise ValueError("invalid SHA-1 manifest")
        binary = root / "build" / version / "main.dol"
        if len(fields) > 1 and (root / fields[1].lstrip("*")).is_file():
            binary = root / fields[1].lstrip("*")
        with binary.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha1").hexdigest()
        return {"status": "exact" if digest == expected else "mismatch", "expected": expected,
                "actual": digest, "built_at": datetime.fromtimestamp(binary.stat().st_mtime, UTC).isoformat()}
    except (OSError, ValueError, IndexError):
        return {"status": "not_built"}


def _integer(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value), 0)
    except (TypeError, ValueError):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


def _instruction_rows(assembly: str) -> list[tuple[int | None, str, str]]:
    rows: list[tuple[int | None, str, str]] = []
    for line in assembly.splitlines():
        match = INSTRUCTION.match(line)
        if match:
            rows.append((int(match.group(1), 16), match.group(2), (match.group(3) or "").strip()))
            continue
        if line.lstrip().startswith((".", "#", "/*")) or not line.strip():
            continue
        match = PLAIN_INSTRUCTION.match(line)
        if match and match.group(1) not in {"global", "text"}:
            rows.append((None, match.group(1), (match.group(2) or "").strip()))
    return rows


def _sort_registers(values: Iterable[str]) -> list[str]:
    def key(value: str) -> tuple[int, int]:
        if value.startswith("r") and value[1:].isdigit():
            return 0, int(value[1:])
        if value.startswith("f") and value[1:].isdigit():
            return 1, int(value[1:])
        if value.startswith("cr") and value[2:].isdigit():
            return 2, int(value[2:])
        return 3, {"lr": 0, "ctr": 1}.get(value, 2)

    return sorted(set(values), key=key)


def parse_assembly_features(assembly: str, symbol: str | None = None) -> dict[str, Any]:
    """Parse a DTK function into stable structural and type-context features."""

    rows = _instruction_rows(assembly)
    opcodes: Counter[str] = Counter()
    calls: set[str] = set()
    globals_: set[str] = set()
    sda: set[str] = set()
    registers: set[str] = set()
    offsets: dict[tuple[str, int], dict[str, Any]] = {}
    conditional = unconditional = indirect_calls = returns = 0
    branch_targets: set[str] = set()
    stack_frame: int | None = None

    for _, opcode, operands in rows:
        opcode = opcode.lower()
        opcodes[opcode] += 1
        registers.update(REGISTER.findall(operands))
        refs = SYMBOL_REFERENCE.findall(operands)
        globals_.update(refs)
        sda.update(SDA_REFERENCE.findall(operands))

        if opcode in {"bl", "bla"}:
            target = operands.split(",", 1)[0].strip()
            match = DIRECT_CALL.match(target)
            if match and not target.startswith(".L_"):
                calls.add(match.group(1))
        elif opcode == "bctrl":
            indirect_calls += 1

        target = operands.split(",")[-1].strip() if operands else ""
        if opcode in CONDITIONAL_BRANCHES or (opcode.startswith("bc") and opcode not in CALL_OPCODES):
            conditional += 1
            if target:
                branch_targets.add(target)
        elif opcode in UNCONDITIONAL_BRANCHES:
            unconditional += 1
            if target:
                branch_targets.add(target)
        elif opcode in RETURN_OPCODES:
            returns += 1

        for raw_offset, base in MEMORY_OFFSET.findall(operands):
            offset = int(raw_offset, 0)
            item = offsets.setdefault(
                (base, offset),
                {"base": base, "offset": offset, "width": 0, "accesses": set()},
            )
            item["width"] = max(item["width"], MEMORY_WIDTHS.get(opcode, 0))
            item["accesses"].add(opcode)
            if opcode == "stwu" and base == "r1" and offset < 0 and stack_frame is None:
                stack_frame = -offset

    offset_rows = [
        {
            "base": item["base"],
            "offset": item["offset"],
            "width": item["width"],
            "accesses": sorted(item["accesses"]),
        }
        for item in sorted(offsets.values(), key=lambda row: (int(row["base"][1:]), row["offset"]))
    ]
    placeholders = []
    by_base: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in offset_rows:
        if item["base"] not in {"r1", "r2", "r13"} and item["offset"] >= 0:
            by_base[item["base"]].append(item)
    for base in sorted(by_base, key=lambda value: int(value[1:])):
        fields = by_base[base]
        extent = max(item["offset"] + max(item["width"], 1) for item in fields)
        placeholders.append({"base": base, "minimum_size": extent, "fields": fields})

    addresses = [address for address, _, _ in rows if address is not None]
    return {
        "symbol": symbol,
        "address": addresses[0] if addresses else None,
        "size": len(rows) * 4,
        "instruction_count": len(rows),
        "calls": sorted(calls),
        "globals": sorted(globals_),
        "sda_refs": sorted(sda),
        "opcodes": dict(sorted(opcodes.items())),
        "branches": {
            "conditional": conditional,
            "unconditional": unconditional,
            "indirect_calls": indirect_calls,
            "returns": returns,
            "targets": sorted(branch_targets),
        },
        "stack_frame": stack_frame,
        "registers": {
            "all": _sort_registers(registers),
            "gpr": _sort_registers(value for value in registers if value.startswith("r")),
            "fpr": _sort_registers(value for value in registers if value.startswith("f")),
            "special": _sort_registers(
                value for value in registers if not value.startswith(("r", "f"))
            ),
        },
        "offsets": offset_rows,
        "placeholder_structs": placeholders,
    }


def _clean_declaration(value: str) -> str | None:
    declaration = " ".join(value.strip().rstrip(";{").split())
    if not declaration or "#" in declaration or len(declaration) > 500:
        return None
    return declaration + ";"


def prototype_type_names(declaration: str) -> tuple[set[str], set[str]]:
    """Identify custom uppercase types handled by the mini-header's opaque fallback."""
    functions = set(re.findall(r"\b([A-Za-z_]\w*)\s*\(", declaration))
    pointers, values = set(), set()
    for name, stars in re.findall(r"\b([A-Z][A-Za-z0-9_]*)\s*(\*+)?", declaration):
        if name not in functions and name != "NULL":
            (pointers if stars else values).add(name)
    return pointers, values


def build_mini_header(
    features: Mapping[str, Any],
    known_prototypes: Mapping[str, str] | None = None,
    target_signature: str | None = None,
) -> str:
    """Build valid, conservative C declarations for m2c's ``--context`` input."""

    known_prototypes = known_prototypes or {}
    lines = [
        "/* Generated m2c context; offset structures are evidence, not recovered types. */",
        "typedef signed char s8;",
        "typedef unsigned char u8;",
        "typedef signed short s16;",
        "typedef unsigned short u16;",
        "typedef signed int s32;",
        "typedef unsigned int u32;",
        "typedef signed long long s64;",
        "typedef unsigned long long u64;",
        "typedef float f32;",
        "typedef double f64;",
        "",
    ]
    declarations = []
    if target_signature:
        declaration = _clean_declaration(target_signature)
        if declaration:
            declarations.append(declaration)
    for call in sorted(set(features.get("calls") or [])):
        if not C_IDENTIFIER.fullmatch(call):
            continue
        declaration = _clean_declaration(known_prototypes.get(call, ""))
        # An invented integer return constrains m2c before it sees PPC return-register evidence.
        if declaration:
            declarations.append(declaration)

    opaque_pointers = set()
    supported_declarations = []
    for declaration in declarations:
        pointers, values = prototype_type_names(declaration)
        # A custom by-value type may be an array, float, enum or aggregate.
        # Its spelling does not establish a 32-bit integer ABI contract.
        if values:
            continue
        supported_declarations.append(declaration)
        opaque_pointers.update(pointers)
    for name in sorted(opaque_pointers):
        lines.append(f"typedef struct {name} {name};")
    if opaque_pointers:
        lines.append("")

    symbol = features.get("symbol")
    lines.extend(supported_declarations)
    if symbol or features.get("calls"):
        lines.append("")

    sda = set(features.get("sda_refs") or [])
    for name in sorted(set(features.get("globals") or [])):
        if C_IDENTIFIER.fullmatch(name):
            suffix = " /* SDA */" if name in sda else ""
            lines.append(f"extern u8 {name}[];{suffix}")
    if features.get("globals"):
        lines.append("")

    for placeholder in features.get("placeholder_structs") or []:
        base = str(placeholder["base"])
        size = max(int(placeholder["minimum_size"]), 1)
        typename = f"M2C_Object_{base}"
        lines.append(f"typedef struct {typename} {{ u8 bytes[0x{size:X}]; }} {typename};")
        for field in placeholder.get("fields") or []:
            offset = int(field["offset"])
            macro = f"M2C_{base.upper()}_OFFSET_{offset:04X}"
            accesses = ",".join(field.get("accesses") or [])
            lines.append(f"#define {macro} 0x{offset:X} /* {accesses} */")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _function_address(unit: Mapping[str, Any], function: Mapping[str, Any]) -> int | None:
    address = _integer((function.get("metadata") or {}).get("virtual_address"))
    if address is not None:
        return address
    relative = _integer(function.get("address"))
    if relative is not None:
        for section in unit.get("sections") or []:
            if section.get("name") == ".text":
                start = _integer((section.get("metadata") or {}).get("virtual_address"))
                if start is not None:
                    return start + relative
    # Generated units can omit section VAs in report.json, while their generated
    # function names preserve the retail address (GameCube/Wii main-memory range).
    name_match = ADDRESS_SUFFIX.search(str(function.get("name") or ""))
    if name_match:
        inferred = int(name_match.group(1), 16)
        if 0x80000000 <= inferred < 0x81800000:
            return inferred
    return None


def _is_exact(unit: Mapping[str, Any], function: Mapping[str, Any]) -> bool:
    percent = function.get("fuzzy_match_percent")
    if percent is not None:
        try:
            return float(percent) == 100.0
        except (TypeError, ValueError):
            return False
    return bool((unit.get("metadata") or {}).get("complete"))


def _symbol_addresses(spec: ProjectSpec) -> dict[str, tuple[int, int]]:
    path = spec.root / "config" / spec.version / "symbols.txt"
    result = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return result
    for line in lines:
        match = TEXT_SYMBOL.match(line)
        if match:
            result[match.group(1)] = (int(match.group(2), 16), int(match.group(3), 16))
    return result


def _compiler_units(root: Path) -> dict[str, dict[str, Any]]:
    units = {}
    for unit in _json(root / "objdiff.json").get("units") or []:
        scratch = unit.get("scratch") or {}
        metadata = unit.get("metadata") or {}
        profile = _compiler_profile(scratch.get("compiler"), scratch.get("c_flags"))
        if not profile:
            continue
        for key in (unit.get("name"), metadata.get("source_path")):
            if key:
                units[str(key)] = profile
    return units


def _flag_value(tokens: Sequence[str], flag: str) -> str | None:
    indices = [index for index, token in enumerate(tokens) if token == flag]
    if not indices:
        return None
    index = indices[-1]
    return tokens[index + 1] if index + 1 < len(tokens) else "on"


def _compiler_profile(compiler: Any, flags_text: Any) -> dict[str, Any] | None:
    if not compiler and not flags_text:
        return None
    compiler = str(compiler or "unknown")
    flags_text = str(flags_text or "")
    try:
        tokens = shlex.split(flags_text)
    except ValueError:
        tokens = flags_text.split()
    optimization = next((token for token in reversed(tokens) if token.startswith("-O")), None)
    settings = {
        "optimization": optimization,
        "char_signedness": _flag_value(tokens, "-char") or "compiler-default",
        "fp_contract": _flag_value(tokens, "-fp_contract") or "compiler-default",
        "peephole": _flag_value(tokens, "-peephole") or _flag_value(tokens, "-opt"),
        "lmw_stmw": _flag_value(tokens, "-use_lmw_stmw") or "compiler-default",
        "inline": _flag_value(tokens, "-inline") or "compiler-default",
        "small_data": _flag_value(tokens, "-sdata") or "compiler-default",
        "enum": _flag_value(tokens, "-enum") or "compiler-default",
    }
    return {
        "compiler": compiler,
        "mw_version": COMPILER_NAMES.get(compiler, compiler),
        "flags_text": flags_text,
        "flags": tokens,
        "settings": settings,
    }


def _report_exact_rows(spec: ProjectSpec) -> list[dict[str, Any]]:
    report = _json(spec.root / "build" / spec.version / "report.json")
    compiler_units = _compiler_units(spec.root)
    symbol_addresses = _symbol_addresses(spec)
    rows = []
    for unit in report.get("units") or []:
        metadata = unit.get("metadata") or {}
        source = metadata.get("source_path")
        if not source:
            continue
        compiler = compiler_units.get(str(unit.get("name"))) or compiler_units.get(str(source))
        for function in unit.get("functions") or []:
            if not _is_exact(unit, function):
                continue
            name = function.get("name")
            if not isinstance(name, str):
                continue
            symbol_address, symbol_size = symbol_addresses.get(name, (None, None))
            rows.append(
                {
                    "project": spec.name,
                    "symbol": name,
                    "address": _function_address(unit, function) or symbol_address,
                    "size": _integer(function.get("size")) or symbol_size or 0,
                    "unit": unit.get("name"),
                    "source_path": str(source),
                    "compiler": compiler,
                }
            )
    return sorted(rows, key=lambda row: (row["address"] is None, row["address"] or 0, row["symbol"]))


def _assembly_blocks(spec: ProjectSpec, wanted: set[str]) -> dict[str, list[tuple[int | None, str]]]:
    found: dict[str, list[tuple[int | None, str]]] = defaultdict(list)
    asm_root = spec.root / "build" / spec.version / "asm"
    for path in sorted(asm_root.rglob("*.s")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        active_name: str | None = None
        start = 0
        for match in re.finditer(r"(?m)^\s*\.(fn|endfn)\s+([^,\s]+)[^\n]*$", text):
            kind, name = match.group(1), match.group(2)
            if kind == "fn":
                active_name, start = name, match.start()
            elif active_name == name:
                if name in wanted:
                    block = text[start:match.end()]
                    features = parse_assembly_features(block, name)
                    found[name].append((features["address"], block))
                active_name = None
    return found


def _tree_stamp(path: Path) -> str:
    digest = hashlib.sha256()
    if not path.is_dir():
        return digest.hexdigest()
    for item in sorted(path.rglob("*.s")):
        try:
            content = item.read_bytes()
        except OSError:
            continue
        digest.update(str(item.relative_to(path)).encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def _index_fingerprint(projects: Sequence[ProjectSpec]) -> str:
    digest = hashlib.sha256()
    digest.update(str(CACHE_VERSION).encode())
    for spec in projects:
        report = spec.root / "build" / spec.version / "report.json"
        digest.update(f"{spec.name}:{spec.version}:".encode())
        try:
            digest.update(hashlib.sha256(report.read_bytes()).digest())
        except OSError:
            digest.update(b"missing")
        digest.update(_tree_stamp(spec.root / "build" / spec.version / "asm").encode())
    return digest.hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def build_reference_index(
    projects: Sequence[ProjectSpec], cache_path: Path | None = None
) -> dict[str, Any]:
    """Index source-backed exact functions and their assembly features."""

    projects = tuple(sorted(projects, key=lambda spec: spec.name))
    fingerprint = _index_fingerprint(projects)
    if cache_path and cache_path.is_file():
        cached = _json(cache_path)
        if cached.get("version") == CACHE_VERSION and cached.get("fingerprint") == fingerprint:
            return cached

    result_rows = []
    for spec in projects:
        report_rows = _report_exact_rows(spec)
        blocks = _assembly_blocks(spec, {row["symbol"] for row in report_rows})
        for row in report_rows:
            candidates = blocks.get(row["symbol"]) or []
            if not candidates:
                continue
            address = row["address"]
            selected = next((block for candidate_address, block in candidates
                             if address is not None and candidate_address == address), candidates[0][1])
            result_rows.append({**row, "features": parse_assembly_features(selected, row["symbol"])})
    result_rows.sort(key=lambda row: (row["project"], row["address"] is None,
                                      row["address"] or 0, row["symbol"], row["source_path"]))
    result = {"version": CACHE_VERSION, "fingerprint": fingerprint, "functions": result_rows}
    if cache_path:
        _atomic_json(cache_path, result)
    return result


def _cosine(left: Mapping[str, int], right: Mapping[str, int]) -> float:
    keys = set(left) | set(right)
    numerator = sum(int(left.get(key, 0)) * int(right.get(key, 0)) for key in keys)
    left_norm = math.sqrt(sum(int(value) ** 2 for value in left.values()))
    right_norm = math.sqrt(sum(int(value) ** 2 for value in right.values()))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def structural_similarity(target: Mapping[str, Any], candidate: Mapping[str, Any]) -> float:
    """Score opcode, size, control-flow, call, and stack-frame similarity in [0, 1]."""

    opcode_score = _cosine(target.get("opcodes") or {}, candidate.get("opcodes") or {})
    left_size, right_size = int(target.get("size") or 0), int(candidate.get("size") or 0)
    size_score = min(left_size, right_size) / max(left_size, right_size) if left_size and right_size else 0.0
    left_branches, right_branches = target.get("branches") or {}, candidate.get("branches") or {}
    dimensions = ("conditional", "unconditional", "indirect_calls", "returns")
    control_distance = sum(
        abs(int(left_branches.get(key, 0)) - int(right_branches.get(key, 0))) for key in dimensions
    )
    control_scale = 1 + sum(int(left_branches.get(key, 0)) for key in dimensions)
    control_score = 1.0 / (1.0 + control_distance / control_scale)
    call_distance = abs(len(target.get("calls") or []) - len(candidate.get("calls") or []))
    call_score = 1.0 / (1.0 + call_distance)
    left_stack, right_stack = target.get("stack_frame"), candidate.get("stack_frame")
    stack_score = 1.0 if left_stack == right_stack else (0.5 if left_stack is None or right_stack is None else 0.0)
    return round(0.55 * opcode_score + 0.20 * size_score + 0.15 * control_score
                 + 0.05 * call_score + 0.05 * stack_score, 8)


def _source_function(text: str, symbol: str, maximum: int = 2400) -> tuple[str | None, str | None]:
    if not C_IDENTIFIER.fullmatch(symbol):
        return None, None
    clean = re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
                   lambda match: "".join("\n" if char == "\n" else " " for char in match[0]),
                   text, flags=re.S)
    for match in re.finditer(rf"\b{re.escape(symbol)}\s*\(", clean):
        before = clean[:match.start()]
        if before.count("{") != before.count("}"):
            continue
        depth = 1
        closing = match.end()
        while closing < len(clean) and depth:
            depth += (clean[closing] == "(") - (clean[closing] == ")")
            closing += 1
        suffix = re.match(r"\s*\{", clean[closing:])
        if depth or not suffix:
            continue
        opening = closing + suffix.end() - 1
        line_start = clean.rfind("\n", 0, match.start()) + 1
        start = line_start
        while start > 0:
            previous_end = start - 1
            previous_start = text.rfind("\n", 0, previous_end) + 1
            previous = clean[previous_start:previous_end].strip()
            if not previous or previous.startswith("#") or previous.endswith((";", "{", "}")):
                break
            start = previous_start
        depth = 0
        quote: str | None = None
        escaped = False
        end = opening
        for index in range(opening, len(clean)):
            character = clean[index]
            if quote:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    quote = None
                continue
            if character in {'"', "'"}:
                quote = character
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        if depth:
            continue
        signature = " ".join(clean[start:opening].strip().split())
        snippet = text[start:end].strip()
        if len(snippet) > maximum:
            snippet = snippet[:maximum].rstrip() + "\n/* snippet truncated */"
        return signature or None, snippet or None
    return None, None


def _enrich_reference(row: Mapping[str, Any], spec: ProjectSpec, score: float,
                      snippet_chars: int) -> dict[str, Any]:
    source_path = spec.root / str(row["source_path"])
    signature = snippet = None
    try:
        signature, snippet = _source_function(
            source_path.read_text(encoding="utf-8", errors="replace"), row["symbol"], snippet_chars
        )
    except OSError:
        pass
    return {
        "project": row["project"],
        "symbol": row["symbol"],
        "address": row["address"],
        "size": row["size"],
        "unit": row["unit"],
        "source_path": row["source_path"],
        "signature": signature,
        "source_snippet": snippet,
        "compiler": row.get("compiler"),
        "similarity": score,
        "features": {
            key: row["features"].get(key)
            for key in ("instruction_count", "opcodes", "branches", "stack_frame", "calls")
        },
    }


def find_similar_references(
    target: Mapping[str, Any],
    projects: Sequence[ProjectSpec],
    limit: int = 5,
    cache_path: Path | None = None,
    snippet_chars: int = 2400,
    index: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return 3-5 exact references, representing both projects when available."""

    if not 3 <= limit <= 5:
        raise ValueError("reference limit must be between 3 and 5")
    index = index or build_reference_index(projects, cache_path)
    project_by_name = {project.name: project for project in projects}
    scored = []
    seen = set()
    for row in index["functions"]:
        if row["symbol"] == target.get("symbol") and row["project"] == target.get("project"):
            continue
        features = row["features"]
        branches = features.get("branches") or {}
        identity = json.dumps({
            "symbol": row["symbol"], "size": row.get("size"),
            "instruction_count": features.get("instruction_count"),
            "opcodes": features.get("opcodes") or {},
            "branches": {key: branches.get(key, 0) for key in
                         ("conditional", "unconditional", "indirect_calls", "returns")},
            "stack_frame": features.get("stack_frame"), "calls": features.get("calls") or [],
        }, sort_keys=True, separators=(",", ":"))
        if identity in seen:
            continue
        seen.add(identity)
        score = structural_similarity(target, row["features"])
        scored.append((score, row))
    scored.sort(key=lambda item: (-item[0], item[1]["project"], item[1]["symbol"],
                                  item[1]["address"] or -1))

    # One best match per project keeps references diverse, but rank those by score: with more
    # projects than ``limit`` the closest projects win, not the alphabetically first ones.
    best_per_project: dict[str, tuple[float, Mapping[str, Any]]] = {}
    for candidate in scored:
        best_per_project.setdefault(candidate[1]["project"], candidate)
    selected: list[tuple[float, Mapping[str, Any]]] = sorted(
        best_per_project.values(), key=lambda item: (-item[0], item[1]["project"], item[1]["symbol"])
    )[:limit]
    for candidate in scored:
        if len(selected) >= limit:
            break
        if candidate not in selected:
            selected.append(candidate)
    selected = sorted(selected[:limit], key=lambda item: (-item[0], item[1]["project"],
                                                           item[1]["symbol"]))
    return [
        _enrich_reference(row, project_by_name[row["project"]], score, snippet_chars)
        for score, row in selected
    ]


def discover_known_prototypes(
    root: Path, version: str, symbols: Iterable[str]
) -> dict[str, str]:
    """Resolve call signatures only from source-backed report entries."""

    wanted = {symbol for symbol in symbols if C_IDENTIFIER.fullmatch(symbol)}
    if not wanted:
        return {}
    spec = ProjectSpec(target_project_id(root, version), root, version)
    declarations = {}
    for row in _report_exact_rows(spec):
        symbol = row["symbol"]
        if symbol not in wanted or symbol in declarations:
            continue
        try:
            text = (root / row["source_path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        signature, _ = _source_function(text, symbol)
        if signature:
            declarations[symbol] = signature
    return dict(sorted(declarations.items()))


def find_reusable_implementation(
    symbol: str,
    address: int | None,
    snapshot_path: Path,
    project_roots: Mapping[str, Path],
    target_project: str | None = None,
    project_versions: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Find a retail-byte-identical implementation outside the target project.

    ``target_project`` defaults to the selected project's id. ``project_versions`` names
    each reference project's build version; configured projects are looked up otherwise.
    """

    target_project = target_project or harness_paths.PROJECT_ID
    snapshot = _json(snapshot_path)
    candidates = []
    for row in snapshot.get("matches") or []:
        if target_project not in (row.get("projects") or []):
            continue
        target_address = _integer((row.get("addresses") or {}).get(target_project))
        if row.get("name") != symbol and (address is None or target_address != address):
            continue
        for linked_rank, key in enumerate(("linked_sources", "exact_sources")):
            for project, source in sorted((row.get(key) or {}).items()):
                if project == target_project or project not in project_roots:
                    continue
                candidates.append((linked_rank, project, source, row))
    if not candidates:
        return None
    linked_rank, project, source, match = min(candidates, key=lambda item: (item[0], item[1], item[2]))
    root = project_roots[project]
    source_file = root / source
    signature = snippet = None
    try:
        signature, snippet = _source_function(
            source_file.read_text(encoding="utf-8", errors="replace"), str(match["name"])
        )
    except OSError:
        pass
    compiler = None
    configured = harness_paths.project(project)
    version = (project_versions or {}).get(project) or (configured.version if configured else "")
    for row in (_report_exact_rows(ProjectSpec(project, root, version)) if version else []):
        if row["symbol"] == match["name"] and row["source_path"] == source:
            compiler = row.get("compiler")
            break
    return {
        "name": match["name"],
        "target_project": target_project,
        "target_address": _integer((match.get("addresses") or {}).get(target_project)),
        "bytes": match.get("bytes"),
        "sha256": match.get("sha256"),
        "project": project,
        "source_path": source,
        "source_kind": "linked_exact" if linked_rank == 0 else "exact",
        "signature": signature,
        "source_snippet": snippet,
        "compiler": compiler,
    }


def _confidence(distance: int | None, evidence_count: int, fallback: bool) -> str:
    if fallback or distance is None:
        return "fallback"
    if distance <= 0x10000 and evidence_count >= 2:
        return "high"
    if distance <= 0x40000:
        return "medium"
    return "low"


def infer_compiler_profiles(
    root: Path, address: int | None, version: str | None = None, limit: int = 5
) -> dict[str, Any]:
    """Rank MWCC configurations using nearby source-backed exact target functions."""

    if limit < 1:
        raise ValueError("profile limit must be positive")
    version = version or harness_paths.VERSION
    rows = [row for row in _report_exact_rows(ProjectSpec(target_project_id(root, version), root, version))
            if row.get("compiler")]
    return _rank_compiler_profiles(rows, address, limit)


def _rank_compiler_profiles(
    rows: Sequence[Mapping[str, Any]], address: int | None, limit: int = 5
) -> dict[str, Any]:
    """Pure ranking core used by both direct and catalog-backed inference."""

    evidence = []
    if address is not None:
        evidence = sorted(
            (row for row in rows if row["address"] is not None),
            key=lambda row: (abs(int(row["address"]) - address), int(row["address"]), row["symbol"]),
        )[:32]

    fallback = not evidence
    if fallback:
        evidence = rows
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in evidence:
        compiler = row["compiler"]
        groups[(compiler["compiler"], compiler["flags_text"])].append(row)

    ranked = []
    for _, members in groups.items():
        profile = members[0]["compiler"]
        ordered = sorted(
            members,
            key=lambda row: (abs(int(row["address"]) - address)
                             if address is not None and row["address"] is not None else 2**63,
                             row["address"] or 0, row["symbol"]),
        )
        distance = (abs(int(ordered[0]["address"]) - address)
                    if address is not None and ordered[0]["address"] is not None else None)
        ranked.append({
            **profile,
            "distance": distance,
            "confidence": _confidence(distance, len(ordered), fallback),
            "fallback": fallback,
            "evidence": [
                {"symbol": row["symbol"], "address": row["address"], "unit": row["unit"],
                 "source_path": row["source_path"],
                 "distance": abs(int(row["address"]) - address)
                 if address is not None and row["address"] is not None else None}
                for row in ordered[:5]
            ],
            "evidence_count": len(ordered),
        })
    ranked.sort(key=lambda row: (row["distance"] is None, row["distance"] or 0,
                                 -row["evidence_count"], row["compiler"], row["flags_text"]))
    ranked = ranked[:limit]
    for rank, row in enumerate(ranked, 1):
        row["rank"] = rank
    return {
        "method": "nearest_source_backed_exact" if not fallback else "build_configuration_fallback",
        "confidence": ranked[0]["confidence"] if ranked else "unavailable",
        "fallback": fallback,
        "selected": ranked[0] if ranked else None,
        "profiles": ranked,
    }


def build_catalog(
    root: Path,
    version: str | None = None,
    reference_projects: Sequence[ProjectSpec] | None = None,
    cross_project_path: Path = DEFAULT_CROSS_PROJECT,
    cache_path: Path | None = None,
    target_project: str | None = None,
) -> dict[str, Any]:
    """Precompute immutable report/assembly joins for a drafting batch.

    The returned value contains only JSON-compatible objects and can itself be cached
    by a caller. Source snippets are intentionally read when selected, so edits to a
    reference source do not require rebuilding the structural catalog. ``version``
    defaults to the selected project's version; the target id comes from configuration.
    """

    root = root.resolve()
    version = version or harness_paths.VERSION
    target_project = target_project or target_project_id(root, version)
    projects = tuple(reference_projects or
                     default_reference_projects(target_project=target_project, target_root=root))
    if cache_path is None:
        cache_path = root / "build/decomp-support" / version / "reference-context-v1.json"
    target_exact_functions = _report_exact_rows(ProjectSpec(target_project, root, version))
    bootstrap_path = root / "build/decomp-support" / version / "bootstrap-evidence.json"
    bootstrap = _json(bootstrap_path)
    if bootstrap.get("version") != version or not bootstrap.get("inputs") or any(
        not (path := Path(str(row.get("path") or ""))).is_file()
        or hashlib.sha256(path.read_bytes()).hexdigest() != row.get("sha256")
        for row in bootstrap.get("inputs") or []
    ):
        bootstrap = {}
    return {
        "version": CACHE_VERSION,
        "target_root": str(root),
        "target_project": target_project,
        "target_version": version,
        "reference_projects": [
            {"name": project.name, "root": str(project.root), "version": project.version}
            for project in projects
        ],
        "reference_index": build_reference_index(projects, cache_path),
        "target_exact_functions": target_exact_functions,
        "cross_project_exact": _json(cross_project_path),
        "cross_project_path": str(cross_project_path.resolve()),
        "reference_cache": str(cache_path.resolve()),
        "bootstrap_evidence": bootstrap,
        "bootstrap_path": str(bootstrap_path.resolve()),
    }


def _catalog_projects(catalog: Mapping[str, Any]) -> tuple[ProjectSpec, ...]:
    return tuple(
        ProjectSpec(str(row["name"]), Path(str(row["root"])), str(row["version"]))
        for row in catalog.get("reference_projects") or []
    )


def _known_prototypes_from_rows(
    rows: Sequence[Mapping[str, Any]], root: Path, symbols: Iterable[str]
) -> dict[str, str]:
    wanted = {symbol for symbol in symbols if C_IDENTIFIER.fullmatch(symbol)}
    declarations = {}
    for row in rows:
        symbol = row.get("symbol")
        if symbol not in wanted or symbol in declarations:
            continue
        try:
            text = (root / str(row["source_path"])).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        signature, _ = _source_function(text, str(symbol))
        if signature:
            declarations[str(symbol)] = signature
    return dict(sorted(declarations.items()))


def _reusable_from_catalog(
    symbol: str, address: int | None, catalog: Mapping[str, Any]
) -> dict[str, Any] | None:
    candidates = []
    snapshot = catalog.get("cross_project_exact") or {}
    projects = {project.name: project for project in _catalog_projects(catalog)}
    target_project = str(catalog.get("target_project") or harness_paths.PROJECT_ID)
    for row in snapshot.get("matches") or []:
        if target_project not in (row.get("projects") or []):
            continue
        target_address = _integer((row.get("addresses") or {}).get(target_project))
        if row.get("name") != symbol and (address is None or target_address != address):
            continue
        for source_rank, key in enumerate(("linked_sources", "exact_sources")):
            for project_name, source in sorted((row.get(key) or {}).items()):
                if project_name != target_project and project_name in projects:
                    candidates.append((source_rank, project_name, source, row))
    if not candidates:
        return None
    source_rank, project_name, source, match = min(
        candidates, key=lambda item: (item[0], item[1], item[2])
    )
    project = projects[project_name]
    signature = snippet = None
    try:
        signature, snippet = _source_function(
            (project.root / source).read_text(encoding="utf-8", errors="replace"), str(match["name"])
        )
    except OSError:
        pass
    compiler = None
    for row in (catalog.get("reference_index") or {}).get("functions") or []:
        if (row.get("project") == project_name and row.get("symbol") == match.get("name")
                and row.get("source_path") == source):
            compiler = row.get("compiler")
            break
    return {
        "name": match["name"],
        "target_project": target_project,
        "target_address": _integer((match.get("addresses") or {}).get(target_project)),
        "bytes": match.get("bytes"),
        "sha256": match.get("sha256"),
        "project": project_name,
        "source_path": source,
        "source_kind": "linked_exact" if source_rank == 0 else "exact",
        "signature": signature,
        "source_snippet": snippet,
        "compiler": compiler,
    }


def _bootstrap_for_address(catalog: Mapping[str, Any], address: int | None) -> dict[str, Any]:
    if address is None:
        return {}
    report = catalog.get("bootstrap_evidence") or {}
    formatted = f"0x{address:08X}"
    result = {
        "scope": report.get("scope"),
        "type_hints": [row for row in report.get("type_hints") or [] if row.get("address") == formatted],
        "symbol_hints": [row for row in report.get("sequence_symbol_hints") or []
                         if row.get("address") == formatted],
        "unit_regions": [row for row in report.get("reference_sequence_regions") or []
                         if _integer(row.get("target_start")) is not None
                         and _integer(row.get("target_start")) <= address < _integer(row.get("target_end"))],
        "call_clusters": [row for row in report.get("call_clusters") or []
                          if formatted in (row.get("addresses") or [])],
    }
    return result if any(result[key] for key in ("type_hints", "symbol_hints", "unit_regions", "call_clusters")) else {}


def _bootstrap_target_signature(bootstrap: Mapping[str, Any], symbol: str) -> str | None:
    signatures = set()
    for row in bootstrap.get("type_hints") or []:
        signature = row.get("target_signature") or row.get("signature")
        target_name = row.get("target_name") or row.get("name")
        if signature and target_name == symbol and re.search(rf"\b{re.escape(symbol)}\s*\(", signature):
            signatures.add(str(signature).strip())
    return signatures.pop() if len(signatures) == 1 else None


@lru_cache(maxsize=8)
def _address_bound_declarations(root_text: str) -> dict[str, str]:
    """Index MWCC absolute-address declarations from reference headers."""
    declarations = {}
    pattern = re.compile(
        r"(?m)^\s*(?P<declaration>[^#;\n]*?\b(?P<name>[A-Za-z_]\w*)\s*"
        r"(?:\[[^\]\n]*\])?)\s+AT_ADDRESS\((?P<address>[^;\n]+)\)\s*;"
    )
    for path in sorted((Path(root_text) / "include").rglob("*.h")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in pattern.finditer(text):
            declarations.setdefault(
                match["name"], f"{match['declaration'].strip()} : ({match['address'].strip()});"
            )
    return declarations


@lru_cache(maxsize=32)
def _preprocessor_macros(root_text: str, source_text: str,
                         defines: tuple[str, ...]) -> dict[str, str]:
    """Capture the effective macro table without parsing reference C syntax."""
    clang = shutil.which("clang")
    root, source = Path(root_text), Path(source_text)
    if not clang or not source.is_file():
        return {}
    include_dirs = {root / "include", root / "src", source.parent}
    try:
        source_code = source.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    for name in re.findall(r'^\s*#\s*include\s*"([^"]+)"', source_code, re.M):
        matches = list((root / "src").rglob(name))
        if len(matches) == 1:
            include_dirs.add(matches[0].parent)
    command = [clang, "-E", "-dM", "-x", "c", "-D__MWERKS__=1", *defines,
               *(flag for path in sorted(include_dirs) if path.is_dir()
                 for flag in ("-I", str(path))), str(source)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    macros = {}
    for line in result.stdout.splitlines():
        match = re.match(r"#define\s+([A-Za-z_]\w*)(?:\([^)]*\))?\s*(.*)", line)
        if match:
            macros[match.group(1)] = line
    return macros


def _required_macros(root: Path, source: Path, snippet: str,
                     compiler: Mapping[str, Any] | None) -> list[str]:
    defines = tuple(flag for flag in (compiler or {}).get("flags") or []
                    if isinstance(flag, str) and flag.startswith("-D"))
    macros = _preprocessor_macros(str(root), str(source), defines)
    needed = set(re.findall(r"\b[A-Za-z_]\w*\b", snippet)) & macros.keys()
    pending = list(needed)
    while pending:
        name = pending.pop()
        dependencies = set(re.findall(r"\b[A-Za-z_]\w*\b", macros[name])) & macros.keys()
        for dependency in dependencies - needed:
            needed.add(dependency)
            pending.append(dependency)
    return [macros[name] for name in sorted(needed)]


def _bootstrap_reference(bootstrap: Mapping[str, Any], catalog: Mapping[str, Any]) -> dict[str, Any] | None:
    candidates = []
    projects = {str(row["name"]): Path(str(row["root"]))
                for row in catalog.get("reference_projects") or []}
    reference_rows = (catalog.get("reference_index") or {}).get("functions") or []
    for hint in bootstrap.get("symbol_hints") or []:
        if hint.get("promotion") != "reviewable_code_match":
            continue
        for type_hint in bootstrap.get("type_hints") or []:
            if (type_hint.get("source") != "code_verified_reference_source"
                    or type_hint.get("name") != hint.get("suggested_name")
                    or type_hint.get("reference_project") != hint.get("reference_project")):
                continue
            path = Path(str(type_hint.get("source_path") or ""))
            try:
                signature, snippet = _source_function(
                    path.read_text(encoding="utf-8", errors="replace"), str(hint["suggested_name"])
                )
            except OSError:
                continue
            if signature and snippet:
                project = str(hint["reference_project"])
                root = projects.get(project)
                bindings = _address_bound_declarations(str(root)) if root else {}
                identifiers = set(re.findall(r"\b[A-Za-z_]\w*\b", snippet))
                required = [bindings[name] for name in sorted(identifiers)
                            if name in bindings]
                compiler = next((row.get("compiler") for row in reference_rows
                                 if row.get("project") == project
                                 and row.get("symbol") == hint.get("suggested_name")
                                 and row.get("source_path") == type_hint.get("reference_source_path")), None)
                if compiler is None:
                    compiler = next((row.get("compiler") for row in reference_rows
                                     if row.get("project") == project
                                     and row.get("symbol") == hint.get("suggested_name")), None)
                required_macros = _required_macros(root, path, snippet, compiler) if root else []
                candidates.append({"project": hint["reference_project"],
                    "symbol": hint["suggested_name"], "similarity": 1.0,
                    "signature": signature, "source_snippet": snippet,
                    "source_path": str(path), "source_kind": "code_verified_bootstrap",
                    "code_comparison": hint.get("code_comparison"),
                    "compiler": compiler, "required_declarations": required,
                    "required_macros": required_macros,
                    "requires_target_compile_validation": True})
    return candidates[0] if len(candidates) == 1 else None


def _priority(
    features: Mapping[str, Any], references: Sequence[Mapping[str, Any]],
    compiler_profile: Mapping[str, Any], exact_reuse: Mapping[str, Any] | None,
    known_prototypes: Mapping[str, str],
) -> dict[str, Any]:
    if exact_reuse:
        return {"score": 100, "reasons": ["byte-identical cross-project source is reusable"]}
    score = 0
    reasons = []
    size = int(features.get("size") or 0)
    if 24 <= size <= 160:
        score += 30
        reasons.append("preferred 24-160 byte size")
    elif 0 < size <= 256:
        score += 12
        reasons.append("manageable function size")
    branches = features.get("branches") or {}
    if int(branches.get("indirect_calls", 0)) == 0:
        score += 12
        reasons.append("no unresolved indirect calls")
    placeholders = features.get("placeholder_structs") or []
    field_count = sum(len(row.get("fields") or []) for row in placeholders)
    if field_count <= 4:
        score += 13
        reasons.append("few offset-based unknown fields")
    elif field_count <= 8:
        score += 6
        reasons.append("moderate offset-based type uncertainty")
    calls = features.get("calls") or []
    known_count = sum(call in known_prototypes for call in calls)
    if not calls or known_count == len(calls):
        score += 15
        reasons.append("all direct callees have known signatures")
    elif known_count:
        score += round(15 * known_count / len(calls))
        reasons.append(f"{known_count}/{len(calls)} direct callees have known signatures")
    confidence = compiler_profile.get("confidence")
    if confidence == "high":
        score += 15
        reasons.append("high-confidence neighboring compiler profile")
    elif confidence == "medium":
        score += 9
        reasons.append("medium-confidence neighboring compiler profile")
    elif confidence == "low":
        score += 3
        reasons.append("low-confidence neighboring compiler profile")
    if references:
        similarity = float(references[0].get("similarity") or 0)
        reference_points = round(15 * similarity)
        score += reference_points
        reasons.append(f"best exact reference similarity {similarity:.3f}")
    return {"score": min(score, 99), "reasons": reasons}


def build_knowledge(
    root: Path,
    version: str,
    row: Mapping[str, Any],
    assembly_text: str | bytes,
    reference_limit: int = 5,
    *,
    catalog: Mapping[str, Any] | None = None,
    reference_projects: Sequence[ProjectSpec] | None = None,
    cross_project_path: Path = DEFAULT_CROSS_PROJECT,
    cache_path: Path | None = None,
) -> dict[str, Any]:
    """Build all deterministic knowledge consumed by one m2c target.

    ``row`` needs ``symbol`` (or ``name``) and may provide ``address``. Passing a
    catalog from :func:`build_catalog` avoids report and assembly rescans.
    """

    root = root.resolve()
    symbol = str(row.get("symbol") or row.get("name") or "")
    if isinstance(assembly_text, bytes):
        assembly_text = assembly_text.decode("utf-8", errors="replace")
    features = parse_assembly_features(assembly_text, symbol or None)
    address = _integer(row.get("address"))
    if address is None:
        address = features.get("address")
    if catalog is None:
        catalog = build_catalog(
            root, version, reference_projects, cross_project_path, cache_path
        )
    projects = _catalog_projects(catalog)
    exact_rows = catalog.get("target_exact_functions") or []
    known = _known_prototypes_from_rows(exact_rows, root, features.get("calls") or [])
    references = find_similar_references(
        features, projects, reference_limit, snippet_chars=2400,
        index=catalog.get("reference_index") or {"functions": []},
    )
    compiler_profile = _rank_compiler_profiles(
        [candidate for candidate in exact_rows if candidate.get("compiler")], address
    )
    exact_reuse = _reusable_from_catalog(symbol, address, catalog)
    bootstrap = _bootstrap_for_address(catalog, address)
    bootstrap_reference = _bootstrap_reference(bootstrap, catalog)
    if bootstrap_reference:
        references = [bootstrap_reference] + [row for row in references
                                              if (row.get("project"), row.get("symbol")) !=
                                              (bootstrap_reference["project"], bootstrap_reference["symbol"])]
        references = references[:reference_limit]
    if features["instruction_count"]:
        header = build_mini_header(
            features, known, exact_reuse.get("signature") if exact_reuse else None
        )
        bootstrap_signature = _bootstrap_target_signature(bootstrap, symbol)
        if bootstrap_signature:
            header += ("\n/* Code-backed ABI hypothesis; whole-unit validation is still required. */\n"
                       + bootstrap_signature.rstrip(";") + ";\n")
        context_result = {"status": "ok", "context": header, "error": None}
    else:
        context_result = {
            "status": "error", "context": "", "error": "no PowerPC instructions were parsed"
        }
    return {
        "context": context_result,
        "features": features,
        "references": references,
        "compiler_profile": compiler_profile,
        "exact_reuse": exact_reuse,
        "bootstrap": bootstrap,
        "priority": _priority(features, references, compiler_profile, exact_reuse, known),
    }


class ContextBuilder:
    """Facade used by drafting and validation pipelines for one target project."""

    def __init__(
        self,
        root: Path | None = None,
        version: str | None = None,
        reference_projects: Sequence[ProjectSpec] | None = None,
        cross_project_path: Path = DEFAULT_CROSS_PROJECT,
        cache_path: Path | None = None,
    ) -> None:
        self.root = (root or harness_paths.DECOMP_ROOT).resolve()
        self.version = version or harness_paths.VERSION
        self.target_project = target_project_id(self.root, self.version)
        self.reference_projects = tuple(reference_projects or default_reference_projects(
            target_project=self.target_project, target_root=self.root))
        self.cross_project_path = cross_project_path.resolve()
        self.cache_path = (cache_path.resolve() if cache_path else
                           self.root / "build/decomp-support" / self.version / "reference-context-v1.json")

    def build(
        self, assembly: str | bytes, symbol: str, address: int | None = None,
        reference_limit: int = 5,
    ) -> dict[str, Any]:
        if isinstance(assembly, bytes):
            assembly = assembly.decode("utf-8", errors="replace")
        features = parse_assembly_features(assembly, symbol)
        address = address if address is not None else features.get("address")
        known = discover_known_prototypes(self.root, self.version, features["calls"])
        references = find_similar_references(
            features, self.reference_projects, reference_limit, self.cache_path
        )
        roots = {project.name: project.root for project in self.reference_projects}
        versions = {project.name: project.version for project in self.reference_projects}
        reusable = find_reusable_implementation(
            symbol, address, self.cross_project_path, roots, self.target_project, versions
        )
        compiler = infer_compiler_profiles(self.root, address, self.version)
        target_signature = reusable.get("signature") if reusable else None
        context = build_mini_header(features, known, target_signature)
        return {
            "status": "ok",
            "version": self.version,
            "symbol": symbol,
            "address": address,
            "context": context,
            "features": features,
            "known_prototypes": known,
            "references": references,
            "reusable": reusable,
            "compiler": compiler,
            "provenance": {
                "reference_projects": [
                    {"name": project.name, "version": project.version, "root": str(project.root)}
                    for project in self.reference_projects
                ],
                "cross_project_snapshot": str(self.cross_project_path),
                "reference_cache": str(self.cache_path),
                "cache_version": CACHE_VERSION,
            },
        }


def build_function_context(
    root: Path,
    assembly: str | bytes,
    symbol: str,
    address: int | None = None,
    version: str | None = None,
    reference_projects: Sequence[ProjectSpec] | None = None,
    cross_project_path: Path = DEFAULT_CROSS_PROJECT,
    cache_path: Path | None = None,
    reference_limit: int = 5,
) -> dict[str, Any]:
    """Functional facade for callers that do not need a persistent builder."""

    return ContextBuilder(
        root, version, reference_projects, cross_project_path, cache_path
    ).build(assembly, symbol, address, reference_limit)
