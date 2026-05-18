"""v4 wire types -- telemetry envelope for the publisher-side runtime.

These schemas are the contract between the v4 ``CathedralEngine``
(publisher-side) and anything that consumes its signed output (the
validator pull loop, downstream training exporters, the public
read surface).

**Architectural position (REVISED 2026-05-17):**

v4 is a **publisher-side private challenge runtime**. The validator
never executes miner code. It only:

  1. Pulls a v4 signed row from the publisher.
  2. Verifies the signature against the publisher's pinned pubkey.
  3. Records the score for weight computation.

The patch-runner, hidden test execution, and all subprocess work
happens on **publisher infra** under a locked-down unprivileged
worker. v4 schemas describe the row the publisher emits and the
validator verifies -- they do NOT describe anything the validator
executes.

**Policy: no chain-of-thought.** ``AgentTurn`` deliberately does NOT
carry an ``agent_thought`` field. We capture tool calls, file reads,
patches, stdout/stderr, timings, and the final explanation -- never
the model's internal reasoning. This is a policy + data-quality
choice baked into the schema.

These types are deliberately separate from the v3 ``Trajectory`` /
``Receipt`` types in ``cathedral.v3.types``: v4 is the patch-validator
shape; v3 is the publisher-pull generalized agentic substrate. The
two coexist; v4 must NOT import from v3 type definitions.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentTurn(BaseModel):
    """One observation in a miner's multi-turn trajectory.

    Captures the tool the agent chose, the arguments it passed, the
    raw system response, and the wall-clock duration. **Does NOT
    capture chain-of-thought.** That is a deliberate policy choice;
    see the module docstring.
    """

    model_config = ConfigDict(extra="forbid")

    turn_index: int
    tool_called: str = Field(..., description="e.g., read_file, write_patch, run_local_compile")
    arguments: dict[str, Any] = Field(default_factory=dict)
    system_response: str = Field(
        ...,
        description="Raw stdout, stderr, or file payload returned by the system",
    )
    duration_ms: int = Field(
        ...,
        ge=0,
        description="Wall-clock duration of the tool call in milliseconds",
    )


class MinerTrajectory(BaseModel):
    """One miner's full attempt at a v4 task."""

    model_config = ConfigDict(extra="forbid")

    miner_hotkey: str
    model_identifier: str
    total_turns: int
    outcome: str = Field(..., description="SUCCESS or FAILURE")
    trace: list[AgentTurn] = Field(default_factory=list)


class ValidationPayload(BaseModel):
    """The complete publisher-side envelope for one v4 task.

    Holds the task metadata, the canonical winning patch (kept
    private to the publisher until signing -- the validator receives
    it inside the signed row for replay verification), every miner's
    trajectory, and a deterministic hash that binds the envelope
    together for downstream consumers.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    difficulty_tier: str
    language: str
    injected_fault_type: str
    winning_patch: str
    trajectories: list[MinerTrajectory] = Field(default_factory=list)
    deterministic_hash: str


class MinerBundle(BaseModel):
    """The wire-safe broken-state bundle the publisher hands to a miner.

    Carries ONLY the broken-state workspace plus the public task
    descriptors the miner needs to attempt a fix. Deliberately omits
    clean_state, winning_patch, hidden_test_code, and rename maps so
    that a transport that naively serializes this object cannot leak
    the answer to the miner.

    The publisher-internal counterpart is
    ``cathedral.v4.cathedral_engine.PublisherHandle``; engine call
    sites that need both receive them as a 2-tuple from the helper
    ``CathedralEngine.build_bundle_and_handle``.

    Added 2026-05-17 in response to Finding 2 of the PR #133 review:
    the previous ``dict`` return value of ``build_miner_bundle``
    carried both broken_state and clean_state in the same payload.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str
    base_repo: str
    language: str
    difficulty_tier: str
    seed: int
    workspace_files: dict[str, str] = Field(
        ...,
        description="Broken-state file content keyed by repo-relative POSIX path",
    )
    compile_command: list[str] = Field(default_factory=list)
    test_entry_path: str | None = None


__all__ = [
    "AgentTurn",
    "MinerBundle",
    "MinerTrajectory",
    "ValidationPayload",
]
