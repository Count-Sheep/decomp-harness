#!/usr/bin/env python3
"""Apply the harness's decomp-permuter patches to a permuter checkout, byte-exact.

The patches (``decomp_permuter_patches/``) make upstream decomp-permuter run the work units'
``compile_cmd.json`` argv directly, with no shell per candidate and no bash, and read already
preprocessed ``base.c`` verbatim when no ``cpp`` is installed. Both matter most on Windows.

    python harness/permuter/apply_permuter_patches.py --root PATH [--check]

Clone the checkout with ``git clone --config core.autocrlf=false`` so its files keep LF endings.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PATCH_DIR = Path(__file__).resolve().parent / "decomp_permuter_patches"
PATCHES = tuple(sorted(PATCH_DIR.glob("[0-9][0-9][0-9][0-9]-*.patch")))
BASE_REVISION = "8556c81"


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-c", "core.autocrlf=false", "-C", str(root), *args],
                          capture_output=True, text=True)


def state(root: Path) -> str:
    """``applied``, ``ready`` or ``conflict`` for the whole series."""
    reverse = git(root, "apply", "--check", "--reverse", *map(str, PATCHES))
    if reverse.returncode == 0:
        return "applied"
    forward = git(root, "apply", "--check", *map(str, PATCHES))
    return "ready" if forward.returncode == 0 else "conflict"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, required=True, help="decomp-permuter checkout")
    parser.add_argument("--check", action="store_true", help="report the state without changing files")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not (root / "permuter.py").is_file():
        parser.error(f"{root} is not a decomp-permuter checkout")
    current = state(root)
    if current == "conflict":
        detail = git(root, "apply", "--check", *map(str, PATCHES)).stderr.strip()
        print(f"Patches do not apply to this checkout (tested against {BASE_REVISION}):\n{detail}\n"
              "(A clone made with core.autocrlf=true has CRLF files; re-clone it with "
              "`git clone --config core.autocrlf=false`.)", file=sys.stderr)
        return 1
    if args.check or current == "applied":
        print("already_applied" if current == "applied" else current)
        return 0
    result = git(root, "apply", *map(str, PATCHES))
    if result.returncode:
        print(result.stderr.strip(), file=sys.stderr)
        return 1
    print("applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
