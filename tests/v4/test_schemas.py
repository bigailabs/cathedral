"""Schema round-trip tests for v4 wire types."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cathedral.v4 import AgentTurn, MinerTrajectory, ValidationPayload


def test_agent_turn_round_trip() -> None:
    turn = AgentTurn(
        turn_index=0,
        tool_called="read_file",
        arguments={"path": "app/calculator.py"},
        system_response="def compute_discount(...): ...",
        duration_ms=12,
    )
    dumped = turn.model_dump(mode="json")
    restored = AgentTurn(**dumped)
    assert restored == turn


def test_agent_turn_has_no_thought_field() -> None:
    """Policy: v4 does NOT capture chain-of-thought. Setting one fails."""
    with pytest.raises(ValidationError):
        AgentTurn(
            turn_index=0,
            tool_called="read_file",
            arguments={},
            system_response="ok",
            duration_ms=1,
            agent_thought="this should not exist",  # type: ignore[call-arg]
        )


def test_agent_turn_requires_duration_ms() -> None:
    with pytest.raises(ValidationError):
        AgentTurn(
            turn_index=0,
            tool_called="read_file",
            arguments={},
            system_response="ok",
        )


def test_miner_trajectory_round_trip() -> None:
    trajectory = MinerTrajectory(
        miner_hotkey="5DfHt...abc",
        model_identifier="echo-mini-v1",
        total_turns=3,
        outcome="SUCCESS",
        trace=[
            AgentTurn(
                turn_index=0,
                tool_called="read_file",
                arguments={"path": "app/calculator.py"},
                system_response="...",
                duration_ms=4,
            ),
            AgentTurn(
                turn_index=1,
                tool_called="write_patch",
                arguments={"diff_string": "--- a/x\n+++ b/x\n"},
                system_response="True",
                duration_ms=8,
            ),
            AgentTurn(
                turn_index=2,
                tool_called="run_local_compile",
                arguments={},
                system_response="exit 0",
                duration_ms=120,
            ),
        ],
    )
    dumped = trajectory.model_dump(mode="json")
    restored = MinerTrajectory(**dumped)
    assert restored == trajectory
    assert len(restored.trace) == 3


def test_validation_payload_round_trip() -> None:
    payload = ValidationPayload(
        task_id="v4t_deadbeef",
        difficulty_tier="bronze",
        language="python",
        injected_fault_type="sign_error_off_by_operator",
        winning_patch="--- a/x\n+++ b/x\n",
        trajectories=[],
        deterministic_hash="0" * 64,
    )
    dumped = payload.model_dump(mode="json")
    restored = ValidationPayload(**dumped)
    assert restored == payload


def test_validation_payload_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ValidationPayload(
            task_id="v4t_x",
            difficulty_tier="b",
            language="python",
            injected_fault_type="x",
            winning_patch="",
            trajectories=[],
            deterministic_hash="0",
            unexpected_field="boom",  # type: ignore[call-arg]
        )
