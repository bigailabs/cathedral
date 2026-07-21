from __future__ import annotations

from types import SimpleNamespace

import pytest

from cathedral_thin.preflight import registration_report


def metagraph():
    return SimpleNamespace(
        uids=[2, 7],
        hotkeys=["miner", "validator"],
        validator_permit=[False, True],
        axons=[
            SimpleNamespace(port=8091, is_serving=True),
            SimpleNamespace(port=0, is_serving=False),
        ],
    )


def test_registration_preflight_covers_miner_and_validator_readiness():
    miner = registration_report(
        metagraph(), hotkey="miner", role="miner", require_serving=True
    )
    validator = registration_report(metagraph(), hotkey="validator", role="validator")
    assert miner == {
        "registered": True,
        "uid": 2,
        "validator_permit": False,
        "axon_serving": True,
        "ready": True,
    }
    assert validator["uid"] == 7
    assert validator["ready"] is True


def test_registration_preflight_fails_closed_for_missing_or_bad_metagraph():
    missing = registration_report(metagraph(), hotkey="unknown", role="miner")
    assert missing["registered"] is False
    assert missing["ready"] is False
    broken = metagraph()
    broken.axons = []
    with pytest.raises(Exception, match="inconsistent"):
        registration_report(broken, hotkey="miner", role="miner")
