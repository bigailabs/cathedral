"""End-to-end smoke test for synthetic_boolean_v1.

Covers the release-gate scenarios from the SAT-lane release plan:

1. Generate one toy satisfiable DIMACS challenge from a fixed seed/tier.
2. Confirm the public payload excludes the planted assignment.
3. Feed the planted assignment through `verify` -> `score`; expect 1.0.
4. Feed a structured `{"assignment": ...}` answer through; expect 1.0.
5. Feed an unsatisfying assignment through; expect 0.0 with
   `unsatisfied_clause`.
6. Feed garbage; expect 0.0 with `missing_answer`.
7. Walk the lifecycle: generated -> active -> scored -> retired -> revealed.
8. Confirm the reveal payload exposes hidden metadata only after RETIRED.
9. Confirm the private sidecar shape can carry a reference to the hidden
   metadata.

The test runs without a live miner, without SSH Hermes, and without
network. It is the pre-Hermes proof that the lane's plug-shape is sound.
"""

from __future__ import annotations

import json

import pytest

from cathedral.lanes.contract import GenerateCtx, Submission
from cathedral.lanes.lifecycle import (
    ChallengeRecord,
    LifecycleState,
    LifecycleTransitionError,
    assert_public_payload_safe,
    transition,
)
from cathedral.lanes.synthetic_boolean_v1 import SyntheticBooleanV1

SEED = 42
TIER = 0
HOTKEY = "5" + "F" * 47
ISSUED = "2026-05-18T00:00:00.000Z"


@pytest.fixture
def lane() -> SyntheticBooleanV1:
    return SyntheticBooleanV1()


def test_generate_produces_public_and_hidden(lane: SyntheticBooleanV1) -> None:
    pub, hid = lane.generate(GenerateCtx(seed=SEED, tier=TIER, issued_at_iso=ISSUED))

    assert pub.task_family == "synthetic_boolean_v1"
    assert pub.schema_version == 1
    assert pub.difficulty_tier == TIER
    assert pub.public_input["format"] == "dimacs_cnf"
    assert "p cnf 10 30" in pub.public_input["dimacs"]
    assert pub.public_input["num_vars"] == 10
    assert pub.public_input["num_clauses"] == 30

    # hidden has the witness and matching task_id
    assert hid.task_id == pub.task_id
    assert "planted_assignment" in hid.hidden_payload
    planted = hid.hidden_payload["planted_assignment"]
    assert set(planted.keys()) == {str(i) for i in range(1, 11)}


def test_public_payload_excludes_planted_assignment(lane: SyntheticBooleanV1) -> None:
    """The serialized wire payload must not leak hidden metadata."""
    pub, hid = lane.generate(GenerateCtx(seed=SEED, tier=TIER, issued_at_iso=ISSUED))
    record = ChallengeRecord(
        public=pub,
        hidden=hid,
        state=LifecycleState.ACTIVE,
        issued_at_iso=ISSUED,
    )
    payload = record.to_public_payload()

    # Direct keys: planted_assignment, generator_version must not be present.
    assert "planted_assignment" not in payload
    assert "generator_version" not in payload
    assert "hidden_payload" not in payload

    # Deep guard: no value from hidden_payload appears in the wire payload.
    assert_public_payload_safe(payload, hid)

    # Sanity: serializing to JSON also produces no leak markers.
    serialized = json.dumps(payload)
    assert "planted_assignment" not in serialized


def test_planted_solver_output_scores_one(lane: SyntheticBooleanV1) -> None:
    pub, hid = lane.generate(GenerateCtx(seed=SEED, tier=TIER, issued_at_iso=ISSUED))
    planted = hid.hidden_payload["planted_assignment"]
    lits = []
    for var in sorted(planted, key=int):
        lits.append(int(var) if planted[var] else -int(var))
    solver_output = "s SATISFIABLE\nv " + " ".join(str(lit) for lit in lits) + " 0\n"

    submission = Submission(
        task_id=pub.task_id, miner_hotkey=HOTKEY, answer={"solver_output": solver_output}
    )
    v = lane.verify(pub, hid, submission)
    s = lane.score(pub, v)
    assert v.parsed_ok is True
    assert s.weighted_score == 1.0
    assert s.rejection_reason is None


def test_structured_assignment_scores_one(lane: SyntheticBooleanV1) -> None:
    pub, hid = lane.generate(GenerateCtx(seed=SEED, tier=TIER, issued_at_iso=ISSUED))
    planted = hid.hidden_payload["planted_assignment"]

    submission = Submission(
        task_id=pub.task_id, miner_hotkey=HOTKEY, answer={"assignment": planted}
    )
    v = lane.verify(pub, hid, submission)
    s = lane.score(pub, v)
    assert s.weighted_score == 1.0


def test_flipped_assignment_scores_zero(lane: SyntheticBooleanV1) -> None:
    """Flip every bit -- the resulting assignment is almost certainly
    NOT a satisfying one of a planted 3-SAT formula with this seed."""
    pub, hid = lane.generate(GenerateCtx(seed=SEED, tier=TIER, issued_at_iso=ISSUED))
    planted = hid.hidden_payload["planted_assignment"]
    flipped = {k: not v for k, v in planted.items()}

    submission = Submission(
        task_id=pub.task_id, miner_hotkey=HOTKEY, answer={"assignment": flipped}
    )
    v = lane.verify(pub, hid, submission)
    s = lane.score(pub, v)
    assert s.weighted_score == 0.0
    assert s.rejection_reason == "unsatisfied_clause"


def test_garbage_answer_scores_zero(lane: SyntheticBooleanV1) -> None:
    pub, hid = lane.generate(GenerateCtx(seed=SEED, tier=TIER, issued_at_iso=ISSUED))
    submission = Submission(
        task_id=pub.task_id, miner_hotkey=HOTKEY, answer={"i_hope_this_works": True}
    )
    v = lane.verify(pub, hid, submission)
    s = lane.score(pub, v)
    assert s.weighted_score == 0.0
    assert s.rejection_reason == "missing_answer"


def test_lifecycle_transitions_walk_to_reveal(lane: SyntheticBooleanV1) -> None:
    pub, hid = lane.generate(GenerateCtx(seed=SEED, tier=TIER, issued_at_iso=ISSUED))
    record = ChallengeRecord(
        public=pub,
        hidden=hid,
        state=LifecycleState.GENERATED,
        issued_at_iso=ISSUED,
    )

    # generated -> active
    state = transition(record.state, LifecycleState.ACTIVE)
    record = record.model_copy(update={"state": state})

    # active -> scored (one scoring round happens)
    state = transition(record.state, LifecycleState.SCORED)
    record = record.model_copy(update={"state": state})

    # scored -> scored is allowed (multiple miners can score against
    # the same active challenge before retirement)
    state = transition(record.state, LifecycleState.SCORED)

    # scored -> retired
    state = transition(record.state, LifecycleState.RETIRED)
    record = record.model_copy(
        update={"state": state, "retired_at_iso": "2026-05-19T00:00:00.000Z"}
    )

    # Public payload still safe in RETIRED
    payload = record.to_public_payload()
    assert "planted_assignment" not in payload
    assert payload["lifecycle_state"] == "retired"

    # reveal_payload not allowed until REVEALED
    with pytest.raises(LifecycleTransitionError):
        record.to_reveal_payload()

    # retired -> revealed
    state = transition(record.state, LifecycleState.REVEALED)
    record = record.model_copy(
        update={"state": state, "revealed_at_iso": "2026-05-26T00:00:00.000Z"}
    )

    reveal = record.to_reveal_payload()
    assert "planted_assignment" in reveal["hidden_payload"]
    assert reveal["generator_version"].startswith("synthetic_boolean_v1/")


def test_illegal_transition_rejected(lane: SyntheticBooleanV1) -> None:
    # active -> revealed is NOT allowed; must go through retired first.
    with pytest.raises(LifecycleTransitionError):
        transition(LifecycleState.ACTIVE, LifecycleState.REVEALED)
    # retired cannot un-retire.
    with pytest.raises(LifecycleTransitionError):
        transition(LifecycleState.RETIRED, LifecycleState.ACTIVE)


def test_sidecar_can_reference_hidden_metadata(lane: SyntheticBooleanV1) -> None:
    """The publisher's private sidecar pairs a signed wire row with the
    hidden metadata for offline re-verification. We don't wire the v3
    sidecar here -- just prove the lane outputs are shaped to carry
    that reference structure cleanly."""
    pub, hid = lane.generate(GenerateCtx(seed=SEED, tier=TIER, issued_at_iso=ISSUED))
    record = ChallengeRecord(
        public=pub,
        hidden=hid,
        state=LifecycleState.SCORED,
        issued_at_iso=ISSUED,
    )

    # Sidecar shape -- this is what bundle_publisher would upload to
    # private storage paired with a scored eval row. The publisher
    # writes the wire row (public-safe) plus this sidecar (private).
    sidecar = {
        "schema": "cathedral.lanes.synthetic_boolean_v1.score_record/1",
        "task_id": pub.task_id,
        "task_family": pub.task_family,
        "difficulty_tier": pub.difficulty_tier,
        "hidden_metadata": hid.model_dump(mode="json"),
        "lifecycle_state": record.state.value,
        "issued_at_iso": record.issued_at_iso,
    }
    # Round-trips as JSON without coercion -- proves Pydantic dumps cleanly.
    serialized = json.dumps(sidecar)
    assert "planted_assignment" in serialized

    # Public wire row, separately, must not carry hidden_metadata.
    wire = record.to_public_payload()
    wire_serialized = json.dumps(wire)
    assert "planted_assignment" not in wire_serialized
    assert "hidden_metadata" not in wire_serialized


def test_verifier_is_pure_python_no_subprocess(lane: SyntheticBooleanV1) -> None:
    """Defensive: verify(...) must never spawn a subprocess. We can't
    easily prove the negative; instead we assert the verifier finishes
    synchronously without yielding to anything async or external."""
    import subprocess
    import unittest.mock

    pub, hid = lane.generate(GenerateCtx(seed=SEED, tier=TIER, issued_at_iso=ISSUED))
    submission = Submission(
        task_id=pub.task_id, miner_hotkey=HOTKEY, answer={"assignment": {"junk": True}}
    )
    with unittest.mock.patch.object(subprocess, "Popen") as popen:
        v = lane.verify(pub, hid, submission)
        lane.score(pub, v)
        popen.assert_not_called()
