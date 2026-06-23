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
from pathlib import Path
import time
from typing import Any

from . import corpus, replay

SCHEMA_TASK = "cathedral.scanner.task.v1"
SCHEMA_SUBMISSION = "cathedral.scanner.submission.v1"
SCHEMA_VERDICT = "cathedral.scanner.verdict.v1"
SCHEMA_LEDGER = "cathedral.scanner.ledger.v1"
SCHEMA_LEADERBOARD = "cathedral.scanner.leaderboard.v1"
SCHEMA_STATE = "cathedral.scanner.state.v1"


def _sha(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


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
        }


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
    report: str = ""
    schema: str = SCHEMA_SUBMISSION

    def as_artifact(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "task_id": self.task_id,
            "miner_hotkey": self.miner_hotkey,
            "nonce": self.nonce,
            "proof_family": self.proof_family,
            "witness": self.witness,
            "trace": self.trace,
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


def issue_task(index: int = 0, *, target_index: int | None = None) -> ScannerTask:
    """Issue one deterministic scanner task from the real Cathedral corpus."""

    targets = corpus.load_targets()
    if not targets:
        raise RuntimeError("audit-hunter target corpus is not available")

    replay_targets = [replay.TARGETS[tid] for tid in sorted(replay.TARGETS)]
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

    n = len(replay.TARGETS) if limit is None else limit
    return [issue_task(i) for i in range(n)]


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

    rows: dict[str, dict[str, Any]] = {}
    for e in read_ledger(path):
        hk = e.get("miner_hotkey") or ""
        row = rows.setdefault(hk, {
            "miner_hotkey": hk,
            "score": 0.0,
            "accepted": 0,
            "rejected": 0,
            "tasks": set(),
            "last_artifact_sha256": "",
        })
        if e.get("accepted"):
            row["score"] += float(e.get("score") or 0.0)
            row["accepted"] += 1
            row["tasks"].add(e.get("task_id"))
        else:
            row["rejected"] += 1
        row["last_artifact_sha256"] = e.get("artifact_sha256") or ""

    ranked = sorted(rows.values(), key=lambda r: (-r["score"], -r["accepted"], r["miner_hotkey"]))
    for i, row in enumerate(ranked, start=1):
        row["rank"] = i
        row["unique_tasks"] = len(row.pop("tasks"))
        row["score"] = round(row["score"], 6)
    return {"schema": SCHEMA_LEADERBOARD, "miners": ranked, "count": len(ranked)}


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
