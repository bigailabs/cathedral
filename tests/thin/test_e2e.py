from __future__ import annotations

import asyncio

from cathedral_thin.e2e import run_e2e


def test_complete_local_subnet_loop():
    evidence = asyncio.run(run_e2e())
    assert evidence["ok"]
    assert evidence["owner_hosted_services"] == 0
    assert evidence["sybil_no_multiplier"]
    assert evidence["historical_offline_gated"]
    assert evidence["confirmed_after_retry"]
