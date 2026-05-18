"""Challenge lifecycle and public/private redaction rules.

This module is **platform code**, not lane code. Lanes are pure
functions of their inputs; the lifecycle state machine is the runtime
that drives them.

## Lifecycle states

A generated challenge moves through these states:

    generated -> active -> scored -> retired -> revealed (optional)

| state     | meaning                                              |
| --------- | ---------------------------------------------------- |
| generated | publisher has called ``TaskFamily.generate``; not    |
|           | yet distributed to miners.                           |
| active    | distributed to one or more miners; accepting         |
|           | submissions.                                         |
| scored    | one or more submissions verified; weights computed.  |
|           | Still active until retired.                          |
| retired   | no longer accepting submissions. Public payload      |
|           | remains visible. Hidden metadata stays private.      |
| revealed  | optional. Hidden metadata published alongside the    |
|           | retired challenge for community auditing or training |
|           | dataset use.                                         |

Hard rules:

* While a challenge is in ``generated``, ``active``, or ``scored``, the
  hidden metadata MUST NOT be exposed on any public surface.
* ``revealed`` is only reachable from ``retired``.
* The transition ``active -> retired`` is one-way for the publisher;
  retirement cannot be undone.
* The reveal decision is per-lane and per-challenge; never automatic.

## Public payload contract

``public_payload`` is the dict shape served to miners over the wire and
served to public read endpoints. It contains:

* ``task_family``
* ``schema_version``
* ``task_id``
* ``difficulty_tier``
* ``public_input``  (the lane-defined miner-visible challenge)
* ``time_limit_seconds``
* ``lifecycle_state``  (one of: active, retired, revealed)

It must NEVER contain hidden_payload, generator state, the planted
witness, or any oracle the verifier uses.

The E2E smoke test asserts ``public_payload(...)`` does not contain any
field name from ``HiddenMetadata.hidden_payload`` for the same task_id.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from cathedral.lanes.contract import HiddenMetadata, PublicProblem


class LifecycleState(str, Enum):
    GENERATED = "generated"
    ACTIVE = "active"
    SCORED = "scored"
    RETIRED = "retired"
    REVEALED = "revealed"


class LifecycleTransitionError(ValueError):
    """Raised when a caller attempts an illegal state transition."""


# Allowed transitions. Anything not in this map is rejected.
_ALLOWED: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.GENERATED: frozenset({LifecycleState.ACTIVE}),
    LifecycleState.ACTIVE: frozenset({LifecycleState.SCORED, LifecycleState.RETIRED}),
    LifecycleState.SCORED: frozenset({LifecycleState.SCORED, LifecycleState.RETIRED}),
    LifecycleState.RETIRED: frozenset({LifecycleState.REVEALED}),
    LifecycleState.REVEALED: frozenset(),
}


def transition(current: LifecycleState, target: LifecycleState) -> LifecycleState:
    """Validate a lifecycle transition. Raises on illegal moves.

    Caller is the publisher orchestrator. The lane itself never sees
    or sets state.
    """
    if target not in _ALLOWED[current]:
        raise LifecycleTransitionError(
            f"cannot transition {current.value} -> {target.value}; "
            f"allowed: {sorted(s.value for s in _ALLOWED[current])}"
        )
    return target


class ChallengeRecord(BaseModel):
    """Publisher-side bookkeeping record for one challenge instance.

    The ``hidden`` field is operator-private. ``to_public_payload``
    elides it; the wire serializer must use that method, never
    ``model_dump`` directly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    public: PublicProblem
    hidden: HiddenMetadata
    state: LifecycleState
    issued_at_iso: str = Field(description="Publisher UTC timestamp at generation.")
    retired_at_iso: str | None = None
    revealed_at_iso: str | None = None

    def to_public_payload(self) -> dict[str, Any]:
        """Project the record to the wire shape served to miners and
        public read surfaces. Strips hidden_payload by construction."""
        return {
            "task_family": self.public.task_family,
            "schema_version": self.public.schema_version,
            "task_id": self.public.task_id,
            "difficulty_tier": self.public.difficulty_tier,
            "public_input": self.public.public_input,
            "time_limit_seconds": self.public.time_limit_seconds,
            "lifecycle_state": self.state.value,
        }

    def to_reveal_payload(self) -> dict[str, Any]:
        """Project the record to a reveal payload. ONLY callable in
        REVEALED state; raises otherwise. This is the single place
        hidden_payload may legally leave the publisher."""
        if self.state is not LifecycleState.REVEALED:
            raise LifecycleTransitionError(
                f"reveal_payload requires REVEALED state, got {self.state.value}"
            )
        public = self.to_public_payload()
        public["hidden_payload"] = self.hidden.hidden_payload
        public["generator_version"] = self.hidden.generator_version
        public["revealed_at_iso"] = self.revealed_at_iso
        return public


def assert_public_payload_safe(
    payload: dict[str, Any],
    hidden: HiddenMetadata,
) -> None:
    """Defensive guard for tests and pre-emit validation.

    Asserts the public payload does not contain any key from the hidden
    metadata's ``hidden_payload`` and that the hidden payload's values
    do not appear under any other key. Useful as a paranoia check in
    the smoke suite and in CI.

    Raises ``LifecycleTransitionError`` if a hidden field leaks.
    """
    hidden_keys = set(hidden.hidden_payload.keys())
    leaked_keys = hidden_keys & payload.keys()
    if leaked_keys:
        raise LifecycleTransitionError(
            f"hidden field(s) present in public payload: {sorted(leaked_keys)}"
        )

    # Deep value scan: catch the case where someone copied a hidden
    # secret (typically a non-trivial string) into public_input.
    #
    # We intentionally limit the value-leak check to strings of length
    # >= 8. Hidden payloads legitimately echo small integers (counts,
    # tier indices, num_vars) into the public payload, so int/float
    # value matching would false-positive constantly. Long opaque
    # strings (witnesses, secrets, blake3 digests) are the realistic
    # leak vector and that's what this catches.
    forbidden_strings: list[str] = []
    for v in hidden.hidden_payload.values():
        if isinstance(v, str) and len(v) >= 8:
            forbidden_strings.append(v)
        elif isinstance(v, (list, tuple)):
            forbidden_strings.extend(x for x in v if isinstance(x, str) and len(x) >= 8)

    def _walk(node: Any) -> bool:
        if isinstance(node, str) and node in forbidden_strings:
            return True
        if isinstance(node, dict):
            return any(_walk(v) for v in node.values())
        if isinstance(node, (list, tuple)):
            return any(_walk(v) for v in node)
        return False

    if forbidden_strings and _walk(payload.get("public_input")):
        raise LifecycleTransitionError(
            "public_input contains a string that also appears in hidden_payload"
        )
