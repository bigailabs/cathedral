"""Operator-run Subtensor clone replay receipt checks for verifiable SAT."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import shlex
import subprocess
from typing import Any

from .coinbase_oracle import CoinbaseChallenge


REPLAY_REQUEST_SCHEMA = "cathedral.subtensor_clone_replay_request.v1"
REPLAY_RECEIPT_SCHEMA = "cathedral.subtensor_clone_replay_receipt.v1"


@dataclass(frozen=True)
class CloneReplayCheck:
    ok: bool
    reason: str
    receipt: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "receipt": self.receipt,
            "details": self.details,
        }


def run_clone_replay_command(
    *,
    command: str,
    challenge: CoinbaseChallenge,
    sat_replay: dict[str, Any],
    allowed_runner_kinds: set[str],
    timeout_s: float = 30.0,
) -> CloneReplayCheck:
    """Run an operator-configured clone replay command and verify its receipt.

    The command is server-side configuration, never miner input. It receives one
    JSON request on stdin and must print one JSON receipt to stdout.
    """
    if not command.strip():
        return CloneReplayCheck(False, "clone_replay_command_not_configured")
    public_artifact = challenge.to_public_artifact()
    public_artifact["artifact_sha256"] = challenge.artifact_sha256
    request = {
        "schema_version": REPLAY_REQUEST_SCHEMA,
        "challenge": public_artifact,
        "decoded": dict(sat_replay.get("decoded") or {}),
        "observed": dict(sat_replay.get("observed") or {}),
        "invariant_id": challenge.invariant_id,
        "source_target": challenge.provenance.get("source_target", ""),
    }
    try:
        proc = subprocess.run(
            shlex.split(command, posix=True),
            input=json.dumps(request, sort_keys=True),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CloneReplayCheck(False, "clone_replay_timeout")
    except OSError as exc:
        return CloneReplayCheck(False, "clone_replay_command_failed", details={"error": str(exc)})
    if proc.returncode != 0:
        return CloneReplayCheck(
            False,
            "clone_replay_nonzero_exit",
            details={"returncode": proc.returncode, "stderr": proc.stderr[-2000:]},
        )
    try:
        receipt = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return CloneReplayCheck(False, "clone_replay_bad_json", details={"stdout": proc.stdout[-2000:]})
    if not isinstance(receipt, dict):
        return CloneReplayCheck(False, "clone_replay_receipt_not_object")
    return verify_clone_replay_receipt(
        challenge=challenge,
        sat_replay=sat_replay,
        receipt=receipt,
        allowed_runner_kinds=allowed_runner_kinds,
    )


def verify_clone_replay_receipt(
    *,
    challenge: CoinbaseChallenge,
    sat_replay: dict[str, Any],
    receipt: dict[str, Any],
    allowed_runner_kinds: set[str],
) -> CloneReplayCheck:
    if receipt.get("schema_version") != REPLAY_RECEIPT_SCHEMA:
        return CloneReplayCheck(False, "clone_replay_schema_mismatch", receipt=receipt)
    runner_kind = str(receipt.get("runner_kind") or "")
    if runner_kind not in allowed_runner_kinds:
        return CloneReplayCheck(
            False,
            "clone_replay_runner_not_allowed",
            receipt=receipt,
            details={"runner_kind": runner_kind, "allowed_runner_kinds": sorted(allowed_runner_kinds)},
        )
    expected = {
        "invariant_id": challenge.invariant_id,
        "source_target": str(challenge.provenance.get("source_target") or ""),
        "challenge_artifact_sha256": challenge.artifact_sha256,
        "cnf_sha256": challenge.cnf_sha256,
        "decode_map_sha256": challenge.to_public_artifact()["decode_map_sha256"],
        "clause_source_map_sha256": challenge.mapping_sha256,
    }
    for key, value in expected.items():
        if str(receipt.get(key) or "") != str(value):
            return CloneReplayCheck(
                False,
                f"clone_replay_{key}_mismatch",
                receipt=receipt,
                details={"expected": value, "actual": receipt.get(key)},
            )
    decoded = dict(sat_replay.get("decoded") or {})
    observed = dict(sat_replay.get("observed") or {})
    if dict(receipt.get("decoded") or {}) != decoded:
        return CloneReplayCheck(False, "clone_replay_decoded_mismatch", receipt=receipt)
    observed_receipt = dict(receipt.get("observed") or {})
    for key in ("parent_emission", "burn_take", "child_take", "after_burn", "parent_left", "total_extracted", "excess", "violation"):
        if observed_receipt.get(key) != observed.get(key):
            return CloneReplayCheck(
                False,
                f"clone_replay_observed_{key}_mismatch",
                receipt=receipt,
                details={"expected": observed.get(key), "actual": observed_receipt.get(key)},
            )
    if receipt.get("accepted") is not True:
        return CloneReplayCheck(False, "clone_replay_not_accepted", receipt=receipt)
    if receipt.get("invariant_broken") is not True:
        return CloneReplayCheck(False, "clone_replay_invariant_not_broken", receipt=receipt)
    if not str(receipt.get("target_commit") or ""):
        return CloneReplayCheck(False, "clone_replay_target_commit_missing", receipt=receipt)
    return CloneReplayCheck(True, "accepted_replay_receipt", receipt=receipt)
