"""ARENA.md handoff claims must be backed by machine-checkable evidence.

This guards the "Off-box solves on Stitch -- LANDED" section against drift:
documented entry points are importable callables, the width param is real, and
the documented off-box receipt table is backed by a sanitized fixture manifest.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

from game.arena import mint

ARENA = Path(__file__).resolve().parents[1]
FIXTURE = ARENA / "offbox_handoff_receipts.json"


def test_documented_offbox_entrypoints_exist():
    for name in ("offbox_on_stitch", "offbox_hardened_on_stitch", "capture_offbox_receipt",
                 "capture_hardened_receipt", "decode_assignment"):
        assert callable(getattr(mint, name, None)), f"ARENA.md documents mint.{name} -- missing"


def test_offbox_hardened_takes_the_documented_width_param():
    # ARENA.md: "pass width=8 for subtensor-root-reborn"
    assert "width" in inspect.signature(mint.offbox_hardened_on_stitch).parameters


def test_documented_offbox_receipts_match_the_handoff_table():
    """The ARENA.md table claims file, direction, rule, model, and evidence.

    The tracked fixture is the CI-verifiable proof that the handoff describes
    captured receipts, not aspirations. Raw operator receipts can be re-checked
    separately by `python -m game.arena.verify --json`.
    """
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert payload["schema"] == "cathedral.arena.offbox_handoff_receipts.v1"
    receipts = {r["file"]: r for r in payload["receipts"]}
    assert set(receipts) == {
        "offbox_stitch_receipt.json",
        "offbox_i1_receipt.json",
        "offbox_hardened_receipt.json",
    }

    expected = {
        "offbox_stitch_receipt.json": ("CRACKED", "B2-fee-silent-zero"),
        "offbox_i1_receipt.json": ("CRACKED", "I1-div-by-zero"),
        "offbox_hardened_receipt.json": ("HARDENED", "A4-fee-split-conservation"),
    }
    for fname, (direction, rule) in expected.items():
        d = receipts[fname]
        assert d["direction"] == direction
        assert d["rule_id"] == rule
        assert d["model"] == "subtensor-amm"
        assert d["host"] == "polarisserver"
        assert d["solver"] == "kissat"
        assert d["remote_wall_ms"] > 0
        assert d["round_trips"] > 0
        assert len(d["cnf_sha256"]) == 64
        if direction == "CRACKED":
            assert d["proof_check"] == "cnf_satisfied"
            assert d["cnf_satisfied"] is True
            assert d["n_lits"] > 0
            assert d["decoded_input"]
        else:
            assert d["proof_check"] == "remote_unsat_and_local_unsat"
            assert d["remote_unsat"] is True
            assert d["local_unsat"] is True
            assert d["cross_confirmed"] is True
