"""Declaration-order search for near-exact functions.

MWCC hands out saved registers largely in the order locals are declared, so a function
whose only remaining differences are register numbers can often be closed by reordering
its declaration block. Reordering declarations is ordinary, semantics-preserving C, and
the policy allows it. This module finds the leading declaration block of a function
and yields its permutations; the runner compiles and scores each one.
"""

from __future__ import annotations

import itertools
import random
import re

# One simple declaration per line: `type name;`, `type *name;`, `type name[N];`, or an
# initializer that is a literal or NULL (moving it cannot reorder side effects).
DECL = re.compile(
    r"^[ \t]*(?:(?:const|volatile|unsigned|signed|struct|union|enum)\s+)*[A-Za-z_]\w*"
    r"(?:\s+[A-Za-z_]\w*)*[\s\*]+[A-Za-z_]\w*(?:\[[^\]]*\])?"
    r"(?:\s*=\s*(?:-?(?:0x[0-9A-Fa-f]+|\d+(?:\.\d*)?f?)|NULL|0))?\s*;[ \t]*(?://[^\n]*)?$"
)
KEYWORDS = ("return", "goto", "if", "while", "for", "switch", "do", "else", "case")


def declaration_block(function_text: str) -> tuple[int, int, list[str]] | None:
    """Offsets and movable locals after optional leading extern/prototype declarations."""
    brace = function_text.find("{")
    if brace < 0:
        return None
    start = function_text.find("\n", brace) + 1
    if start <= 0:
        return None
    lines, offset = [], start
    for line in function_text[start:].splitlines(keepends=True):
        stripped = line.strip()
        if not stripped:
            if lines:
                break
            offset += len(line)
            start = offset
            continue
        # m2c candidates commonly carry function-local externs before the real locals.
        # They are not allocation candidates, but must not hide the movable block.
        if not lines and (stripped.startswith("extern ") or
                          ("(" in stripped and stripped.endswith(";"))):
            offset += len(line)
            start = offset
            continue
        if stripped.split()[0].rstrip("(") in KEYWORDS or "(" in stripped or not DECL.match(line.rstrip("\n")):
            break
        lines.append(line)
        offset += len(line)
    if len(lines) < 2:
        return None
    return start, start + sum(len(line) for line in lines), lines


def orderings(lines: list[str], cap: int = 720, seed: int = 0):
    """Distinct reorderings of the block, original order excluded; exhaustive up to `cap`."""
    count = 1
    for n in range(2, len(lines) + 1):
        count *= n
    original = tuple(lines)
    if count <= cap + 1:
        for order in itertools.permutations(lines):
            if order != original:
                yield list(order)
        return
    rng, seen = random.Random(seed), {original}
    while len(seen) <= cap:
        order = lines[:]
        rng.shuffle(order)
        key = tuple(order)
        if key not in seen:
            seen.add(key)
            yield order
