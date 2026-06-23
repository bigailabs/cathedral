"""The arena runs the REAL audit_lane AuditTarget code (verbatim pinned
invariants, content-addressed) — not a re-port. replay_succeeds is driven by the
actual harness/invariant from code/audit-lane/audit_lane.py.
"""
from __future__ import annotations

from game.arena import replay
from game.arena.engine import ArenaEngine, REPRODUCING_TARGETS


def test_audit_lane_targets_are_wired():
    # if the audit-lane repo is present, its targets must be registered as REAL code
    assert "sn5-hone:exact_match_rate@HEAD" in replay.AUDIT_LANE_TARGETS
    for tid in replay.AUDIT_LANE_TARGETS:
        t = replay.TARGETS[tid]
        assert t.source == "audit_lane"
        assert len(t.code_sha256) == 64          # content-addressed pinned target


def test_real_hone_coverage_inflation_reproduces():
    o = replay.run_replay("sn5-hone:exact_match_rate@HEAD",
                          replay.TARGETS["sn5-hone:exact_match_rate@HEAD"].known_witness)
    assert o.reproduced is True                  # reported_rate > honest_accuracy + 0.05
    assert o.observed["reported_rate"] > o.observed["honest_accuracy"]


def test_real_hone_safe_witness_does_not_reproduce():
    # answering all tasks honestly => reported == honest => no finding
    safe = {"predictions": [{"predicted_output": "a", "expected_output": "a"}] * 3,
            "total_tasks": 3}
    o = replay.run_replay("sn5-hone:exact_match_rate@HEAD", safe)
    assert o.reproduced is False


def test_arena_round_replays_via_audit_lane_code():
    assert "sn5-hone:exact_match_rate@HEAD" in REPRODUCING_TARGETS
    r = ArenaEngine().run(1)
    rows = [t for t in r.replay_theater if t.get("source") == "audit_lane"]
    assert rows                                  # at least one agent replayed real code
    assert any(t["reproduced"] and t["code_sha256"] for t in rows)


def test_bad_witness_against_audit_lane_is_graceful():
    o = replay.run_replay("sn5-hone:exact_match_rate@HEAD", {"predictions": 0, "total_tasks": 0})
    assert o.reproduced is False                 # malformed witness -> no repro, no crash
