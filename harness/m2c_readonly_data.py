"""Capture target-scoped, relocation-free readonly bytes for native m2c folding."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

import harness_paths  # noqa: F401  (shared harness configuration)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def capture(owner: Path, symbol: str, assembly: bytes) -> tuple[bytes, dict]:
    from permuter.owner_extract import read_elf
    return capture_object(read_elf(owner), symbol, assembly)


def capture_object(obj, symbol: str, assembly: bytes) -> tuple[bytes, dict]:
    functions = [item for item in obj.symbols if item.name == symbol and item.type == 2
                 and item.shndx and item.size]
    if len(functions) != 1:
        raise ValueError(f"Expected one defined target function in {obj.path}: {symbol}")
    function = functions[0]
    referenced = {rel.symbol_index for rel in obj.relocations
                  if rel.target_section_index == function.shndx
                  and function.value <= rel.offset < function.value + function.size}
    text = assembly.decode("utf-8")
    present = {match.group(1).strip('"') for match in re.finditer(
        r'(?m)^\.obj\s+("[^"\n]+"|[^,\s]+),', text)}
    info = {"owner": str(obj.path), "owner_sha256": digest(obj.data), "target": symbol,
            "added": [], "skipped": []}
    blocks = []
    names = [item.name for item in obj.symbols]
    for index in sorted(referenced):
        item = obj.symbols[index]
        reason = None
        if item.name in present:
            reason = "already captured"
        elif not item.shndx or item.shndx >= len(obj.sections):
            reason = "not defined in target owner"
        elif item.type not in (0, 1):
            reason = "not a data symbol"
        elif not re.fullmatch(r"[@A-Za-z_.$][@A-Za-z_0-9.$]*", item.name):
            reason = "unsupported assembly symbol spelling"
        elif names.count(item.name) != 1:
            reason = "ambiguous owner-local symbol"
        else:
            section = obj.sections[item.shndx]
            if section.type != 1 or not section.flags & 2 or section.flags & (1 | 4):
                reason = "not allocated readonly data"
            elif item.size <= 0 or item.size > 65536:
                reason = "unbounded or oversized symbol"
            elif item.value < 0 or item.value + item.size > len(section.data):
                reason = "symbol exceeds section bytes"
            elif any(rel.target_section_index == item.shndx
                     and item.value <= rel.offset < item.value + item.size for rel in obj.relocations):
                reason = "symbol contains relocations"
        if reason:
            info["skipped"].append({"symbol": item.name, "reason": reason})
            continue
        payload = section.data[item.value:item.value + item.size]
        label = json.dumps(item.name)
        lines = ['.section .rodata, "a"', f".obj {label}, local"]
        for offset in range(0, len(payload), 16):
            lines.append(".byte " + ", ".join(f"0x{byte:02X}" for byte in payload[offset:offset + 16]))
        lines.append(f".endobj {label}")
        blocks.append("\n".join(lines))
        info["added"].append({"symbol": item.name, "section": section.name,
                              "offset": item.value, "size": item.size, "sha256": digest(payload)})
    if blocks:
        text = text.rstrip() + "\n\n" + "\n\n".join(blocks) + "\n"
    captured = text.encode()
    info["assembly_sha256"] = digest(captured)
    return captured, info


def owner_path(root: Path, version: str, unit: str) -> Path:
    base = (root / "build" / version / "obj").resolve()
    path = (base / (unit.removeprefix("main/") + ".o")).resolve()
    if not path.is_relative_to(base):
        raise ValueError("Target owner escapes object directory")
    return path


def owner_fingerprints(root: Path, version: str, units: list[str]) -> dict[str, str]:
    paths = {owner_path(root, version, unit) for unit in units}
    return {str(path): digest(path.read_bytes()) for path in sorted(paths)}
