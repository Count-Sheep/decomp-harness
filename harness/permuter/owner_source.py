"""Fail-closed source policy for mutable full-owner functions.

The lexical pass removes constructs which must never reach a compiler.  A
second, real Clang AST pass then enforces the target policy and performs a
small definite-assignment analysis.  Full-owner targets are intentionally a
narrow lane: an unsupported AST shape is rejected instead of guessed about.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable


class OwnerSourceError(ValueError):
    pass


# Per-function intrinsic allowlists.  Every list is empty unless a reviewer
# supplies one (``permuter.intrinsic_allowlists`` in the harness config, or
# ``--intrinsic`` on the command line).  In particular, a model cannot
# introduce an MWCC/codegen intrinsic merely by spelling a plausible ``__name``.
TARGET_INTRINSIC_ALLOWLISTS: dict[str, frozenset[str]] = {}
OWNER_PARSER_MODE = "clang-json-ast-dataflow-v3"


def _intrinsic_allowlist(
    function: str | None, allowlist: Iterable[str] | None
) -> frozenset[str]:
    if allowlist is not None:
        return frozenset(allowlist)
    return TARGET_INTRINSIC_ALLOWLISTS.get(function or "", frozenset())


def _clean_environment() -> dict[str, str]:
    """A minimal, locale-neutral environment that still lets Windows processes start."""
    env = {"LANG": "C", "LC_ALL": "C"}
    if sys.platform == "win32":
        for name in ("SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "WINDIR"):
            if os.environ.get(name):
                env[name] = os.environ[name]
    return env


def _mask_comments_and_literals(source: str) -> str:
    """Replace comments/string contents with spaces while preserving lines."""
    chars = list(source)
    index = 0
    while index < len(chars):
        if chars[index] in ('"', "'"):
            quote = chars[index]
            index += 1
            while index < len(chars):
                if chars[index] == "\\":
                    chars[index] = " "
                    if index + 1 < len(chars) and chars[index + 1] != "\n":
                        chars[index + 1] = " "
                    index += 2
                    continue
                if chars[index] == quote:
                    index += 1
                    break
                if chars[index] != "\n":
                    chars[index] = " "
                index += 1
            continue
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            end = len(chars) if end < 0 else end
            for cursor in range(index, end):
                chars[cursor] = " "
            index = end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                raise OwnerSourceError("unterminated block comment")
            for cursor in range(index, end + 2):
                if chars[cursor] != "\n":
                    chars[cursor] = " "
            index = end + 2
            continue
        index += 1
    return "".join(chars)


OWNER_FORBIDDEN = (
    (re.compile(r"(?m)^\s*(?:#|%:|\?\?=)"), "preprocessor directive or pragma"),
    (re.compile(r"(?:<%|%>|<:|:>|%:%:)"), "C digraph token"),
    (re.compile(r"\b_Pragma\s*\("), "_Pragma"),
    (re.compile(r"\b(?:__asm__|__asm|asm)\b"), "inline assembly"),
    (re.compile(r"\.inc\b", re.IGNORECASE), ".inc reference"),
    (re.compile(r"\bgoto\b"), "goto"),
    (re.compile(r"\b(?:volatile|register|restrict)\b"), "layout-forcing qualifier"),
    (re.compile(r"\b(?:__attribute__|__declspec)\b"), "compiler layout attribute"),
    (
        re.compile(r"\b([A-Za-z_]\w*)\s*=\s*\1\s*;"),
        "self-assignment shaper",
    ),
    (
        re.compile(r"(?m)(?:^|[;{}])\s*(?!(?:case|default)\b)[A-Za-z_]\w*\s*:"),
        "explicit control-flow label",
    ),
)


def _resolve_parser(parser: str | Path | None) -> Path:
    if parser is None:
        raise OwnerSourceError("full-owner AST parser must be explicitly attested")
    resolved = Path(parser).expanduser().resolve()
    if not resolved.is_file() or not resolved.stat().st_mode & 0o111:
        raise OwnerSourceError(f"full-owner AST parser is not executable: {resolved}")
    return resolved


def owner_policy_record(
    function: str,
    parser: str | Path | None = None,
    *,
    intrinsic_allowlist: Iterable[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema": 1,
        "function": function,
        "parser": str(_resolve_parser(parser)),
        "parser_mode": OWNER_PARSER_MODE,
        "intrinsic_allowlist": sorted(_intrinsic_allowlist(function, intrinsic_allowlist)),
    }


def _children(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [child for child in node.get("inner", ()) if isinstance(child, dict)]


def _walk(node: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield node
    for child in _children(node):
        yield from _walk(child)


def _type_name(node: dict[str, Any]) -> str:
    value = node.get("type")
    if not isinstance(value, dict):
        return ""
    return str(value.get("desugaredQualType") or value.get("qualType") or "")


def _is_pointer_type(value: str) -> bool:
    return "*" in value or "[" in value


class _Dataflow:
    """Conservative definite-assignment checker over one Clang C AST."""

    def __init__(self, function: dict[str, Any]):
        self.function = function
        self.locals = {
            str(node.get("id")): str(node.get("name") or "<anonymous>")
            for node in _walk(function)
            if node.get("kind") == "VarDecl"
        }
        self.parameters = {
            str(node.get("id"))
            for node in _children(function)
            if node.get("kind") == "ParmVarDecl"
        }
        self.local_reads: set[str] = set()
        # Track an affine value as (source lvalue, coefficient, constant).
        # This catches transformed save/write/restore shapers, not merely an
        # unchanged temporary copied back to its source.
        self.provenance: dict[
            str, tuple[tuple[Any, ...] | None, int, int]
        ] = {}
        # Dependencies survive branch merges and non-affine transformations.
        # If exact provenance is lost, a later write back to a possible source
        # rejects instead of laundering the alias through control flow.
        self.dependencies: dict[str, set[tuple[Any, ...]]] = {}
        self.control_dependencies: set[tuple[Any, ...]] = set()
        # Known current values of memory lvalues, expressed relative to their
        # entry values.  This exposes duplicate writes and inverse update
        # pairs such as ``field = field + 1; field = field - 1``.
        self.memory_values: dict[
            tuple[Any, ...], tuple[tuple[Any, ...] | None, int, int]
        ] = {}
        self.local_value_shapes: dict[str, tuple[Any, ...]] = {}
        self.memory_value_shapes: dict[tuple[Any, ...], tuple[Any, ...]] = {}
        self.assignment_keys: dict[int, tuple[Any, ...]] = {}
        self.node_memory_reads: dict[int, set[tuple[Any, ...]]] = {}
        self.condition_writes: dict[tuple[Any, ...], set[tuple[Any, ...]]] = {}
        self.pending_local_writes: set[str] = set()

    def reject(self, detail: str) -> None:
        raise OwnerSourceError(f"full-owner target rejected: {detail}")

    def _decl_id(self, node: dict[str, Any]) -> str | None:
        referenced = node.get("referencedDecl")
        if not isinstance(referenced, dict):
            return None
        value = referenced.get("id")
        return str(value) if value is not None else None

    def _read(self, node: dict[str, Any], defined: set[str]) -> None:
        decl_id = self._decl_id(node)
        if decl_id in self.locals and decl_id not in defined:
            self.reject(f"local {self.locals[decl_id]!r} is read before definition")
        if decl_id in self.locals:
            dependencies = self.dependencies.get(decl_id)
            if dependencies is not None and not dependencies:
                self.reject(
                    f"constant-only local {self.locals[decl_id]!r} is forbidden"
                )
            self.local_reads.add(decl_id)
            self.pending_local_writes.discard(decl_id)

    def expression(self, node: dict[str, Any], defined: set[str]) -> None:
        kind = str(node.get("kind") or "")
        children = _children(node)
        self.node_memory_reads[id(node)] = self._external_keys(
            self._source_keys(node, resolve_locals=True)
        )
        if kind == "DeclRefExpr":
            self._read(node, defined)
            return
        if kind == "BinaryOperator":
            opcode = node.get("opcode")
            if opcode == ",":
                self.reject("comma expressions are forbidden")
            if opcode == "=":
                self.reject("assignment inside an expression is forbidden")
            if opcode in {"&", "|", "^", "<<", ">>"}:
                self.reject("bit-mask and shift shaping expressions are forbidden")
            if opcode not in {"+", "-", "==", "!=", "<", "<=", ">", ">="}:
                self.reject(f"binary operator {opcode!r} is forbidden")
            raw_value = self._affine_source(node, resolve_locals=False)
            raw_sources = self._source_keys(node, resolve_locals=False)
            if opcode in {"+", "-"} and raw_value is None:
                self.reject("non-affine arithmetic is forbidden")
            if opcode in {"==", "!=", "<", "<=", ">", ">="} and any(
                self._affine_source(child, resolve_locals=False) is None
                for child in children
            ):
                self.reject("non-affine comparison is forbidden")
            if opcode in {"==", "!=", "<", "<=", ">", ">="}:
                comparison_values = [
                    self._affine_source(child, resolve_locals=True)
                    for child in children
                ]
                source_operands = sum(
                    value is not None and value[0] is not None
                    for value in comparison_values
                )
                if source_operands != 1:
                    self.reject(
                        "comparison must have one source and one constant operand"
                    )
            if (
                opcode in {"+", "-", "*", "/", "%"}
                and raw_value is not None
                and raw_value[1] == 0
                and raw_sources
            ):
                self.reject("algebraically cancelled reads are forbidden")
            if len(children) == 2 and opcode in {"-", "/", "%", "==", "!="}:
                left = self._affine_source(children[0], resolve_locals=False)
                right = self._affine_source(children[1], resolve_locals=False)
                if left is not None and left == right and raw_sources:
                    self.reject("same-value comparison/arithmetic is forbidden")
            if len(children) == 2 and opcode in {
                "-",
                "==",
                "!=",
                "<",
                "<=",
                ">",
                ">=",
            }:
                resolved_left = self._affine_source(
                    children[0], resolve_locals=True
                )
                resolved_right = self._affine_source(
                    children[1], resolve_locals=True
                )
                if (
                    resolved_left is not None
                    and resolved_left == resolved_right
                    and self._source_keys(node, resolve_locals=True)
                ):
                    self.reject("same resolved value arithmetic/comparison is forbidden")
        if kind == "CompoundAssignOperator":
            self.reject("compound assignment inside an expression is forbidden")
        if kind == "CallExpr":
            self.reject("function calls are forbidden in strict owner targets")
        if kind == "ConditionalOperator" and len(children) == 3:
            condition = self._affine_source(children[0], resolve_locals=True)
            then_value = self._affine_source(children[1], resolve_locals=True)
            else_value = self._affine_source(children[2], resolve_locals=True)
            if condition is not None and condition[0] is None:
                self.reject("constant conditional-expression condition is forbidden")
            if self._structural_key(children[1]) == self._structural_key(
                children[2]
            ) or (then_value is not None and then_value == else_value):
                self.reject("identical conditional-expression arms are forbidden")
        if kind == "UnaryOperator" and node.get("opcode") in ("++", "--"):
            self.reject("increment inside an expression is forbidden")
        if kind == "UnaryOperator" and node.get("opcode") in {"!", "~"}:
            self.reject("logical/bitwise unary shaping is forbidden")
        if kind == "UnaryOperator" and node.get("opcode") == "&":
            self.reject("address-taking and alias shaping are forbidden")
        if kind == "CStyleCastExpr" and children:
            destination = _type_name(node)
            source = _type_name(children[0])
            if _is_pointer_type(destination) or _is_pointer_type(source):
                self.reject("explicit pointer casts and pointer/integer aliasing are forbidden")
        if kind == "ImplicitCastExpr" and node.get("castKind") in {
            "ArrayToPointerDecay",
            "BitCast",
            "FunctionToPointerDecay",
            "IntegralToPointer",
            "PointerToIntegral",
        }:
            self.reject("implicit pointer decay/aliasing is forbidden")
        if (
            kind in {"ArraySubscriptExpr", "MemberExpr"}
            or (kind == "UnaryOperator" and node.get("opcode") == "*")
        ) and self._lvalue_key(node) is None:
            self.reject("memory lvalue cannot be canonicalized safely")
        if kind in {
            "AddrLabelExpr",
            "AtomicExpr",
            "ChooseExpr",
            "CompoundLiteralExpr",
            "GenericSelectionExpr",
            "OffsetOfExpr",
            "StatementExpr",
            "UnaryExprOrTypeTraitExpr",
            "VAArgExpr",
        }:
            self.reject(f"ambiguous expression {kind} is forbidden")
        supported = {
            "ArraySubscriptExpr",
            "BinaryOperator",
            "CallExpr",
            "CharacterLiteral",
            "ConditionalOperator",
            "ConstantExpr",
            "CStyleCastExpr",
            "DeclRefExpr",
            "FloatingLiteral",
            "ImplicitCastExpr",
            "IntegerLiteral",
            "MemberExpr",
            "ParenExpr",
            "StringLiteral",
            "UnaryOperator",
        }
        if kind not in supported:
            self.reject(f"unsupported expression AST node {kind}")
        for child in children:
            self.expression(child, defined)

    def _assignment(self, node: dict[str, Any], defined: set[str]) -> set[str]:
        children = _children(node)
        if len(children) != 2:
            self.reject("ambiguous assignment AST")
        lhs, rhs = children
        key = self._lvalue_key(lhs)
        if key is None:
            self.reject("assignment target cannot be canonicalized safely")
        if key[0] == "decl" and str(key[1]) in self.parameters:
            self.reject("assignment to a by-value parameter is forbidden")
        if key[0] == "decl" and str(key[1]) not in self.locals:
            self.reject("assignment to a global/static object is forbidden")
        raw_rhs = self._affine_source(rhs, resolve_locals=False)
        resolved_rhs = self._affine_source(rhs, resolve_locals=True)
        raw_sources = self._source_keys(rhs, resolve_locals=False)
        resolved_sources = self._source_keys(rhs, resolve_locals=True)
        rhs_shape = self._structural_key(rhs)
        if key in self.memory_values and self.memory_values[key] == resolved_rhs:
            self.reject("duplicate memory write is forbidden")
        if self.memory_value_shapes.get(key) == rhs_shape:
            self.reject("duplicate memory write is forbidden")
        for form, read_keys in (
            (raw_rhs, raw_sources),
            (resolved_rhs, resolved_sources),
        ):
            if key in read_keys and not self._reviewed_update(form, key, lhs):
                self.reject(
                    "self/alias assignment cannot prove a non-identity update"
                )
        self.expression(rhs, defined)
        result = set(defined)
        if lhs.get("kind") == "DeclRefExpr":
            decl_id = self._decl_id(lhs)
            if decl_id in self.locals:
                if (
                    (
                        self.provenance.get(decl_id) == resolved_rhs
                        or self.local_value_shapes.get(decl_id) == rhs_shape
                    )
                    and self.dependencies.get(decl_id)
                    == (resolved_sources | self.control_dependencies)
                ):
                    self.reject("redundant local reload is forbidden")
                if decl_id in self.pending_local_writes:
                    self.reject("local value is overwritten before it is read")
                result.add(decl_id)
                if resolved_rhs is None:
                    self.provenance.pop(decl_id, None)
                else:
                    self.provenance[decl_id] = resolved_rhs
                self.dependencies[decl_id] = (
                    set(resolved_sources) | self.control_dependencies
                )
                self.local_value_shapes[decl_id] = rhs_shape
                self.pending_local_writes.add(decl_id)
                return result
        # Member/dereference writes still read the base address.
        self.expression(lhs, defined)
        self.assignment_keys[id(node)] = key
        self.node_memory_reads[id(node)] = self._external_keys(
            resolved_sources
        )
        if resolved_rhs is None:
            self.memory_values.pop(key, None)
        else:
            self.memory_values[key] = resolved_rhs
        self.memory_value_shapes[key] = rhs_shape
        return result

    def _affine_source(
        self,
        node: dict[str, Any],
        *,
        resolve_locals: bool,
    ) -> tuple[tuple[Any, ...] | None, int, int] | None:
        """Return a conservative affine description of one expression.

        Casts are intentionally transparent here.  If a candidate assigns a
        transformed lvalue back to itself, the strict lane refuses to guess
        about truncation or code-generation intent.
        """
        while node.get("kind") in {"ConstantExpr", "ParenExpr"}:
            children = _children(node)
            if len(children) != 1:
                return None
            node = children[0]

        if node.get("kind") in {"CStyleCastExpr", "ImplicitCastExpr"}:
            children = _children(node)
            if len(children) != 1:
                return None
            value = self._affine_source(
                children[0], resolve_locals=resolve_locals
            )
            if value is None or value[0] is not None:
                return value
            normalized = self._cast_integer_constant(value[2], _type_name(node))
            return (None, 0, normalized) if normalized is not None else value

        key = self._lvalue_key(node)
        if key is not None:
            current_memory = self.memory_values.get(key)
            if current_memory is not None:
                return current_memory
            if resolve_locals and key[0] == "decl":
                return self.provenance.get(str(key[1]), (key, 1, 0))
            return (key, 1, 0)

        children = _children(node)
        kind = node.get("kind")
        opcode = node.get("opcode")
        if kind in {"CharacterLiteral", "IntegerLiteral"}:
            try:
                return (None, 0, int(str(node.get("value")), 0))
            except (TypeError, ValueError):
                return None
        if kind == "UnaryOperator" and opcode in {"+", "-"} and len(children) == 1:
            value = self._affine_source(children[0], resolve_locals=resolve_locals)
            if value is None or opcode == "+":
                return value
            return (value[0], -value[1], -value[2])
        if kind == "BinaryOperator" and len(children) == 2:
            left, right = children
            left_value = self._affine_source(left, resolve_locals=resolve_locals)
            right_value = self._affine_source(right, resolve_locals=resolve_locals)
            if left_value is None or right_value is None:
                return None
            if opcode in {"+", "-"}:
                sign = 1 if opcode == "+" else -1
                left_base, left_coefficient, left_constant = left_value
                right_base, right_coefficient, right_constant = right_value
                if left_base is not None and right_base is not None:
                    if left_base != right_base:
                        return None
                    coefficient = left_coefficient + sign * right_coefficient
                    constant = left_constant + sign * right_constant
                    return (
                        (left_base, coefficient, constant)
                        if coefficient != 0
                        else (None, 0, constant)
                    )
                if left_base is not None:
                    return (
                        left_base,
                        left_coefficient,
                        left_constant + sign * right_constant,
                    )
                if right_base is not None:
                    return (
                        right_base,
                        sign * right_coefficient,
                        left_constant + sign * right_constant,
                    )
                return (None, 0, left_constant + sign * right_constant)
            if opcode == "*":
                if left_value[0] is None:
                    factor = left_value[2]
                    return (right_value[0], right_value[1] * factor, right_value[2] * factor)
                if right_value[0] is None:
                    factor = right_value[2]
                    return (left_value[0], left_value[1] * factor, left_value[2] * factor)
            if opcode == "/" and right_value[0] is None and right_value[2] in {1, -1}:
                divisor = right_value[2]
                return (
                    left_value[0],
                    left_value[1] // divisor,
                    left_value[2] // divisor,
                )
        if kind == "ConditionalOperator" and len(children) == 3:
            then_value = self._affine_source(
                children[1], resolve_locals=resolve_locals
            )
            else_value = self._affine_source(
                children[2], resolve_locals=resolve_locals
            )
            if then_value is not None and then_value == else_value:
                return then_value
        return None

    def _cast_integer_constant(self, value: int, type_name: str) -> int | None:
        lowered = type_name.lower()
        if "*" in lowered or any(word in lowered for word in ("float", "double")):
            return None
        if "bool" in lowered:
            return int(value != 0)
        if "char" in lowered:
            width = 8
        elif "short" in lowered:
            width = 16
        elif "long long" in lowered:
            width = 64
        elif any(word in lowered for word in ("int", "long")):
            width = 32
        else:
            return None
        modulus = 1 << width
        narrowed = value % modulus
        if "unsigned" not in lowered and narrowed >= (1 << (width - 1)):
            narrowed -= modulus
        return narrowed

    def _source_keys(
        self,
        node: dict[str, Any],
        *,
        resolve_locals: bool,
    ) -> set[tuple[Any, ...]]:
        while node.get("kind") in {
            "ConstantExpr",
            "CStyleCastExpr",
            "ImplicitCastExpr",
            "ParenExpr",
        }:
            children = _children(node)
            if len(children) != 1:
                return set()
            node = children[0]
        key = self._lvalue_key(node)
        if key is not None:
            if resolve_locals and key[0] == "decl":
                dependencies = self.dependencies.get(str(key[1]))
                if dependencies is not None:
                    return set(dependencies)
            value = self._affine_source(node, resolve_locals=resolve_locals)
            if value is not None and value[0] is not None:
                return {value[0]}
            return {key}
        result: set[tuple[Any, ...]] = set()
        for child in _children(node):
            result.update(self._source_keys(child, resolve_locals=resolve_locals))
        return result

    def _external_keys(
        self, keys: set[tuple[Any, ...]]
    ) -> set[tuple[Any, ...]]:
        return {
            key
            for key in keys
            if not (
                key[0] == "decl"
                and str(key[1]) in self.locals.keys() | self.parameters
            )
        }

    def _reviewed_update(
        self,
        value: tuple[tuple[Any, ...] | None, int, int] | None,
        key: tuple[Any, ...],
        lhs: dict[str, Any],
    ) -> bool:
        if value is None or value[0] != key or value[1] != 1:
            return False
        delta = value[2]
        type_name = _type_name(lhs).lower()
        if "*" in type_name:
            return delta % (1 << 32) != 0
        width = None
        if "char" in type_name:
            width = 8
        elif "short" in type_name:
            width = 16
        elif any(word in type_name for word in ("int", "long")):
            width = 32
        if width is not None:
            return delta % (1 << width) != 0
        return delta != 0

    def _dereference_key(self, node: dict[str, Any], offset: int = 0) -> tuple[Any, ...] | None:
        pointer = self._affine_source(node, resolve_locals=True)
        if pointer is None or pointer[0] is None or pointer[1] != 1:
            return None
        return ("element", pointer[0], pointer[2] + offset)

    def _structural_key(self, node: dict[str, Any]) -> tuple[Any, ...]:
        return (
            node.get("kind"),
            node.get("opcode"),
            node.get("value"),
            node.get("name"),
            node.get("castKind"),
            node.get("isArrow"),
            node.get("referencedMemberDecl"),
            self._decl_id(node),
            _type_name(node),
            tuple(self._structural_key(child) for child in _children(node)),
        )

    def _original_type(self, node: dict[str, Any]) -> str:
        while node.get("kind") in {
            "ConstantExpr",
            "CStyleCastExpr",
            "ImplicitCastExpr",
            "ParenExpr",
        }:
            children = _children(node)
            if len(children) != 1:
                break
            node = children[0]
        return _type_name(node).lower()

    def _condition_key(self, node: dict[str, Any]) -> tuple[Any, ...]:
        while node.get("kind") in {"ConstantExpr", "ImplicitCastExpr", "ParenExpr"}:
            children = _children(node)
            if len(children) != 1:
                break
            node = children[0]
        children = _children(node)
        if node.get("kind") == "BinaryOperator" and len(children) == 2:
            opcode = str(node.get("opcode"))
            left = self._affine_source(children[0], resolve_locals=True)
            right = self._affine_source(children[1], resolve_locals=True)
            if left is not None and right is not None:
                left_constant = left[0] is None
                right_constant = right[0] is None
                if left_constant != right_constant:
                    value = right if left_constant else left
                    constant = left[2] if left_constant else right[2]
                    value_node = children[1] if left_constant else children[0]
                    value_unsigned = "unsigned" in self._original_type(value_node)
                    normalized_opcode = opcode
                    if left_constant:
                        normalized_opcode = {
                            "<": ">",
                            "<=": ">=",
                            ">": "<",
                            ">=": "<=",
                        }.get(opcode, opcode)
                    if constant == 0 and normalized_opcode == "!=":
                        return ("truthy", value)
                    if constant == 0 and normalized_opcode == "==":
                        return ("zero", value)
                    if value_unsigned and (
                        (constant == 0 and normalized_opcode == ">")
                        or (constant == 1 and normalized_opcode == ">=")
                    ):
                        return ("truthy", value)
                    if value_unsigned and (
                        (constant == 0 and normalized_opcode == "<=")
                        or (constant == 1 and normalized_opcode == "<")
                    ):
                        return ("zero", value)
                if opcode in {"==", "!="} and repr(left) > repr(right):
                    left, right = right, left
                return ("compare", opcode, left, right)
        value = self._affine_source(node, resolve_locals=True)
        if value is not None:
            return ("truthy", value)
        return ("shape", self._structural_key(node))

    def _definitely_returns(self, node: dict[str, Any]) -> bool:
        kind = node.get("kind")
        children = _children(node)
        if kind == "ReturnStmt":
            return True
        if kind == "CompoundStmt":
            return any(self._definitely_returns(child) for child in children)
        if kind == "IfStmt" and len(children) == 3:
            return self._definitely_returns(children[1]) and self._definitely_returns(
                children[2]
            )
        return False

    def _memory_liveness(
        self,
        node: dict[str, Any],
        live_out: set[tuple[Any, ...]],
        exit_live: set[tuple[Any, ...]],
    ) -> set[tuple[Any, ...]]:
        kind = node.get("kind")
        children = _children(node)
        if kind == "CompoundStmt":
            live = set(live_out)
            for child in reversed(children):
                live = self._memory_liveness(child, live, exit_live)
            return live
        if kind == "IfStmt":
            then_live = self._memory_liveness(children[1], live_out, exit_live)
            else_live = (
                self._memory_liveness(children[2], live_out, exit_live)
                if len(children) == 3
                else set(live_out)
            )
            return (
                then_live
                | else_live
                | self.node_memory_reads.get(id(children[0]), set())
            )
        if kind == "ReturnStmt":
            reads = (
                self.node_memory_reads.get(id(children[0]), set())
                if children
                else set()
            )
            return set(exit_live) | reads
        if kind == "BinaryOperator" and node.get("opcode") == "=":
            reads = self.node_memory_reads.get(id(node), set())
            key = self.assignment_keys.get(id(node))
            if key is None:
                return set(live_out) | reads
            if key not in live_out:
                self.reject("memory write is overwritten before it is observable")
            return (set(live_out) - {key}) | reads
        if kind == "DeclStmt":
            reads: set[tuple[Any, ...]] = set()
            for declaration in children:
                for initializer in _children(declaration):
                    reads.update(self.node_memory_reads.get(id(initializer), set()))
            return set(live_out) | reads
        return set(live_out) | self.node_memory_reads.get(id(node), set())

    def _lvalue_key(self, node: dict[str, Any]) -> tuple[Any, ...] | None:
        while node.get("kind") in {"ImplicitCastExpr", "ParenExpr"}:
            children = _children(node)
            if len(children) != 1:
                return None
            node = children[0]
        kind = node.get("kind")
        if kind == "DeclRefExpr":
            return ("decl", self._decl_id(node))
        if kind == "MemberExpr":
            children = _children(node)
            if len(children) != 1:
                return None
            base = (
                self._dereference_key(children[0])
                if node.get("isArrow")
                else self._lvalue_key(children[0])
            )
            if base is None:
                return None
            return (
                "member",
                node.get("referencedMemberDecl"),
                base,
            )
        if kind == "ArraySubscriptExpr":
            children = _children(node)
            if len(children) != 2:
                return None
            pointer_nodes = [child for child in children if _is_pointer_type(_type_name(child))]
            if len(pointer_nodes) != 1:
                return None
            pointer_node = pointer_nodes[0]
            index_node = children[1] if children[0] is pointer_node else children[0]
            index = self._affine_source(index_node, resolve_locals=True)
            if index is None or index[0] is not None or index[1] != 0:
                return None
            return self._dereference_key(pointer_node, index[2])
        if kind == "UnaryOperator" and node.get("opcode") == "*":
            children = _children(node)
            if len(children) != 1:
                return None
            return self._dereference_key(children[0])
        return None

    def statement(self, node: dict[str, Any], defined: set[str]) -> set[str]:
        kind = str(node.get("kind") or "")
        children = _children(node)
        if kind == "CompoundStmt":
            state = set(defined)
            terminated = False
            for child in children:
                if terminated:
                    self.reject("unreachable statement is forbidden")
                state = self.statement(child, state)
                terminated = self._definitely_returns(child)
            return state
        if kind == "DeclStmt":
            state = set(defined)
            for declaration in children:
                if declaration.get("kind") != "VarDecl":
                    self.reject("local typedef or aggregate declaration is forbidden")
                if declaration.get("storageClass"):
                    self.reject("local storage-class declarations are forbidden")
                if "[" in _type_name(declaration):
                    self.reject("local arrays are forbidden in strict owner targets")
                decl_id = str(declaration.get("id"))
                initializer = _children(declaration)
                if len(initializer) > 1:
                    self.reject("ambiguous local initializer AST")
                if initializer:
                    self.expression(initializer[0], state)
                    state.add(decl_id)
                    source_value = self._affine_source(
                        initializer[0], resolve_locals=True
                    )
                    if source_value is not None:
                        self.provenance[decl_id] = source_value
                    self.dependencies[decl_id] = self._source_keys(
                        initializer[0], resolve_locals=True
                    ) | self.control_dependencies
                    self.local_value_shapes[decl_id] = self._structural_key(
                        initializer[0]
                    )
                    self.pending_local_writes.add(decl_id)
            return state
        if kind == "IfStmt":
            if len(children) not in (2, 3):
                self.reject("ambiguous if-statement AST")
            condition = self._affine_source(children[0], resolve_locals=True)
            if condition is not None and condition[0] is None:
                self.reject("constant if-statement condition is forbidden")
            if len(children) == 3 and self._structural_key(
                children[1]
            ) == self._structural_key(children[2]):
                self.reject("identical if/else branches are forbidden")
            self.expression(children[0], defined)
            condition_dependencies = self._source_keys(
                children[0], resolve_locals=True
            )
            incoming_provenance = dict(self.provenance)
            incoming_dependencies = {
                decl_id: set(values)
                for decl_id, values in self.dependencies.items()
            }
            incoming_memory_values = dict(self.memory_values)
            incoming_local_value_shapes = dict(self.local_value_shapes)
            incoming_memory_value_shapes = dict(self.memory_value_shapes)
            incoming_control_dependencies = set(self.control_dependencies)
            incoming_pending_local_writes = set(self.pending_local_writes)
            self.provenance = dict(incoming_provenance)
            self.dependencies = {
                decl_id: set(values)
                for decl_id, values in incoming_dependencies.items()
            }
            self.memory_values = dict(incoming_memory_values)
            self.local_value_shapes = dict(incoming_local_value_shapes)
            self.memory_value_shapes = dict(incoming_memory_value_shapes)
            self.control_dependencies = (
                incoming_control_dependencies | condition_dependencies
            )
            self.pending_local_writes = set(incoming_pending_local_writes)
            then_state = self.statement(children[1], set(defined))
            then_provenance = dict(self.provenance)
            then_dependencies = {
                decl_id: set(values)
                for decl_id, values in self.dependencies.items()
            }
            then_memory_values = dict(self.memory_values)
            then_local_value_shapes = dict(self.local_value_shapes)
            then_memory_value_shapes = dict(self.memory_value_shapes)
            then_pending_local_writes = set(self.pending_local_writes)
            self.provenance = dict(incoming_provenance)
            self.dependencies = {
                decl_id: set(values)
                for decl_id, values in incoming_dependencies.items()
            }
            self.memory_values = dict(incoming_memory_values)
            self.local_value_shapes = dict(incoming_local_value_shapes)
            self.memory_value_shapes = dict(incoming_memory_value_shapes)
            self.control_dependencies = (
                incoming_control_dependencies | condition_dependencies
            )
            self.pending_local_writes = set(incoming_pending_local_writes)
            else_state = (
                self.statement(children[2], set(defined))
                if len(children) == 3
                else set(defined)
            )
            else_provenance = dict(self.provenance)
            else_dependencies = {
                decl_id: set(values)
                for decl_id, values in self.dependencies.items()
            }
            else_memory_values = dict(self.memory_values)
            else_local_value_shapes = dict(self.local_value_shapes)
            else_memory_value_shapes = dict(self.memory_value_shapes)
            else_pending_local_writes = set(self.pending_local_writes)
            self.provenance = {
                decl_id: value
                for decl_id, value in then_provenance.items()
                if else_provenance.get(decl_id) == value
            }
            self.dependencies = {
                decl_id: then_dependencies.get(decl_id, set())
                | else_dependencies.get(decl_id, set())
                for decl_id in then_dependencies.keys() | else_dependencies.keys()
            }
            self.memory_values = {
                key: value
                for key, value in then_memory_values.items()
                if else_memory_values.get(key) == value
            }
            self.local_value_shapes = {
                decl_id: value
                for decl_id, value in then_local_value_shapes.items()
                if else_local_value_shapes.get(decl_id) == value
            }
            self.memory_value_shapes = {
                key: value
                for key, value in then_memory_value_shapes.items()
                if else_memory_value_shapes.get(key) == value
            }
            self.control_dependencies = incoming_control_dependencies
            self.pending_local_writes = (
                then_pending_local_writes | else_pending_local_writes
            )
            condition_key = self._condition_key(children[0])
            branch_writes = {
                self.assignment_keys[id(candidate)]
                for branch in children[1:]
                for candidate in _walk(branch)
                if id(candidate) in self.assignment_keys
            }
            previous_writes = self.condition_writes.get(condition_key, set())
            overlap = previous_writes & branch_writes
            if overlap:
                self.reject("repeated condition mutates the same memory lvalue")
            self.condition_writes[condition_key] = previous_writes | branch_writes
            return then_state & else_state
        if kind == "ReturnStmt":
            if len(children) > 1:
                self.reject("ambiguous return AST")
            if children:
                self.expression(children[0], defined)
            if self.pending_local_writes:
                names = sorted(
                    self.locals[decl_id] for decl_id in self.pending_local_writes
                )
                self.reject("unread local value at return: " + ", ".join(names))
            return set(defined)
        if kind == "BinaryOperator" and node.get("opcode") == "=":
            return self._assignment(node, defined)
        if kind == "CompoundAssignOperator":
            self.reject("compound assignments are forbidden in strict owner targets")
        if kind == "UnaryOperator" and node.get("opcode") in ("++", "--"):
            self.reject("increment and decrement are forbidden in strict owner targets")
        if kind in {"BreakStmt", "ContinueStmt", "GotoStmt", "IndirectGotoStmt"}:
            self.reject(f"explicit control flow {kind} is forbidden")
        if kind in {"DoStmt", "ForStmt", "SwitchStmt", "WhileStmt"}:
            self.reject(f"unsupported control-flow AST node {kind}")
        if kind in {"CaseStmt", "DefaultStmt", "LabelStmt", "NullStmt"}:
            self.reject(f"control-flow shaping AST node {kind} is forbidden")
        self.reject(f"standalone expression statement {kind} is forbidden")

    def validate(self) -> None:
        bodies = [node for node in _children(self.function) if node.get("kind") == "CompoundStmt"]
        if len(bodies) != 1:
            self.reject("function has no unambiguous body AST")
        self.statement(bodies[0], set(self.parameters))
        exit_live = set(self.assignment_keys.values())
        self._memory_liveness(bodies[0], exit_live, exit_live)
        if self.pending_local_writes:
            names = sorted(
                self.locals[decl_id] for decl_id in self.pending_local_writes
            )
            self.reject("unread local value at function exit: " + ", ".join(names))
        unused = sorted(
            self.locals[decl_id]
            for decl_id in self.locals.keys() - self.local_reads
        )
        if unused:
            self.reject("unused local declaration is forbidden: " + ", ".join(unused))


def _scan_functions(source: str) -> list[tuple[str, int, int, int, int | None]]:
    """Locate top-level function definitions in preprocessed owner source."""
    index, length = 0, len(source)
    depth = 0
    last_semicolon = 0
    functions: list[tuple[str, int, int, int, int | None]] = []
    while index < length:
        char = source[index]
        if char in ('"', "'"):
            quote = char
            index += 1
            while index < length:
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == quote:
                    break
                index += 1
        elif char == "{" and depth == 0:
            cursor = index - 1
            while cursor >= 0 and source[cursor].isspace():
                cursor -= 1
            knr = False
            if cursor >= 0 and source[cursor] == ";":
                knr_cursor = cursor
                allowed = set(
                    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                    "0123456789_*,;[] \t\r\n"
                )
                while knr_cursor >= 0 and source[knr_cursor] in allowed:
                    knr_cursor -= 1
                if knr_cursor >= 0 and source[knr_cursor] == ")":
                    knr = True
                    cursor = knr_cursor
            if cursor >= 0 and source[cursor] == ")":
                paren, paren_depth = cursor, 0
                while paren >= 0:
                    if source[paren] == ")":
                        paren_depth += 1
                    elif source[paren] == "(":
                        paren_depth -= 1
                        if paren_depth == 0:
                            break
                    paren -= 1
                name_cursor = paren - 1
                while name_cursor >= 0 and source[name_cursor].isspace():
                    name_cursor -= 1
                name_end = name_cursor + 1
                while name_cursor >= 0 and (
                    source[name_cursor].isalnum() or source[name_cursor] == "_"
                ):
                    name_cursor -= 1
                name = source[name_cursor + 1 : name_end]
                body_depth, body_end = 0, index
                while body_end < length:
                    body_char = source[body_end]
                    if body_char in ('"', "'"):
                        quote = body_char
                        body_end += 1
                        while body_end < length:
                            if source[body_end] == "\\":
                                body_end += 2
                                continue
                            if source[body_end] == quote:
                                break
                            body_end += 1
                    elif body_char == "{":
                        body_depth += 1
                    elif body_char == "}":
                        body_depth -= 1
                        if body_depth == 0:
                            break
                    body_end += 1
                if not name or body_end >= length:
                    raise OwnerSourceError("ambiguous function boundary in owner context")
                header_start = last_semicolon
                if knr:
                    header_cursor = name_cursor
                    while header_cursor >= 0 and (
                        source[header_cursor].isalnum()
                        or source[header_cursor] in "_* \t\r\n"
                    ):
                        header_cursor -= 1
                    header_start = header_cursor + 1
                functions.append(
                    (name, header_start, index, body_end, paren if knr else None)
                )
                index = body_end
                last_semicolon = body_end + 1
            else:
                # A top-level initializer is not a function.  Skip its braces.
                body_depth, body_end = 0, index
                while body_end < length:
                    if source[body_end] == "{":
                        body_depth += 1
                    elif source[body_end] == "}":
                        body_depth -= 1
                        if body_depth == 0:
                            break
                    body_end += 1
                index = body_end
                last_semicolon = body_end + 1
        elif char == "{" and depth > 0:
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
        elif char == ";" and depth == 0:
            last_semicolon = index + 1
        index += 1
    return functions


def _definition_span(source: str, function: str) -> tuple[int, int]:
    matches = [entry for entry in _scan_functions(source) if entry[0] == function]
    if len(matches) != 1:
        raise OwnerSourceError(
            f"expected one owner-context definition of {function}, got {len(matches)}"
        )
    _, _, body_open, body_close, _ = matches[0]
    names = list(
        re.finditer(rf"\b{re.escape(function)}\s*\(", source[:body_open])
    )
    if not names:
        raise OwnerSourceError(f"could not locate owner-context signature for {function}")
    start = source.rfind("\n", 0, names[-1].start()) + 1
    return start, body_close + 1


def _pruned_ast_context(context: str, function: str, candidate: str) -> str:
    start, end = _definition_span(context, function)
    candidate_start, candidate_end = _definition_span(candidate, function)
    combined = context[:start] + candidate[candidate_start:candidate_end].strip() + context[end:]
    functions = _scan_functions(combined)
    if len([entry for entry in functions if entry[0] == function]) != 1:
        raise OwnerSourceError("target context became ambiguous while splicing")
    output: list[str] = []
    position = 0
    for name, header_start, body_open, body_close, knr_paren in functions:
        if name == function:
            continue
        output.append(combined[position:header_start])
        if knr_paren is not None:
            header = combined[header_start:knr_paren].rstrip() + "()"
        else:
            header = combined[header_start:body_open].rstrip()
        header = re.sub(r"(^|\s)asm(\s)", r"\1\2", header)
        header = re.sub(r"(^|\s)static(\s)", r"\1extern\2", header)
        output.append(header + ";\n")
        position = body_close + 1
    output.append(combined[position:])
    return "".join(output)


def _parse_ast(
    source: str,
    function: str,
    parser: Path,
    context_source: str | None,
) -> dict[str, Any]:
    translation_unit = (
        _pruned_ast_context(context_source, function, source)
        if context_source is not None
        else source
    )
    if not translation_unit.endswith("\n"):
        translation_unit += "\n"
    try:
        result = subprocess.run(
            [
                str(parser),
                "--target=powerpc-unknown-eabi",
                "-x",
                "c",
                "-std=c99",
                "-pedantic-errors",
                "-nostdinc",
                "-fno-builtin",
                "-Werror",
                "-Wno-unknown-pragmas",
                "-Wno-strict-prototypes",
                "-Wno-deprecated-non-prototype",
                "-fsyntax-only",
                "-Xclang",
                "-ast-dump=json",
                "-",
            ],
            input=translation_unit,
            capture_output=True,
            text=True,
            timeout=10,
            env=_clean_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OwnerSourceError("full-owner AST parser could not run") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        suffix = detail[-1] if detail else "unknown parser failure"
        raise OwnerSourceError(f"full-owner target rejected by AST parser: {suffix}")
    try:
        root = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OwnerSourceError("full-owner AST parser emitted invalid JSON") from exc
    definitions = [
        node
        for node in _children(root)
        if node.get("kind") == "FunctionDecl"
        and node.get("name") == function
        and any(child.get("kind") == "CompoundStmt" for child in _children(node))
    ]
    if len(definitions) != 1:
        raise OwnerSourceError(
            f"full-owner target rejected: expected one parsed definition of {function}"
        )
    return definitions[0]


def validate_owner_target(
    source: str,
    function: str | None = None,
    *,
    parser: str | Path | None = None,
    context_source: str | None = None,
    intrinsic_allowlist: Iterable[str] | None = None,
) -> None:
    """Validate one already-extracted definition under the strict owner policy.

    ``intrinsic_allowlist`` names reviewed ``__`` intrinsics the target may use; by
    default it comes from ``TARGET_INTRINSIC_ALLOWLISTS`` (empty unless configured).
    """
    if re.search(r"\\\r?\n", source):
        raise OwnerSourceError(
            "full-owner target rejected: backslash-newline translation is forbidden"
        )
    if re.search(r"\?\?[=()/!'<>-]", source):
        raise OwnerSourceError("full-owner target rejected: C trigraph is forbidden")
    masked = _mask_comments_and_literals(source)
    for pattern, label in OWNER_FORBIDDEN:
        if pattern.search(masked):
            raise OwnerSourceError(f"full-owner target rejected: {label} is forbidden")

    intrinsic_allowlist = _intrinsic_allowlist(function, intrinsic_allowlist)
    identifiers = set(re.findall(r"\b__[A-Za-z0-9_]*\b", masked))
    forbidden_intrinsics = sorted(identifiers - intrinsic_allowlist)
    if forbidden_intrinsics:
        raise OwnerSourceError(
            "full-owner target rejected: compiler/codegen intrinsic is forbidden: "
            + ", ".join(forbidden_intrinsics)
        )

    if not function:
        raise OwnerSourceError("full-owner target rejected: target policy needs a function")
    parsed = _parse_ast(source, function, _resolve_parser(parser), context_source)
    for node in _walk(parsed):
        kind = node.get("kind")
        referenced = node.get("referencedDecl")
        referenced_name = (
            str(referenced.get("name") or "")
            if isinstance(referenced, dict)
            else ""
        )
        if referenced_name.startswith("__") and referenced_name not in intrinsic_allowlist:
            raise OwnerSourceError(
                "full-owner target rejected: referenced compiler/codegen intrinsic "
                f"is forbidden: {referenced_name}"
            )
        if kind == "TypedefDecl":
            raise OwnerSourceError("full-owner target rejected: local typedef is forbidden")
        if kind in {"RecordDecl", "EnumDecl"} and node.get("completeDefinition"):
            raise OwnerSourceError(
                "full-owner target rejected: local aggregate definition is forbidden"
            )
        if kind == "BinaryOperator" and node.get("opcode") == ",":
            raise OwnerSourceError("full-owner target rejected: comma expressions are forbidden")
    _Dataflow(parsed).validate()


def validate_definition_only(source: str, start: int, end: int) -> None:
    """Require comments/whitespace only outside one selected definition."""
    if not (0 <= start < end <= len(source)):
        raise OwnerSourceError("invalid target definition span")
    outside = _mask_comments_and_literals(source[:start] + source[end:])
    if outside.strip():
        raise OwnerSourceError("owner seed must contain exactly one function definition")


def validate_candidate_translation_unit(
    base: str,
    candidate: str,
    function: str,
    *,
    parser: str | Path,
    intrinsic_allowlist: Iterable[str] | None = None,
) -> None:
    """Prove only the guarded target changed within an authentic owner TU."""
    base_start, base_end = _definition_span(base, function)
    candidate_start, candidate_end = _definition_span(candidate, function)
    if base[:base_start] != candidate[:candidate_start]:
        raise OwnerSourceError("full-owner candidate changed context before the target")
    if base[base_end:] != candidate[candidate_end:]:
        raise OwnerSourceError("full-owner candidate changed context after the target")
    base_open = base.index("{", base_start, base_end)
    candidate_open = candidate.index("{", candidate_start, candidate_end)
    base_signature = " ".join(base[base_start:base_open].split())
    candidate_signature = " ".join(candidate[candidate_start:candidate_open].split())
    if base_signature != candidate_signature:
        raise OwnerSourceError(
            "full-owner target signature drifted: expected "
            f"{base_signature!r}, got {candidate_signature!r}"
        )
    definition = candidate[candidate_start:candidate_end]
    validate_owner_target(
        definition,
        function,
        parser=parser,
        context_source=candidate,
        intrinsic_allowlist=intrinsic_allowlist,
    )


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-tu", type=Path, required=True)
    parser.add_argument("--candidate-tu", type=Path, required=True)
    parser.add_argument("--function", required=True)
    parser.add_argument("--parser", type=Path, required=True)
    parser.add_argument(
        "--intrinsic",
        action="append",
        default=[],
        metavar="NAME",
        help="reviewed __ intrinsic the target may use (repeatable; default: none)",
    )
    args = parser.parse_args(argv)
    try:
        validate_candidate_translation_unit(
            args.base_tu.read_text(encoding="utf-8"),
            args.candidate_tu.read_text(encoding="utf-8"),
            args.function,
            parser=args.parser,
            intrinsic_allowlist=args.intrinsic,
        )
    except (OSError, OwnerSourceError) as exc:
        print(f"owner source error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
