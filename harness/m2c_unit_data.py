"""Recover typed integer storage and explicit absolute-address pointer tables."""

from __future__ import annotations

import copy


def recover(declarations: list, owner) -> list[dict]:
    from m2c_pycparser import c_ast as ca
    aliases = {node.name: node.type for node in declarations if isinstance(node, ca.Typedef)}
    recovered = []

    def resolved(node, seen=frozenset()):
        if isinstance(node, ca.TypeDecl) and isinstance(node.type, ca.IdentifierType):
            names = node.type.names
            if len(names) == 1 and names[0] in aliases and names[0] not in seen:
                return resolved(aliases[names[0]], seen | {names[0]})
        return node

    def pointer_values(node, symbol, section, array):
        pointer = resolved(node.type.type if array else node.type)
        if not isinstance(pointer, ca.PtrDecl) or section.type != 1 or symbol.size % 4:
            return None
        if not array and symbol.size != 4:
            return None
        if symbol.value % 4 or len(section.data) != section.size:
            return None
        count = symbol.size // 4
        if array and node.type.dim is not None:
            if not isinstance(node.type.dim, ca.Constant):
                return None
            try:
                if int(node.type.dim.value, 0) != count:
                    return None
            except ValueError:
                return None
        relocations = [r for r in owner.relocations if r.target_section_index == symbol.shndx]
        # Only R_PPC_ADDR32 has a decoded footprint here. Reject mixed sections
        # until other relocation widths can be checked for overlapping writes.
        if any(getattr(r, "type", None) != 1 or r.offset % 4 for r in relocations):
            return None
        declared = {d.name for d in declarations if isinstance(d, ca.Decl) and d.name}
        values, references = [], []
        for offset in range(symbol.value, symbol.value + symbol.size, 4):
            matches = [r for r in relocations if r.offset == offset]
            payload = section.data[offset:offset + 4]
            if payload != bytes(4) or len(matches) > 1:
                return None
            expression = ca.Constant("int", "0")
            if matches:
                rel = matches[0]
                if getattr(rel, "addend", None) != 0 or not 0 <= rel.symbol_index < len(owner.symbols):
                    return None
                target = owner.symbols[rel.symbol_index]
                if (not target.name or target.name not in declared or target.type not in (0, 1, 2)
                        or target.shndx >= 0xFF00 or target.other != 0
                        or sum(s.name == target.name for s in owner.symbols) != 1):
                    return None
                expression = ca.UnaryOp("&", ca.ID(target.name))
                references.append({"offset": offset, "type": 1, "symbol": target.name, "addend": 0})
            cast_type = copy.deepcopy(pointer)

            def anonymous(value):
                if isinstance(value, ca.TypeDecl):
                    value.declname = None
                for _, child in value.children():
                    anonymous(child)

            anonymous(cast_type)
            values.append(ca.Cast(ca.Typename(None, [], None, [], cast_type), expression))
        return values, references

    def integer_type(node, seen=frozenset()):
        if isinstance(node, ca.TypeDecl):
            return integer_type(node.type, seen)
        if not isinstance(node, ca.IdentifierType):
            return None
        names = node.names
        if len(names) == 1 and names[0] in aliases and names[0] not in seen:
            return integer_type(aliases[names[0]], seen | {names[0]})
        if not set(names) <= {"signed", "unsigned", "char", "short", "int", "long"}:
            return None
        if names == ["char"]:
            return None  # Plain char signedness belongs to the compiler profile.
        width = 1 if "char" in names else 2 if "short" in names else 8 if names.count("long") == 2 else 4
        return width, "unsigned" not in names

    for node in declarations:
        if not isinstance(node, ca.Decl) or not node.name or node.init is not None:
            continue
        candidates = [s for s in owner.symbols if s.name == node.name and s.type == 1
                      and 0 < s.shndx < len(owner.sections)]
        if len(candidates) != 1:
            continue
        symbol = candidates[0]
        section = owner.sections[symbol.shndx]
        if (section.type not in (1, 8) or not section.flags & 2 or section.flags & 4
                or symbol.bind not in (0, 1) or symbol.other != 0 or symbol.size <= 0
                or symbol.value < 0 or symbol.value + symbol.size > section.size):
            continue
        if any(s.index != symbol.index and s.shndx == symbol.shndx and s.type == 1 and s.size
               and s.value < symbol.value + symbol.size and symbol.value < s.value + s.size
               for s in owner.symbols):
            continue
        array = isinstance(node.type, ca.ArrayDecl)
        pointers = pointer_values(node, symbol, section, array)
        if pointers is not None:
            values, references = pointers
            node.storage = ["static"] if symbol.bind == 0 else []
            if array:
                node.type.dim = ca.Constant("int", str(len(values)))
            node.init = ca.InitList(values) if array else values[0]
            recovered.append({"symbol": node.name, "section": section.name, "offset": symbol.value,
                              "size": symbol.size, "element_width": 4, "binding": symbol.bind,
                              "kind": "absolute_pointer_storage", "relocations": references})
            continue
        if any(r.target_section_index == symbol.shndx for r in owner.relocations):
            continue
        element = node.type
        dimensions = []
        while isinstance(element, ca.ArrayDecl):
            if element.dim is None:
                dimensions.append(None)
            elif isinstance(element.dim, ca.Constant):
                try:
                    dimensions.append(int(element.dim.value, 0))
                except ValueError:
                    break
            else:
                break
            element = element.type
        shape = integer_type(element)
        if shape is None:
            continue
        width, signed = shape
        if symbol.size % width or (not array and symbol.size != width):
            continue
        count = symbol.size // width
        if dimensions:
            # Only the outer extent may be inferred. Inner extents define the
            # row-major layout and must be explicit positive integer constants.
            if any(value is None or value <= 0 for value in dimensions[1:]):
                continue
            inner_count = 1
            for value in dimensions[1:]:
                inner_count *= value
            if dimensions[0] is None:
                if count % inner_count:
                    continue
                dimensions[0] = count // inner_count
            if dimensions[0] <= 0 or dimensions[0] * inner_count != count:
                continue
        if section.type == 1 and len(section.data) != section.size:
            continue
        payload = section.data[symbol.value:symbol.value + symbol.size] if section.type == 1 else bytes(symbol.size)
        values = [int.from_bytes(payload[i:i + width], "big", signed=signed)
                  for i in range(0, len(payload), width)]
        suffix = ("LL" if signed else "ULL") if width == 8 else "" if signed else "U"

        def constant(value):
            magnitude = ca.Constant("int", str(abs(value)) + suffix)
            return ca.UnaryOp("-", magnitude) if value < 0 else magnitude

        def initializer(values, extents):
            if not extents:
                return constant(values[0])
            stride = len(values) // extents[0]
            return ca.InitList([initializer(values[i:i + stride], extents[1:])
                                for i in range(0, len(values), stride)])

        node.storage = ["static"] if symbol.bind == 0 else []
        if array:
            node.type.dim = ca.Constant("int", str(dimensions[0]))
        if section.type != 8:
            node.init = initializer(values, dimensions)
        recovered.append({"symbol": node.name, "section": section.name, "offset": symbol.value,
                          "size": symbol.size, "element_width": width, "signed": signed,
                          "binding": symbol.bind, "zero_fill": section.type == 8,
                          "dimensions": dimensions})
    return recovered
