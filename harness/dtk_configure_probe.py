#!/usr/bin/env python3
"""Print the matching objects a dtk-template ``configure.py`` declares, without a disc image.

Run inside a dtk-template checkout:

    python path/to/dtk_configure_probe.py [CONFIGURE_ARGS...]

``configure.py`` is executed with ``tools.project.generate_build`` replaced by a capture, so it
writes nothing. For every object marked complete (``Matching``) the probe prints one JSON line
with its source path, MWCC version, flags and Shift-JIS setting, resolved exactly as dtk-template
resolves them for the real build. ``reference_corpus.py`` uses this for ``"kind": "dtk"``.
"""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from typing import Any


def _flags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return value.split()
    return [str(item) for item in value]


def main() -> int:
    root = Path.cwd()
    sys.path.insert(0, str(root))
    import tools.project as project  # the corpus's own dtk-template module

    captured: dict[str, Any] = {}

    def capture(config: Any, *args: Any, **kwargs: Any) -> None:
        captured["config"] = config

    project.generate_build = capture
    sys.argv = ["configure.py", *sys.argv[1:]]
    try:
        runpy.run_path(str(root / "configure.py"), run_name="__main__")
    except SystemExit as error:
        if error.code not in (None, 0) and "config" not in captured:
            raise
    config = captured.get("config")
    if config is None:
        print("configure.py did not reach generate_build", file=sys.stderr)
        return 2
    count = 0
    for lib in getattr(config, "libs", None) or []:
        for obj in lib.get("objects", []):
            if getattr(obj, "completed", False) is not True:
                continue
            if hasattr(obj, "resolve"):
                resolved = obj.resolve(config, lib)
                options = resolved.options
                source = getattr(resolved, "src_path", None) or \
                    Path(options.get("src_dir") or config.src_dir) / options.get("source", obj.name)
            else:  # older dtk-template: merge library options by hand
                options = {**lib, **{k: v for k, v in obj.options.items() if v is not None}}
                source = Path(options.get("src_dir") or getattr(config, "src_dir", "src")) / \
                    options.get("source", obj.name)
            version = options.get("mw_version") or getattr(config, "linker_version", None)
            if not version or not str(source).lower().endswith((".c", ".cpp", ".cp", ".cc")):
                continue
            print(json.dumps({
                "source": Path(source).as_posix(),
                "mw_version": str(version).replace("\\", "/"),
                "cflags": _flags(options.get("cflags")) + _flags(options.get("extra_cflags")),
                "shift_jis": bool(options.get("shift_jis")),
            }))
            count += 1
    print(f"{count} matching objects", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
