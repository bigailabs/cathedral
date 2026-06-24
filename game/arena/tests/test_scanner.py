"""Scanner contract tests.

These assert the Bitsec-inspired interface while preserving Cathedral's
deterministic proof standard: reports and categories do not score unless replay
reproduces.
"""
from __future__ import annotations

import json
from dataclasses import replace

from game.arena import replay, scanner


def test_issue_task_uses_real_corpus_and_pinned_replay_target():
    task = scanner.issue_task(0)
    assert task.schema == scanner.SCHEMA_TASK
    assert task.task_id.startswith("scan-")
    assert task.target_netuid > 0
    assert task.replay_target_id in replay.TARGETS
    assert task.expected_family == replay.TARGETS[task.replay_target_id].family
    assert tuple(task.required_fields) == replay.TARGETS[task.replay_target_id].decode
    assert task.manifest()["reward_shape"] == "linear_metric_x_boolean_gate"
    assert task.manifest()["optional_claim_schema"]["scoring"] == "metadata_only_replay_required"
    assert scanner.task_by_id(task.task_id) == task
    assert scanner.task_by_id("missing") is None


def test_scan_request_intake_routes_to_replay_tasks_without_scoring():
    intake = scanner.intake_scan_request({
        "requester": "customer-a",
        "repo": "https://github.com/acme/subnet",
        "commit": "abc123",
        "objective": "Find reproducible incentive bugs.",
        "scope": "validator.py, rewards.py",
        "requested_families": ["money_math", "incentive"],
        "max_tasks": 2,
    })

    assert intake["schema"] == scanner.SCHEMA_SCAN_INTAKE
    assert intake["accepted"] is True
    assert intake["ledger_written"] is False
    assert intake["scored"] is False
    assert intake["request"]["schema"] == scanner.SCHEMA_SCAN_REQUEST
    assert intake["request"]["request_id"].startswith("req-")
    assert intake["request"]["scope"] == ["validator.py", "rewards.py"]
    assert intake["request"]["scoring"] == "metadata_only_until_routed_to_replay_task"
    assert intake["routed_count"] == 2
    assert all(t["schema"] == scanner.SCHEMA_TASK for t in intake["routed_tasks"])
    assert intake["verifier_policy"]["reports_score"] is False
    assert intake["verifier_policy"]["claims_score"] is False
    assert intake["verifier_policy"]["requires_replay_task"] is True


def test_scan_request_intake_clamps_task_count():
    intake = scanner.intake_scan_request({"max_tasks": 10_000})
    assert intake["routed_count"] == 12
    assert intake["request"]["max_tasks"] == 12


def test_good_submission_accepts_and_scores():
    task = scanner.issue_task(0)
    sub = scanner.example_accepted_submission(task)
    verdict = scanner.verify_submission(task, sub)
    assert verdict.accepted is True
    assert verdict.score == task.bounty_weight
    assert all(verdict.gates.values())
    assert verdict.observed
    assert len(verdict.artifact_sha256) == 64


def test_report_only_and_category_only_do_not_score():
    task = scanner.issue_task(0)
    sub = scanner.ScannerSubmission(
        task_id=task.task_id,
        miner_hotkey="hk_reporter",
        nonce=task.nonce,
        proof_family=task.expected_family,
        witness=None,
        claim={
            "schema": scanner.SCHEMA_CLAIM,
            "title": "Correct-looking report",
            "category": task.expected_family,
            "severity": "critical",
            "impact": "sounds expensive",
        },
        report="This is the right bug class and a convincing explanation.",
    )
    verdict = scanner.verify_submission(task, sub)
    assert verdict.accepted is False
    assert verdict.score == 0.0
    assert verdict.gates["family_aligned"] is True
    assert verdict.gates["decode_map_present"] is False
    assert verdict.gates["replay_succeeds"] is False
    assert sub.as_artifact()["claim"]["category"] == task.expected_family
    assert len(sub.as_artifact()["claim_sha256"]) == 64


def test_malformed_claim_metadata_cannot_break_or_score(tmp_path):
    ledger = tmp_path / "scanner.jsonl"
    task = scanner.issue_task(0)
    sub = replace(
        scanner.example_accepted_submission(task, miner_hotkey="hk_odd_claim"),
        claim=["not", "a", "dict"],
    )

    artifact = sub.as_artifact()
    assert artifact["claim_valid"] is False
    assert artifact["claim"]["_invalid"] is True
    assert artifact["claim"]["raw_type"] == "list"
    assert len(artifact["claim_sha256"]) == 64

    verdict = scanner.record_submission(ledger, task, sub)
    assert verdict["accepted"] is True
    assert verdict["score"] == task.bounty_weight
    assert verdict["ledger_entry"]["claim_present"] is True
    assert verdict["ledger_entry"]["claim_valid"] is False


def test_wrong_family_rejected_even_with_reproducing_witness():
    task = scanner.issue_task(0)
    sub = scanner.example_accepted_submission(task)
    wrong = replace(sub, proof_family="Z_wrong")
    verdict = scanner.verify_submission(task, wrong)
    assert verdict.accepted is False
    assert verdict.score == 0.0
    assert verdict.gates["family_aligned"] is False
    assert verdict.gates["replay_succeeds"] is True
    assert "proof_family_mismatch" in verdict.reasons


def test_bad_witness_rejected_even_with_right_family_and_nonce():
    task = scanner.issue_task(0)
    sub = scanner.example_accepted_submission(task)
    bad_witness = {k: 0 for k in task.required_fields}
    verdict = scanner.verify_submission(task, replace(sub, witness=bad_witness))
    assert verdict.accepted is False
    assert verdict.score == 0.0
    assert verdict.gates["family_aligned"] is True
    assert verdict.gates["replay_succeeds"] is False


def test_catalog_is_deterministic_and_all_tasks_have_required_fields():
    c1 = scanner.benchmark_catalog()
    c2 = scanner.benchmark_catalog()
    assert [t.task_id for t in c1] == [t.task_id for t in c2]
    assert c1
    assert all(t.required_fields for t in c1)


def test_routed_benchmark_tasks_have_replay_positive_example_witnesses():
    tasks = scanner.benchmark_catalog(limit=12)
    assert len(tasks) == 12
    for task in tasks:
        verdict = scanner.verify_submission(task, scanner.example_accepted_submission(task))
        assert verdict.accepted is True, (task.task_id, task.replay_target_id, verdict.reasons)
        assert scanner.task_by_id(task.task_id) == task


def test_record_submission_persists_and_leaderboards(tmp_path):
    ledger = tmp_path / "scanner.jsonl"
    task0 = scanner.issue_task(0)
    task1 = scanner.issue_task(1)
    good0 = scanner.example_accepted_submission(task0, miner_hotkey="hk_a")
    good1 = scanner.example_accepted_submission(task1, miner_hotkey="hk_b")
    bad = replace(scanner.example_accepted_submission(task1, miner_hotkey="hk_b"),
                  witness={k: 0 for k in task1.required_fields})

    v0 = scanner.record_submission(ledger, task0, good0)
    vb = scanner.record_submission(ledger, task1, bad)
    v1 = scanner.record_submission(ledger, task1, good1)

    assert v0["accepted"] is True and v0["score"] > 0
    assert vb["accepted"] is False and vb["score"] == 0
    assert v1["accepted"] is True and v1["score"] > 0

    entries = scanner.read_ledger(ledger)
    assert len(entries) == 3
    assert entries[0]["claim_present"] is True
    assert len(entries[0]["claim_sha256"]) == 64
    board = scanner.leaderboard(ledger)
    assert board["count"] == 2
    assert board["miners"][0]["score"] >= board["miners"][1]["score"]
    assert sum(m["accepted"] for m in board["miners"]) == 2
    assert sum(m["rejected"] for m in board["miners"]) == 1
    assert board["miners"][0]["benchmark_tasks"] == len(scanner.benchmark_catalog())
    assert 0 < board["miners"][0]["kill_rate"] <= 1
    assert 0 < board["miners"][0]["weighted_kill_rate"] <= 1


def test_duplicate_accepted_task_does_not_double_score(tmp_path):
    ledger = tmp_path / "scanner.jsonl"
    task = scanner.issue_task(0)
    sub = scanner.example_accepted_submission(task, miner_hotkey="hk_dup")
    first = scanner.record_submission(ledger, task, sub)
    second = scanner.record_submission(ledger, task, sub)

    assert first["accepted"] is True and first["score"] > 0
    assert second["accepted"] is False
    assert second["score"] == 0.0
    assert second["gates"]["not_duplicate_credit"] is False
    assert "duplicate_task_credit" in second["reasons"]

    board = scanner.leaderboard(ledger)
    assert board["miners"][0]["score"] == first["score"]
    assert board["miners"][0]["accepted"] == 1
    assert board["miners"][0]["rejected"] == 1


def test_benchmark_metric_is_replay_kill_rate_not_report_quality(tmp_path):
    ledger = tmp_path / "scanner.jsonl"
    task0 = scanner.issue_task(0)
    task1 = scanner.issue_task(1)
    scanner.record_submission(
        ledger,
        task0,
        scanner.example_accepted_submission(task0, miner_hotkey="hk_killer"),
    )
    scanner.record_submission(
        ledger,
        task1,
        scanner.ScannerSubmission(
            task_id=task1.task_id,
            miner_hotkey="hk_report",
            nonce=task1.nonce,
            proof_family=task1.expected_family,
            witness=None,
            report="Convincing vulnerability report with the right family.",
        ),
    )

    bench = scanner.benchmark(ledger)
    killer = next(m for m in bench["miners"] if m["miner_hotkey"] == "hk_killer")
    reporter = next(m for m in bench["miners"] if m["miner_hotkey"] == "hk_report")

    assert bench["schema"] == scanner.SCHEMA_BENCHMARK
    assert bench["metric"] == "replay_kill_rate"
    assert "replay_succeeds" in bench["boolean_gate"]
    assert killer["kills"] == 1
    assert killer["kill_rate"] == round(1 / len(scanner.benchmark_catalog()), 6)
    assert reporter["kills"] == 0
    assert reporter["kill_rate"] == 0.0


def test_weighted_kill_rate_ignores_non_benchmark_score(tmp_path):
    ledger = tmp_path / "scanner.jsonl"
    task = scanner.issue_task(0)
    accepted = scanner.record_submission(
        ledger,
        task,
        scanner.example_accepted_submission(task, miner_hotkey="hk_killer"),
    )

    extra = dict(accepted["ledger_entry"])
    extra["task_id"] = "external-private-task"
    extra["score"] = 999.0
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(extra, sort_keys=True, separators=(",", ":")) + "\n")

    row = scanner.leaderboard(ledger)["miners"][0]
    possible = sum(t.bounty_weight for t in scanner.benchmark_catalog())
    assert row["score"] > row["benchmark_score"]
    assert row["weighted_kill_rate"] == round(row["benchmark_score"] / possible, 6)
    assert row["weighted_kill_rate"] <= 1.0


def test_miner_state_projects_ledger_for_one_hotkey(tmp_path):
    ledger = tmp_path / "scanner.jsonl"
    task0 = scanner.issue_task(0)
    task1 = scanner.issue_task(1)
    good0 = scanner.example_accepted_submission(task0, miner_hotkey="hk_state")
    bad1 = replace(scanner.example_accepted_submission(task1, miner_hotkey="hk_state"),
                   witness={k: 0 for k in task1.required_fields})
    other = scanner.example_accepted_submission(task1, miner_hotkey="hk_other")

    scanner.record_submission(ledger, task0, good0)
    scanner.record_submission(ledger, task1, bad1)
    scanner.record_submission(ledger, task1, other)

    state = scanner.miner_state(ledger, "hk_state")
    assert state["schema"] == scanner.SCHEMA_STATE
    assert state["miner_hotkey"] == "hk_state"
    assert state["accepted"] == 1
    assert state["rejected"] == 1
    assert state["attempts"] == 2
    assert state["accepted_task_ids"] == [task0.task_id]
    assert state["rejected_task_ids"] == [task1.task_id]
    assert state["score"] == task0.bounty_weight
    assert state["rank"] is not None
