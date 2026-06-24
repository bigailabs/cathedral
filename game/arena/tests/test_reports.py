"""Deliverable reports - the score report carries a full self-auditing verification
block, and the anti-cheat report maps every rejection to its gate + the 12-axis
taxonomy. The reports are comprehensive, independently meaningful artifacts.
"""
from __future__ import annotations

import json
import sys

from game.arena import reports
from game.arena import __main__ as arena_main
from game.arena.engine import ArenaEngine
from game.arena.models import GateOutcome


def test_score_report_carries_a_verification_block():
    eng = ArenaEngine(); r = eng.run(1)
    sr = reports.score_report(r, eng.roster)
    assert sr["reward_rule"] == "reward = linear_metric x boolean_gate"
    v = sr["verification"]
    # the scoring rule self-audits clean
    assert v["scoring_audit"]["ok"] is True
    # the round commitment is the Merkle anchor root
    assert v["anchor_merkle_root"] == r.anchor["merkle_root"]
    assert v["attestation"]["round_commitment"] == r.anchor["merkle_root"]
    # the real audit vault + minted families are summarized
    assert isinstance(v["real_audit_vault"], list)
    # every agent row has the full gate set
    for a in sr["agents"]:
        assert set(a["gates"]) == set(GateOutcome.GATES)
    # the report is JSON-serializable (a real deliverable file)
    json.dumps(sr, default=str)


def test_metric_breakdown_makes_the_reward_rule_concrete():
    """Every agent row shows reward = linear_metric x boolean_gate broken out: a
    cheater's gate is 0 -> contrib 0 (no matter how fast); an honest agent's contrib
    is exactly tier_weight x speed."""
    eng = ArenaEngine(); r = eng.run(1)
    sr = reports.score_report(r, eng.roster)
    for row in sr["agents"]:
        mb = row["metric_breakdown"]
        expected = mb["boolean_gate"] * mb["tier_weight"] * mb["speed"]
        # product identity holds within 4-decimal rounding of the displayed factors
        assert abs(mb["linear_metric_contrib"] - expected) < 1e-3
        if row["passed"]:
            assert mb["boolean_gate"] == 1.0 and mb["linear_metric_contrib"] > 0
        else:
            assert mb["boolean_gate"] == 0.0 and mb["linear_metric_contrib"] == 0.0  # gate zeroes it
    assert any(row["metric_breakdown"]["linear_metric_contrib"] > 0 for row in sr["agents"])


def test_score_report_attestation_reflects_real_quote_if_present():
    eng = ArenaEngine(); r = eng.run(1)
    att = reports.score_report(r, eng.roster)["verification"]["attestation"]
    # if a real Intel-verified quote is on file, the report shows it re-verified
    if att["instance"]:
        assert att["live_quote_reverified"] is True
        assert att["binding_reverified_locally"] is True and att["intel_verified"] is True


def test_trace_rows_are_a_labeled_training_dataset():
    """traces become training data: each row carries the real agent operation as
    FEATURES (commands, files, hypothesis, gates, method) and SUPERVISED LABELS
    (archetype, label honest|cheat, cheat_type, passed, rejected_by_gate) so a
    verifier-classifier can be trained to detect cheating from a trace."""
    eng = ArenaEngine(); r = eng.run(1)
    arch = {s.hotkey: s.archetype for s in eng.roster}
    rows = [reports.trace_training_row(a, r.weights.get(a.run.miner_hotkey, 0.0),
                                       arch.get(a.run.miner_hotkey, "honest"), 1)
            for a in r.agents]
    # features present
    for row in rows:
        assert row["commands"] and "gates" in row and row["method"]
    # labels present + internally consistent
    for row in rows:
        assert row["label"] in ("honest", "cheat")
        assert row["outcome_label"] in {
            "accepted_honest", "rejected_cheat", "sybil_collapsed", "accepted_cheat",
        }
        assert (row["label"] == "honest") == (row["cheat_type"] is None)
        if row["label"] == "honest":
            assert row["passed"] is True and row["rejected_by_gate"] is None
            assert row["caught_by"] == "n/a"
            assert row["outcome_label"] == "accepted_honest"
        elif not row["passed"]:
            assert row["rejected_by_gate"] and row["caught_by"] == "gate"   # gate-caught
            assert row["outcome_label"] == "rejected_cheat"
        else:
            # an honest nuance: a cheat that passes per-submission gates is a sybil
            # (hotkey_stacking), penalized by coldkey-collapse, not a boolean gate
            if row["cheat_type"] == "hotkey_stacking":
                assert row["caught_by"] == "coldkey_collapse"
                assert row["outcome_label"] == "sybil_collapsed"
            else:
                assert row["caught_by"] == "uncaught_passed_cheat"
                assert row["outcome_label"] == "accepted_cheat"
    # the dataset is non-degenerate: BOTH classes present (trainable)
    labels = {row["label"] for row in rows}
    assert labels == {"honest", "cheat"}
    # JSON-serializable (it's written to traces.jsonl)
    json.dumps(rows, default=str)


def test_dataset_card_documents_and_validates_the_training_set():
    """The traces.jsonl dataset card is self-describing + trainability-checkable: the
    schema names the feature/label columns, the taxonomy covers both classes, and the
    class distribution sums to the row count over the REAL rows."""
    eng = ArenaEngine(); r = eng.run(1)
    arch = {s.hotkey: s.archetype for s in eng.roster}
    rows = [reports.trace_training_row(a, r.weights.get(a.run.miner_hotkey, 0.0),
                                       arch.get(a.run.miner_hotkey, "honest"), 1)
            for a in r.agents]
    card = reports.dataset_card(rows)
    assert card["schema"] == "cathedral.arena.traces.v1" and card["n_rows"] == len(rows)
    # every declared column actually exists on the rows
    for col in card["feature_columns"] + card["label_columns"]:
        assert all(col in row for row in rows), f"missing column: {col}"
    # both classes present, and the label histogram sums to n_rows
    assert set(card["class_distribution"]["label"]) == {"honest", "cheat"}
    assert sum(card["class_distribution"]["label"].values()) == card["n_rows"]
    assert sum(card["class_distribution"]["outcome_label"].values()) == card["n_rows"]
    # the taxonomy enumerates the cheat types actually present
    assert set(card["label_taxonomy"]["cheat_type"]) == {r["cheat_type"] for r in rows if r["cheat_type"]}


def test_main_writes_dataset_card(tmp_path, monkeypatch):
    monkeypatch.setattr(arena_main, "OUT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["python", "1"])
    arena_main.main()
    card = json.loads((tmp_path / "traces_dataset.json").read_text())
    n_traces = sum(1 for _ in (tmp_path / "traces.jsonl").read_text().splitlines())
    assert card["n_rows"] == n_traces and card["schema"] == "cathedral.arena.traces.v1"


def test_anticheat_report_maps_rejections_to_gates():
    eng = ArenaEngine(); r = eng.run(1)
    ar = reports.anticheat_report(r)
    assert ar["axes_count"] == 12                       # the full anti-cheat taxonomy
    assert ar["total_rejected"] >= 10
    # every rejection names the gate that caught it + a reason
    for x in ar["rejected"]:
        assert x["rejected_by_gate"] and x["reasons"]
    # the gates exercised this round are real GateOutcome gates
    assert all(g in GateOutcome.GATES for g in ar["gates_exercised_this_round"])


def test_anticheat_axes_cover_the_named_vectors():
    # the goal's named anti-cheat vectors each map to a gate
    axes = reports.ANTICHEAT_AXES
    for vector in ("copied_witness", "wrong_owner", "stale_replay", "fake_attestation",
                   "fake_compute_profile", "spam", "invalid_cnf", "missing_decode_map",
                   "invalid_replay_harness", "hotkey_stacking", "trace_forgery",
                   "mislabeled_finding"):
        assert vector in axes and axes[vector]


def test_main_writes_scanner_benchmark_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(arena_main, "OUT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["python", "1"])

    arena_main.main()

    contract = json.loads((tmp_path / "scanner_contract.json").read_text())
    assert "structured claims are metadata" in contract["rule"]
    assert contract["report_only_submission"]["claim"]["severity"] == "critical"
    assert contract["report_only_verdict"]["accepted"] is False
    assert contract["report_only_verdict"]["score"] == 0.0
    assert contract["report_only_verdict"]["gates"]["decode_map_present"] is False

    request = json.loads((tmp_path / "scanner_request.json").read_text())
    assert request["schema"] == "cathedral.scanner.request_intake.v1"
    assert request["scored"] is False
    assert request["ledger_written"] is False
    assert request["routed_count"] == 3
    assert request["verifier_policy"]["requires_replay_task"] is True

    bench = json.loads((tmp_path / "scanner_benchmark.json").read_text())
    assert bench["schema"] == "cathedral.scanner.benchmark.v1"
    assert bench["metric"] == "replay_kill_rate"
    assert bench["benchmark_tasks"] > 0

    by_hotkey = {m["miner_hotkey"]: m for m in bench["miners"]}
    assert by_hotkey["hk_example"]["kills"] == 1
    assert by_hotkey["hk_example"]["kill_rate"] > 0
    assert by_hotkey["hk_report_only"]["kills"] == 0
    assert by_hotkey["hk_report_only"]["kill_rate"] == 0.0

    play = json.loads((tmp_path / "scanner_playthrough.json").read_text())
    assert play["schema"] == "cathedral.arena.playthrough.v1"
    assert play["ok"] is True
    assert play["checks"]["report_only_rejected"] is True
    assert play["checks"]["forged_witness_rejected"] is True
    assert play["checks"]["seal_scores_once"] is True
