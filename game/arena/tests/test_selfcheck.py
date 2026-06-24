"""The operator self-check consolidates the arena's realness signals into one verdict."""
from __future__ import annotations

from game.arena.selfcheck import selfcheck_report, main


def test_selfcheck_reports_healthy_on_a_real_arena():
    rep = selfcheck_report(out_dir="/nonexistent-so-round-is-skipped")
    assert rep["ok"] is True
    names = {c["name"]: c for c in rep["checks"]}
    # the core realness checks are present and pass
    assert names["replay_is_a_real_gate"]["ok"] is True
    assert names["multi_model_coverage"]["ok"] is True
    assert names["gate_and_anticheat_set"]["ok"] is True
    # multi-model: at least the AMM + root families are wired
    assert len(rep["families"]) >= 2
    assert "R_roundtrip" in rep["families"]            # the root NAV family
    assert not rep["required_failed"]


def test_selfcheck_round_check_is_optional_when_no_round_on_disk():
    rep = selfcheck_report(out_dir="/nonexistent")
    last = next(c for c in rep["checks"] if c["name"] == "last_round_verifies")
    assert last["optional"] is True and last["ok"] is True


def test_selfcheck_main_exits_zero_when_healthy():
    import sys
    argv = sys.argv
    sys.argv = ["selfcheck", "/nonexistent"]
    try:
        assert main() == 0
    finally:
        sys.argv = argv
