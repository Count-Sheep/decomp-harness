"""Immutable local evidence store. Similarity and scorer matches are not verification."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import zlib
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Protocol

from harness_paths import DECOMP_ROOT, PROJECT_ID, env, project

SCHEMA_VERSION = 2
FEATURE_VERSION = "dtk-simindex-pilot-1"
RETRIEVAL_VERSION = "body-registers-production-clones-v6"


def _default_database() -> Path:
    """``DECOMP_KNOWLEDGE_DB``, else the configured project's ``build/decomp-support/knowledge``.

    Snapshots record their own workspace and version, so one store serves every frozen
    workspace of a project; ``DECOMP_ROOT`` pointing at such a workspace does not move it.
    """
    configured = env("KNOWLEDGE_DB")
    if configured:
        return Path(configured).expanduser()
    selected = project(PROJECT_ID)
    root = selected.root if selected else DECOMP_ROOT
    return root / "build" / "decomp-support" / "knowledge" / "evidence.sqlite3"


DEFAULT_DATABASE = _default_database()
OBSERVED_OUTCOMES = {
    "scorer_exact", "non_exact", "compile_error", "baseline_compile_error", "objdiff_error",
    "missing_draft", "missing_compile_command", "missing_function", "no_generated_function",
    "compiled_without_target", "validation_error",
}
UNKNOWN_VERIFICATION = {
    "raw_candidate_bytes": None, "relocations": None, "owner_object": None,
    "linked_checksum": None, "human_review": None, "policy_approved": None,
}


def encoded(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def body_shape(core: list[str]) -> list[str]:
    """Rebase normalized register IDs after frame removal, preserving their aliases."""
    maps = {"G": {}, "F": {}}

    def replace(match):
        kind = match[1]
        mapping = maps[kind]
        return kind + str(mapping.setdefault(match[0], len(mapping)))

    return [re.sub(r"\b([GF])\d+\b", replace, line) for line in core]


def operand_role_shape(core: list[str]) -> list[str]:
    # Struct displacements can differ; arithmetic constants and relocation kinds cannot.
    return [re.sub(r"\b[SMAL]\((G\d+)\)", r"DISP(\1)", line) for line in body_shape(core)]


def idiom_windows(core: list[str]) -> dict[str, int]:
    """Local alias-preserving patterns, not whole-function or semantic matches."""
    operations = {"lwz", "lbz", "lhz", "lha", "lfs", "lfd", "stw", "stb", "sth", "stfs", "stfd",
                  "add", "subf", "mullw", "mulli", "divw", "divwu", "mulhw", "mulhwu",
                  "rlwinm", "rlwinm.", "clrlwi", "clrlwi.", "slwi", "srwi", "srawi",
                  "and", "and.", "or", "xor", "fadds", "fsubs", "fmuls", "fdivs",
                  "fadd", "fsub", "fmul", "fdiv", "fmadds", "fmsubs", "fctiwz"}
    windows = {}
    for index in range(max(0, len(core) - 7)):
        window = core[index:index + 8]
        opcodes = [line.split()[0] for line in window]
        if len(set(opcodes)) >= 4 and sum(op in operations for op in opcodes) >= 4:
            windows.setdefault(digest("\n".join(operand_role_shape(window))), index)
    return windows


def retrieval_terms(features: dict) -> set[str]:
    core = body_shape(features["core"])
    grams = sorted({digest("\n".join(core[i:i + 4])) for i in range(max(0, len(core) - 3))})[:16]
    return ({"gram-v2:" + value for value in grams} | {"call-v1:" + value for value in features["calls"]}
            | {"idiom-v1:" + value for value in sorted(idiom_windows(features["core"]))[:64]})


class Retriever(Protocol):
    def retrieve(self, function_id: str, limit: int = 3,
                 excluded: set[str] | None = None, *, exclude_clone_shapes: bool = True) -> list[dict]: ...


class KnowledgeStore:
    def __init__(self, path: Path, readonly: bool = False):
        self.path = path.resolve()
        self._held_core_cache = {}
        self._idiom_cache = {}
        if readonly:
            self.connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(self.path, timeout=30)
            self.path.chmod(0o600)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        if not readonly:
            self.connection.execute("PRAGMA journal_mode=WAL")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, SCHEMA_VERSION) or readonly and version != SCHEMA_VERSION:
            self.close()
            raise ValueError(f"Unsupported evidence schema: {version}")
        if not readonly:
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS artifacts (
                    sha256 TEXT PRIMARY KEY, content BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    id TEXT PRIMARY KEY, provenance_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS toolchains (
                    id TEXT PRIMARY KEY, inputs_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS functions (
                    id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
                    module TEXT NOT NULL, address INTEGER NOT NULL, symbol TEXT NOT NULL,
                    unit TEXT NOT NULL, source_path TEXT, owner TEXT,
                    toolchain_id TEXT REFERENCES toolchains(id),
                    source_sha256 TEXT REFERENCES artifacts(sha256),
                    assembly_sha256 TEXT NOT NULL REFERENCES artifacts(sha256),
                    bytes_hash TEXT, strict_hash TEXT NOT NULL, shape_hash TEXT NOT NULL,
                    feature_version TEXT NOT NULL, features_json TEXT NOT NULL,
                    insns INTEGER NOT NULL, eligible INTEGER NOT NULL CHECK(eligible IN (0,1)),
                    eligibility_json TEXT NOT NULL,
                    UNIQUE(snapshot_id,module,address)
                );
                CREATE INDEX IF NOT EXISTS functions_bytes ON functions(snapshot_id,bytes_hash);
                CREATE INDEX IF NOT EXISTS functions_shape ON functions(snapshot_id,shape_hash);
                CREATE INDEX IF NOT EXISTS functions_size ON functions(snapshot_id,eligible,insns);
                CREATE TABLE IF NOT EXISTS retrieval_terms (
                    snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
                    function_id TEXT NOT NULL REFERENCES functions(id), term TEXT NOT NULL,
                    PRIMARY KEY(function_id,term)
                );
                CREATE INDEX IF NOT EXISTS terms_lookup ON retrieval_terms(snapshot_id,term,function_id);
                CREATE TABLE IF NOT EXISTS variants (
                    id TEXT PRIMARY KEY, function_id TEXT NOT NULL REFERENCES functions(id),
                    source_sha256 TEXT NOT NULL REFERENCES artifacts(sha256), origin TEXT NOT NULL,
                    parent_id TEXT REFERENCES variants(id), provenance_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observations (
                    id TEXT PRIMARY KEY, variant_id TEXT NOT NULL REFERENCES variants(id),
                    outcome TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)
            self.connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def artifact(self, text: str) -> str:
        key = digest(text)
        self.connection.execute("INSERT OR IGNORE INTO artifacts VALUES (?,?)",
                                (key, zlib.compress(text.encode(), 6)))
        return key

    def text(self, key: str) -> str:
        row = self.connection.execute("SELECT content FROM artifacts WHERE sha256=?", (key,)).fetchone()
        if row is None:
            raise ValueError(f"Missing evidence artifact: {key}")
        value = zlib.decompress(row[0]).decode()
        if digest(value) != key:
            raise ValueError("Evidence artifact hash mismatch")
        return value

    def add_snapshot(self, provenance: dict) -> str:
        key = digest(encoded(provenance))
        self.connection.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?,?)",
                                (key, encoded(provenance), datetime.now(UTC).isoformat()))
        return key

    def add_function(self, snapshot_id: str, row: dict) -> str:
        key = digest(encoded([snapshot_id, row["module"], row["address"]]))
        source_hash = self.artifact(row["source_text"]) if row.get("source_text") else None
        assembly_hash = self.artifact(row["assembly"])
        toolchain = row.get("toolchain")
        toolchain_id = digest(encoded(toolchain)) if toolchain else None
        if toolchain:
            self.connection.execute("INSERT OR IGNORE INTO toolchains VALUES (?,?)",
                                    (toolchain_id, encoded(toolchain)))
        values = (key, snapshot_id, row["module"], row["address"], row["symbol"], row["unit"],
                  row.get("source_path"), row.get("owner"), toolchain_id, source_hash, assembly_hash,
                  row.get("bytes_hash"), row["strict_hash"], row["shape_hash"], FEATURE_VERSION,
                  encoded(row["features"]), row["insns"], int(row.get("eligible", False)),
                  encoded(row.get("eligibility", {})))
        existing = self.connection.execute("SELECT * FROM functions WHERE id=?", (key,)).fetchone()
        if existing and tuple(existing) != values:
            raise ValueError("An immutable function observation changed; import a new snapshot")
        self.connection.execute("INSERT OR IGNORE INTO functions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
        self.connection.executemany("INSERT OR IGNORE INTO retrieval_terms VALUES (?,?,?)",
                                    ((snapshot_id, key, term) for term in retrieval_terms(row["features"])))
        if source_hash:
            variant = self.add_variant(key, row["source_text"], "project_report",
                                       {"snapshot_id": snapshot_id})
            self.add_observation(variant, "scorer_exact" if row.get("pct") == 100 else "non_exact", {
                "score": row.get("pct"), "verification": UNKNOWN_VERIFICATION,
                "reported_complete_unit": row.get("reported_complete", False),
                "policy_screen": row.get("eligibility", {}), "measurement": "dtk_report",
            })
        return key

    def add_variant(self, function_id: str, source: str, origin: str,
                    provenance: dict, parent_id: str | None = None) -> str:
        source_hash = self.artifact(source)
        values = (function_id, source_hash, origin, parent_id, encoded(provenance))
        key = digest(encoded(values))
        if parent_id:
            parent = self.connection.execute("SELECT function_id FROM variants WHERE id=?", (parent_id,)).fetchone()
            if parent is None or parent[0] != function_id:
                raise ValueError("Candidate lineage must remain within its target function")
        self.connection.execute("INSERT OR IGNORE INTO variants VALUES (?,?,?,?,?,?)", (key, *values))
        return key

    def add_observation(self, variant_id: str, outcome: str, evidence: dict) -> str:
        if outcome not in OBSERVED_OUTCOMES:
            raise ValueError("Unknown diagnostic outcome; verification promotion is not supported")
        verification = evidence.get("verification", UNKNOWN_VERIFICATION)
        if set(verification) != set(UNKNOWN_VERIFICATION) or any(value is not None for value in verification.values()):
            raise ValueError("This pilot imports observations, not verification certificates")
        key = digest(encoded([variant_id, outcome, evidence]))
        self.connection.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?)",
                                (key, variant_id, outcome, encoded(evidence), datetime.now(UTC).isoformat()))
        return key

    def function(self, function_id: str) -> dict:
        row = self.connection.execute("SELECT * FROM functions WHERE id=?", (function_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown function observation")
        return dict(row)

    def resolve(self, snapshot_id: str, symbol: str, module: str = "main") -> str:
        rows = self.connection.execute("SELECT id FROM functions WHERE snapshot_id=? AND symbol=? AND module=?",
                                       (snapshot_id, symbol, module)).fetchall()
        if len(rows) != 1:
            raise ValueError("Function identity is missing or ambiguous; specify a snapshot and module")
        return rows[0][0]

    def index_features(self, snapshot_id: str) -> int:
        count = 0
        with self.connection:
            self.connection.execute("DELETE FROM retrieval_terms WHERE snapshot_id=?", (snapshot_id,))
            for row in self.connection.execute("SELECT id,features_json FROM functions WHERE snapshot_id=?", (snapshot_id,)):
                self.connection.executemany("INSERT OR IGNORE INTO retrieval_terms VALUES (?,?,?)",
                    ((snapshot_id, row["id"], term) for term in retrieval_terms(json.loads(row["features_json"]))))
                count += 1
        return count

    def retrieve(self, function_id: str, limit: int = 3,
                 excluded: set[str] | None = None, *, exclude_clone_shapes: bool = True) -> list[dict]:
        target = self.function(function_id)
        features = json.loads(target["features_json"])
        target_core = body_shape(features["core"])
        target_idioms = idiom_windows(features["core"])
        blocked = (excluded or set()) | {function_id}
        held_shapes = set()
        if excluded and exclude_clone_shapes:
            revision = self.connection.execute("PRAGMA data_version").fetchone()[0]
            cache_key = (frozenset(excluded), revision)
            if cache_key not in self._held_core_cache:
                placeholders = ",".join("?" for _ in excluded)
                shapes = set()
                for row in self.connection.execute(
                        f"SELECT features_json FROM functions WHERE id IN ({placeholders})", sorted(excluded)):
                    core = json.loads(row[0])["core"]
                    shapes.update((tuple(body_shape(core)), tuple(operand_role_shape(core))))
                if len(self._held_core_cache) >= 8:
                    self._held_core_cache.clear()
                self._held_core_cache[cache_key] = shapes
            held_shapes = self._held_core_cache[cache_key]
        # Exact hashes are retrieval reasons, never proof of semantic equivalence.
        exact = self.connection.execute(
            "SELECT * FROM functions WHERE snapshot_id=? AND eligible=1 AND feature_version=? "
            "AND (bytes_hash=? OR strict_hash=? OR shape_hash=?) ORDER BY id",
            (target["snapshot_id"], FEATURE_VERSION, target["bytes_hash"], target["strict_hash"], target["shape_hash"]),
        ).fetchall()
        # Apply holdouts before candidate limits so excluded owners cannot crowd out references.
        exclusion = ",".join("?" for _ in blocked)
        blocked_args = sorted(blocked)
        pool = self.connection.execute(
            "SELECT * FROM functions WHERE snapshot_id=? AND eligible=1 AND feature_version=? "
            f"AND id NOT IN ({exclusion}) "
            "AND insns BETWEEN ? AND ? ORDER BY ABS(insns-?),id LIMIT 800",
            (target["snapshot_id"], FEATURE_VERSION, *blocked_args, max(4, int(target["insns"] * .5)),
             int(target["insns"] * 2) + 1, target["insns"]),
        ).fetchall()
        terms = sorted(retrieval_terms(features))
        idiom_pool = []
        if terms:
            placeholders = ",".join("?" for _ in terms)
            related = self.connection.execute(
                "SELECT f.* FROM retrieval_terms t JOIN functions f ON f.id=t.function_id "
                f"WHERE t.snapshot_id=? AND t.term IN ({placeholders}) AND f.eligible=1 "
                f"AND f.id NOT IN ({exclusion}) "
                "AND f.feature_version=? AND f.insns BETWEEN ? AND ? "
                "GROUP BY f.id ORDER BY COUNT(*) DESC,f.id LIMIT 240",
                (target["snapshot_id"], *terms, *blocked_args, FEATURE_VERSION, max(4, int(target["insns"] * .5)),
                 int(target["insns"] * 2) + 1),
            ).fetchall()
            pool = [*related, *pool]
        if target_idioms:
            # Local compiler idioms need not occupy similarly sized whole functions.
            idiom_terms = sorted("idiom-v1:" + key for key in target_idioms)
            placeholders = ",".join("?" for _ in idiom_terms)
            idiom_pool = self.connection.execute(
                "SELECT f.* FROM retrieval_terms t JOIN functions f ON f.id=t.function_id "
                f"WHERE t.snapshot_id=? AND t.term IN ({placeholders}) AND f.eligible=1 "
                f"AND f.id NOT IN ({exclusion}) AND f.feature_version=? "
                "GROUP BY f.id ORDER BY COUNT(*) DESC,f.id LIMIT 240",
                (target["snapshot_id"], *idiom_terms, *blocked_args, FEATURE_VERSION),
            ).fetchall()
        def shortlist(record):
            other = json.loads(record["features_json"])
            a, b = features["opcode_counts"], other["opcode_counts"]
            overlap = sum(min(count, b.get(op, 0)) for op, count in a.items())
            shared = len(set(features["calls"]) & set(other["calls"]))
            return -(overlap / max(1, target["insns"], record["insns"]) + .1 * shared), record["id"]

        pool = list({record["id"]: record for record in pool if record["id"] not in blocked}.values())
        pool = sorted(pool, key=shortlist)[:160]
        rows, seen = [], set()
        for record in [*exact, *pool, *idiom_pool]:
            row = dict(record)
            if row["id"] in blocked or row["id"] in seen:
                continue
            seen.add(row["id"])
            other = json.loads(row["features_json"])
            rebased_core = body_shape(other["core"])
            if tuple(rebased_core) in held_shapes or tuple(operand_role_shape(other["core"])) in held_shapes:
                continue
            original_shape = SequenceMatcher(None, features["core"], other["core"], autojunk=False).ratio()
            rebased_shape = SequenceMatcher(None, target_core, rebased_core, autojunk=False).ratio()
            shape = max(original_shape, rebased_shape)
            left, right = set(features["calls"]), set(other["calls"])
            calls = len(left & right) / len(left | right) if left | right else 0.0
            opcode_overlap = sum(min(count, other["opcode_counts"].get(op, 0))
                                 for op, count in features["opcode_counts"].items()) / \
                             max(1, target["insns"], row["insns"])
            call_context = len(left & right) >= 2 and calls >= .5 and opcode_overlap >= .55
            shared_idioms = sorted(target_idioms.keys() & self._idioms(record).keys())
            idiom = None
            if shared_idioms:
                key = shared_idioms[0]
                idiom = {"window_sha256": key, "instruction_count": 8,
                         "target_core_offset": target_idioms[key],
                         "reference_core_offset": self._idioms(record)[key],
                         "shared_window_count": len(shared_idioms)}
            strict = row["strict_hash"] == target["strict_hash"]
            same_toolchain = bool(target["toolchain_id"]) and row["toolchain_id"] == target["toolchain_id"]
            identical_bytes = bool(target["bytes_hash"]) and row["bytes_hash"] == target["bytes_hash"]
            if min(len(features["core"]), len(other["core"])) < 4 or shape < .65 and not call_context and not idiom:
                continue
            why = ("IDENTICAL_TARGET_BYTES" if identical_bytes else "IDENTICAL_INSTRUCTION_TEXT" if strict
                   else "STRUCTURAL_ANALOGUE" if shape >= .65 else "SHARED_CALLEE_REFERENCE" if call_context
                   else "COMPILER_IDIOM_REFERENCE")
            rows.append({"function_id": row["id"], "snapshot_id": row["snapshot_id"],
                         "symbol": row["symbol"], "module": row["module"], "address": row["address"],
                         "source_path": row["source_path"], "owner": row["owner"],
                         "source_sha256": row["source_sha256"], "toolchain_id": row["toolchain_id"],
                         "reason": why, "retrieval_version": RETRIEVAL_VERSION,
                         "idiom_match": idiom,
                         "score_components": {"instruction_shape": shape, "original_instruction_shape": original_shape,
                         "body_rebased_shape": rebased_shape, "callee_overlap": calls,
                         "shared_callee_count": len(left & right), "opcode_overlap": opcode_overlap,
                         "same_observed_toolchain": same_toolchain,
                         "strict_instruction_identity": strict, "target_byte_identity": identical_bytes},
                         "evidence_class": "REPORTED_SCORER_EXACT", "verification": UNKNOWN_VERIFICATION.copy(),
                         "ranking_score": 2 * identical_bytes + strict + .8 * shape + .2 * calls + .1 * same_toolchain
                                          - (.5 if why == "SHARED_CALLEE_REFERENCE" else
                                             1.0 if why == "COMPILER_IDIOM_REFERENCE" else 0)})
        rows.sort(key=lambda row: (-row["ranking_score"], row["function_id"]))
        for row in rows[:max(0, min(limit, 12))]:
            row["source_text"] = self.text(row["source_sha256"])
            function = self.function(row["function_id"])
            row["assembly"] = self.text(function["assembly_sha256"])
        return rows[:max(0, min(limit, 12))]

    def _idioms(self, row) -> dict[str, int]:
        key = (row["id"], row["features_json"])
        if key not in self._idiom_cache:
            if len(self._idiom_cache) >= 8192:
                self._idiom_cache.clear()
            self._idiom_cache[key] = idiom_windows(json.loads(row["features_json"])["core"])
        return self._idiom_cache[key]

    def stats(self) -> dict:
        counts = {name: self.connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                  for name in ("snapshots", "functions", "variants", "observations", "artifacts", "toolchains", "retrieval_terms")}
        counts["retrieval_eligible"] = self.connection.execute("SELECT COUNT(*) FROM functions WHERE eligible=1").fetchone()[0]
        counts["outcomes"] = dict(self.connection.execute("SELECT outcome,COUNT(*) FROM observations GROUP BY outcome"))
        counts["verified_exact"] = 0
        counts["database_bytes"] = self.path.stat().st_size
        counts["storage_bytes"] = sum(path.stat().st_size for path in
            (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")) if path.exists())
        return counts


def clone_groups(rows: list[dict]) -> dict[str, str]:
    """Group owners and conservative clone families transitively for source holdouts."""
    parents = {row["id"]: row["id"] for row in rows}

    def find(key):
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    seen = {}
    for row in sorted(rows, key=lambda row: row["id"]):
        for field in ("owner", "bytes_hash", "strict_hash", "shape_hash", "source_sha256"):
            value = row.get(field)
            if not value:
                continue
            key = (field, value)
            if key in seen:
                a, b = find(row["id"]), find(seen[key])
                parents[max(a, b)] = min(a, b)
            seen[key] = row["id"]
    return {key: find(key) for key in parents}


def retrieval_pack(store: KnowledgeStore, function_id: str, excluded: set[str] | None = None,
                   budget: int = 6000, limit: int = 3, *, exclude_clone_shapes: bool = True) -> dict:
    target = store.function(function_id)
    references, used = [], 0
    for row in store.retrieve(function_id, limit=12 if limit > 0 else 0, excluded=excluded,
                              exclude_clone_shapes=exclude_clone_shapes):
        source = row["source_text"]
        # A partial source body can teach the wrong idiom; omit it instead of truncating it.
        if len(source) > budget - used:
            continue
        available = min(700, max(0, budget - used - len(source)))
        assembly, excerpt = reference_assembly(store, row, available)
        if row["reason"] == "COMPILER_IDIOM_REFERENCE" and not excerpt:
            continue
        references.append({**row, "assembly": assembly, "assembly_excerpt": excerpt})
        used += len(source) + len(assembly)
        if len(references) >= max(0, min(limit, 12)):
            break
    return {"target_id": function_id, "target_assembly": store.text(target["assembly_sha256"]),
            "references": references, "reference_content_chars": used, "budget": budget,
            "exclude_clone_shapes": exclude_clone_shapes,
            "notice": "Scorer-exact observations, not verification certificates. Similarity does not establish target semantics."}


def full_source_pack(store: KnowledgeStore, function_id: str, excluded: set[str]) -> dict:
    """Production C seeds retain complete bodies and assembly, not prompt excerpts."""
    target = store.function(function_id)
    references = store.retrieve(function_id, limit=12, excluded=excluded, exclude_clone_shapes=False)
    return {"target_id": function_id, "target_assembly": store.text(target["assembly_sha256"]),
            "references": references, "budget": None, "limit": 12, "exclude_clone_shapes": False,
            "reference_content_chars": sum(len(row["source_text"]) + len(row["assembly"]) for row in references),
            "notice": "Production references, including matched clones; not a blind holdout or verification certificate."}


def reference_assembly(store: KnowledgeStore, row: dict, budget: int) -> tuple[str, dict | None]:
    """Include a complete matched window, rather than an unrelated frame prefix."""
    lines = row["assembly"].splitlines()
    match = row.get("idiom_match")
    if row["reason"] == "COMPILER_IDIOM_REFERENCE" and match:
        features = json.loads(store.function(row["function_id"])["features_json"])
        normalized = features.get("normalized", [])
        if len(lines) != len(normalized):
            return "", None
        positions, cursor = [], 0
        for instruction in features["core"]:
            while cursor < len(normalized) and normalized[cursor] != instruction:
                cursor += 1
            if cursor == len(normalized):
                return "", None
            positions.append(cursor)
            cursor += 1
        offset = match["reference_core_offset"]
        first, last = positions[offset], positions[offset + match["instruction_count"] - 1] + 1
        text = "\n".join(lines[first:last])
        if len(text) > budget:
            return "", None
        return text, {"first_instruction": first, "last_instruction_exclusive": last,
                      "full_assembly_sha256": digest(row["assembly"]), "complete_function": False}
    kept, used = [], 0
    for line in lines:
        cost = len(line) + bool(kept)
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    return "\n".join(kept), None
