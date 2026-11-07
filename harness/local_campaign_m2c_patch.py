"""Apply the harness's tested m2c patch without resetting a tool checkout.

Patches are applied byte-exact. They are stored with LF line endings (see ``.gitattributes``);
a checkout that converted them to CRLF is detected and transparently restored, since git
emits patch headers with LF only and an all-CRLF patch can only be a converted copy.
"""

from __future__ import annotations

import argparse
import subprocess
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from harness_paths import REPO_ROOT, tool_path

BASE_REVISION = "708d2d2cb2698f091a92492b328f73b24209f72d"
PATCH_DIR = Path(__file__).parent / "m2c_patches"
PATCH = PATCH_DIR / "0001-ppc-condition-registers.patch"
PATCHES = tuple(sorted(PATCH_DIR.glob("[0-9][0-9][0-9][0-9]-*.patch")))


def default_root() -> Path:
    """The m2c checkout: ``tools.m2c`` / ``DECOMP_M2C``, else ``build/tools/m2c`` in the harness."""
    return tool_path("m2c", REPO_ROOT / "build/tools/m2c")


def _git(directory: Path, *args: str, binary: bool = False) -> subprocess.CompletedProcess:
    # Byte-exact on every host: Git for Windows enables core.autocrlf system-wide, which would
    # rewrite line endings of the preflight copies and of patched files.
    text = {} if binary else {"text": True, "encoding": "utf-8", "errors": "replace"}
    return subprocess.run(["git", "-c", "core.autocrlf=false", "-C", str(directory), *args],
                          capture_output=True, timeout=30, **text)


def _lf_only(data: bytes) -> bytes | None:
    """LF bytes for a patch whose every line was converted to CRLF, else None."""
    lines = data.count(b"\n")
    if lines and data.count(b"\r\n") == lines:
        return data.replace(b"\r\n", b"\n")
    return None


@contextmanager
def patch_files(patches: tuple[Path, ...] | None = None) -> Iterator[list[str]]:
    """Absolute paths of byte-exact patches, restoring LF copies of CRLF-converted ones."""
    patches = PATCHES if patches is None else patches
    with tempfile.TemporaryDirectory(prefix="m2c-patches-") as temporary:
        names = []
        for patch in patches:
            try:
                restored = _lf_only(patch.read_bytes())
            except OSError:
                restored = None
            if restored is None:
                names.append(str(patch.resolve()))
                continue
            copy = Path(temporary) / patch.name
            copy.write_bytes(restored)
            names.append(str(copy))
        yield names


def overlapping_series(root: Path, names: list[str] | None = None) -> bytes | None:
    """Preflight dependent patches on copies, returning one atomic combined diff."""
    if not root.is_dir():
        return None
    if names is None:
        with patch_files() as names:
            return overlapping_series(root, names)

    paths = set()
    for name in names:
        stats = _git(root, "apply", "--numstat", "-z", name)
        if stats.returncode:
            return None
        for entry in stats.stdout.split("\0"):
            if not entry:
                continue
            relative = entry.split("\t", 2)[-1]
            path = Path(relative)
            if path.anchor or ".." in path.parts or not relative:
                return None
            paths.add(path)
    with tempfile.TemporaryDirectory(prefix="m2c-patch-preflight-") as temporary:
        before, after = Path(temporary) / "before", Path(temporary) / "after"
        before.mkdir()
        after.mkdir()
        for path in paths:
            source = root / path
            if source.is_symlink():
                return None
            if source.is_file():
                for directory in (before, after):
                    destination = directory / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
            elif source.exists():
                return None
        for name in reversed(names):
            if _git(after, "apply", "--reverse", "--check", name).returncode == 0:
                if _git(after, "apply", "--reverse", name).returncode:
                    return None
        for name in names:
            if _git(after, "apply", "--check", name).returncode:
                return None
            if _git(after, "apply", name).returncode:
                return None
        # Bytes, not text: universal-newline decoding would rewrite any CR in patched content.
        diff = _git(Path(temporary), "-c", "core.quotepath=false",
                    "diff", "--no-index", "--binary", "before", "after", binary=True)
        if diff.returncode not in (0, 1):
            return None
        lines = []
        for line in diff.stdout.splitlines(keepends=True):
            if line.startswith((b"diff --git ", b"--- ", b"+++ ")):
                line = line.replace(b"a/before/", b"a/").replace(b"a/after/", b"a/")
                line = line.replace(b"b/after/", b"b/").replace(b"b/before/", b"b/")
            lines.append(line)
        return b"".join(lines)


def apply(root: Path, check_only: bool = False) -> str:
    def git(*args: str) -> subprocess.CompletedProcess:
        return _git(root, *args)

    revision = git("rev-parse", "HEAD")
    if revision.returncode or revision.stdout.strip() != BASE_REVISION:
        raise ValueError(f"Patch requires m2c {BASE_REVISION}; checkout was left unchanged")
    with patch_files() as names:
        pending = []
        for name in names:
            ready = git("apply", "--check", name)
            if ready.returncode:
                installed = git("apply", "--reverse", "--check", name)
                if installed.returncode == 0:
                    continue
                combined = overlapping_series(root, names)
                if combined is not None:
                    if not combined:
                        return "already_applied"
                    with tempfile.TemporaryDirectory(prefix="m2c-patch-") as temporary:
                        path = Path(temporary) / "combined.patch"
                        path.write_bytes(combined)
                        checked = git("apply", "--check", str(path))
                        if checked.returncode:
                            raise ValueError("Patch conflicts with this checkout; no files changed:\n" + checked.stderr)
                        if check_only:
                            return "ready"
                        applied = git("apply", str(path))
                        if applied.returncode:
                            raise ValueError("Patch failed:\n" + applied.stderr)
                        return "applied"
                raise ValueError("Patch conflicts with this checkout; no files changed:\n" + ready.stderr
                                 + "\n(An m2c clone made with core.autocrlf=true has CRLF files; "
                                 "re-clone it with `git clone --config core.autocrlf=false`.)")
            pending.append(name)
        if not pending:
            return "already_applied"
        if check_only:
            return "ready"
        result = git("apply", *pending)
        if result.returncode:
            raise ValueError("Patch failed:\n" + result.stderr)
        return "applied"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None,
                        help="m2c checkout (default: tools.m2c / DECOMP_M2C, else build/tools/m2c)")
    parser.add_argument("--check", action="store_true", help="check applicability without changing the checkout")
    args = parser.parse_args()
    try:
        print(apply(args.root or default_root(), args.check))
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
