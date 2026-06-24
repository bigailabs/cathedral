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
SCHEMA_CLAIM = "cathedral.scanner.claim.v1"
SCHEMA_SCAN_REQUEST = "cathedral.scanner.request.v1"
SCHEMA_SCAN_INTAKE = "cathedral.scanner.request_intake.v1"


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
                "fields": [
                    "title",
                    "category",
                    "severity",
                    "location",
                    "impact",
                    "exploit_summary",
                    "fix_summary",
                ],
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
            "impact": "Replay harness reproduces the invariant violation.",
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


def leaderboard(path: str | Path) -> dict[str, Any]:
    """Aggregate local scanner ledger into miner rankings."""

    catalog = benchmark_catalog()
    catalog_task_ids = {t.task_id for t in catalog}
    possible_score = sum(t.bounty_weight for t in catalog)
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
        row["kill_rate"] = round(
            catalog_kills / len(catalog_task_ids), 6
        ) if catalog_task_ids else 0.0
        row["weighted_kill_rate"] = round(
            row["benchmark_score"] / possible_score, 6
        ) if possible_score else 0.0
        row["benchmark_score"] = round(row["benchmark_score"], 6)
        row["score"] = round(row["score"], 6)
    return {"schema": SCHEMA_LEADERBOARD, "miners": ranked, "count": len(ranked)}


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
        "miners": board["miners"],
    }


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
