"""Freeze the source and compiler workspace used by an m2c experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from harness_paths import DECOMP_ROOT, VERSION
from local_campaign_coordination import Coordinator
from local_campaign_decompile import link_directory, write_json


def copy_paths(version: str | None = None) -> tuple[str, ...]:
    """Inputs an m2c experiment depends on, relative to the project root."""
    version = version or VERSION
    return ("src", "include", "build.ninja", "objdiff.json", f"build/{version}/report.json",
            f"build/{version}/include", f"build/{version}/obj", f"build/{version}/asm",
            "build/tools", "build/compilers")


def fingerprint(root: Path, version: str | None = None) -> str:
    result = hashlib.sha256()
    for name in copy_paths(version):
        path = root / name
        files = sorted(path.rglob("*")) if path.is_dir() else [path]
        for file in files:
            if file.is_file():
                result.update(file.relative_to(root).as_posix().encode())
                result.update(file.read_bytes())
    return result.hexdigest()


def snapshot(root: Path, destination: Path, version: str | None = None) -> Path:
    paths = copy_paths(version)
    destination.mkdir(parents=True, exist_ok=True)
    coordinator = Coordinator(root / "build/local_llm_campaign")
    with coordinator.build("m2c experiment", "Freeze sources, compiler and retail objects"):
        before = fingerprint(root, version)
        folder = Path(tempfile.mkdtemp(prefix="baseline-", dir=destination)).resolve()
        for name in paths:
            source, target = root / name, folder / name
            if not source.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        # Harness imports and local headers can still use the project's helper tools.
        if (root / "tools").is_dir():
            link_directory(folder / "tools", (root / "tools").resolve())
        captured = fingerprint(folder, version)
        after = fingerprint(root, version)
        stable = before == captured == after
        write_json(folder / "snapshot.json", {
            "created_at": datetime.now(UTC).isoformat(), "source_root": str(root.resolve()),
            "workspace": str(folder), "paths": list(paths), "sha256": captured,
            "source_sha256_before": before, "source_sha256_after": after, "stable": stable,
            "version": version or VERSION,
        })
        if not stable:
            raise ValueError(f"Project changed during capture; retained diagnostic snapshot at {folder}")
    return folder


def main() -> int:
    from local_campaign_m2c_report import support_root

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DECOMP_ROOT)
    parser.add_argument("--version", default=VERSION, help="project build version (default: configured)")
    parser.add_argument("--destination", type=Path,
                        help="default: <project>/build/decomp-support/<VERSION>/workspaces")
    args = parser.parse_args()
    if not args.version:
        parser.error("no project version configured; pass --version or set DECOMP_VERSION")
    destination = args.destination or support_root(args.version) / "workspaces"
    print(json.dumps({"workspace": str(snapshot(args.root, destination, args.version))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
