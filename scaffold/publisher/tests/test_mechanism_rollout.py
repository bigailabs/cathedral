"""Mechanism rollout must not need a lockstep deploy.

A mechanism upgrade ships in three steps: validators take an acceptance set,
the producer flips its manifest, the old id is retired. These tests pin the
property that makes that possible, and the property that stops it widening
into something an evidence producer could abuse.
"""

from __future__ import annotations

import pytest

from scaffold.provenance_audit import MECHANISM_ACCEPTED, MECHANISM_DEFAULT
from scaffold.validator_thin import MECHANISM_BURN_FRACTION


def test_old_pin_accepts_new_manifest() -> None:
    """The rollout window: a v1-pinned validator admits a v2 manifest.

    Without this the producer flip rejects every vector until every validator
    has redeployed.
    """
    assert "validated_supply_v2" in MECHANISM_ACCEPTED["validated_supply_v1"]


def test_new_pin_refuses_downgrade() -> None:
    """Acceptance is forward-only: once pinned to v2, v1 evidence is refused."""
    assert "validated_supply_v1" not in MECHANISM_ACCEPTED["validated_supply_v2"]


def test_every_accepted_mechanism_has_a_burn_contract() -> None:
    """Acceptance must never outrun the burn table.

    Admitting a mechanism with no pinned burn contract would reach the
    ``mechanism ... has no pinned burn contract`` raise at submission time,
    turning a rollout into an outage.
    """
    for pinned, accepted in MECHANISM_ACCEPTED.items():
        assert pinned in MECHANISM_BURN_FRACTION
        for mechanism in accepted:
            assert mechanism in MECHANISM_BURN_FRACTION


@pytest.mark.parametrize("mechanism", sorted(MECHANISM_BURN_FRACTION))
def test_burn_is_ten_percent_for_every_mechanism(mechanism: str) -> None:
    """The fixed burn is the one thing a mechanism bump may never change."""
    assert MECHANISM_BURN_FRACTION[mechanism] == pytest.approx(0.10, abs=1e-12)


def test_default_pin_is_a_known_mechanism() -> None:
    assert MECHANISM_DEFAULT in MECHANISM_ACCEPTED
    assert MECHANISM_DEFAULT in MECHANISM_BURN_FRACTION
