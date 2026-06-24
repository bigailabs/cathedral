"""Subnet Breaker playthrough verification.

This is the no-new-dependency smoke test for the interactive game loop: the UI
calls these same scanner endpoints in the same order.
"""
from __future__ import annotations

import json

from game.arena import playthrough


def test_playthrough_proves_the_scoreful_game_loop(tmp_path):
    report = playthrough.run_playthrough(
        ledger_path=str(tmp_path / "scanner.jsonl"),
        miner_hotkey="hk_test_player",
    )

    assert report["schema"] == playthrough.SCHEMA_PLAYTHROUGH
    assert report["ok"] is True
    assert all(report["checks"].values())
    assert report["steps"]["report_only_replay"]["accepted"] is False
    assert report["steps"]["forged_replay"]["accepted"] is False
    assert report["steps"]["dry_replay"]["accepted"] is True
    assert report["steps"]["dry_replay"]["ledger_written"] is False
    assert report["steps"]["submit_without_attestation"]["accepted"] is False
    assert report["steps"]["submit_without_attestation"]["gates"]["attestation_receipt"] is False
    assert report["steps"]["attest"]["attested"] is True
    assert report["steps"]["attest"]["receipt"]["production_tee"] is False
    assert report["steps"]["seal"]["accepted"] is True
    assert report["checks"]["attestation_ignores_mutable_report"] is True
    assert report["steps"]["seal"]["score"] > 0
    assert report["steps"]["duplicate"]["accepted"] is False
    assert report["state"]["accepted"] == 1
    assert report["leaderboard_top"]["miner_hotkey"] == "hk_test_player"
    assert report["benchmark_top"]["kill_rate"] > 0
    json.dumps(report, default=str)


def test_playthrough_cli_writes_artifact(tmp_path, monkeypatch, capsys):
    # main() writes beside its module; point __file__ at a temp module path.
    out_dir = tmp_path / "out"
    monkeypatch.setattr(playthrough, "__file__", str(tmp_path / "playthrough.py"))
    code = playthrough.main()

    assert code == 0
    written = out_dir / "scanner_playthrough.json"
    body = json.loads(written.read_text())
    assert body["ok"] is True
    assert "playthrough OK" in capsys.readouterr().out
