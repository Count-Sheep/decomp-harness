"""Fail-closed presentation of existing verification evidence; never an acceptance gate."""

from __future__ import annotations

import hashlib
import json
import math

GATES = (
    ("compile", "Compilation", (), "Compile with the assigned project toolchain"),
    ("scorer", "Scorer Exact", ("compile",), "Measure the complete target function"),
    ("signature", "Required Signature", (), "Verify the required declaration is unchanged"),
    ("policy", "Source Policy", (), "Screen the actual candidate source"),
    ("bytes", "Target Bytes", ("compile",), "Compare complete target instruction bytes"),
    ("relocations", "Relocations", ("compile",), "Verify relocation identities, addends and destinations"),
    ("siblings", "Owner And Siblings", ("compile",), "Audit untouched sibling functions and data"),
    ("link", "Linked Checksum", ("bytes", "relocations", "siblings"), "Link the actual changed owners and verify the checksum"),
    ("project_build", "Full Project Build", ("link",), "Run the project's canonical full build and checksum checks"),
    ("review", "Maintainer Review", ("policy", "signature", "project_build"), "Obtain maintainer source review"),
)


def ledger(measurement: dict, *, candidate_sha256: str | None = None) -> dict:
    verification = measurement.get("verification") or {}
    bound = bool(candidate_sha256 and verification.get("candidate_sha256") == candidate_sha256
                 and measurement.get("draft_sha256", candidate_sha256) == candidate_sha256)
    pct = measurement.get("pct")
    valid_score = type(pct) in (int, float) and math.isfinite(pct) and 0 <= pct <= 100
    compile_ok = measurement.get("compile_ok")
    if compile_ok is None and valid_score:
        # Legacy verification returncode is nonzero for a successfully compiled non-exact function.
        compile_ok = True
    facts = {"compile": compile_ok,
             "scorer": pct == 100 if valid_score else None,
             "signature": measurement.get("signature_preserved") if bound else None,
             "policy": verification.get("policy_screen_passed") if bound else None,
             "bytes": verification.get("raw_candidate_bytes") if bound else None,
             "relocations": verification.get("relocations") if bound else None,
             "siblings": verification.get("owner_object") if bound else None,
             "link": verification.get("linked_checksum") if bound else None,
             # General ready/linked booleans and retail-reference links never imply a full build or review.
             "project_build": None, "review": None}
    gates = []
    states = {}
    for key, title, dependencies, action in GATES:
        value = facts[key]
        state = "passed" if value is True else "failed" if value is False else "pending"
        if state == "passed" and any(states[dep] != "passed" for dep in dependencies):
            state = "blocked"
        states[key] = state
        gates.append({"id": key, "title": title, "state": state, "dependencies": list(dependencies),
                      "next_action": action, "evidence_source": "measurement" if key in {"compile", "scorer"} else
                      "candidate_bound_verification" if bound and value is not None else None})
    output = {"version": 1, "candidate_sha256": candidate_sha256, "verification_bound": bound,
              "gates": gates, "remaining": [row["id"] for row in gates if row["state"] != "passed"],
              "ready": False, "authority": "Presentation only; existing project/source/review gates remain authoritative"}
    output["evidence_sha256"] = hashlib.sha256(json.dumps(measurement, sort_keys=True).encode()).hexdigest()
    return output
