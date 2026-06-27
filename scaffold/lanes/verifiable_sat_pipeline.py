"""End-to-end verifier for agent-published verifiable SAT work."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .coinbase_oracle import (
    CANONICAL_INVARIANT_ID,
    CoinbaseChallenge,
    attestation_report_data,
    verify_coinbase_sat_assignment,
)
from .clone_replay import run_clone_replay_command
from ..publisher.solver_artifacts import SolverArtifact, verify_solver_artifact


@dataclass(frozen=True)
class VerifiableSatVerdict:
    accepted: bool
    rewardable: bool
    outcome: str
    score: float
    gates: dict[str, bool]
    reasons: list[str] = field(default_factory=list)
    solver: dict[str, Any] = field(default_factory=dict)
    replay: dict[str, Any] = field(default_factory=dict)
    challenge: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "rewardable": self.rewardable,
            "outcome": self.outcome,
            "score": self.score,
            "gates": self.gates,
            "reasons": self.reasons,
            "solver": self.solver,
            "replay": self.replay,
            "challenge": self.challenge,
        }


def verify_coinbase_pipeline(
    challenge: CoinbaseChallenge,
    solver_artifact: SolverArtifact | dict[str, Any],
    *,
    require_attestation: bool = False,
    observed_report_data_hex: str = "",
    expected_report_data_hex: str | None = None,
    require_system_replay: bool = False,
    system_replay_command: str = "",
    allowed_replay_runner_kinds: set[str] | None = None,
    system_replay_timeout_s: float = 30.0,
    allow_unsat_reward: bool = False,
) -> VerifiableSatVerdict:
    """Verify one complete solver discharge for a canonical coinbase challenge."""
    expected_report_data = expected_report_data_hex or attestation_report_data(challenge)
    gates: dict[str, bool] = {
        "canonical_invariant": challenge.invariant_id == CANONICAL_INVARIANT_ID,
        "clause_source_map_present": bool(challenge.clause_source_map.get("sections")),
        "decode_map_present": bool(challenge.decode_map.get("fields")),
        "attestation_bound": (
            not require_attestation
            or observed_report_data_hex == expected_report_data
        ),
    }
    reasons = [name for name, ok in gates.items() if not ok]
    if reasons:
        return _verdict(False, False, "INVALID", 0.0, gates, reasons, challenge)

    solver_check = verify_solver_artifact(challenge.cnf_text, solver_artifact)
    gates["solver_artifact_verified"] = solver_check.ok
    if not solver_check.ok:
        return _verdict(
            False,
            False,
            solver_check.outcome or "INVALID",
            0.0,
            gates,
            [solver_check.rejection_reason or "solver_artifact_invalid"],
            challenge,
            solver=solver_check.to_dict(),
        )

    if solver_check.outcome == "SAT":
        replay = verify_coinbase_sat_assignment(challenge, solver_check.assignment)
        gates["real_replay_verified"] = replay.ok
        gates["ckburn_sat_side"] = challenge.ckb_enabled
        replay_dict = replay.to_dict()
        if require_system_replay and replay.ok:
            clone_replay = run_clone_replay_command(
                command=system_replay_command,
                challenge=challenge,
                sat_replay=replay_dict,
                allowed_runner_kinds=allowed_replay_runner_kinds or {"subtensor_clone_rust_v1"},
                timeout_s=system_replay_timeout_s,
            )
            replay_dict["system_replay"] = clone_replay.to_dict()
            gates["system_replay_verified"] = clone_replay.ok
        else:
            gates["system_replay_verified"] = not require_system_replay
            if require_system_replay:
                replay_dict["system_replay"] = {
                    "ok": False,
                    "reason": "sat_replay_failed_before_system_replay",
                }
        ok = replay.ok and challenge.ckb_enabled and gates["system_replay_verified"]
        reasons = [] if ok else [
            name for name in ("real_replay_verified", "ckburn_sat_side", "system_replay_verified") if not gates[name]
        ]
        return _verdict(
            ok,
            ok,
            "SAT",
            1.0 if ok else 0.0,
            gates,
            reasons,
            challenge,
            solver=solver_check.to_dict(),
            replay=replay_dict,
        )

    if solver_check.outcome == "UNSAT":
        gates["ckburn_unsat_side"] = not challenge.ckb_enabled
        gates["unsat_reward_enabled"] = bool(allow_unsat_reward)
        ok = not challenge.ckb_enabled
        return _verdict(
            ok,
            ok and bool(allow_unsat_reward),
            "UNSAT",
            1.0 if ok and allow_unsat_reward else 0.0,
            gates,
            [] if ok else ["ckburn_unsat_side"],
            challenge,
            solver=solver_check.to_dict(),
        )

    return _verdict(
        False,
        False,
        solver_check.outcome or "INVALID",
        0.0,
        gates,
        ["unsupported_solver_outcome"],
        challenge,
        solver=solver_check.to_dict(),
    )


def _verdict(
    accepted: bool,
    rewardable: bool,
    outcome: str,
    score: float,
    gates: dict[str, bool],
    reasons: list[str],
    challenge: CoinbaseChallenge,
    *,
    solver: dict[str, Any] | None = None,
    replay: dict[str, Any] | None = None,
) -> VerifiableSatVerdict:
    return VerifiableSatVerdict(
        accepted=accepted,
        rewardable=rewardable,
        outcome=outcome,
        score=score,
        gates=dict(gates),
        reasons=list(reasons),
        solver=solver or {},
        replay=replay or {},
        challenge={
            "artifact_sha256": challenge.artifact_sha256,
            "cnf_sha256": challenge.cnf_sha256,
            "mapping_sha256": challenge.mapping_sha256,
            "invariant_id": challenge.invariant_id,
            "width": challenge.width,
            "ckb_enabled": challenge.ckb_enabled,
        },
    )
