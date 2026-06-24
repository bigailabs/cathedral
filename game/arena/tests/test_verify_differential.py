"""The round verifier enforces real replay-harness differentials.

`replay_differential.json` proves every pinned invariant separates exploit input
from benign input, or holds as a conserved invariant. If that artifact says a
harness is not real, the round verifier must reject it.
"""
from __future__ import annotations

import json

from game.arena.verify import verify_round


def _check(result, name):
    return next(check for check in result["checks"] if check["name"] == name)


def test_verifier_passes_a_real_differential(tmp_path):
    (tmp_path / "replay_differential.json").write_text(json.dumps({
        "schema": "cathedral.arena.replay_differential.v1",
        "targets": [],
        "total": 10,
        "discriminators": 10,
        "exploit": 6,
        "conserved": 4,
        "all_real": True,
    }))
    result = verify_round(tmp_path)
    check = _check(result, "replay_differential")
    assert check["status"] == "pass"
    assert check["ok"] is True
    assert "10/10" in check["detail"]


def test_verifier_rejects_a_differential_that_is_not_all_real(tmp_path):
    (tmp_path / "replay_differential.json").write_text(json.dumps({
        "schema": "cathedral.arena.replay_differential.v1",
        "targets": [],
        "total": 10,
        "discriminators": 9,
        "exploit": 6,
        "conserved": 4,
        "all_real": False,
    }))
    result = verify_round(tmp_path)
    check = _check(result, "replay_differential")
    assert check["ok"] is False
    assert check["optional"] is False
    assert "replay_differential" in result["required_failed"]


def test_verifier_skips_when_no_differential_present(tmp_path):
    result = verify_round(tmp_path)
    check = _check(result, "replay_differential")
    assert check["status"] == "absent"
    assert check["optional"] is True
