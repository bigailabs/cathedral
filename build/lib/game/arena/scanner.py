"""Cathedral Scanner contract.

This is the Bitsec lesson, translated into Cathedral terms:

    Bitsec:    code -> vulnerability report -> report similarity score
    Cathedral: target -> witness+harness proof -> deterministic replay score

The module deliberately stays small. It defines the request/response shape a
miner-facing scanner or bounty product can use, and the verifier accepts only
proof that reproduces against a pinned replay target. Category, severity, and
prose are metadata; they never create score by themselves.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
import time
from typing import Any

from . import corpus, replay

SCHEMA_TASK = "cathedral.scanner.task.v1"
SCHEMA_SUBMISSION = "cathedral.scanner.submission.v1"
SCHEMA_VERDICT = "cathedral.scanner.verdict.v1"
SCHEMA_LEDGER = "cathedral.scanner.ledger.v1"
SCHEMA_LEADERBOARD = "cathedral.scanner.leaderboard.v1"
SCHEMA_BENCHMARK = "cathedral.scanner.benchmark.v1"
SCHEMA_STATE = "cathedral.scanner.state.v1"
SCHEMA_CONTRACT = "cathedral.scanner.contract.v1"
SCHEMA_ROUTE = "cathedral.scanner.route.v1"
SCHEMA_AUDIT_TRACE = "cathedral.audit_trace.v1"
SCHEMA_AUDIT_TRACE_DATASET = "cathedral.audit_trace_dataset.v1"
SCHEMA_CLAIM = "cathedral.scanner.claim.v1"
SCHEMA_SCAN_REQUEST = "cathedral.scanner.request.v1"
SCHEMA_SCAN_INTAKE = "cathedral.scanner.request_intake.v1"
SCHEMA_FAMILY_TAXONOMY = "cathedral.scanner.family_taxonomy.v1"

FAMILY_NOTES = {
    "A_conservation": (
        "Conservation",
        "Money in must equal money out; no silent value creation or loss.",
    ),
    "B_bounds": (
        "Bounds",
        "Accounting paths must stay inside explicit numeric and economic bounds.",
    ),
    "F_emission": (
        "Emission",
        "Reward or take splits must not distribute more than the available pool.",
    ),
    "G_scoring": (
        "Scoring",
        "Validator scoring math must measure the intended work, not a shortcut.",
    ),
}

BITSEC_CATEGORY_ROUTES: tuple[dict[str, Any], ...] = (
    {
        "category": "incorrect calculation",
        "cathedral_focus": "money-math and accounting mistakes",
        "proof_families": ("A_conservation", "B_bounds"),
    },
    {
        "category": "rounding error",
        "cathedral_focus": "precision loss, fee drift, and dust extraction",
        "proof_families": ("A_conservation", "B_bounds"),
    },
    {
        "category": "arithmetic overflow and underflow vulnerability",
        "cathedral_focus": "bounded integer math and impossible balances",
        "proof_families": ("B_bounds", "A_conservation"),
    },
    {
        "category": "oracle/price manipulation",
        "cathedral_focus": "unsafe external price assumptions and value transfer",
        "proof_families": ("B_bounds", "A_conservation"),
    },
    {
        "category": "governance attacks",
        "cathedral_focus": "reward-policy and control-plane capture",
        "proof_families": ("F_emission", "G_scoring"),
    },
    {
        "category": "frontrunning",
        "cathedral_focus": "ordering-dependent reward or allocation wins",
        "proof_families": ("F_emission", "G_scoring"),
    },
    {
        "category": "weak access control",
        "cathedral_focus": "unauthorized scoring, ownership, or validator control",
        "proof_families": ("G_scoring",),
    },
    {
        "category": "improper input validation",
        "cathedral_focus": "malformed values that bypass validator assumptions",
        "proof_families": ("G_scoring", "B_bounds"),
    },
    {
        "category": "replay attacks/signature malleability",
        "cathedral_focus": "public-answer reuse, copied proofs, and stale receipts",
        "proof_families": ("G_scoring",),
    },
    {
        "category": "bad randomness vulnerability",
        "cathedral_focus": "predictable sampling or validator selection shortcuts",
        "proof_families": ("G_scoring",),
    },
    {
        "category": "reentrancy",
        "cathedral_focus": "state-update ordering that violates accounting invariants",
        "proof_families": ("A_conservation", "B_bounds"),
    },
    {
        "category": "self destruct",
        "cathedral_focus": "liveness and custody failures that need sandbox replay",
        "proof_families": (),
    },
    {
        "category": "uninitialized proxy",
        "cathedral_focus": "deployment-state bugs that need sandbox replay",
        "proof_families": (),
    },
)

CLAIM_CATEGORIES = tuple(route["category"] for route in BITSEC_CATEGORY_ROUTES)


def _sha(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _normalize_claim(claim: Any) -> tuple[dict[str, Any], bool]:
    """Return a hashable claim dict and whether it was structurally valid.

    Claim metadata must never be able to crash verification or create score. If
    a miner sends malformed claim data, preserve a hash/type marker for audit
    and training, then keep scoring tied only to replay gates.
    """

    if not claim:
        return {}, True
    if isinstance(claim, dict):
        return dict(claim), True
    return {
        "schema": SCHEMA_CLAIM,
        "_invalid": True,
        "raw_type": type(claim).__name__,
        "raw_sha256": _sha(claim),
    }, False


@dataclass(frozen=True)
class ScannerTask:
    """A single product-facing audit task.

    `expected_family` is an anti-hallucination gate, not a reward term. The
    rewardable object is the replayable witness for `replay_target_id`.
    """

    task_id: str
    target_netuid: int
    target_name: str
    repo: str
    objective: str
    replay_target_id: str
    expected_family: str
    required_fields: tuple[str, ...]
    nonce: str
    bounty_weight: float = 1.0
    schema: str = SCHEMA_TASK

    def manifest(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "task_id": self.task_id,
            "target": {
                "netuid": self.target_netuid,
                "name": self.target_name,
                "repo": self.repo,
            },
            "objective": self.objective,
            "replay_target_id": self.replay_target_id,
            "expected_family": self.expected_family,
            "required_fields": list(self.required_fields),
            "nonce": self.nonce,
            "bounty_weight": self.bounty_weight,
            "reward_shape": "linear_metric_x_boolean_gate",
            "optional_claim_schema": {
                "schema": SCHEMA_CLAIM,
                "source_lesson": "bitsec_report_shape_cathedral_replay_gate",
                "fields": [
                    "title",
                    "category",
                    "severity",
                    "location",
                    "line_ranges",
                    "impact",
                    "description",
                    "vulnerable_code",
                    "code_to_exploit",
                    "rewritten_code_to_fix_vulnerability",
                    "exploit_summary",
                    "fix_summary",
                ],
                "accepted_categories": list(CLAIM_CATEGORIES),
                "scoring": "metadata_only_replay_required",
            },
        }


@dataclass(frozen=True)
class ScannerRequest:
    """Buyer/operator-facing scan request.

    This is the Bitsec-style product surface: send a repo and objective. The
    Cathedral-specific rule is that intake only routes work. It never scores
    reports until a miner submits a replayable witness against a routed task.
    """

    requester: str
    repo: str
    commit: str
    objective: str
    scope: tuple[str, ...] = ()
    requested_families: tuple[str, ...] = ()
    max_tasks: int = 3
    schema: str = SCHEMA_SCAN_REQUEST

    def manifest(self) -> dict[str, Any]:
        body = {
            "schema": self.schema,
            "requester": self.requester,
            "repo": self.repo,
            "commit": self.commit,
            "objective": self.objective,
            "scope": list(self.scope),
            "requested_families": list(self.requested_families),
            "max_tasks": self.max_tasks,
        }
        body["request_id"] = "req-" + _sha(body)[:16]
        body["scoring"] = "metadata_only_until_routed_to_replay_task"
        return body


@dataclass(frozen=True)
class ScannerSubmission:
    """Miner output for a scanner task.

    A prose report may be included for humans, but it is ignored by the score.
    """

    task_id: str
    miner_hotkey: str
    nonce: str
    proof_family: str
    witness: dict[str, Any] | None
    trace: list[dict[str, Any]] = field(default_factory=list)
    claim: Any = field(default_factory=dict)
    report: str = ""
    schema: str = SCHEMA_SUBMISSION

    def as_artifact(self) -> dict[str, Any]:
        claim, claim_valid = _normalize_claim(self.claim)
        return {
            "schema": self.schema,
            "task_id": self.task_id,
            "miner_hotkey": self.miner_hotkey,
            "nonce": self.nonce,
            "proof_family": self.proof_family,
            "witness": self.witness,
            "trace": self.trace,
            "claim_schema": claim.get("schema", SCHEMA_CLAIM) if claim else SCHEMA_CLAIM,
            "claim": claim,
            "claim_valid": claim_valid,
            "claim_sha256": _sha(claim),
            "report_sha256": _sha(self.report),
        }


@dataclass(frozen=True)
class ScannerVerdict:
    schema: str
    accepted: bool
    score: float
    gates: dict[str, bool]
    reasons: list[str]
    replay_target_id: str
    observed: dict[str, Any]
    artifact_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "accepted": self.accepted,
            "score": self.score,
            "gates": self.gates,
            "reasons": self.reasons,
            "replay_target_id": self.replay_target_id,
            "observed": self.observed,
            "artifact_sha256": self.artifact_sha256,
        }


@lru_cache(maxsize=1)
def _replay_positive_targets() -> tuple[replay.ReplayTarget, ...]:
    """Targets whose bundled witness actually violates the pinned invariant."""

    targets = tuple(
        replay.TARGETS[tid]
        for tid in sorted(replay.TARGETS)
        if replay.run_replay(tid, replay.TARGETS[tid].known_witness).reproduced
    )
    if not targets:
        raise RuntimeError("no replay-positive scanner targets are available")
    return targets


def issue_task(index: int = 0, *, target_index: int | None = None) -> ScannerTask:
    """Issue one deterministic scanner task from the real Cathedral corpus."""

    targets = corpus.load_targets()
    if not targets:
        raise RuntimeError("audit-hunter target corpus is not available")

    replay_targets = _replay_positive_targets()
    proof = replay_targets[index % len(replay_targets)]
    target = targets[target_index if target_index is not None else index % len(targets)]
    body = {
        "target_netuid": target.netuid,
        "target_name": target.name,
        "repo": target.repo,
        "replay_target_id": proof.target_id,
        "family": proof.family,
        "index": index,
    }
    task_id = "scan-" + _sha(body)[:16]
    nonce = _sha(("scanner-nonce", task_id, proof.target_id))[:32]
    return ScannerTask(
        task_id=task_id,
        target_netuid=target.netuid,
        target_name=target.name,
        repo=target.repo,
        objective=(
            f"Produce a witness that violates the pinned invariant: "
            f"{proof.property_desc}"
        ),
        replay_target_id=proof.target_id,
        expected_family=proof.family,
        required_fields=proof.decode,
        nonce=nonce,
        bounty_weight=max(1.0, proof.severity / 5.0),
    )


def benchmark_catalog(limit: int | None = None) -> list[ScannerTask]:
    """Return the deterministic scanner benchmark catalog."""

    n = len(_replay_positive_targets()) if limit is None else limit
    return [issue_task(i) for i in range(n)]


def claim_category_catalog(
    backed_families: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return public vulnerability categories mapped onto Cathedral proof lanes.

    These categories are intake and claim metadata. A category is useful for
    routing, but it does not score until a replay-backed family verifies the
    witness.
    """

    if backed_families is None:
        backed_families = {target.family for target in _replay_positive_targets()}
    categories: list[dict[str, Any]] = []
    for route in BITSEC_CATEGORY_ROUTES:
        proof_families = list(route["proof_families"])
        live_families = [fam for fam in proof_families if fam in backed_families]
        categories.append({
            "category": route["category"],
            "cathedral_focus": route["cathedral_focus"],
            "proof_families": proof_families,
            "live_replay_families": live_families,
            "support_status": (
                "replay_backed" if live_families else "intake_metadata_only"
            ),
            "reward_gate": "deterministic_replay",
            "scoring": "metadata_only_replay_required",
        })
    return categories


def family_taxonomy() -> dict[str, Any]:
    """Return the scanner proof-family taxonomy.

    This is the useful Bitsec lesson without importing report-similarity
    scoring: vulnerability families organize work, but replay remains the
    reward gate.
    """

    rows: dict[str, dict[str, Any]] = {}
    for target in _replay_positive_targets():
        title, description = FAMILY_NOTES.get(
            target.family,
            (target.family, "Pinned replay family from the local audit corpus."),
        )
        row = rows.setdefault(target.family, {
            "family": target.family,
            "title": title,
            "description": description,
            "targets": 0,
            "classes": set(),
            "sources": set(),
            "required_fields": set(),
            "reachable_targets": 0,
            "max_severity": 0,
            "examples": [],
        })
        row["targets"] += 1
        row["classes"].add(target.cls)
        row["sources"].add(target.source)
        row["required_fields"].update(target.decode)
        row["reachable_targets"] += 1 if target.reachable else 0
        row["max_severity"] = max(row["max_severity"], target.severity)
        if len(row["examples"]) < 3:
            row["examples"].append({
                "target_id": target.target_id,
                "property": target.property_desc,
            })

    categories = claim_category_catalog(set(rows))
    categories_by_family: dict[str, list[str]] = {family: [] for family in rows}
    for category in categories:
        for family in category["live_replay_families"]:
            categories_by_family.setdefault(family, []).append(category["category"])

    families = []
    for row in rows.values():
        row["classes"] = sorted(row["classes"])
        row["sources"] = sorted(row["sources"])
        row["required_fields"] = sorted(row["required_fields"])
        row["claim_categories"] = sorted(categories_by_family.get(row["family"], []))
        families.append(row)
    families.sort(key=lambda r: (-r["max_severity"], r["family"]))
    return {
        "schema": SCHEMA_FAMILY_TAXONOMY,
        "count": len(families),
        "families": families,
        "claim_categories": categories,
        "scoring": "family_is_gate_replay_is_score",
        "category_scoring": "claim_category_is_metadata_only",
        "reward_gate": "deterministic_replay",
        "lesson": "Taxonomy organizes targets; category overlap never scores without a replayed witness.",
    }


def task_by_id(task_id: str) -> ScannerTask | None:
    """Return a deterministic scanner task by stable artifact id."""

    if not task_id:
        return None
    return next((task for task in benchmark_catalog(limit=12) if task.task_id == task_id), None)


def _string_list(value: Any) -> tuple[str, ...]:
    """Normalize API list-ish input into deterministic non-empty strings."""

    if value is None:
        return ()
    if isinstance(value, str):
        raw = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = [value]
    return tuple(str(v).strip() for v in raw if str(v).strip())


def scan_request_from_payload(payload: dict[str, Any]) -> ScannerRequest:
    """Build a bounded scan request from untrusted API JSON."""

    try:
        max_tasks = int(payload.get("max_tasks", 3))
    except (TypeError, ValueError):
        max_tasks = 3
    max_tasks = max(1, min(12, max_tasks))
    return ScannerRequest(
        requester=str(payload.get("requester") or "local-operator"),
        repo=str(payload.get("repo") or payload.get("url") or "unknown"),
        commit=str(payload.get("commit") or payload.get("ref") or "HEAD"),
        objective=str(payload.get("objective") or "Find replayable security or incentive bugs."),
        scope=_string_list(payload.get("scope")),
        requested_families=_string_list(payload.get("requested_families") or payload.get("families")),
        max_tasks=max_tasks,
    )


def intake_scan_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Route an organic scan request to replay-backed scanner tasks.

    This is deliberately not a score path. It is an intake/router artifact a
    customer or operator can inspect before miners submit real proof.
    """

    req = scan_request_from_payload(payload)
    tasks = benchmark_catalog(limit=req.max_tasks)
    return {
        "schema": SCHEMA_SCAN_INTAKE,
        "accepted": True,
        "request": req.manifest(),
        "routed_tasks": [t.manifest() for t in tasks],
        "routed_count": len(tasks),
        "ledger_written": False,
        "scored": False,
        "verifier_policy": {
            "reports_score": False,
            "claims_score": False,
            "requires_replay_task": True,
            "score_endpoint": "/api/scanner/submit",
            "dry_run_endpoint": "/api/scanner/replay",
        },
    }


def verify_submission(task: ScannerTask, sub: ScannerSubmission) -> ScannerVerdict:
    """Verify a scanner submission.

    The linear metric is `task.bounty_weight`. The boolean gate is the AND of:
    matching task, fresh nonce, family alignment, complete witness, and real
    replay reproduction.
    """

    reasons: list[str] = []
    gates = {
        "task_matches": sub.task_id == task.task_id,
        "nonce_matches": sub.nonce == task.nonce,
        "family_aligned": sub.proof_family == task.expected_family,
        "decode_map_present": bool(
            sub.witness and all(k in sub.witness for k in task.required_fields)
        ),
        "replay_succeeds": False,
    }

    if not gates["task_matches"]:
        reasons.append("task_mismatch")
    if not gates["nonce_matches"]:
        reasons.append("nonce_mismatch")
    if not gates["family_aligned"]:
        reasons.append("proof_family_mismatch")
    if not gates["decode_map_present"]:
        reasons.append("missing_decode_map")

    outcome = replay.run_replay(task.replay_target_id, sub.witness)
    gates["replay_succeeds"] = outcome.reproduced
    if not outcome.reproduced:
        reasons.append(outcome.reason or "proof_did_not_reproduce")

    accepted = all(gates.values())
    return ScannerVerdict(
        schema=SCHEMA_VERDICT,
        accepted=accepted,
        score=task.bounty_weight if accepted else 0.0,
        gates=gates,
        reasons=reasons,
        replay_target_id=task.replay_target_id,
        observed=outcome.observed,
        artifact_sha256=_sha(sub.as_artifact()),
    )


def example_accepted_submission(task: ScannerTask, miner_hotkey: str = "hk_example") -> ScannerSubmission:
    """A deterministic accepted submission for docs/tests."""

    proof = replay.TARGETS[task.replay_target_id]
    return ScannerSubmission(
        task_id=task.task_id,
        miner_hotkey=miner_hotkey,
        nonce=task.nonce,
        proof_family=task.expected_family,
        witness=dict(proof.known_witness),
        trace=[
            {"tool": "fetch_target", "target": task.target_name},
            {"tool": "encode_invariant", "replay_target_id": task.replay_target_id},
            {"tool": "solve_or_decode_witness", "fields": list(task.required_fields)},
            {"tool": "submit_witness", "artifact": "witness+trace"},
        ],
        claim={
            "schema": SCHEMA_CLAIM,
            "title": "Pinned invariant violation",
            "category": task.expected_family,
            "severity": "high",
            "location": {"target": task.target_name, "replay_target_id": task.replay_target_id},
            "line_ranges": [],
            "impact": "Replay harness reproduces the invariant violation.",
            "description": "The submitted witness drives the pinned model into a bad state.",
            "vulnerable_code": task.replay_target_id,
            "code_to_exploit": json.dumps(proof.known_witness, sort_keys=True),
            "rewritten_code_to_fix_vulnerability": (
                "Harden the invariant guard and keep this witness in CI replay tests."
            ),
            "exploit_summary": "Witness values drive the pinned model into a bad state.",
            "fix_summary": "Harden the invariant guard and replay this witness in CI.",
        },
        report="Human-readable explanation. Ignored by scoring.",
    )


def read_ledger(path: str | Path) -> list[dict[str, Any]]:
    """Read scanner submission ledger entries from JSONL."""

    p = Path(path)
    if not p.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entries.append(json.loads(line))
    return entries


def ledger_gate(verdict: ScannerVerdict, sub: ScannerSubmission,
                entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply stateful local gates that need history.

    The verifier itself remains pure. The ledger gate prevents double-credit for
    the same miner and task, so repeated accepted submissions do not farm score.
    """

    out = verdict.as_dict()
    out["gates"] = dict(out["gates"])
    out["reasons"] = list(out["reasons"])
    already_accepted = any(
        e.get("miner_hotkey") == sub.miner_hotkey
        and e.get("task_id") == sub.task_id
        and e.get("accepted") is True
        for e in entries
    )
    out["gates"]["not_duplicate_credit"] = not already_accepted
    if already_accepted and out["accepted"]:
        out["accepted"] = False
        out["score"] = 0.0
        out["reasons"].append("duplicate_task_credit")
    return out


def append_ledger(path: str | Path, task: ScannerTask, sub: ScannerSubmission,
                  verdict: dict[str, Any]) -> dict[str, Any]:
    """Append one scanner attempt to the local JSONL ledger."""

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    artifact = sub.as_artifact()
    entry = {
        "schema": SCHEMA_LEDGER,
        "created_at": time.time(),
        "task": task.manifest(),
        "task_id": task.task_id,
        "target_netuid": task.target_netuid,
        "target_name": task.target_name,
        "replay_target_id": task.replay_target_id,
        "expected_family": task.expected_family,
        "miner_hotkey": sub.miner_hotkey,
        "accepted": bool(verdict["accepted"]),
        "score": float(verdict["score"]),
        "reasons": list(verdict["reasons"]),
        "gates": dict(verdict["gates"]),
        "artifact_sha256": verdict["artifact_sha256"],
        "claim_sha256": artifact["claim_sha256"],
        "claim_present": bool(artifact["claim"]),
        "claim_valid": bool(artifact["claim_valid"]),
        "verifier": {
            "schema": SCHEMA_VERDICT,
            "accepted": bool(verdict["accepted"]),
            "score": float(verdict["score"]),
            "gates": dict(verdict["gates"]),
            "reasons": list(verdict["reasons"]),
            "replay_target_id": verdict.get("replay_target_id", task.replay_target_id),
            "observed": {},
            "observed_values_exported": False,
            "artifact_sha256": verdict["artifact_sha256"],
        },
    }
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n")
    return entry


def record_submission(path: str | Path, task: ScannerTask,
                      sub: ScannerSubmission) -> dict[str, Any]:
    """Verify, de-duplicate, append, and return the ledger-backed verdict."""

    entries = read_ledger(path)
    verdict = ledger_gate(verify_submission(task, sub), sub, entries)
    entry = append_ledger(path, task, sub, verdict)
    verdict["ledger_entry"] = entry
    return verdict


def public_ledger_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Return a hash/metadata-only ledger row safe for public debug views."""

    out = {
        key: value
        for key, value in entry.items()
        if key not in {"artifact"}
    }
    verifier = dict(out.get("verifier") or {})
    if verifier:
        verifier["observed"] = {}
        verifier["observed_values_exported"] = False
        out["verifier"] = verifier
    out["redaction"] = {
        "artifact_body_exported": False,
        "witness_exported": False,
        "report_body_exported": False,
        "trace_body_exported": False,
        "observed_values_exported": False,
    }
    return out


def _task_manifest_for_entry(entry: dict[str, Any]) -> dict[str, Any]:
    task = entry.get("task")
    if isinstance(task, dict):
        return task
    found = task_by_id(str(entry.get("task_id") or ""))
    if found is not None:
        return found.manifest()
    return {
        "schema": SCHEMA_TASK,
        "task_id": str(entry.get("task_id") or ""),
        "target": {
            "netuid": entry.get("target_netuid"),
            "name": entry.get("target_name") or "",
            "repo": "",
        },
        "objective": "",
        "replay_target_id": entry.get("replay_target_id") or "",
        "expected_family": entry.get("expected_family") or "",
        "required_fields": [],
        "nonce": "",
        "bounty_weight": 0.0,
        "artifact_available": False,
    }


def _artifact_for_entry(entry: dict[str, Any]) -> dict[str, Any]:
    artifact = entry.get("artifact")
    if isinstance(artifact, dict):
        claim = artifact.get("claim") if isinstance(artifact.get("claim"), dict) else {}
        return {
            "schema": artifact.get("schema", SCHEMA_SUBMISSION),
            "task_id": artifact.get("task_id") or entry.get("task_id") or "",
            "miner_hotkey": artifact.get("miner_hotkey") or entry.get("miner_hotkey") or "",
            "nonce_sha256": _sha(artifact.get("nonce") or ""),
            "proof_family": artifact.get("proof_family") or entry.get("expected_family") or "",
            "artifact_sha256": entry.get("artifact_sha256") or _sha(artifact),
            "claim_sha256": entry.get("claim_sha256") or _sha(claim),
            "claim_present": bool(claim or entry.get("claim_present")),
            "claim_valid": bool(entry.get("claim_valid")),
            "artifact_available": True,
            "witness_exported": False,
            "report_body_exported": False,
            "trace_body_exported": False,
        }
    return {
        "schema": SCHEMA_SUBMISSION,
        "task_id": str(entry.get("task_id") or ""),
        "miner_hotkey": str(entry.get("miner_hotkey") or ""),
        "nonce_sha256": "",
        "proof_family": str(entry.get("expected_family") or ""),
        "artifact_sha256": entry.get("artifact_sha256") or "",
        "claim_sha256": entry.get("claim_sha256") or "",
        "claim_present": bool(entry.get("claim_present")),
        "claim_valid": bool(entry.get("claim_valid")),
        "artifact_available": False,
        "witness_exported": False,
        "report_body_exported": False,
        "trace_body_exported": False,
    }


def _verifier_for_entry(entry: dict[str, Any]) -> dict[str, Any]:
    verifier = entry.get("verifier")
    if isinstance(verifier, dict):
        out = dict(verifier)
        out["observed"] = {}
        out["observed_values_exported"] = False
        return out
    return {
        "schema": SCHEMA_VERDICT,
        "accepted": bool(entry.get("accepted")),
        "score": float(entry.get("score") or 0.0),
        "gates": dict(entry.get("gates") or {}),
        "reasons": list(entry.get("reasons") or []),
        "replay_target_id": entry.get("replay_target_id") or "",
        "observed": {},
        "observed_values_exported": False,
        "artifact_sha256": entry.get("artifact_sha256") or "",
    }


def audit_trace_dataset(
    path: str | Path,
    *,
    miner_hotkey: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """Export ledger attempts as replay-labeled training traces.

    The label is the verifier verdict, not human severity. Raw witness, report,
    tool trace, and observed replay values are not exported here; public routes
    can serve this dataset without leaking solved artifacts.
    """

    entries = read_ledger(path)
    if miner_hotkey:
        entries = [
            entry for entry in entries
            if entry.get("miner_hotkey") == miner_hotkey
        ]
    if limit > 0:
        entries = entries[-limit:]

    traces: list[dict[str, Any]] = []
    for entry in entries:
        accepted = bool(entry.get("accepted"))
        verifier = _verifier_for_entry(entry)
        artifact = _artifact_for_entry(entry)
        trace_id = "trace-" + _sha({
            "task_id": entry.get("task_id"),
            "miner_hotkey": entry.get("miner_hotkey"),
            "artifact_sha256": entry.get("artifact_sha256"),
            "accepted": accepted,
            "created_at": entry.get("created_at"),
        })[:16]
        traces.append({
            "schema": SCHEMA_AUDIT_TRACE,
            "trace_id": trace_id,
            "label": "accepted" if accepted else "rejected",
            "training_use": (
                "positive_replay_witness"
                if accepted else "negative_replay_failure"
            ),
            "created_at": entry.get("created_at"),
            "miner_hotkey": entry.get("miner_hotkey") or "",
            "task": _task_manifest_for_entry(entry),
            "artifact": artifact,
            "artifact_sha256": entry.get("artifact_sha256") or "",
            "claim_sha256": entry.get("claim_sha256") or "",
            "verifier": verifier,
            "redaction": {
                "artifact_body_exported": False,
                "witness_exported": False,
                "report_body_exported": False,
                "trace_body_exported": False,
                "observed_values_exported": False,
                "raw_external_code_exported": False,
                "artifact_available": bool(artifact.get("artifact_available")),
            },
        })

    accepted_count = sum(1 for trace in traces if trace["label"] == "accepted")
    return {
        "schema": SCHEMA_AUDIT_TRACE_DATASET,
        "trace_schema": SCHEMA_AUDIT_TRACE,
        "count": len(traces),
        "accepted": accepted_count,
        "rejected": len(traces) - accepted_count,
        "miner_hotkey": miner_hotkey,
        "label_source": "deterministic_replay_verdict",
        "scoring": "accepted replay is positive label; rejected replay is negative label",
        "redaction_policy": (
            "trace rows export labels, hashes, task metadata, and proof-family "
            "metadata only; raw witnesses, reports, trace bodies, and observed "
            "replay values are not exported"
        ),
        "contains_witnesses": False,
        "contains_reports": False,
        "contains_trace_bodies": False,
        "traces": traces,
    }


def leaderboard(path: str | Path) -> dict[str, Any]:
    """Aggregate local scanner ledger into miner rankings."""

    catalog = benchmark_catalog()
    catalog_by_id = {t.task_id: t for t in catalog}
    catalog_task_ids = set(catalog_by_id)
    possible_score = sum(t.bounty_weight for t in catalog)
    family_totals: dict[str, dict[str, Any]] = {}
    for task in catalog:
        fam = family_totals.setdefault(task.expected_family, {
            "family": task.expected_family,
            "tasks": 0,
            "possible_score": 0.0,
        })
        fam["tasks"] += 1
        fam["possible_score"] += task.bounty_weight
    rows: dict[str, dict[str, Any]] = {}
    for e in read_ledger(path):
        hk = e.get("miner_hotkey") or ""
        row = rows.setdefault(hk, {
            "miner_hotkey": hk,
            "score": 0.0,
            "accepted": 0,
            "rejected": 0,
            "benchmark_score": 0.0,
            "tasks": set(),
            "last_artifact_sha256": "",
        })
        if e.get("accepted"):
            score = float(e.get("score") or 0.0)
            row["score"] += score
            if e.get("task_id") in catalog_task_ids:
                row["benchmark_score"] += score
            row["accepted"] += 1
            row["tasks"].add(e.get("task_id"))
        else:
            row["rejected"] += 1
        row["last_artifact_sha256"] = e.get("artifact_sha256") or ""

    ranked = sorted(rows.values(), key=lambda r: (-r["score"], -r["accepted"], r["miner_hotkey"]))
    for i, row in enumerate(ranked, start=1):
        task_ids = row.pop("tasks")
        catalog_kills = len(task_ids.intersection(catalog_task_ids))
        row["rank"] = i
        row["unique_tasks"] = len(task_ids)
        row["benchmark_tasks"] = len(catalog_task_ids)
        row["kills"] = catalog_kills
        family_coverage: list[dict[str, Any]] = []
        for family, total in sorted(family_totals.items()):
            family_tasks = [
                task for task in catalog
                if task.expected_family == family
            ]
            killed_tasks = [
                task for task in family_tasks
                if task.task_id in task_ids
            ]
            score = sum(task.bounty_weight for task in killed_tasks)
            possible = float(total["possible_score"])
            kills = len(killed_tasks)
            family_coverage.append({
                "family": family,
                "kills": kills,
                "tasks": int(total["tasks"]),
                "kill_rate": round(kills / int(total["tasks"]), 6)
                if total["tasks"] else 0.0,
                "score": round(score, 6),
                "possible_score": round(possible, 6),
                "weighted_kill_rate": round(score / possible, 6)
                if possible else 0.0,
            })
        row["covered_families"] = sum(
            1 for family in family_coverage if family["kills"] > 0
        )
        row["family_count"] = len(family_coverage)
        row["family_coverage"] = family_coverage
        row["kill_rate"] = round(
            catalog_kills / len(catalog_task_ids), 6
        ) if catalog_task_ids else 0.0
        row["weighted_kill_rate"] = round(
            row["benchmark_score"] / possible_score, 6
        ) if possible_score else 0.0
        row["benchmark_score"] = round(row["benchmark_score"], 6)
        row["score"] = round(row["score"], 6)
    return {
        "schema": SCHEMA_LEADERBOARD,
        "miners": ranked,
        "count": len(ranked),
        "family_totals": [
            {
                "family": row["family"],
                "tasks": int(row["tasks"]),
                "possible_score": round(float(row["possible_score"]), 6),
            }
            for row in sorted(family_totals.values(), key=lambda r: r["family"])
        ],
    }


def benchmark(path: str | Path) -> dict[str, Any]:
    """Return the live benchmark metric miners should optimize.

    A kill is an accepted replayable witness for a deterministic scanner task.
    Prose, category overlap, and severity claims do not count.
    """

    board = leaderboard(path)
    tasks = benchmark_catalog()
    return {
        "schema": SCHEMA_BENCHMARK,
        "metric": "replay_kill_rate",
        "reward_shape": "linear_metric_x_boolean_gate",
        "linear_metric": "accepted_replayable_task_kills / benchmark_tasks",
        "boolean_gate": "task_matches && nonce_matches && family_aligned && decode_map_present && replay_succeeds",
        "benchmark_tasks": len(tasks),
        "possible_score": round(sum(t.bounty_weight for t in tasks), 6),
        "family_totals": board["family_totals"],
        "miners": board["miners"],
    }


def task_risk(task: ScannerTask) -> int:
    """Risk score mirrored by the game UI for route planning."""

    return min(42, round(8 + len(task.required_fields) * 4 + task.bounty_weight * 8))


def miner_state(path: str | Path, miner_hotkey: str) -> dict[str, Any]:
    """Return ledger-derived state for one miner hotkey."""

    entries = [e for e in read_ledger(path) if e.get("miner_hotkey") == miner_hotkey]
    accepted = [e for e in entries if e.get("accepted")]
    rejected = [e for e in entries if not e.get("accepted")]
    accepted_ids = sorted({str(e.get("task_id")) for e in accepted if e.get("task_id")})
    rejected_ids = sorted({
        str(e.get("task_id")) for e in rejected
        if e.get("task_id") and str(e.get("task_id")) not in accepted_ids
    })
    board = leaderboard(path)
    row = next((m for m in board["miners"] if m["miner_hotkey"] == miner_hotkey), None)
    return {
        "schema": SCHEMA_STATE,
        "miner_hotkey": miner_hotkey,
        "score": round(sum(float(e.get("score") or 0.0) for e in accepted), 6),
        "accepted": len(accepted),
        "rejected": len(rejected),
        "accepted_task_ids": accepted_ids,
        "rejected_task_ids": rejected_ids,
        "attempts": len(entries),
        "rank": row.get("rank") if row else None,
        "leaderboard_row": row,
    }


def contract_status(
    path: str | Path,
    miner_hotkey: str,
    *,
    proof_goal: int = 5,
    family_goal: int | None = None,
) -> dict[str, Any]:
    """Return backend-owned scanner contract progress for one miner.

    The game can render this, but the contract is intentionally computed from
    the ledger and benchmark taxonomy so the client is not the source of truth.
    """

    proof_goal = max(1, int(proof_goal))
    board = leaderboard(path)
    family_count = len(board["family_totals"])
    family_goal = max(1, int(family_goal or min(3, family_count or 3)))
    row = next((m for m in board["miners"] if m["miner_hotkey"] == miner_hotkey), None)
    family_coverage = list(row.get("family_coverage", [])) if row else []
    covered_families = sorted(
        str(family["family"]) for family in family_coverage
        if int(family.get("kills") or 0) > 0
    )
    proofs = int(row.get("accepted") or 0) if row else 0
    family_covered = len(covered_families)
    proof_progress = min(1.0, proofs / proof_goal)
    family_progress = min(1.0, family_covered / family_goal)
    complete = proofs >= proof_goal and family_covered >= family_goal
    return {
        "schema": SCHEMA_CONTRACT,
        "miner_hotkey": miner_hotkey,
        "reward_shape": "linear_metric_x_boolean_gate",
        "linear_metric": "accepted_replayable_task_kills",
        "boolean_gate": "proof_goal_met && family_goal_met",
        "proofs": proofs,
        "proof_goal": proof_goal,
        "family_covered": family_covered,
        "family_goal": family_goal,
        "family_count": family_count,
        "covered_families": covered_families,
        "kill_rate": float(row.get("kill_rate") or 0.0) if row else 0.0,
        "score": float(row.get("score") or 0.0) if row else 0.0,
        "progress": round(((proof_progress + family_progress) / 2) * 100, 2),
        "complete": complete,
        "missing": {
            "proofs": max(0, proof_goal - proofs),
            "families": max(0, family_goal - family_covered),
        },
        "leaderboard_row": row,
    }


def route_recommendation(
    path: str | Path,
    miner_hotkey: str,
    *,
    mode: str = "family",
    limit: int | None = None,
) -> dict[str, Any]:
    """Recommend the next scanner task from verifier-owned state."""

    mode = mode if mode in {"bounty", "safe", "family"} else "family"
    tasks = benchmark_catalog(limit=limit)
    state = miner_state(path, miner_hotkey)
    accepted = set(state["accepted_task_ids"])
    contract = contract_status(path, miner_hotkey)
    covered = set(contract["covered_families"])
    open_tasks = [task for task in tasks if task.task_id not in accepted]
    candidates = [
        task for task in open_tasks
        if mode != "family" or task.expected_family not in covered
    ]
    exhausted_reason = ""
    if not candidates and mode == "family":
        exhausted_reason = "all_live_families_covered"
    elif not candidates:
        exhausted_reason = "no_open_tasks"

    if mode == "safe":
        candidates.sort(key=lambda task: (
            task_risk(task),
            -task.bounty_weight,
            task.task_id,
        ))
    else:
        candidates.sort(key=lambda task: (
            -task.bounty_weight,
            task_risk(task),
            task.task_id,
        ))

    task = candidates[0] if candidates else None
    return {
        "schema": SCHEMA_ROUTE,
        "miner_hotkey": miner_hotkey,
        "mode": mode,
        "task": task.manifest() if task else None,
        "task_id": task.task_id if task else "",
        "risk": task_risk(task) if task else 0,
        "reason": (
            "highest_bounty" if mode == "bounty"
            else "lowest_risk" if mode == "safe"
            else "highest_bounty_uncovered_family"
        ) if task else exhausted_reason,
        "open_tasks": len(open_tasks),
        "candidates": len(candidates),
        "covered_families": sorted(covered),
        "contract_complete": bool(contract["complete"]),
    }
