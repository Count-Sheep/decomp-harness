"""Supply DTK split jump tables and resolve case offsets to real instruction labels."""

from __future__ import annotations

import re
from pathlib import Path

from harness_paths import VERSION

FN =re.compile(r"(?m)^\.fn\s+([^,\s]+)")
INSN = re.compile(r"^/\* ([0-9A-F]{8}) [0-9A-F]{8}\s")
OBJECT = re.compile(r'(?ms)^\.obj\s+("[^"\n]+"|[\w$]+),[^\n]*\n.*?^\.endobj[^\n]*')
ENTRY = re.compile(r"^(\s*\.4byte\s+)([A-Za-z_$][\w$]*)(?:\s*\+\s*(0x[\da-fA-F]+|\d+))?\s*$")
_CATALOG: dict[str, dict[str, list[str]]] = {}


def table_name(match: re.Match) -> str:
    return match.group(1).strip('"')


def is_offset_table(body: str) -> bool:
    entries = [line.strip() for line in body.splitlines()[1:-1]
               if line.strip() and not line.lstrip().startswith(('#', '.hidden'))]
    return len(entries) >= 2 and all(ENTRY.fullmatch(line) for line in entries)


def jump_tables(root: Path, version: str | None = None) -> dict[str, list[str]]:
    version = version or VERSION
    key = f"{root.resolve()}:{version}"
    if key not in _CATALOG:
        catalog = {}
        for path in sorted((root / "build" / version / "asm").rglob("*.s")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in OBJECT.finditer(text):
                name, body = table_name(match), match.group()
                if not name.startswith("jumptable_") and not is_offset_table(body):
                    continue
                variants = catalog.setdefault(name, [])
                if body not in variants:
                    variants.append(body)
        _CATALOG[key] = catalog
    return _CATALOG[key]


def prepare(root: Path, symbol: str, original: bytes, version: str | None = None) -> tuple[bytes, dict]:
    text = original.decode("utf-8", errors="replace")
    function = re.search(rf"(?ms)^\.fn\s+{re.escape(symbol)},[^\n]*\n(.*?)^\.endfn\b", text)
    referenced = {value.strip('"') for value in re.findall(
        r'("[^"\n]+"|[A-Za-z_$][\w$]*)@(?:ha|h|l|sda21)\b', function.group(1))} if function else set()
    catalog = jump_tables(root, version) if referenced else {}
    inline = {table_name(match) for match in OBJECT.finditer(text) if is_offset_table(match.group())}
    wanted = {name for name in referenced if name.startswith("jumptable_") or name in catalog or name in inline}
    info = {"added_tables": [], "normalized_entries": 0, "added_labels": 0, "missing_tables": []}
    if not wanted:
        return original, info
    # DTK comments give actual addresses; never infer an offset from instruction count.
    bases, addresses, current = {}, {}, None
    for line in text.splitlines():
        match = FN.match(line)
        if match:
            current = match.group(1)
        instruction = INSN.match(line)
        if instruction:
            address = int(instruction.group(1), 16)
            if current:
                bases.setdefault(current, address)
                addresses.setdefault(current, set()).add(address)
        if line.startswith(".endfn"):
            current = None
    labels = set()
    defined = set(re.findall(r"(?m)^([.$A-Za-z_][\w.$]*):", text))
    def normalize_table(body: str) -> str:
        lines, new_labels, entries = [], set(), 0
        for line in body.splitlines():
            entry = ENTRY.match(line)
            if entry and entry.group(2) in bases:
                target = bases[entry.group(2)] + int(entry.group(3) or "0", 0)
                if target not in addresses[entry.group(2)]:
                    raise ValueError(f"Jump-table entry {line.strip()} does not target a captured instruction")
                label = f".L_m2c_{target:08X}"
                new_labels.add(target)
                line = entry.group(1) + label
                entries += 1
            elif entry and entry.group(3):
                raise ValueError(f"Cannot resolve jump-table case {line.strip()} from target assembly")
            relative = re.match(r"\s*\.rel\s+[^,]+,\s*(\S+)\s*$", line)
            if relative and relative.group(1) not in defined:
                raise ValueError(f"Jump-table label {relative.group(1)} is absent from captured assembly")
            lines.append(line)
        labels.update(new_labels)
        info["normalized_entries"] += entries
        return "\n".join(lines)
    present = {table_name(match) for match in OBJECT.finditer(text)}
    text = OBJECT.sub(lambda match: normalize_table(match.group()) if table_name(match) in wanted else match.group(), text)
    for name in sorted(wanted - present):
        for body in catalog.get(name, []):
            try:
                normalized = normalize_table(body)
            except ValueError:
                continue
            text += "\n.data\n" + normalized + "\n"
            info["added_tables"].append(name)
            break
        else:
            info["missing_tables"].append(name)
    lines = []
    for line in text.splitlines():
        instruction = INSN.match(line)
        if instruction and int(instruction.group(1), 16) in labels:
            label = f".L_m2c_{int(instruction.group(1), 16):08X}:"
            if label not in text:
                lines.append(label)
                info["added_labels"] += 1
        lines.append(line)
    return ("\n".join(lines) + "\n").encode(), info
