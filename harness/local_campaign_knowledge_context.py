"""Opt-in evidence retrieval for normal campaign prompts; never a blind benchmark path."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from harness_paths import env
from local_campaign_knowledge import KnowledgeStore, digest


def prompt_context(root: Path, report_file: Path, item: dict, clean_body, budget: int = 4000) -> str:
    """Only when ``DECOMP_KNOWLEDGE_DB`` names a store; prompts never pick one up implicitly."""
    path = env("KNOWLEDGE_DB")
    if not path:
        return ""
    try:
        report_hash = digest(report_file.read_bytes())
        with KnowledgeStore(Path(path), readonly=True) as store:
            snapshot_id = None
            for row in store.connection.execute("SELECT id,provenance_json FROM snapshots ORDER BY created_at DESC,id"):
                provenance = json.loads(row["provenance_json"])
                source_root = provenance.get("source_root")
                if source_root and Path(source_root).resolve() == root.resolve() and provenance["report_sha256"] == report_hash:
                    snapshot_id = row["id"]
                    break
            if snapshot_id is None:
                return ""
            function_id = store.resolve(snapshot_id, item["symbol"], item.get("module", "main"))
            target = store.function(function_id)
            if "address" in item:
                value = str(item["address"])
                if int(value, 16 if value.startswith("0x") else 10) != target["address"]:
                    return ""
            header = ("## Frozen compiler/source evidence\n"
                      "These are report scorer-exact observations, not independent verification certificates.\n"
                      "Similarity does not establish target types, constants, or semantics.\n")
            parts, used = [], len(header)
            for row in store.retrieve(function_id, limit=12):
                body = clean_body(row["symbol"], row["source_path"], budget - used)
                if not body or digest(body) != row["source_sha256"]:
                    continue
                text = (f"### {row['symbol']} ({row['source_path']}; {row['reason']})\n"
                        f"Snapshot: {snapshot_id}; source SHA-256: {row['source_sha256']}\n"
                        f"Compiler observation: {row['toolchain_id']}\n"
                        f"```c\n{body}\n```\n")
                if used + len(text) > budget:
                    continue
                parts.append(text)
                used += len(text)
                if len(parts) >= 3:
                    break
            return header + "\n".join(parts) if parts else ""
    except (OSError, ValueError, KeyError, sqlite3.Error):
        return ""
