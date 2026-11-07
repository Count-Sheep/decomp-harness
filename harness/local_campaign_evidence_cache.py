"""Bounded process-local scratch evidence cache, invalidated by input contents."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
from pathlib import Path


class EvidenceCache:
    def __init__(self, limit=16):
        self.limit = limit
        self.entries = OrderedDict()

    def get(self, key):
        if key not in self.entries:
            return None
        self.entries.move_to_end(key)
        return json.loads(self.entries[key])

    def put(self, key, value):
        self.entries[key] = json.dumps(value)
        self.entries.move_to_end(key)
        while len(self.entries) > self.limit:
            self.entries.popitem(last=False)


class ReadAhead:
    """One bounded read-only preparation job; callers validate its input key."""

    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="campaign-read-ahead")
        self.key = None
        self.future = None

    def submit(self, key, prepare, *args):
        if key == self.key or self.future is not None and not self.future.done():
            return
        self.key = key
        self.future = self.executor.submit(prepare, *args)

    def take(self, key):
        if key != self.key or self.future is None or not self.future.done():
            return None
        try:
            return self.future.result()
        except (OSError, ValueError, KeyError):
            return None


def fingerprint(root: Path, command: list[str], target: Path, diff_tool: Path, symbol: str,
                version: str | None = None):
    """Hash the project source trees and tools; missing inputs disable caching.

    ``version`` (default: the selected project's) names the ``build/<VERSION>/include`` tree."""
    if version is None:
        from harness_paths import VERSION as version
    paths = {target, diff_tool, root / "build.ninja", root / "objdiff.json"}
    generated = [root / "build" / version / "include"] if version else []
    for directory in (root / "src", root / "include", *generated):
        if directory.is_dir():
            paths.update(path for path in directory.rglob("*") if path.is_file())
    for arg in command:
        path = root / arg
        if path.is_file():
            paths.add(path)
    for index, arg in enumerate(command):
        if arg == "-i" and index + 1 < len(command):
            directory = (root / command[index + 1]).resolve()
            if not directory.is_relative_to(root.resolve()) or not directory.is_dir():
                return None
            paths.update(path for path in directory.rglob("*") if path.is_file())
    # A wrapper's contents alone do not identify its compiler implementation.
    compilers = root / "build/compilers"
    if not compilers.is_dir():
        return None
    paths.update(path for path in compilers.rglob("*") if path.is_file())
    digest = hashlib.sha256(json.dumps([str(root.resolve()), command, symbol]).encode())
    try:
        for path in sorted(paths):
            digest.update(str(path.resolve()).encode())
            with path.open("rb") as handle:
                digest.update(hashlib.file_digest(handle, "sha256").digest())
    except OSError:
        return None
    return digest.hexdigest()
