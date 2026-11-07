#!/usr/bin/env python3
"""Conservative compile repair and assembly feedback for m2c drafts.

The repair pass deliberately fixes only mechanically provable C issues.  It
returns an audit trail and blockers so callers can keep uncertain drafts out of
the expensive matching loop instead of silently changing their semantics.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Iterable


_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_CONTROL_WORDS = {"if", "for", "while", "switch", "return", "sizeof"}
_TYPE_WORDS = {
    "char", "double", "f32", "f64", "float", "int", "long", "s8", "s16",
    "s32", "s64", "short", "signed", "u8", "u16", "u32", "u64",
    "unsigned", "void",
}
_KNOWN_FUNCTIONS = {
    "__frsqrte": ("f64", ("f64",)),
    "memcpy": ("void *", ("void *", "const void *", "u32")),
    "memmove": ("void *", ("void *", "const void *", "u32")),
    "memset": ("void *", ("void *", "s32", "u32")),
}


@dataclass(frozen=True)
class Transformation:
    kind: str
    line: int
    before: str
    after: str
    detail: str


@dataclass(frozen=True)
class RepairReport:
    source: str
    transformations: tuple[Transformation, ...]
    unresolved_blockers: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.transformations)

    @property
    def ready_for_compile(self) -> bool:
        return not self.unresolved_blockers

    def to_dict(self) -> dict:
        return {
            "code": self.source,
            "changed": self.changed,
            "ready_for_compile": self.ready_for_compile,
            "transformations": [asdict(item) for item in self.transformations],
            "unresolved": list(self.unresolved_blockers),
        }


@dataclass(frozen=True)
class AssemblyComparison:
    target_count: int
    compiled_count: int
    first_divergent_index: int | None
    target_instruction: str | None
    compiled_instruction: str | None
    target_opcode: str | None
    compiled_opcode: str | None
    opcode_mismatch: bool
    target_stack_frame: int | None
    compiled_stack_frame: int | None
    stack_frame_delta: int | None
    target_shape: dict[str, int]
    compiled_shape: dict[str, int]
    target_only_registers: tuple[str, ...]
    compiled_only_registers: tuple[str, ...]

    @property
    def exact(self) -> bool:
        return self.first_divergent_index is None

    def to_dict(self) -> dict:
        result = asdict(self)
        result["exact"] = self.exact
        return result

    def prompt_text(self) -> str:
        if self.exact:
            return f"Assembly is instruction-exact ({self.target_count} instructions)."
        target = self.target_instruction or "<end>"
        compiled = self.compiled_instruction or "<end>"
        frame = (f"target={_display_frame(self.target_stack_frame)}, "
                 f"compiled={_display_frame(self.compiled_stack_frame)}, "
                 f"delta={_display_delta(self.stack_frame_delta)}")
        registers = (f"target-only={_compact_set(self.target_only_registers)}, "
                     f"compiled-only={_compact_set(self.compiled_only_registers)}")
        return (f"First divergence at instruction {self.first_divergent_index}: "
                f"target `{target}`; compiled `{compiled}`. "
                f"Opcode: {self.target_opcode or '<end>'} -> "
                f"{self.compiled_opcode or '<end>'}. Stack frame: {frame}. "
                f"Branch shape target={_shape_text(self.target_shape)}; "
                f"compiled={_shape_text(self.compiled_shape)}. Registers: {registers}.")


def _record_line_change(
    records: list[Transformation], kind: str, line: int, before: str, after: str, detail: str
) -> None:
    if before != after:
        records.append(Transformation(kind, line, before, after, detail))


def _replace_unknown_types_in_code(code: str) -> str:
    if "M2C_UNK" not in code:
        return code
    # A function pointer's leading type is a return value and must remain a
    # scalar ABI value.  Other pointer-qualified unknowns are safely opaque.
    code = re.sub(r"\bM2C_UNK\b(?=\s*\(\s*\*+)", "s32", code)
    code = re.sub(r"\bM2C_UNK\b(?=\s*\*+)", "void", code)
    return re.sub(r"\bM2C_UNK\b", "s32", code)


def _replace_unknown_types(line: str, in_block_comment: bool) -> tuple[str, bool]:
    """Transform code spans while preserving comments and quoted literals."""
    if line.lstrip().startswith("#"):
        return line, in_block_comment
    result: list[str] = []
    code_start = 0
    index = 0
    while index < len(line):
        if in_block_comment:
            end = line.find("*/", index)
            if end < 0:
                result.append(line[index:])
                return "".join(result), True
            result.append(line[index:end + 2])
            index = end + 2
            code_start = index
            in_block_comment = False
            continue
        if line.startswith("//", index):
            result.append(_replace_unknown_types_in_code(line[code_start:index]))
            result.append(line[index:])
            return "".join(result), False
        if line.startswith("/*", index):
            result.append(_replace_unknown_types_in_code(line[code_start:index]))
            end = line.find("*/", index + 2)
            if end < 0:
                result.append(line[index:])
                return "".join(result), True
            result.append(line[index:end + 2])
            index = end + 2
            code_start = index
            continue
        if line[index] in "\"'":
            result.append(_replace_unknown_types_in_code(line[code_start:index]))
            quote = line[index]
            end = index + 1
            while end < len(line):
                if line[end] == "\\":
                    end += 2
                    continue
                if line[end] == quote:
                    end += 1
                    break
                end += 1
            result.append(line[index:end])
            index = end
            code_start = index
            continue
        index += 1
    result.append(_replace_unknown_types_in_code(line[code_start:]))
    return "".join(result), in_block_comment


def _void_pointer_names(source: str) -> set[str]:
    return set(re.findall(r"\bvoid\s*\*(?!\s*\*)\s*(" + _IDENTIFIER + r")\b", source))


def _repair_void_arithmetic(line: str, names: Iterable[str]) -> str:
    for name in sorted(names, key=len, reverse=True):
        escaped = re.escape(name)
        # Keep the rewrite local to an uncast, direct pointer operand.  Member
        # expressions and pointer subtraction need type evidence we do not have.
        line = re.sub(
            rf"(?<![&\w])\b{escaped}\b\s*\+(?=\s*(?:0[xX][0-9A-Fa-f]+|\d+|[A-Za-z_(]))",
            f"(char *){name} +",
            line,
        )
        line = re.sub(
            rf"\b(0[xX][0-9A-Fa-f]+|\d+)\s*\+\s*\b{escaped}\b",
            rf"(char *){name} + \1",
            line,
        )
    return line


def _void_arithmetic_blockers(source: str, names: Iterable[str]) -> list[str]:
    blockers: list[str] = []
    clean = _mask_comments_and_strings(source)
    for line_number, line in enumerate(clean.splitlines(), 1):
        for name in names:
            escaped = re.escape(name)
            risky = re.search(
                rf"(?<!\))\b{escaped}\b\s*(?:-|\+=|-=|\+\+|--)", line
            )
            if risky:
                blockers.append(
                    f"line {line_number}: unsupported void-pointer arithmetic for {name}"
                )
    return blockers


def _balanced(text: str) -> bool:
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    for char in text:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char in "([{":
            stack.append(char)
        elif char in ")]}":
            if not stack or stack.pop() != pairs[char]:
                return False
    return not stack and quote is None


def _looks_like_safe_missing_semicolon(stripped: str) -> bool:
    if not _balanced(stripped) or stripped.endswith((";", "{", "}", ":", ",", "\\")):
        return False
    if stripped.startswith(("#", "//")):
        return False
    if re.match(r"^(?:if|for|while|switch)\s*\(", stripped):
        return False
    # Calls and explicit mutations have an observable side effect.  A bare
    # dereference/cast does not provide enough evidence for an assignment.
    call = re.match(rf"^{_IDENTIFIER}\s*\(.*\)$", stripped)
    assignment = re.search(r"(?<![=!<>])(?:\+=|-=|\*=|/=|%=|&=|\|=|\^=|=(?!=))", stripped)
    increment = re.search(r"(?:\+\+|--)$", stripped)
    return bool(call or assignment or increment)


def _looks_like_orphan_expression(stripped: str) -> bool:
    if not _balanced(stripped) or stripped.endswith((";", "{", "}", ":", ",", "\\")):
        return False
    return bool(re.match(r"^(?:\*|\(|" + _IDENTIFIER + r"\s*(?:\[|\+|-))", stripped))


def _mask_comments_and_strings(source: str) -> str:
    pattern = re.compile(r"/\*.*?\*/|//[^\n]*|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'", re.S)
    return pattern.sub(lambda match: "".join("\n" if c == "\n" else " " for c in match.group()), source)


def _declarations_and_definitions(source: str) -> tuple[set[str], set[str], set[str]]:
    clean = _mask_comments_and_strings(source)
    functions = set(re.findall(
        rf"(?m)^\s*(?!(?:return|if|for|while|switch)\b)(?:extern\s+)?"
        rf"(?:[A-Za-z_]\w*\s+|\*\s*)+(\w+)\s*\([^;{{}}]*\)\s*[;{{]",
        clean,
    ))
    definitions = set(re.findall(
        rf"(?m)^\s*(?!(?:return|if|for|while|switch)\b)"
        rf"(?:[A-Za-z_]\w*\s+|\*\s*)+(\w+)\s*\([^;{{}}]*\)\s*{{",
        clean,
    ))
    macros = set(re.findall(r"(?m)^\s*#\s*define\s+(\w+)", clean))
    return functions, definitions, macros


def _matching_paren(text: str, start: int) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _split_arguments(arguments: str) -> list[str]:
    if not arguments.strip() or arguments.strip() == "void":
        return []
    result: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(arguments):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            result.append(arguments[start:index].strip())
            start = index + 1
    result.append(arguments[start:].strip())
    return result


def _declared_types(source: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in re.finditer(
        rf"\b((?:const\s+)?(?:void|char|s8|u8|s16|u16|s32|u32|s64|u64|f32|f64)\s*\*?)\s*({_IDENTIFIER})\b",
        source,
    ):
        result[match.group(2)] = re.sub(r"\s+", " ", match.group(1)).replace(" *", " *")
    for match in re.finditer(
        rf"\b((?:const\s+)?(?:char|s8|u8|s16|u16|s32|u32|s64|u64|f32|f64))\s+({_IDENTIFIER})\s*\[",
        _mask_comments_and_strings(source),
    ):
        result[match.group(2)] = match.group(1) + " *"
    return result


def _argument_type(expression: str, declared: dict[str, str]) -> str:
    value = expression.strip()
    if value.startswith("&") or value == "NULL" or re.match(r'^"', value):
        return "void *"
    cast = re.match(r"^\(\s*((?:const\s+)?(?:void|char|s8|u8|s16|u16|s32|u32|s64|u64|f32|f64)\s*\*?)\s*\)", value)
    if cast:
        return cast.group(1)
    if value in declared:
        return declared[value]
    if re.search(r"(?:^|\W)(?:farg|temp_f|var_f)\w*", value) or re.search(r"\d+\.\d*(?:[fF])?", value):
        return "f32"
    return "s32"


def _call_sites(source: str) -> dict[str, list[tuple[list[str], int, str]]]:
    clean = _mask_comments_and_strings(source)
    result: dict[str, list[tuple[list[str], int, str]]] = {}
    for match in re.finditer(r"\b(" + _IDENTIFIER + r")\s*\(", clean):
        name = match.group(1)
        if name in _CONTROL_WORDS or name in _TYPE_WORDS:
            continue
        close = _matching_paren(clean, clean.find("(", match.start()))
        if close is None:
            continue
        line_start = clean.rfind("\n", 0, match.start()) + 1
        line_end = clean.find("\n", close)
        if line_end < 0:
            line_end = len(clean)
        statement_start = max(line_start, *(clean.rfind(delimiter, line_start, match.start()) + 1
                                           for delimiter in ";{}"))
        prefix = clean[statement_start:match.start()]
        suffix = clean[close + 1:line_end]
        # A declaration/definition has a type-like prefix and ends at ; or {.
        if (re.search(r"(?:^|\s)(?:extern\s+)?(?:const\s+)?(?:" + "|".join(_TYPE_WORDS) +
                      r")\b[^=]*$", prefix) and re.match(r"\s*[;{]", suffix)):
            continue
        arguments = _split_arguments(clean[clean.find("(", match.start()) + 1:close])
        line = clean.count("\n", 0, match.start()) + 1
        result.setdefault(name, []).append((arguments, line, prefix))
    return result


def _infer_return_type(name: str, sites: list[tuple[list[str], int, str]], declared: dict[str, str]) -> str:
    if name in _KNOWN_FUNCTIONS:
        return _KNOWN_FUNCTIONS[name][0]
    for _, _, prefix in sites:
        assignment = re.search(rf"({_IDENTIFIER})\s*=\s*$", prefix)
        if assignment and declared.get(assignment.group(1), "").endswith("*"):
            return "void *"
        if re.search(r"\breturn\s*$", prefix) or re.search(r"(?:=|!=|==|<|>)\s*$", prefix):
            return "s32"
    return "void"


def _missing_function_externs(source: str) -> tuple[list[str], list[str]]:
    declared_functions, definitions, macros = _declarations_and_definitions(source)
    declared_types = _declared_types(source)
    externs: list[str] = []
    blockers: list[str] = []
    for name, sites in sorted(_call_sites(source).items()):
        if name in declared_functions or name in definitions or name in macros or name == "M2C_FIELD":
            continue
        arities = {len(arguments) for arguments, _, _ in sites}
        if len(arities) != 1:
            blockers.append(f"inconsistent call arity for {name}: {sorted(arities)}")
            continue
        if name in _KNOWN_FUNCTIONS:
            return_type, parameter_types = _KNOWN_FUNCTIONS[name]
        else:
            argument_lists = [arguments for arguments, _, _ in sites]
            parameter_types = tuple(
                _argument_type(argument_lists[0][index], declared_types)
                for index in range(next(iter(arities)))
            )
            return_type = _infer_return_type(name, sites, declared_types)
        parameters = ", ".join(parameter_types) if parameter_types else "void"
        externs.append(f"extern {return_type} {name}({parameters});")
    return externs, blockers


def _specialize_open_prototypes(source: str, records: list[Transformation]) -> str:
    """Retain context return types while supplying arguments required by C++ MWCC."""
    clean = _mask_comments_and_strings(source)
    sites = _call_sites(source)
    declared = _declared_types(source)
    changes = []
    pattern = rf"(?m)^\s*extern\s+[^;{{}}\n]+?\b({_IDENTIFIER})\s*\(\s*\)\s*;"
    for match in re.finditer(pattern, clean):
        name = match[1]
        calls = sites.get(name, [])
        # An explicit prototype in the draft has stronger evidence than this context stub.
        declarations = re.findall(
            rf"(?m)^\s*(?!(?:return|if|while|for|switch)\b)(?:extern\s+)?(?:[A-Za-z_]\w*\s+|\*\s*)+{re.escape(name)}\s*\([^;{{}}]+\)\s*[;{{]",
            clean[:match.start()] + clean[match.end():],
        )
        if declarations:
            changes.append((match.start(), match.end(), "", "existing prototype takes precedence"))
            continue
        if not calls:
            continue
        signatures = {tuple(_argument_type(arg, declared) for arg in args) for args, _, _ in calls}
        if len(signatures) != 1:
            continue
        parameters = ", ".join(next(iter(signatures))) or "void"
        before = source[match.start():match.end()]
        after = re.sub(r"\(\s*\)", lambda _: f"({parameters})", before, count=1)
        detail = "preserved context return type and inferred call arguments"
        if name in _KNOWN_FUNCTIONS:
            return_type, parameter_types = _KNOWN_FUNCTIONS[name]
            after = f"extern {return_type} {name}({', '.join(parameter_types) or 'void'});"
            detail = "used the known library ABI in place of an unspecified context stub"
        elif re.search(r"\bextern\s+(?:s32|u32)\s+", before):
            assignments = [re.search(rf"({_IDENTIFIER})\s*=\s*$", prefix) for _, _, prefix in calls]
            pointer_types = {declared.get(value[1], "") for value in assignments if value}
            if assignments and all(assignments) and len(pointer_types) == 1 and next(iter(pointer_types)).endswith("*"):
                pointer_type = next(iter(pointer_types))
                after = re.sub(r"\bextern\s+(?:s32|u32)\s+", f"extern {pointer_type} ", after, count=1)
                detail = "refined generic integer return using consistent pointer assignments"
        changes.append((match.start(), match.end(), after, detail))
    for start, end, after, detail in reversed(changes):
        records.append(Transformation("context_prototype", source.count("\n", 0, start) + 1,
                                      source[start:end], after, detail))
        source = source[:start] + after + source[end:]
    return source


def _declared_identifiers(source: str) -> set[str]:
    names = set(re.findall(r"(?m)^\s*extern\b[^;]*\b(" + _IDENTIFIER + r")\s*(?:\[|;|\()", source))
    names.update(re.findall(
        rf"\b(?:void|char|s8|u8|s16|u16|s32|u32|s64|u64|f32|f64)\s*\**\s*({_IDENTIFIER})\b",
        source,
    ))
    return names


def _missing_data_externs(source: str) -> tuple[list[str], list[str]]:
    clean = _mask_comments_and_strings(source)
    declared = _declared_identifiers(clean)
    calls = set(_call_sites(clean))
    candidates = set(re.findall(r"\b(?:lbl|[A-Za-z][A-Za-z0-9]*)_[0-9A-Fa-f]{8}\b", clean))
    externs: list[str] = []
    blockers: list[str] = []
    for name in sorted(candidates - declared - calls):
        if name.startswith("fn_"):
            continue
        array_use = bool(re.search(rf"\b{re.escape(name)}\s*\[", clean))
        address_use = bool(re.search(rf"&\s*{re.escape(name)}\b", clean))
        pointer_use = bool(re.search(rf"\bM2C_FIELD\s*\(\s*{re.escape(name)}\s*,", clean))
        if sum((array_use, address_use, pointer_use)) > 1:
            blockers.append(f"conflicting inferred data uses for {name}")
            continue
        if array_use:
            externs.append(f"extern u8 {name}[];")
        elif pointer_use:
            externs.append(f"extern void *{name};")
        elif address_use:
            externs.append(f"extern u8 {name};")
        else:
            externs.append(f"extern u32 {name};")
    return externs, blockers


def _delimiter_blockers(source: str) -> list[str]:
    blockers: list[str] = []
    clean = _mask_comments_and_strings(source)
    if not _balanced(clean):
        blockers.append("unbalanced delimiter or quoted literal remains")
    if re.search(r"\bM2C_UNK\b", clean):
        blockers.append("unresolved M2C_UNK remains outside a recognized type context")
    return blockers


def _repair_m2c_draft(source: str) -> RepairReport:
    """Repair deterministic m2c compile failures without guessing semantics."""
    records: list[Transformation] = []
    blockers: list[str] = []
    source = _specialize_open_prototypes(source, records)
    lines = source.splitlines()

    in_block_comment = False
    for index, original in enumerate(lines):
        replaced, in_block_comment = _replace_unknown_types(original, in_block_comment)
        _record_line_change(records, "unknown_type", index + 1, original, replaced,
                            "replaced m2c placeholder with an ABI-sized scalar or opaque pointer type")
        lines[index] = replaced

    intermediate = "\n".join(lines)
    void_names = _void_pointer_names(intermediate)
    for index, original in enumerate(lines):
        replaced = _repair_void_arithmetic(original, void_names)
        _record_line_change(records, "void_pointer_arithmetic", index + 1, original, replaced,
                            "made byte-wise pointer arithmetic explicit")
        lines[index] = replaced

    masked_lines = _mask_comments_and_strings("\n".join(lines)).splitlines()
    for index, original in enumerate(lines):
        stripped = masked_lines[index].strip()
        if _looks_like_safe_missing_semicolon(stripped):
            end = len(masked_lines[index].rstrip())
            lines[index] = original[:end] + ";" + original[end:]
            _record_line_change(records, "orphan_statement", index + 1, original, lines[index],
                                "completed a balanced side-effecting expression statement")
        elif _looks_like_orphan_expression(stripped):
            blockers.append(f"line {index + 1}: orphan expression requires semantic reconstruction: {stripped[:100]}")

    repaired = "\n".join(lines)
    blockers.extend(_void_arithmetic_blockers(repaired, void_names))
    prefix: list[str] = []
    if "M2C_FIELD(" in repaired and not re.search(r"(?m)^\s*#\s*define\s+M2C_FIELD\b", repaired):
        field = "#define M2C_FIELD(base, type, offset) (*(type)((char *)(base) + (offset)))"
        prefix.append(field)
        records.append(Transformation("field_macro", 0, "", field,
                                      "defined the existing m2c byte-offset field operation"))

    function_externs, function_blockers = _missing_function_externs(repaired)
    data_externs, data_blockers = _missing_data_externs(repaired)
    blockers.extend(function_blockers)
    blockers.extend(data_blockers)
    for declaration in function_externs:
        prefix.append(declaration)
        records.append(Transformation("function_extern", 0, "", declaration,
                                      "declared a directly called unresolved function"))
    for declaration in data_externs:
        prefix.append(declaration)
        records.append(Transformation("data_extern", 0, "", declaration,
                                      "declared an unresolved address-labelled data symbol"))

    if prefix:
        repaired = "\n".join(prefix) + "\n\n" + repaired
    if source.endswith("\n"):
        repaired += "\n"
    blockers.extend(_delimiter_blockers(repaired))
    return RepairReport(repaired, tuple(records), tuple(dict.fromkeys(blockers)))


def repair_draft(code: str, symbol: str | None = None, context_header: str = "") -> dict:
    """Return a JSON-serializable deterministic repair report.

    ``context_header`` participates in declaration discovery and is emitted
    ahead of the draft.  ``symbol`` is optional, but when supplied a missing
    definition is reported instead of guessed.
    """
    source = code
    if context_header.strip():
        source = context_header.rstrip() + "\n\n" + code.lstrip("\n")
    report = _repair_m2c_draft(source)
    result = report.to_dict()
    if symbol and not re.search(r"\b" + re.escape(symbol) + r"\s*\([^;{}]*\)\s*{", report.source):
        result["unresolved"].append(f"assigned function {symbol} is not defined")
        result["ready_for_compile"] = False
    return result


def repair_m2c_draft(code: str, symbol: str | None = None, context_header: str = "") -> dict:
    """Descriptive alias for :func:`repair_draft`."""
    return repair_draft(code, symbol, context_header)


def _instructions(text: str) -> list[str]:
    labels = {
        label: f".L{index}"
        for index, label in enumerate(dict.fromkeys(re.findall(r"(?m)^\s*(\.L_[0-9A-Fa-f]+):", text)))
    }
    result: list[str] = []
    for line in text.splitlines():
        if "*/" not in line:
            continue
        instruction = line.split("*/", 1)[1].strip()
        if not instruction:
            continue
        instruction = re.sub(r"\.L_[0-9A-Fa-f]+", lambda match: labels.get(match.group(), match.group()), instruction)
        result.append(re.sub(r"\s+", " ", instruction))
    return result


def _opcode(instruction: str | None) -> str | None:
    return instruction.split(None, 1)[0] if instruction else None


def _stack_frame(instructions: list[str]) -> int | None:
    for instruction in instructions[:12]:
        match = re.match(r"stwu\s+r1,\s*(-?(?:0x[0-9A-Fa-f]+|\d+))\(r1\)", instruction)
        if match:
            value = int(match.group(1), 0)
            return abs(value)
    return 0 if instructions else None


def _branch_shape(instructions: list[str]) -> dict[str, int]:
    shape = {"calls": 0, "conditional": 0, "unconditional": 0, "returns": 0}
    call_opcodes = {"bl", "bla", "bctrl", "blrl"}
    return_opcodes = {"blr", "bclr"}
    unconditional = {"b", "ba", "bctr"}
    for instruction in instructions:
        opcode = _opcode(instruction) or ""
        if opcode in call_opcodes:
            shape["calls"] += 1
        elif opcode in return_opcodes:
            shape["returns"] += 1
        elif opcode in unconditional:
            shape["unconditional"] += 1
        elif opcode.startswith("b"):
            shape["conditional"] += 1
    return shape


def _registers(instructions: list[str]) -> set[str]:
    return set(re.findall(r"(?<![A-Za-z0-9_])(?:r(?:[12]?\d|3[01])|f(?:[12]?\d|3[01])|cr[0-7])\b",
                          "\n".join(instructions)))


def _register_key(register: str) -> tuple[int, int]:
    family = {"r": 0, "f": 1, "c": 2}[register[0]]
    number = int(register[2:] if register.startswith("cr") else register[1:])
    return family, number


def _compare_assembly(target_text: str, compiled_text: str) -> AssemblyComparison:
    target = _instructions(target_text)
    compiled = _instructions(compiled_text)
    divergence: int | None = None
    for index in range(max(len(target), len(compiled))):
        if index >= len(target) or index >= len(compiled) or target[index] != compiled[index]:
            divergence = index
            break
    target_instruction = target[divergence] if divergence is not None and divergence < len(target) else None
    compiled_instruction = compiled[divergence] if divergence is not None and divergence < len(compiled) else None
    target_frame = _stack_frame(target)
    compiled_frame = _stack_frame(compiled)
    frame_delta = (compiled_frame - target_frame
                   if target_frame is not None and compiled_frame is not None else None)
    target_registers = _registers(target)
    compiled_registers = _registers(compiled)
    return AssemblyComparison(
        target_count=len(target),
        compiled_count=len(compiled),
        first_divergent_index=divergence,
        target_instruction=target_instruction,
        compiled_instruction=compiled_instruction,
        target_opcode=_opcode(target_instruction),
        compiled_opcode=_opcode(compiled_instruction),
        opcode_mismatch=(_opcode(target_instruction) != _opcode(compiled_instruction)),
        target_stack_frame=target_frame,
        compiled_stack_frame=compiled_frame,
        stack_frame_delta=frame_delta,
        target_shape=_branch_shape(target),
        compiled_shape=_branch_shape(compiled),
        target_only_registers=tuple(sorted(target_registers - compiled_registers, key=_register_key)),
        compiled_only_registers=tuple(sorted(compiled_registers - target_registers, key=_register_key)),
    )


def compare_assembly(target_text: str, actual_text: str) -> dict:
    """Return a JSON-serializable comparison of target and dtk assembly."""
    return _compare_assembly(target_text, actual_text).to_dict()


def structured_assembly_comparison(target_text: str, actual_text: str) -> dict:
    """Descriptive alias for consumers that do not use the shorter API name."""
    return compare_assembly(target_text, actual_text)


def format_feedback(diff: dict) -> str:
    """Format a comparison dictionary as compact model feedback."""
    if diff.get("exact"):
        return f"Assembly is instruction-exact ({diff.get('target_count', 0)} instructions)."
    target = diff.get("target_instruction") or "<end>"
    compiled = diff.get("compiled_instruction") or "<end>"
    frame = (f"target={_display_frame(diff.get('target_stack_frame'))}, "
             f"compiled={_display_frame(diff.get('compiled_stack_frame'))}, "
             f"delta={_display_delta(diff.get('stack_frame_delta'))}")
    registers = (f"target-only={_compact_set(tuple(diff.get('target_only_registers', ())))}, "
                 f"compiled-only={_compact_set(tuple(diff.get('compiled_only_registers', ())))}")
    target_shape = diff.get("target_shape") or {}
    compiled_shape = diff.get("compiled_shape") or {}
    return (f"First divergence at instruction {diff.get('first_divergent_index')}: "
            f"target `{target}`; compiled `{compiled}`. "
            f"Opcode: {diff.get('target_opcode') or '<end>'} -> "
            f"{diff.get('compiled_opcode') or '<end>'}. Stack frame: {frame}. "
            f"Branch shape target={_shape_text(target_shape)}; "
            f"compiled={_shape_text(compiled_shape)}. Registers: {registers}.")


def compact_prompt_text(target_text: str, compiled_text: str) -> str:
    return format_feedback(compare_assembly(target_text, compiled_text))


def _display_frame(value: int | None) -> str:
    return "unknown" if value is None else f"0x{value:X}"


def _display_delta(value: int | None) -> str:
    if value is None:
        return "unknown"
    return f"{value:+#x}"


def _compact_set(values: tuple[str, ...]) -> str:
    return ",".join(values) if values else "none"


def _shape_text(shape: dict[str, int]) -> str:
    return "/".join(f"{key}:{shape[key]}" for key in ("calls", "conditional", "unconditional", "returns"))


__all__ = [
    "AssemblyComparison",
    "RepairReport",
    "Transformation",
    "compact_prompt_text",
    "compare_assembly",
    "format_feedback",
    "repair_draft",
    "repair_m2c_draft",
    "structured_assembly_comparison",
]
