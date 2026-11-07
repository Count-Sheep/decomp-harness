"""Conservative C89 repairs for model drafts; compilation remains authoritative."""

from __future__ import annotations

import sys
import copy

import local_campaign_decompile as decompile


class RepairError(ValueError):
    pass


def declaration_order_block(candidate: str, declarations: str, symbol: str) -> tuple[int, int, list[str]] | None:
    """Find reorderable uninitialized scalar locals, preserving every source byte.

    The existing line matcher supplies offsets; the parser rejects declarations
    with initialization, qualifiers, storage, attributes or array lifetimes.
    """
    import local_campaign as campaign
    import local_campaign_permute as permute

    first, last = campaign.function_span(candidate, symbol)
    function = candidate[first:last]
    brace = campaign.source_mask(function).index("{")
    block = permute.declaration_block(campaign.source_mask(function[:brace]) + function[brace:])
    if block is None:
        return None
    start, end, lines = block
    parser_root = str(decompile.tool().parent)
    if parser_root not in sys.path:
        sys.path.insert(0, parser_root)
    from m2c import c_types
    from m2c.error import DecompFailure
    from m2c_pycparser import c_ast as ca

    try:
        ast, _ = c_types.parse_c(c_types.strip_comments(declarations + "\n" + candidate), None)
    except DecompFailure as error:
        raise RepairError("Cannot screen declaration order: " + str(error)) from error
    functions = [node for node in ast.ext if isinstance(node, ca.FuncDef)]
    if len(functions) != 1 or functions[0].decl.name != symbol:
        raise RepairError("Expected only the assigned function definition")
    typedefs = {node.name: node for node in ast.ext if isinstance(node, ca.Typedef)}

    def safe_type(node, seen=frozenset(), indirection=False):
        if any(getattr(node, key, None) for key in ("quals", "align", "gcc_attributes")):
            return False
        if isinstance(node, ca.PtrDecl):
            return safe_type(node.type, seen, indirection=True)
        if isinstance(node, ca.TypeDecl):
            return safe_type(node.type, seen, indirection=indirection)
        if isinstance(node, ca.IdentifierType):
            if len(node.names) == 1 and node.names[0] in typedefs:
                name = node.names[0]
                alias = typedefs[name]
                return (name not in seen and not getattr(alias, "gcc_attributes", None)
                        and safe_type(alias.type, seen | {name}, indirection=indirection))
            return (set(node.names) <= {"signed", "unsigned", "char", "short", "int", "long", "float", "double", "_Bool"}
                    or indirection and node.names == ["void"])
        if isinstance(node, (ca.Struct, ca.Union)):
            return indirection
        if isinstance(node, ca.Enum):
            return True
        return False

    leading = (functions[0].body.block_items or [])[:len(lines)]
    if (len(leading) != len(lines) or any(not isinstance(node, ca.Decl) or not node.name
            or node.init is not None or node.storage or node.funcspec or node.bitsize
            or any(getattr(node, key, None) for key in ("quals", "align", "gcc_attributes"))
            or not safe_type(node.type) for node in leading)
            or len({node.name for node in leading}) != len(leading)):
        raise RepairError("Declaration-order search requires distinct uninitialized, unqualified scalar locals")
    return first + start, first + end, lines


def bind_signature(candidate: str, declarations: str, signature: str, symbol: str,
                   *, primitive_aliases: bool = False) -> str:
    """Restore contract spelling only when parsed declarations are identical."""
    import local_campaign as campaign

    parser_root = str(decompile.tool().parent)
    if parser_root not in sys.path:
        sys.path.insert(0, parser_root)
    from m2c import c_types
    from m2c_pycparser import c_ast as ca
    from m2c_pycparser.c_generator import CGenerator

    ast, _ = c_types.parse_c(c_types.strip_comments(declarations + "\n" + candidate), None)
    required, _ = c_types.parse_c(c_types.strip_comments(declarations + "\n" + signature + ";"), None)
    definitions = [node for node in ast.ext if isinstance(node, ca.FuncDef)]
    contracts = [node for node in required.ext if isinstance(node, ca.Decl) and node.name == symbol]
    if len(definitions) != 1 or definitions[0].decl.name != symbol or not contracts:
        raise RepairError("Expected one target definition and its declared contract")
    generator = CGenerator()
    typedefs = {node.name: node for node in required.ext if isinstance(node, ca.Typedef)}

    def canonical(declaration):
        declaration = copy.deepcopy(declaration)

        def primitive(node, seen=frozenset()):
            if not isinstance(node, ca.TypeDecl) or not isinstance(node.type, ca.IdentifierType) or node.align:
                return None
            names = node.type.names
            if len(names) == 1 and names[0] in typedefs:
                name = names[0]
                alias = typedefs[name]
                if name in seen or getattr(alias, "gcc_attributes", None):
                    return None
                resolved = primitive(alias.type, seen | {name})
                if resolved is None:
                    return None
                result = copy.deepcopy(resolved)
                result.declname = node.declname
                result.quals = sorted(set(result.quals) | set(node.quals))
                return result
            if set(names) <= {"void", "signed", "unsigned", "char", "short", "int", "long", "float", "double", "_Bool"}:
                return node
            return None

        class Expand(ca.NodeVisitor):
            def visit_TypeDecl(self, node):
                expanded = primitive(node)
                if expanded is not None:
                    node.type = expanded.type
                    node.quals = expanded.quals

        if primitive_aliases:
            Expand().visit(declaration)
        return generator.visit(declaration)

    if canonical(definitions[0].decl) != canonical(contracts[-1]):
        raise RepairError("Generated signature is not identical to the required declaration")
    start, end = campaign.function_span(candidate, symbol)
    brace = candidate.index("{", start, end)
    return candidate[:start] + signature + "\n" + candidate[brace:]


def repair_declarations(candidate: str, declarations: str, symbol: str) -> tuple[str, int]:
    """Hoist scalar declarations, leaving initializer execution at its old position.

    Reject ambiguous shadowing and lifetime/type constructs rather than guessing.
    Unchanged functions retain their original spelling and comments.
    """
    import local_campaign as campaign

    parser_root = str(decompile.tool().parent)
    if parser_root not in sys.path:
        sys.path.insert(0, parser_root)
    from m2c import c_types
    from m2c_pycparser import c_ast as ca
    from m2c_pycparser.c_generator import CGenerator

    ast, _ = c_types.parse_c(c_types.strip_comments(declarations + "\n" + candidate), None)
    functions = [node for node in ast.ext if isinstance(node, ca.FuncDef)]
    if len(functions) != 1 or functions[0].decl.name != symbol:
        raise RepairError("Expected only the assigned function definition")
    function = functions[0]
    typedefs = {node.name: node.type for node in ast.ext if isinstance(node, ca.Typedef)}

    class Names(ca.NodeVisitor):
        def __init__(self):
            self.names = set()

        def visit_ID(self, node):
            self.names.add(node.name)

        def visit_Decl(self, node):
            self.names.add(node.name)
            self.generic_visit(node)

        def visit_IdentifierType(self, node):
            self.names.update(node.names)

    class Safety(ca.NodeVisitor):
        def __init__(self):
            self.unsafe = False

        def generic_visit(self, node):
            if isinstance(node, (ca.Goto, ca.Label, ca.ArrayDecl, ca.CompoundLiteral)):
                self.unsafe = True
            super().generic_visit(node)

    def scalar(node, seen=frozenset()):
        if getattr(node, "quals", None):
            return False
        if isinstance(node, ca.PtrDecl):
            check = Safety()
            check.visit(node)
            return not check.unsafe
        if isinstance(node, ca.TypeDecl):
            return scalar(node.type, seen)
        if isinstance(node, ca.IdentifierType):
            if len(node.names) == 1 and node.names[0] in typedefs:
                name = node.names[0]
                return name not in seen and scalar(typedefs[name], seen | {name})
            return set(node.names) <= {"signed", "unsigned", "char", "short", "int", "long", "float", "double", "_Bool"}
        return isinstance(node, ca.Enum) and node.values is None

    changes = 0

    def visit(node):
        nonlocal changes
        if isinstance(node, ca.Compound):
            leading, body, seen, statement = [], [], set(), False
            for child in node.block_items or []:
                names = Names()
                names.visit(child)
                if isinstance(child, (ca.Decl, ca.Typedef)) and not statement:
                    leading.append(child)
                elif isinstance(child, (ca.Decl, ca.Typedef)):
                    if (not isinstance(child, ca.Decl) or not child.name or child.name in seen
                            or child.storage or child.quals or child.bitsize or child.funcspec
                            or not scalar(child.type) or isinstance(child.init, ca.InitList)):
                        raise RepairError("Cannot safely hoist declaration: " + str(getattr(child, "name", "")))
                    initializer = child.init
                    child.init = None
                    leading.append(child)
                    if initializer is not None:
                        body.append(ca.Assignment("=", ca.ID(child.name), initializer))
                    changes += 1
                else:
                    statement = True
                    body.append(child)
                seen.update(names.names)
            node.block_items = leading + body
        for _, child in node.children():
            visit(child)

    visit(function.body)
    if not changes:
        return candidate, 0
    safety = Safety()
    safety.visit(function.body)
    if safety.unsafe:
        raise RepairError("Declaration repair does not support labels, arrays or compound literals")
    start, end = campaign.function_span(candidate, symbol)
    # Keep the required signature's original pointer spacing and storage spelling.
    brace = candidate.index("{", start, end)
    repaired = candidate[:brace] + CGenerator().visit(function.body) + candidate[end:]
    return repaired, changes
