"""Assemble parsed C drafts without resolving conflicting declarations by guesswork."""

from __future__ import annotations

import copy
import sys
import re

import local_campaign_decompile as decompile

NULL_DEFINITION = re.compile(r"\s*#\s*define\s+NULL\s+\(\(\s*void\s*\*\s*\)\s*0\s*\)\s*")


def m2c_available() -> bool:
    """Whether the configured m2c checkout (an external tool) can be imported for parsing."""
    parser_root = str(decompile.tool().parent)
    if parser_root not in sys.path:
        sys.path.insert(0, parser_root)
    try:
        from m2c import c_types  # noqa: F401
        from m2c_pycparser import c_ast  # noqa: F401
    except ImportError:
        return False
    return True


def validate_directives(clean: str, captured_pragmas: set[str] | None = None) -> None:
    for line in clean.splitlines():
        if NULL_DEFINITION.fullmatch(line):
            continue
        if line.strip() in (captured_pragmas or set()):
            continue
        if line.lstrip().startswith("#") and not re.fullmatch(
                r"\s*#\s*pragma\s+(?:use_lmw_stmw|fp_contract)\s+(?:on|off)\s*", line):
            raise ValueError("Preprocessor directives require a captured unit context")


def overlay_native_source(source: str, candidates: dict[str, str], required: set[str],
                          *, cpp: bool = False) -> str:
    """Preserve native context and C++ bodies while testing bounded C definitions."""
    from local_campaign import function_span, source_mask

    for symbol, candidate in candidates.items():
        if symbol not in required:
            raise ValueError("Function override is outside target owner: " + symbol)
        parsed = source_drafts(candidate)
        if len(parsed) != 1 or parsed[0]["symbol"] != symbol:
            raise ValueError("Function override must define only its assigned symbol: " + symbol)
        definition = parsed[0]["source"]
        try:
            start, end = function_span(source, symbol)
        except ValueError as error:
            if "function definition not found" not in str(error):
                raise
            # A mangled name is not a C++ source-level identifier. Appending it
            # would bypass the owner's class/signature contract.
            if cpp and "__" in symbol:
                raise ValueError("C++ member requires source-level symbol mapping: " + symbol) from error
            definition = ('extern "C" {\n' + definition + '\n}') if cpp else definition
            source += "\n" + definition + "\n"
        else:
            # Preserve the owner's declaration and linkage when parameters match.
            # Different contracts remain compiler errors, never guessed casts.
            original = source[start:end]
            old_mask, new_mask = source_mask(original), source_mask(definition)
            old_body, new_body = old_mask.index("{"), new_mask.index("{")
            old_params = old_mask[old_mask.index("("):old_body].strip()
            new_params = new_mask[new_mask.index("("):new_body].strip()
            replacement = (original[:old_body] + definition[new_body:]
                           if old_params == new_params else definition)
            source = source[:start] + replacement + source[end:]
    return source


def source_drafts(source: str, shared: str = "") -> list[dict]:
    """Import explicit source bodies as hypotheses, never as exact-match evidence."""
    parser_root = str(decompile.tool().parent)
    if parser_root not in sys.path:
        sys.path.insert(0, parser_root)
    from m2c import c_types
    from m2c_pycparser import c_ast as ca
    from m2c_pycparser.c_generator import CGenerator
    from m2c_pycparser.c_parser import CParser

    clean = c_types.strip_comments(shared + "\n" + source)
    validate_directives(clean)
    macros = [line for line in clean.splitlines() if NULL_DEFINITION.fullmatch(line)]
    clean = "\n".join("" if NULL_DEFINITION.fullmatch(line) else line for line in clean.splitlines())
    try:
        seed_count = len(CParser().parse(c_types.add_builtin_typedefs("")).ext)
        shared_clean = "\n".join("" if NULL_DEFINITION.fullmatch(line) else line
                                 for line in c_types.strip_comments(shared).splitlines())
        shared_nodes = CParser().parse(c_types.add_builtin_typedefs(shared_clean)).ext[seed_count:]
        if any(isinstance(node, ca.FuncDef) for node in shared_nodes):
            raise ValueError("Shared context cannot supply source-backed function bodies")
        nodes = CParser().parse(c_types.add_builtin_typedefs(clean)).ext[seed_count:]
    except Exception as error:
        raise ValueError("Cannot parse source-backed unit input: " + str(error)) from error
    definitions = []
    declarations = []
    for node in nodes:
        if isinstance(node, ca.FuncDef):
            definitions.append(node)
            declarations.append(node.decl)
        else:
            if definitions and isinstance(node, ca.Pragma):
                raise ValueError("Scoped source pragmas require captured per-function settings")
            declarations.append(node)
    names = [node.decl.name for node in definitions]
    if not names or len(names) != len(set(names)):
        raise ValueError("Source must provide unique function definitions")
    generator = CGenerator()
    context = "\n".join(macros) + "\n" + generator.visit(ca.FileAST(declarations))
    return [{"symbol": node.decl.name, "source": generator.visit(node), "context": context,
             "static_inline": node.decl.storage == ["static"] and "inline" in node.decl.funcspec}
            for node in definitions]


def assemble(drafts: list[dict], function_order: list[str], shared: str = "", *,
             data_owner=None, recovered_data: list | None = None,
             helper_drafts: list[dict] | None = None) -> str:
    parser_root = str(decompile.tool().parent)
    if parser_root not in sys.path:
        sys.path.insert(0, parser_root)
    from m2c import c_types
    from m2c_pycparser import c_ast as ca
    from m2c_pycparser.c_generator import CGenerator
    from m2c_pycparser.c_parser import CParser

    generator = CGenerator()
    macros = set()
    implicit_typedefs = set()

    def parse(source):
        # Preprocessing and pragmas need a captured compiler context, not silent removal.
        clean = c_types.strip_comments(source)
        validate_directives(clean)
        lines = []
        for line in clean.splitlines():
            if NULL_DEFINITION.fullmatch(line):
                macros.add("#define NULL ((void *)0)")
                lines.append("")
            else:
                lines.append(line)
        clean = "\n".join(lines)
        try:
            builtins = CParser().parse(c_types.add_builtin_typedefs(""))
            tree = CParser().parse(c_types.add_builtin_typedefs(clean))
            explicit = tree.ext[len(builtins.ext):]
            supplied = {node.name for node in explicit if isinstance(node, ca.Typedef)}
            defaults = [node for node in builtins.ext if node.name not in supplied]
            implicit_typedefs.update(defaults)
            tree.ext = defaults + explicit
            return tree
        except Exception as error:
            raise ValueError("Cannot parse unit input: " + str(error)) from error

    def signature(decl):
        value = copy.deepcopy(decl)
        value.storage = [s for s in value.storage if s != "extern"]

        def unnamed(node):
            if isinstance(node, ca.TypeDecl):
                node.declname = None
            for _, child in node.children():
                if isinstance(child, ca.Decl):
                    child.name = None
                unnamed(child)

        unnamed(value.type)
        return generator.visit(value)

    declarations = []
    seen = {}
    declaration_slots = {}
    tags = {}
    prototypes = {}
    functions = {}

    def add(node):
        if isinstance(node, ca.FuncDef):
            raise ValueError("Shared context cannot contain function bodies")
        rendered = generator.visit(node)
        name = getattr(node, "name", None)
        if isinstance(node, ca.Decl) and isinstance(node.type, ca.FuncDecl):
            current = signature(node)
            if name in prototypes and prototypes[name] != current:
                raise ValueError("Conflicting function declarations: " + name)
            if name in prototypes:
                return
            prototypes[name] = current
        key = (("pragma", node.string.split()[0]) if isinstance(node, ca.Pragma)
               else ("ordinary", name) if name else ("anonymous", rendered))
        if key in seen:
            if seen[key] != rendered:
                previous = declarations[declaration_slots[key]]
                if isinstance(previous, ca.Typedef) and isinstance(node, ca.Typedef):
                    if node in implicit_typedefs and previous not in implicit_typedefs:
                        return
                    if previous in implicit_typedefs and node not in implicit_typedefs:
                        declarations[declaration_slots[key]] = node
                        seen[key] = rendered
                        return

                def external_reference(value):
                    return (isinstance(value, ca.Decl) and not isinstance(value.type, ca.FuncDecl)
                            and value.storage == ["extern"] and value.init is None)

                def object_contract(value):
                    value = copy.deepcopy(value)
                    value.storage = [s for s in value.storage if s != "extern"]
                    value.init = None
                    return generator.visit(value)

                if (isinstance(previous, ca.Decl) and isinstance(node, ca.Decl)
                        and not isinstance(previous.type, ca.FuncDecl) and not isinstance(node.type, ca.FuncDecl)
                        and (external_reference(previous) or external_reference(node))
                        and object_contract(previous) == object_contract(node)):
                    if external_reference(previous):
                        declarations[declaration_slots[key]] = node
                        seen[key] = rendered
                    return
                raise ValueError("Conflicting declarations: " + str(name))
            return

        def check_tags(value):
            if isinstance(value, (ca.Struct, ca.Union, ca.Enum)) and value.name:
                definition = getattr(value, "decls", None) or getattr(value, "values", None)
                if definition:
                    tag = (type(value).__name__, value.name)
                    text = generator.visit(value)
                    if tag in tags:
                        raise ValueError("Repeated tag definition requires shared context: " + value.name)
                    tags[tag] = text
            for _, child in value.children():
                check_tags(child)

        check_tags(node)
        seen[key] = rendered
        declaration_slots[key] = len(declarations)
        declarations.append(node)

    shared_ast = parse(shared)
    for node in shared_ast.ext:
        add(node)
    helpers = helper_drafts or []
    helper_names = [draft["symbol"] for draft in helpers]
    if len(set(helper_names)) != len(helper_names) or set(helper_names) & set(function_order):
        raise ValueError("Helper names conflict with target function order")
    for draft in [*helpers, *drafts]:
        context = parse(shared + "\n" + draft.get("context", ""))
        # Retained m2c context may include reference bodies; only their contracts
        # belong in the assembled hypothesis, never their implementations.
        declarations_only = ca.FileAST([node.decl if isinstance(node, ca.FuncDef) else node
                                       for node in context.ext if node not in implicit_typedefs])
        tree = parse(generator.visit(declarations_only) + "\n" + draft["source"])
        definitions = [node for node in tree.ext if isinstance(node, ca.FuncDef)]
        name = draft["symbol"]
        if len(definitions) != 1 or definitions[0].decl.name != name:
            raise ValueError("Expected exactly the assigned function: " + name)
        if name in functions:
            raise ValueError("Duplicate function draft: " + name)
        for node in tree.ext:
            if not isinstance(node, ca.FuncDef):
                add(node)
        function = definitions[0]
        if name in helper_names and (function.decl.storage != ["static"] or "inline" not in function.decl.funcspec):
            raise ValueError("Non-owner source helpers must be static inline: " + name)
        if name in prototypes and prototypes[name] != signature(function.decl):
            raise ValueError("Definition conflicts with prototype: " + name)
        functions[name] = function
    emitted_order = helper_names + function_order
    if len(function_order) != len(set(function_order)) or set(emitted_order) != set(functions):
        raise ValueError("Function drafts do not cover the requested unit order")
    # Declare every function before any body so target order does not create implicit calls.
    for name in emitted_order:
        if name in prototypes and prototypes[name] != signature(functions[name].decl):
            raise ValueError("Definition conflicts with prototype: " + name)
        if name not in prototypes:
            declarations.append(copy.deepcopy(functions[name].decl))
    if data_owner is not None:
        from m2c_unit_data import recover
        recovered = recover(declarations, data_owner)
        if recovered_data is not None:
            recovered_data.extend(recovered)
    result = generator.visit(ca.FileAST(declarations + [functions[name] for name in emitted_order])) + "\n"
    parse(result)
    return "".join(macro + "\n" for macro in sorted(macros)) + result
