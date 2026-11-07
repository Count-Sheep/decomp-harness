"""Policy-safe source rewrites for near-exact functions.

Register and scheduling walls often move with an ordinary change of how a statement is
written: operand order, `!x` against `x == 0`, `i++` against `i += 1`, a `for` written as a
`while`, the branches of an if/else swapped. This module lists such rewrites for one
function. Every rewrite keeps the meaning of the code, adds no variable and changes no type:
- operands are swapped only when both are side-effect-free (names, member and index chains,
  constants) and the operators around them bind more loosely, so precedence cannot change;
- `&&`/`||` are never swapped (short-circuit guards), `-`, `/`, `<<` are not commutative;
- a relational comparison is mirrored (`a < b` to `b > a`), never negated, so NaN
  behaviour is unchanged.
The runner compiles and scores each variant; the result still goes through the source policy
and a human audit before promotion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PUNCTUATORS = sorted("""
>>= <<= -> ++ -- << >> <= >= == != && || += -= *= /= %= &= |= ^= ... ( ) [ ] { } . , ; ? : ~ ! + - * / % & | ^ < > =
""".split(), key=len, reverse=True)
TOKEN = re.compile(r"\s*(?:(?P<ident>[A-Za-z_]\w*)|(?P<number>0[xX][0-9A-Fa-f]+[uUlL]*|\d+(?:\.\d*)?(?:[eE][-+]?\d+)?[fFuUlL]*|\.\d+[fF]?)"
                   r"|(?P<punct>" + "|".join(re.escape(p) for p in PUNCTUATORS) + r")|(?P<other>\S))")
BINARY = {"*": 13, "/": 13, "%": 13, "+": 12, "-": 12, "<<": 11, ">>": 11, "<": 10, ">": 10, "<=": 10, ">=": 10,
          "==": 9, "!=": 9, "&": 8, "^": 7, "|": 6, "&&": 5, "||": 4, "?": 3, "=": 2, "+=": 2, "-=": 2, "*=": 2,
          "/=": 2, "%=": 2, "&=": 2, "|=": 2, "^=": 2, "<<=": 2, ">>=": 2, ",": 1}
OPENERS = {"(", "[", "{", ";", ",", "?", ":", "return", "case"}
CLOSERS = {")", "]", "}", ";", ",", "?", ":"}
COMMUTATIVE = {"+", "*", "&", "|", "^", "==", "!="}
MIRROR = {"<": ">", ">": "<", "<=": ">=", ">=": "<="}
KEYWORDS = {"if", "else", "while", "for", "do", "return", "switch", "case", "default", "break", "continue",
            "goto", "sizeof", "extern", "static", "const", "volatile", "struct", "union", "enum", "typedef"}


@dataclass
class Token:
    kind: str
    text: str
    start: int
    end: int


@dataclass
class Rewrite:
    start: int
    end: int
    text: str
    label: str

    def apply(self, source: str) -> str:
        return source[:self.start] + self.text + source[self.end:]


def mask(source: str) -> str:
    """Blank comments, string and character literals and preprocessor lines, keeping offsets."""
    def blank(match: re.Match) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))
    return re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|^[ \t]*#[^\n]*',
                  blank, source, flags=re.DOTALL | re.MULTILINE)


def tokenize(source: str) -> list[Token]:
    masked, tokens, position = mask(source), [], 0
    while True:
        match = TOKEN.match(masked, position)
        if not match or match.end() == position:
            break
        kind = match.lastgroup
        tokens.append(Token(kind, match.group(kind), match.start(kind), match.end(kind)))
        position = match.end()
    return tokens


def _operand_end(tokens: list[Token], i: int, postfix: bool = False) -> int | None:
    """End index (exclusive) of a side-effect-free operand starting at i, or None.

    With `postfix`, an operand followed by `++`/`--` is still returned (for the increment
    rewrites, which handle that operator themselves)."""
    if i >= len(tokens) or tokens[i].kind not in ("ident", "number") or tokens[i].text in KEYWORDS:
        return None
    if i > 0 and tokens[i - 1].text in (".", "->"):
        return None
    j = i + 1
    if tokens[i].kind == "ident":
        while j < len(tokens):
            if tokens[j].text in (".", "->") and j + 1 < len(tokens) and tokens[j + 1].kind == "ident":
                j += 2
            elif tokens[j].text == "[":
                depth, k = 0, j
                while k < len(tokens):
                    depth += {"[": 1, "]": -1}.get(tokens[k].text, 0)
                    if tokens[k].text in ("(", "=", "++", "--") or tokens[k].text in BINARY and tokens[k].text.endswith("=") \
                            and tokens[k].text not in ("==", "!=", "<=", ">="):
                        return None
                    if depth == 0:
                        break
                    k += 1
                if k >= len(tokens):
                    return None
                j = k + 1
            else:
                break
    if j < len(tokens) and (tokens[j].text == "(" or tokens[j].text in ("++", "--") and not postfix):
        return None  # a call or a post-increment is not side-effect free
    return j


def _operand_ending(token: Token) -> bool:
    return token.kind in ("ident", "number") and token.text not in KEYWORDS or token.text in (")", "]")


def _left_precedence(tokens: list[Token], i: int) -> int | None:
    """How tightly the context left of token i binds; None when unsafe (a cast, a unary operator)."""
    if i == 0:
        return 0
    before = tokens[i - 1]
    if before.text in OPENERS:
        return 0
    if before.text in BINARY and i >= 2 and _operand_ending(tokens[i - 2]):
        return BINARY[before.text]
    return None


def _right_precedence(tokens: list[Token], j: int) -> int | None:
    if j >= len(tokens):
        return 0
    after = tokens[j]
    if after.text in CLOSERS:
        return 0
    return BINARY.get(after.text)


def _text(source: str, tokens: list[Token], i: int, j: int) -> str:
    return source[tokens[i].start:tokens[j - 1].end]


def operand_swaps(source: str, tokens: list[Token]) -> list[Rewrite]:
    found = []
    for i in range(len(tokens)):
        a_end = _operand_end(tokens, i)
        if a_end is None or a_end >= len(tokens):
            continue
        op = tokens[a_end].text
        if op not in COMMUTATIVE and op not in MIRROR:
            continue
        b_end = _operand_end(tokens, a_end + 1)
        if b_end is None:
            continue
        left, right = _left_precedence(tokens, i), _right_precedence(tokens, b_end)
        if left is None or right is None or not (left < BINARY[op] and right <= BINARY[op]):
            continue
        if op == "*" and (i == 0 or tokens[i - 1].text in (";", "{", "}", "(", ",")) \
                and b_end < len(tokens) and tokens[b_end].text in (";", "=", ",", "[", ")"):
            continue  # `type * name` is a declaration or parameter, not a product
        a, b = _text(source, tokens, i, a_end), _text(source, tokens, a_end + 1, b_end)
        if a == b:
            continue
        found.append(Rewrite(tokens[i].start, tokens[b_end - 1].end, f"{b} {MIRROR.get(op, op)} {a}",
                             f"swap operands of {a} {op} {b}"))
    return found


def zero_tests(source: str, tokens: list[Token]) -> list[Rewrite]:
    """`(!x)` <-> `(x == 0)` and `(x)` <-> `(x != 0)` as a whole if/while condition."""
    found = []
    for i, token in enumerate(tokens):
        if token.text not in ("if", "while") or i + 1 >= len(tokens) or tokens[i + 1].text != "(":
            continue
        open_ = i + 1
        negated = open_ + 1 < len(tokens) and tokens[open_ + 1].text == "!"
        start = open_ + 2 if negated else open_ + 1
        end = _operand_end(tokens, start)
        if end is None or end >= len(tokens):
            continue
        name = _text(source, tokens, start, end)
        span = (tokens[open_ + 1].start, tokens[end - 1].end)
        if tokens[end].text == ")":
            text = f"{name} == 0" if negated else f"{name} != 0"
            found.append(Rewrite(span[0], span[1], text, f"write the test of {name} as {text}"))
        elif not negated and end + 2 < len(tokens) and tokens[end].text in ("==", "!=") \
                and tokens[end + 1].text in ("0", "NULL") and tokens[end + 2].text == ")":
            text = f"!{name}" if tokens[end].text == "==" else name
            found.append(Rewrite(span[0], tokens[end + 1].end, text, f"write the test of {name} as ({text})"))
    return found


def increments(source: str, tokens: list[Token]) -> list[Rewrite]:
    """`x++`, `++x`, `x += 1` and `x = x + 1` as a statement or a for step."""
    found = []
    for i in range(len(tokens)):
        if i and tokens[i - 1].text not in (";", "{", "}", ")", "else"):
            continue
        prefix = tokens[i].text if tokens[i].text in ("++", "--") else None
        start = i + 1 if prefix else i
        end = _operand_end(tokens, start, postfix=True)
        if end is None or end >= len(tokens):
            continue
        name = _text(source, tokens, start, end)
        rest = [t.text for t in tokens[end:end + 5]]
        if prefix:
            sign, stop = prefix[0], end
        elif rest[:1] in (["++"], ["--"]):
            sign, stop = rest[0][0], end + 1
        elif rest[:2] in (["+=", "1"], ["-=", "1"]):
            sign, stop = rest[0][0], end + 2
        elif len(rest) >= 4 and rest[0] == "=" and rest[2] in ("+", "-") and rest[3] == "1" \
                and _text(source, tokens, end + 1, end + 2) == name and _operand_end(tokens, end + 1) == end + 2:
            sign, stop = rest[2], end + 4
        else:
            continue
        if stop >= len(tokens) or tokens[stop].text not in (";", ")"):
            continue
        current = source[tokens[i].start:tokens[stop - 1].end]
        for form in (f"{name}{sign}{sign}", f"{sign}{sign}{name}", f"{name} {sign}= 1", f"{name} = {name} {sign} 1"):
            if form.replace(" ", "") != current.replace(" ", ""):
                found.append(Rewrite(tokens[i].start, tokens[stop - 1].end, form, f"write {current} as {form}"))
    return found


def compound_assignments(source: str, tokens: list[Token]) -> list[Rewrite]:
    """`x op= y;` -> `x = x op (y);` and back, for a side-effect-free x."""
    found = []
    for i in range(len(tokens)):
        if i and tokens[i - 1].text not in (";", "{", "}", ")", "else"):
            continue
        end = _operand_end(tokens, i)
        if end is None or end + 1 >= len(tokens):
            continue
        name, op = _text(source, tokens, i, end), tokens[end].text
        stop, depth = end + 1, 0
        while stop < len(tokens) and not (tokens[stop].text == ";" and depth == 0):
            depth += {"(": 1, ")": -1, "[": 1, "]": -1}.get(tokens[stop].text, 0)
            stop += 1
        if stop >= len(tokens) or stop == end + 1:
            continue
        value = _text(source, tokens, end + 1, stop)
        if op in ("+=", "-=", "*=", "&=", "|=", "^=", "<<=", ">>=") and value.strip() != "1":
            simple = _operand_end(tokens, end + 1) == stop
            rhs = value if simple else f"({value})"
            found.append(Rewrite(tokens[i].start, tokens[stop - 1].end, f"{name} = {name} {op[:-1]} {rhs}",
                                 f"expand {name} {op} {value}"))
        elif op == "=" and stop - end >= 4 and _operand_end(tokens, end + 1) is not None \
                and _text(source, tokens, end + 1, _operand_end(tokens, end + 1)) == name:
            mid = _operand_end(tokens, end + 1)
            operator = tokens[mid].text
            if operator in ("+", "-", "*", "&", "|", "^", "<<", ">>") and _operand_end(tokens, mid + 1) == stop \
                    and _text(source, tokens, mid + 1, stop).strip() != "1":
                found.append(Rewrite(tokens[i].start, tokens[stop - 1].end,
                                     f"{name} {operator}= {_text(source, tokens, mid + 1, stop)}",
                                     f"contract {name} = {name} {operator} ..."))
    return found


def index_forms(source: str, tokens: list[Token]) -> list[Rewrite]:
    """`a[i]` -> `*(a + i)` for a name or member chain (`p->list[i]`) and a side-effect-free index."""
    found = []
    for k in range(1, len(tokens) - 2):
        if tokens[k].text != "[" or tokens[k - 1].kind != "ident" or tokens[k - 1].text in KEYWORDS:
            continue
        i = k - 1  # walk back over `a.b->c` to the chain's first name
        while i >= 2 and tokens[i - 1].text in (".", "->") and tokens[i - 2].kind == "ident":
            i -= 2
        if i and tokens[i - 1].text in (".", "->", "]", ")"):
            continue
        index_end = _operand_end(tokens, k + 1)
        if index_end is None or index_end >= len(tokens) or tokens[index_end].text != "]":
            continue
        if index_end + 1 < len(tokens) and tokens[index_end + 1].text in (".", "->", "[", "++", "--", "("):
            continue
        base, index = _text(source, tokens, i, k), _text(source, tokens, k + 1, index_end)
        found.append(Rewrite(tokens[i].start, tokens[index_end].end, f"*({base} + {index})",
                             f"write {base}[{index}] as *({base} + {index})"))
    return found


def _matching(tokens: list[Token], i: int, open_: str, close: str) -> int | None:
    depth = 0
    for k in range(i, len(tokens)):
        depth += (tokens[k].text == open_) - (tokens[k].text == close)
        if depth == 0:
            return k
    return None


def loop_forms(source: str, tokens: list[Token]) -> list[Rewrite]:
    """`for (init; cond; step) { body }` -> `init; while (cond) { body step; }` (no `continue`)."""
    found = []
    for i, token in enumerate(tokens):
        if token.text != "for" or i + 1 >= len(tokens) or tokens[i + 1].text != "(":
            continue
        close = _matching(tokens, i + 1, "(", ")")
        if close is None or close + 1 >= len(tokens) or tokens[close + 1].text != "{":
            continue
        semis = [k for k in range(i + 2, close) if tokens[k].text == ";"]
        body_end = _matching(tokens, close + 1, "{", "}")
        if len(semis) != 2 or body_end is None:
            continue
        if any(t.text == "continue" for t in tokens[close + 1:body_end]):
            continue
        init = source[tokens[i + 2].start:tokens[semis[0]].start].strip() if semis[0] > i + 2 else ""
        cond = source[tokens[semis[0]].end:tokens[semis[1]].start].strip() or "1"
        step = source[tokens[semis[1]].end:tokens[close].start].strip()
        if not step or re.match(r"(?:[A-Za-z_]\w*\s+)+[\*\s]*[A-Za-z_]\w*\s*=", init):
            continue  # a declaration in the init would change scope
        indent = re.search(r"[ \t]*$", source[:token.start]).group(0)
        body = source[tokens[close + 1].end:tokens[body_end].start].rstrip()
        text = (f"{init};\n{indent}" if init else "") + f"while ({cond}) {{{body}\n{indent}    {step};\n{indent}}}"
        found.append(Rewrite(token.start, tokens[body_end].end, text, f"write the for ({cond}) loop as a while"))
    return found


def branch_swaps(source: str, tokens: list[Token]) -> list[Rewrite]:
    """`if (c) { a } else { b }` -> `if (!(c)) { b } else { a }` (both branches braced)."""
    found = []
    for i, token in enumerate(tokens):
        if token.text != "if" or i + 1 >= len(tokens) or tokens[i + 1].text != "(" or (i and tokens[i - 1].text == "else"):
            continue
        close = _matching(tokens, i + 1, "(", ")")
        if close is None or close + 1 >= len(tokens) or tokens[close + 1].text != "{":
            continue
        then_end = _matching(tokens, close + 1, "{", "}")
        if then_end is None or then_end + 2 >= len(tokens) or tokens[then_end + 1].text != "else" \
                or tokens[then_end + 2].text != "{":
            continue
        else_end = _matching(tokens, then_end + 2, "{", "}")
        if else_end is None:
            continue
        cond = source[tokens[i + 2].start:tokens[close - 1].end]
        simple = re.fullmatch(r"!\s*([A-Za-z_][\w\.\->\[\]]*)", cond.strip())
        flipped = simple.group(1) if simple else f"!({cond})"
        for a, b in ((" == ", " != "), (" != ", " == ")):
            if cond.count(a) == 1 and "&&" not in cond and "||" not in cond and "?" not in cond and \
                    _operand_end(tokens, i + 2) is not None:
                flipped = cond.replace(a, b)
                break
        then_body = source[tokens[close + 1].start:tokens[then_end].end]
        else_body = source[tokens[then_end + 2].start:tokens[else_end].end]
        found.append(Rewrite(token.start, tokens[else_end].end, f"if ({flipped}) {else_body} else {then_body}",
                             f"swap the branches of if ({cond})"))
    return found


DECLARATION = re.compile(
    r"^[ \t]*(?:(?:const|volatile|unsigned|signed|struct|union|enum|register)\s+)*[A-Za-z_]\w*"
    r"[\s\*]+[A-Za-z_]\w*(?:\[[^\]\n]*\])?(?:\s*,\s*[\*\s]*[A-Za-z_]\w*(?:\[[^\]\n]*\])?)*"
    r"(?:\s*=\s*(?:-?(?:0[xX][0-9A-Fa-f]+|\d+(?:\.\d*)?)[fFuUlL]*|NULL))?\s*;[ \t]*(?://[^\n]*)?$"
)
NOT_DECLARATION = ("return", "goto", "break", "continue", "case", "default", "else", "do")


def _lines_with_offsets(text: str) -> list[tuple[int, str]]:
    out, offset = [], 0
    for line in text.splitlines(keepends=True):
        out.append((offset, line))
        offset += len(line)
    return out


def _is_declaration(line: str) -> bool:
    words = line.split()
    return bool(words) and words[0] not in NOT_DECLARATION and "(" not in line and bool(DECLARATION.match(line.rstrip("\n")))


def declaration_moves(source: str, tokens: list[Token]) -> list[Rewrite]:
    """Reorder the declarations opening any block: adjacent swaps, and moves to the run's ends.

    Initializers are literals only, so no declaration depends on another's value."""
    lines, found = _lines_with_offsets(source), []
    k = 0
    while k < len(lines):
        if not lines[k][1].rstrip().endswith("{"):
            k += 1
            continue
        run, j = [], k + 1
        while j < len(lines) and _is_declaration(lines[j][1]):
            run.append(j)
            j += 1
        k = j if run else k + 1
        if len(run) < 2:
            continue
        start, end = lines[run[0]][0], lines[run[-1]][0] + len(lines[run[-1]][1])
        block = [lines[r][1] for r in run]
        orders = []
        for a in range(len(block) - 1):
            order = block[:]
            order[a], order[a + 1] = order[a + 1], order[a]
            orders.append((order, f"swap declarations {block[a].strip()} / {block[a + 1].strip()}"))
        for a in range(1, len(block)):
            orders.append(([block[a]] + block[:a] + block[a + 1:], f"move {block[a].strip()} to the top of its block"))
        for a in range(len(block) - 1):
            orders.append((block[:a] + block[a + 1:] + [block[a]], f"move {block[a].strip()} to the end of its block"))
        seen = {"".join(block)}
        for order, label in orders:
            text = "".join(order)
            if text not in seen:
                seen.add(text)
                found.append(Rewrite(start, end, text, label))
    return found


SIMPLE_ASSIGN = re.compile(r"^(?P<indent>[ \t]*)(?P<lhs>[A-Za-z_]\w*)\s*(?:[-+*&|^]|<<|>>)?=(?!=)\s*(?P<rhs>[^;]*);[ \t]*$")


def _names(text: str) -> set[str]:
    """Identifiers read in an expression, leaving out member names after `.`/`->`."""
    masked = re.sub(r"(?:\.|->)\s*[A-Za-z_]\w*", " ", text)
    return set(re.findall(r"\b[A-Za-z_]\w*\b", masked)) - KEYWORDS


def statement_swaps(source: str, tokens: list[Token]) -> list[Rewrite]:
    """Swap two adjacent one-line statements when the order provably cannot matter.

    One of the two must be an assignment to a local whose address is never taken, from an
    expression with no call, assignment or increment. If the other statement is not of that
    kind too, the simple one may read only such locals and constants (no memory through
    `->`, `[`, `.` or `*`), and the two may not share a name the other writes."""
    body = source.find("{")
    signature = source[:body]
    params = set(re.findall(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*[,)]", signature))
    declared = set()
    for match in re.finditer(r"^[ \t]*(?:(?:const|volatile|unsigned|signed|struct|union|enum|register)\s+)*"
                             r"([A-Za-z_]\w*)[\s\*]+([A-Za-z_]\w*)\s*(?:\[|=|;|,)", source[body:], flags=re.MULTILINE):
        if match.group(1) not in KEYWORDS and match.group(1) not in NOT_DECLARATION:
            declared.add(match.group(2))  # `return x;` or `goto out;` declares nothing
    locals_ = (params | declared) - KEYWORDS
    locals_ -= {name for name in locals_ if re.search(rf"&\s*{name}\b", source.replace("&&", "  "))}

    def pure(line: str):
        match = SIMPLE_ASSIGN.match(line.rstrip("\n"))
        if not match or match["lhs"] not in locals_:
            return None
        rhs = match["rhs"]
        if re.search(r"\b[A-Za-z_]\w*\s*\(", re.sub(r"\bsizeof\s*\(", "", rhs)) or re.search(r"(?<![=!<>])=(?!=)|\+\+|--", rhs):
            return None
        memory = bool(re.search(r"->|\[|\.|\*", rhs))
        return match["lhs"], _names(rhs), memory

    lines, found = _lines_with_offsets(source), []
    for k in range(len(lines) - 1):
        (a_at, a), (b_at, b) = lines[k], lines[k + 1]
        if not a.strip().endswith(";") or not b.strip().endswith(";") or _is_declaration(a) or _is_declaration(b):
            continue
        indent_a, indent_b = re.match(r"[ \t]*", a).group(0), re.match(r"[ \t]*", b).group(0)
        if indent_a != indent_b or a.lstrip().startswith(NOT_DECLARATION) or b.lstrip().startswith(NOT_DECLARATION):
            continue
        pa, pb = pure(a), pure(b)
        if pa and pb:
            # Neither writes memory or calls anything: only their own locals can interact.
            ok = pa[0] != pb[0] and pa[0] not in pb[1] and pb[0] not in pa[1]
        elif pa or pb:
            (lhs, reads, memory), other = (pa, b) if pa else (pb, a)
            other_names = _names(other)
            # The simple statement reads only non-address-taken locals (and upper-case constants),
            # and the other statement mentions none of its names, so nothing it does can reach them.
            ok = not memory and reads <= locals_ | {n for n in reads if n.isupper()} \
                and lhs not in other_names and not (reads & other_names)
        else:
            ok = False
        if ok:
            found.append(Rewrite(a_at, b_at + len(b), b + a if b.endswith("\n") else b + "\n" + a.rstrip("\n"),
                                 f"swap statements {a.strip()} / {b.strip()}"))
    return found


GENERATORS = (operand_swaps, zero_tests, increments, compound_assignments, index_forms, loop_forms, branch_swaps,
              declaration_moves, statement_swaps)


def rewrites(function_text: str) -> list[Rewrite]:
    """Every single rewrite of the function body (the signature is never touched)."""
    body = function_text.find("{")
    if body < 0:
        return []
    tokens = [t for t in tokenize(function_text) if t.start > body]
    # Skip declarations of externs and prototypes: their `*` and parameters are not expressions.
    skip, out = set(), []
    for k, token in enumerate(tokens):
        if token.text in ("extern", "typedef"):
            end = k
            while end < len(tokens) and tokens[end].text != ";":
                end += 1
            skip.update(range(tokens[k].start, tokens[min(end, len(tokens) - 1)].end + 1))
    for generator in GENERATORS:
        for rewrite in generator(function_text, tokens):
            if rewrite.start not in skip:
                out.append(rewrite)
    return out
