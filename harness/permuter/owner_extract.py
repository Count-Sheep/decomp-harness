#!/usr/bin/env python3
"""Audit a full-owner MWCC object and emit one relocation-preserving function.

This is intentionally a small ELF32/PowerPC reader/writer rather than a
general ELF library.  The accepted surface is narrow and fail-closed:

* both inputs must be big-endian PowerPC ET_REL objects;
* all allocatable non-text sections, non-target symbols, sibling text bytes,
  and sibling relocations must be unchanged;
* target relocations may name undefined symbols or symbols within the target,
  but may not depend on a sibling or data definition;
* the output contains exactly one STT_FUNC symbol.

The target body may change size.  Text after it is compared relative to the
target end, which permits the mechanical address shift caused by that size
change while rejecting every sibling byte or relocation change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ELF_MAGIC = b"\x7fELF"
ET_REL = 1
EM_PPC = 20
SHN_UNDEF = 0
SHN_ABS = 0xFFF1
SHT_NULL = 0
SHT_PROGBITS = 1
SHT_SYMTAB = 2
SHT_STRTAB = 3
SHT_RELA = 4
SHT_REL = 9
SHF_ALLOC = 0x2
STT_NOTYPE = 0
STT_OBJECT = 1
STT_FUNC = 2
STT_SECTION = 3
STB_LOCAL = 0


class ExtractError(RuntimeError):
    """An owner object is outside the audited extraction contract."""


def _slice(data: bytes, offset: int, size: int, label: str) -> bytes:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise ExtractError(f"{label} is outside the ELF file")
    return data[offset : offset + size]


def _cstring(data: bytes, offset: int, label: str) -> str:
    if offset < 0 or offset >= len(data):
        raise ExtractError(f"{label} has an invalid string offset")
    end = data.find(b"\0", offset)
    if end < 0:
        raise ExtractError(f"{label} is not NUL terminated")
    try:
        return data[offset:end].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExtractError(f"{label} is not UTF-8") from exc


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _sha256_bytes(encoded)


@dataclass(frozen=True)
class Section:
    index: int
    name: str
    type: int
    flags: int
    addr: int
    offset: int
    size: int
    link: int
    info: int
    addralign: int
    entsize: int
    data: bytes


@dataclass(frozen=True)
class Symbol:
    index: int
    name: str
    value: int
    size: int
    info: int
    other: int
    shndx: int

    @property
    def bind(self) -> int:
        return self.info >> 4

    @property
    def type(self) -> int:
        return self.info & 0xF


@dataclass(frozen=True)
class Relocation:
    section_index: int
    target_section_index: int
    offset: int
    symbol_index: int
    type: int
    addend: int | None


@dataclass(frozen=True)
class ElfObject:
    path: Path
    data: bytes
    flags: int
    sections: tuple[Section, ...]
    symbols: tuple[Symbol, ...]
    relocations: tuple[Relocation, ...]

    def section(self, index: int) -> Section:
        if index < 0 or index >= len(self.sections):
            raise ExtractError(f"invalid section index {index} in {self.path}")
        return self.sections[index]


def read_elf(path: Path) -> ElfObject:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ExtractError(f"cannot read owner object: {path}") from exc
    if len(data) < 52 or data[:4] != ELF_MAGIC:
        raise ExtractError(f"not an ELF file: {path}")
    if data[4] != 1 or data[5] != 2 or data[6] != 1:
        raise ExtractError(f"owner must be ELF32 big-endian version 1: {path}")
    try:
        (
            _,
            e_type,
            e_machine,
            e_version,
            _e_entry,
            _e_phoff,
            e_shoff,
            e_flags,
            e_ehsize,
            _e_phentsize,
            e_phnum,
            e_shentsize,
            e_shnum,
            e_shstrndx,
        ) = struct.unpack_from(">16sHHIIIIIHHHHHH", data, 0)
    except struct.error as exc:
        raise ExtractError(f"truncated ELF header: {path}") from exc
    if e_type != ET_REL or e_machine != EM_PPC or e_version != 1:
        raise ExtractError(f"owner must be a PowerPC ET_REL object: {path}")
    if e_ehsize != 52 or e_phnum != 0 or e_shentsize != 40:
        raise ExtractError(f"unsupported ELF header layout: {path}")
    if e_shnum < 1 or e_shnum >= 0xFF00 or e_shstrndx >= e_shnum:
        raise ExtractError(f"unsupported section table layout: {path}")

    raw_headers: list[tuple[int, ...]] = []
    for index in range(e_shnum):
        raw = _slice(data, e_shoff + index * e_shentsize, 40, "section header")
        raw_headers.append(struct.unpack(">IIIIIIIIII", raw))
    shstr_header = raw_headers[e_shstrndx]
    shstr = _slice(data, shstr_header[4], shstr_header[5], "section name table")

    sections: list[Section] = []
    for index, header in enumerate(raw_headers):
        (
            sh_name,
            sh_type,
            sh_flags,
            sh_addr,
            sh_offset,
            sh_size,
            sh_link,
            sh_info,
            sh_addralign,
            sh_entsize,
        ) = header
        name = "" if index == 0 and sh_name == 0 else _cstring(shstr, sh_name, "section")
        payload = b"" if sh_type == 8 else _slice(data, sh_offset, sh_size, f"section {name}")
        sections.append(
            Section(
                index,
                name,
                sh_type,
                sh_flags,
                sh_addr,
                sh_offset,
                sh_size,
                sh_link,
                sh_info,
                sh_addralign,
                sh_entsize,
                payload,
            )
        )

    symtabs = [section for section in sections if section.type == SHT_SYMTAB]
    if len(symtabs) != 1:
        raise ExtractError(f"owner must contain exactly one symbol table: {path}")
    symtab = symtabs[0]
    if symtab.entsize != 16 or symtab.size % 16 or symtab.link >= len(sections):
        raise ExtractError(f"invalid symbol table: {path}")
    strtab = sections[symtab.link]
    if strtab.type != SHT_STRTAB:
        raise ExtractError(f"symbol table links to a non-string section: {path}")
    symbols: list[Symbol] = []
    for index in range(symtab.size // 16):
        st_name, st_value, st_size, st_info, st_other, st_shndx = struct.unpack_from(
            ">IIIBBH", symtab.data, index * 16
        )
        name = "" if st_name == 0 else _cstring(strtab.data, st_name, "symbol")
        if st_shndx not in (SHN_UNDEF, SHN_ABS) and st_shndx >= len(sections):
            raise ExtractError(f"symbol {name or index} has an invalid section")
        symbols.append(
            Symbol(index, name, st_value, st_size, st_info, st_other, st_shndx)
        )
    if not symbols or any((symbols[0].name, symbols[0].value, symbols[0].shndx)):
        raise ExtractError(f"symbol zero is malformed: {path}")

    relocations: list[Relocation] = []
    for section in sections:
        if section.type not in (SHT_REL, SHT_RELA):
            continue
        expected_size = 8 if section.type == SHT_REL else 12
        if (
            section.entsize != expected_size
            or section.size % expected_size
            or section.link != symtab.index
            or section.info >= len(sections)
        ):
            raise ExtractError(f"invalid relocation section {section.name}: {path}")
        for offset in range(0, section.size, expected_size):
            if section.type == SHT_RELA:
                r_offset, r_info, r_addend = struct.unpack_from(">IIi", section.data, offset)
                addend: int | None = r_addend
            else:
                r_offset, r_info = struct.unpack_from(">II", section.data, offset)
                addend = None
            symbol_index = r_info >> 8
            if symbol_index >= len(symbols):
                raise ExtractError(f"relocation in {section.name} has an invalid symbol")
            target_section = sections[section.info]
            if r_offset >= target_section.size:
                raise ExtractError(f"relocation in {section.name} is outside its section")
            relocations.append(
                Relocation(
                    section.index,
                    section.info,
                    r_offset,
                    symbol_index,
                    r_info & 0xFF,
                    addend,
                )
            )
    return ElfObject(path, data, e_flags, tuple(sections), tuple(symbols), tuple(relocations))


@dataclass(frozen=True)
class Target:
    symbol: Symbol
    section: Section

    @property
    def start(self) -> int:
        return self.symbol.value

    @property
    def end(self) -> int:
        return self.symbol.value + self.symbol.size


def find_target(owner: ElfObject, function: str) -> Target:
    matches = [
        symbol
        for symbol in owner.symbols
        if symbol.name == function and symbol.type == STT_FUNC and symbol.shndx != SHN_UNDEF
    ]
    if len(matches) != 1:
        raise ExtractError(
            f"expected one defined STT_FUNC named {function} in {owner.path}, got {len(matches)}"
        )
    symbol = matches[0]
    section = owner.section(symbol.shndx)
    if section.name != ".text" or section.type != SHT_PROGBITS:
        raise ExtractError(f"target {function} is not in a .text PROGBITS section")
    if symbol.size <= 0 or symbol.value + symbol.size > section.size:
        raise ExtractError(f"target {function} has an invalid range")
    return Target(symbol, section)


def _compiler_local_symbol(symbol: Symbol) -> bool:
    return (
        symbol.bind == STB_LOCAL
        and symbol.shndx not in (SHN_UNDEF, SHN_ABS)
        and re.fullmatch(r"@[0-9]+", symbol.name) is not None
    )


def _symbol_identity(owner: ElfObject, target: Target, symbol: Symbol) -> tuple[Any, ...]:
    # MWCC renumbers compiler-generated @123 labels when target code shape
    # changes.  The number carries no identity: section, normalized position,
    # type/binding, visibility, and size do.  Never normalize undefined,
    # global, named-static, or data symbols.
    name = "<compiler-local>" if _compiler_local_symbol(symbol) else symbol.name
    common = (name, symbol.type, symbol.bind, symbol.other, symbol.size)
    if symbol.shndx == SHN_UNDEF:
        return ("UND", *common, symbol.value)
    if symbol.shndx == SHN_ABS:
        return ("ABS", *common, symbol.value)
    section = owner.section(symbol.shndx)
    if symbol.shndx != target.section.index:
        return ("SECTION", section.name, *common, symbol.value)
    if symbol.value < target.start:
        position: tuple[str, int] = ("TEXT_PRE", symbol.value)
    elif symbol.value < target.end:
        # A sibling relocation may legitimately keep naming the target while
        # a candidate changes its size.  Size is target-owned state, not part
        # of the sibling relocation identity.
        return (
            "TEXT_TARGET",
            symbol.value - target.start,
            name,
            symbol.type,
            symbol.bind,
            symbol.other,
        )
    else:
        position = ("TEXT_POST", symbol.value - target.end)
    return (*position, *common)


def _normalized_relocations(
    owner: ElfObject,
    target: Target,
    relocations: Iterable[Relocation],
    *,
    position_mode: str,
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for relocation in relocations:
        if position_mode == "target":
            normalized_offset: tuple[str, int] = (
                "TARGET",
                relocation.offset - target.start,
            )
        elif relocation.target_section_index == target.section.index:
            if relocation.offset < target.start:
                normalized_offset = ("TEXT_PRE", relocation.offset)
            elif relocation.offset >= target.end:
                normalized_offset = ("TEXT_POST", relocation.offset - target.end)
            else:
                raise ExtractError("target relocation leaked into sibling audit")
        else:
            normalized_offset = (
                owner.section(relocation.target_section_index).name,
                relocation.offset,
            )
        rows.append(
            (
                *normalized_offset,
                relocation.type,
                relocation.addend,
                _symbol_identity(owner, target, owner.symbols[relocation.symbol_index]),
            )
        )
    return sorted(rows, key=repr)


def target_relocations(owner: ElfObject, target: Target) -> list[Relocation]:
    return sorted(
        [
            relocation
            for relocation in owner.relocations
            if relocation.target_section_index == target.section.index
            and target.start <= relocation.offset < target.end
        ],
        key=lambda relocation: (relocation.offset, relocation.type, relocation.symbol_index),
    )


def _target_topology(target: Target) -> dict[str, Any]:
    """Return link-visible target state which candidate code may not change."""
    return {
        "symbol": {
            "st_info": target.symbol.info,
            "st_other": target.symbol.other,
        },
        "text_section": {
            "sh_flags": target.section.flags,
            "sh_addr": target.section.addr,
            "sh_addralign": target.section.addralign,
        },
    }


def target_record(owner: ElfObject, target: Target) -> dict[str, Any]:
    code = target.section.data[target.start : target.end]
    relocations = _normalized_relocations(
        owner, target, target_relocations(owner, target), position_mode="target"
    )
    topology = _target_topology(target)
    record = {
        "start": target.start,
        "size": target.symbol.size,
        "bytes_sha256": _sha256_bytes(code),
        "relocations": relocations,
        "relocations_sha256": _canonical_sha256(relocations),
        "topology": topology,
    }
    record["fingerprint_sha256"] = _canonical_sha256(
        {
            "size": record["size"],
            "bytes_sha256": record["bytes_sha256"],
            "relocations": relocations,
            "topology": topology,
        }
    )
    return record


def _function_symbols(owner: ElfObject, target: Target) -> dict[str, Symbol]:
    result: dict[str, Symbol] = {}
    for symbol in owner.symbols:
        if symbol.type != STT_FUNC or symbol.shndx != target.section.index:
            continue
        if _compiler_local_symbol(symbol):
            # Stable identity is checked by _defined_sibling_symbols; the
            # unstable generated spelling must not enter the named set.
            continue
        if not symbol.name:
            raise ExtractError(f"anonymous function symbol in {owner.path}")
        if symbol.name in result:
            raise ExtractError(f"duplicate function symbol {symbol.name} in {owner.path}")
        result[symbol.name] = symbol
    return result


def _program_sections(owner: ElfObject, target: Target) -> dict[str, Section]:
    result: dict[str, Section] = {}
    for section in owner.sections:
        if section.index == target.section.index or not section.flags & SHF_ALLOC:
            continue
        if not section.name or section.name in result:
            raise ExtractError(f"ambiguous allocatable section in {owner.path}")
        result[section.name] = section
    return result


def _defined_sibling_symbols(owner: ElfObject, target: Target) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for symbol in owner.symbols:
        if symbol.index == target.symbol.index or symbol.shndx in (SHN_UNDEF,):
            continue
        if (
            symbol.shndx == target.section.index
            and target.start <= symbol.value < target.end
        ):
            continue
        if symbol.type == 4:  # STT_FILE is provenance, not emitted program state.
            continue
        rows.append(_symbol_identity(owner, target, symbol))
    return sorted(rows, key=repr)


def standalone_text_record(path: Path) -> dict[str, Any]:
    """Fingerprint a scorer/reference ELF without requiring a sized FUNC."""
    owner = read_elf(path)
    text_sections = [
        section
        for section in owner.sections
        if section.name == ".text" and section.type == SHT_PROGBITS
    ]
    if len(text_sections) != 1:
        raise ExtractError(f"expected one .text section in {path}")
    text = text_sections[0]
    relocations = []
    for relocation in owner.relocations:
        if relocation.target_section_index != text.index:
            continue
        symbol = owner.symbols[relocation.symbol_index]
        if symbol.shndx == SHN_UNDEF:
            symbol_location: tuple[Any, ...] = ("UND", symbol.name)
        elif symbol.shndx == SHN_ABS:
            symbol_location = ("ABS", symbol.name, symbol.value)
        else:
            section = owner.section(symbol.shndx)
            symbol_location = (section.name, symbol.name, symbol.value)
        relocations.append(
            (
                relocation.offset,
                relocation.type,
                relocation.addend,
                symbol_location,
                symbol.type,
                symbol.bind,
                symbol.other,
                symbol.size,
            )
        )
    relocations.sort(key=repr)
    record = {
        "size": text.size,
        "bytes_sha256": _sha256_bytes(text.data),
        "relocations": relocations,
        "relocations_sha256": _canonical_sha256(relocations),
        "elf_sha256": _sha256_bytes(owner.data),
    }
    record["fingerprint_sha256"] = _canonical_sha256(
        {"bytes_sha256": record["bytes_sha256"], "relocations": relocations}
    )
    return record


def audit_siblings(
    baseline: ElfObject,
    candidate: ElfObject,
    baseline_target: Target,
    candidate_target: Target,
) -> dict[str, Any]:
    if baseline.flags != candidate.flags:
        raise ExtractError("owner ELF flags drifted")
    if (
        baseline_target.symbol.info,
        baseline_target.symbol.other,
    ) != (
        candidate_target.symbol.info,
        candidate_target.symbol.other,
    ):
        raise ExtractError("target symbol topology drifted")
    if _target_topology(baseline_target)["text_section"] != _target_topology(
        candidate_target
    )["text_section"]:
        raise ExtractError("target text section topology drifted")
    if baseline_target.start != candidate_target.start:
        raise ExtractError("target start drifted; a preceding definition changed")
    if baseline_target.section.data[: baseline_target.start] != candidate_target.section.data[: candidate_target.start]:
        raise ExtractError("text before the target drifted")
    if baseline_target.section.data[baseline_target.end :] != candidate_target.section.data[candidate_target.end :]:
        raise ExtractError("text after the target drifted")

    baseline_functions = _function_symbols(baseline, baseline_target)
    candidate_functions = _function_symbols(candidate, candidate_target)
    if set(baseline_functions) != set(candidate_functions):
        raise ExtractError("sibling function symbol set drifted")
    post_shift = candidate_target.end - baseline_target.end
    for name in sorted(baseline_functions):
        if name == baseline_target.symbol.name:
            continue
        before = baseline_functions[name]
        after = candidate_functions[name]
        if before.size != after.size or before.info != after.info or before.other != after.other:
            raise ExtractError(f"sibling function symbol drifted: {name}")
        if before.value < baseline_target.start:
            expected = before.value
        elif before.value >= baseline_target.end:
            expected = before.value + post_shift
        else:
            raise ExtractError(f"sibling function overlaps target: {name}")
        if after.value != expected:
            raise ExtractError(f"sibling function address drifted unexpectedly: {name}")

    baseline_sections = _program_sections(baseline, baseline_target)
    candidate_sections = _program_sections(candidate, candidate_target)
    if set(baseline_sections) != set(candidate_sections):
        raise ExtractError("allocatable section set drifted")
    for name in sorted(baseline_sections):
        before = baseline_sections[name]
        after = candidate_sections[name]
        identity_before = (
            before.type,
            before.flags,
            before.addr,
            before.size,
            before.addralign,
            before.entsize,
            before.data,
        )
        identity_after = (
            after.type,
            after.flags,
            after.addr,
            after.size,
            after.addralign,
            after.entsize,
            after.data,
        )
        if identity_before != identity_after:
            raise ExtractError(f"allocatable section drifted: {name}")

    if _defined_sibling_symbols(baseline, baseline_target) != _defined_sibling_symbols(
        candidate, candidate_target
    ):
        raise ExtractError("defined sibling symbol table drifted")

    baseline_sibling_relocs = [
        relocation
        for relocation in baseline.relocations
        if not (
            relocation.target_section_index == baseline_target.section.index
            and baseline_target.start <= relocation.offset < baseline_target.end
        )
    ]
    candidate_sibling_relocs = [
        relocation
        for relocation in candidate.relocations
        if not (
            relocation.target_section_index == candidate_target.section.index
            and candidate_target.start <= relocation.offset < candidate_target.end
        )
    ]
    normalized_baseline = _normalized_relocations(
        baseline, baseline_target, baseline_sibling_relocs, position_mode="sibling"
    )
    normalized_candidate = _normalized_relocations(
        candidate, candidate_target, candidate_sibling_relocs, position_mode="sibling"
    )
    if normalized_baseline != normalized_candidate:
        raise ExtractError("sibling relocation table drifted")

    def unused_undefined(owner: ElfObject, target: Target) -> list[tuple[Any, ...]]:
        target_refs = {
            relocation.symbol_index for relocation in target_relocations(owner, target)
        }
        return sorted(
            [
                _symbol_identity(owner, target, symbol)
                for symbol in owner.symbols
                if symbol.shndx == SHN_UNDEF
                and symbol.index != 0
                and symbol.index not in target_refs
            ],
            key=repr,
        )

    if unused_undefined(baseline, baseline_target) != unused_undefined(
        candidate, candidate_target
    ):
        raise ExtractError("undefined sibling symbol table drifted")

    return {
        "state": "passed",
        "sibling_functions": len(baseline_functions) - 1,
        "sibling_relocations": len(baseline_sibling_relocs),
        "allocatable_non_text_sections": len(baseline_sections),
        "post_target_shift": post_shift,
        "pre_text_sha256": _sha256_bytes(
            baseline_target.section.data[: baseline_target.start]
        ),
        "post_text_sha256": _sha256_bytes(
            baseline_target.section.data[baseline_target.end :]
        ),
        "sibling_relocations_sha256": _canonical_sha256(normalized_baseline),
    }


def _align(value: int, alignment: int) -> int:
    alignment = max(1, alignment)
    return (value + alignment - 1) & ~(alignment - 1)


def _string_table(strings: Iterable[str]) -> tuple[bytes, dict[str, int]]:
    data = bytearray(b"\0")
    offsets = {"": 0}
    for value in strings:
        if value in offsets:
            continue
        offsets[value] = len(data)
        data.extend(value.encode("utf-8") + b"\0")
    return bytes(data), offsets


def build_single_target_elf(owner: ElfObject, target: Target) -> bytes:
    code = target.section.data[target.start : target.end]
    relocations = target_relocations(owner, target)

    referenced_indices: list[int] = []
    for relocation in relocations:
        if relocation.symbol_index not in referenced_indices:
            referenced_indices.append(relocation.symbol_index)

    local_symbols: list[tuple[int | None, Symbol]] = []
    global_symbols: list[tuple[int | None, Symbol]] = []
    section_symbol = Symbol(-1, "", 0, 0, (STB_LOCAL << 4) | STT_SECTION, 0, 1)
    local_symbols.append((None, section_symbol))
    target_copy = Symbol(
        target.symbol.index,
        target.symbol.name,
        0,
        target.symbol.size,
        target.symbol.info,
        target.symbol.other,
        1,
    )
    (local_symbols if target_copy.bind == STB_LOCAL else global_symbols).append(
        (target.symbol.index, target_copy)
    )

    for symbol_index in referenced_indices:
        if symbol_index == target.symbol.index:
            continue
        symbol = owner.symbols[symbol_index]
        if symbol.shndx == SHN_UNDEF:
            copy = symbol
        elif symbol.shndx == target.section.index and target.start <= symbol.value < target.end:
            if symbol.type == STT_FUNC:
                raise ExtractError(
                    f"target relocation references another function: {symbol.name or symbol.index}"
                )
            copy = Symbol(
                symbol.index,
                symbol.name,
                symbol.value - target.start,
                symbol.size,
                symbol.info,
                symbol.other,
                1,
            )
        else:
            raise ExtractError(
                "target relocation references a definition outside the target: "
                f"{symbol.name or symbol.index}"
            )
        (local_symbols if copy.bind == STB_LOCAL else global_symbols).append(
            (symbol.index, copy)
        )

    emitted_symbols = local_symbols + global_symbols
    old_to_new: dict[int, int] = {}
    for new_index, (old_index, _symbol) in enumerate(emitted_symbols, 1):
        if old_index is not None:
            old_to_new[old_index] = new_index
    if target.symbol.index not in old_to_new:
        raise ExtractError("target symbol was not emitted")

    strtab, str_offsets = _string_table(symbol.name for _, symbol in emitted_symbols)
    symtab_data = bytearray(b"\0" * 16)
    for _, symbol in emitted_symbols:
        symtab_data.extend(
            struct.pack(
                ">IIIBBH",
                str_offsets[symbol.name],
                symbol.value,
                symbol.size,
                symbol.info,
                symbol.other,
                symbol.shndx,
            )
        )

    relocation_kind: int | None = None
    relocation_data = bytearray()
    for relocation in relocations:
        section = owner.section(relocation.section_index)
        if relocation_kind is None:
            relocation_kind = section.type
        elif relocation_kind != section.type:
            raise ExtractError("target uses both REL and RELA relocation sections")
        new_symbol = old_to_new.get(relocation.symbol_index)
        if new_symbol is None:
            raise ExtractError("target relocation symbol was not emitted")
        r_info = (new_symbol << 8) | relocation.type
        if section.type == SHT_RELA:
            assert relocation.addend is not None
            relocation_data.extend(
                struct.pack(">IIi", relocation.offset - target.start, r_info, relocation.addend)
            )
        elif section.type == SHT_REL:
            relocation_data.extend(
                struct.pack(">II", relocation.offset - target.start, r_info)
            )
        else:
            raise ExtractError("target relocation came from an invalid section")

    section_names = ["", ".text"]
    if relocation_kind is not None:
        section_names.append(".rela.text" if relocation_kind == SHT_RELA else ".rel.text")
    section_names.extend([".symtab", ".strtab", ".shstrtab"])
    shstrtab, shstr_offsets = _string_table(section_names)
    text_index = 1
    relocation_index = 2 if relocation_kind is not None else None
    symtab_index = 3 if relocation_kind is not None else 2
    strtab_index = symtab_index + 1
    shstrtab_index = symtab_index + 2

    payloads: list[tuple[str, bytes, int]] = [(".text", code, max(1, target.section.addralign))]
    if relocation_kind is not None:
        payloads.append((section_names[2], bytes(relocation_data), 4))
    payloads.extend(
        [(".symtab", bytes(symtab_data), 4), (".strtab", strtab, 1), (".shstrtab", shstrtab, 1)]
    )

    output = bytearray(b"\0" * 52)
    section_offsets: dict[str, int] = {}
    for name, payload, alignment in payloads:
        padded = _align(len(output), alignment)
        output.extend(b"\0" * (padded - len(output)))
        section_offsets[name] = len(output)
        output.extend(payload)
    shoff = _align(len(output), 4)
    output.extend(b"\0" * (shoff - len(output)))

    headers: list[bytes] = [b"\0" * 40]
    headers.append(
        struct.pack(
            ">IIIIIIIIII",
            shstr_offsets[".text"],
            SHT_PROGBITS,
            target.section.flags,
            target.section.addr,
            section_offsets[".text"],
            len(code),
            0,
            0,
            target.section.addralign,
            0,
        )
    )
    if relocation_kind is not None:
        relocation_name = ".rela.text" if relocation_kind == SHT_RELA else ".rel.text"
        headers.append(
            struct.pack(
                ">IIIIIIIIII",
                shstr_offsets[relocation_name],
                relocation_kind,
                0,
                0,
                section_offsets[relocation_name],
                len(relocation_data),
                symtab_index,
                text_index,
                4,
                12 if relocation_kind == SHT_RELA else 8,
            )
        )
    headers.extend(
        [
            struct.pack(
                ">IIIIIIIIII",
                shstr_offsets[".symtab"],
                SHT_SYMTAB,
                0,
                0,
                section_offsets[".symtab"],
                len(symtab_data),
                strtab_index,
                1 + len(local_symbols),
                4,
                16,
            ),
            struct.pack(
                ">IIIIIIIIII",
                shstr_offsets[".strtab"],
                SHT_STRTAB,
                0,
                0,
                section_offsets[".strtab"],
                len(strtab),
                0,
                0,
                1,
                0,
            ),
            struct.pack(
                ">IIIIIIIIII",
                shstr_offsets[".shstrtab"],
                SHT_STRTAB,
                0,
                0,
                section_offsets[".shstrtab"],
                len(shstrtab),
                0,
                0,
                1,
                0,
            ),
        ]
    )
    for header in headers:
        output.extend(header)

    ident = ELF_MAGIC + bytes((1, 2, 1, 0, 0)) + b"\0" * 7
    elf_header = struct.pack(
        ">16sHHIIIIIHHHHHH",
        ident,
        ET_REL,
        EM_PPC,
        1,
        0,
        0,
        shoff,
        owner.flags,
        52,
        0,
        0,
        40,
        len(headers),
        shstrtab_index,
    )
    output[:52] = elf_header
    return bytes(output)


def _run_tool(argv: list[str], label: str) -> None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExtractError(f"{label} could not run") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ExtractError(f"{label} rejected extracted ELF: {detail[-1200:]}")


def extract(
    *,
    baseline_path: Path,
    candidate_path: Path,
    function: str,
    output_path: Path,
    objcopy: Path,
    readelf: Path,
    expected_baseline_fingerprint: str | None = None,
) -> dict[str, Any]:
    baseline = read_elf(baseline_path)
    candidate = read_elf(candidate_path)
    baseline_target = find_target(baseline, function)
    candidate_target = find_target(candidate, function)
    baseline_record = target_record(baseline, baseline_target)
    candidate_record = target_record(candidate, candidate_target)
    if expected_baseline_fingerprint is not None and (
        baseline_record["fingerprint_sha256"] != expected_baseline_fingerprint
    ):
        raise ExtractError(
            "baseline target fingerprint drifted: expected "
            f"{expected_baseline_fingerprint}, got {baseline_record['fingerprint_sha256']}"
        )
    sibling_audit = audit_siblings(
        baseline, candidate, baseline_target, candidate_target
    )
    raw = build_single_target_elf(candidate, candidate_target)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="owner-extract-") as temporary:
        raw_path = Path(temporary) / "raw.o"
        raw_path.write_bytes(raw)
        _run_tool(
            [str(objcopy), "-O", "elf32-powerpc", str(raw_path), str(output_path)],
            "powerpc-eabi-objcopy",
        )
    _run_tool([str(readelf), "-h", "-S", "-s", "-r", str(output_path)], "powerpc-eabi-readelf")

    extracted = read_elf(output_path)
    extracted_target = find_target(extracted, function)
    functions = [symbol.name for symbol in extracted.symbols if symbol.type == STT_FUNC]
    if functions != [function]:
        raise ExtractError(f"extracted ELF is not single-target: {functions}")
    extracted_record = target_record(extracted, extracted_target)
    if extracted_record["fingerprint_sha256"] != candidate_record["fingerprint_sha256"]:
        raise ExtractError("objcopy changed target bytes, relocations, or topology")
    return {
        "schema": 1,
        "function": function,
        "baseline_owner_sha256": _sha256_bytes(baseline.data),
        "candidate_owner_sha256": _sha256_bytes(candidate.data),
        "baseline": baseline_record,
        "candidate": candidate_record,
        "sibling_audit": sibling_audit,
        "output": {
            **extracted_record,
            "elf_sha256": _sha256_bytes(extracted.data),
            "functions": functions,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--function", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--objcopy", type=Path, required=True)
    parser.add_argument("--readelf", type=Path, required=True)
    parser.add_argument("--expected-baseline-fingerprint")
    parser.add_argument("--report", type=Path)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.expected_baseline_fingerprint is not None and not (
            len(args.expected_baseline_fingerprint) == 64
            and all(c in "0123456789abcdef" for c in args.expected_baseline_fingerprint)
        ):
            raise ExtractError("expected baseline fingerprint must be lowercase SHA-256")
        for label, path in (("objcopy", args.objcopy), ("readelf", args.readelf)):
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ExtractError(f"{label} is not executable: {path}")
        report = extract(
            baseline_path=args.baseline,
            candidate_path=args.candidate,
            function=args.function,
            output_path=args.output,
            objcopy=args.objcopy,
            readelf=args.readelf,
            expected_baseline_fingerprint=args.expected_baseline_fingerprint,
        )
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        return 0
    except ExtractError as exc:
        args.output.unlink(missing_ok=True)
        print(f"owner extract error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
