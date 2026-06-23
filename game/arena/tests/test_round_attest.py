"""Whole-round attestation readiness — bind a TDX quote's report_data to the
round's Merkle anchor so one quote attests every proof in the round, tied to the
deterministic-scoring output. The live quote is gated (attestor box + approval, no
spend); these prove the BINDING is real and round-specific, offline.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

from game.arena import attestation
from game.arena.engine import ArenaEngine

FIXTURE = Path(__file__).resolve().parent / "dcap_verify_fixture.py"
PUB = base64.b64encode(b"e2e-round-pub".ljust(32, b"0")).decode()


def _quote(nonce, pubkey=PUB):
    lo = hashlib.sha256((nonce + pubkey).encode()).digest()
    return base64.b64encode(b"\x00" * 568 + lo + b"\x00" * 32).decode()


def test_round_commitment_is_the_merkle_root():
    r = ArenaEngine().run(1)
    ra = attestation.round_attest_readiness(r.anchor["merkle_root"])
    assert ra["available"] and ra["commitment"] == r.anchor["merkle_root"]
    assert ra["live_quote"] is False and ra["attested_to_this_round"] is False  # gated, no spend


def test_quote_bound_to_the_round_root_attests_it(tmp_path, monkeypatch):
    monkeypatch.setenv("CATHEDRAL_ATTEST_DCAP_VERIFY_CMD", f"{sys.executable} {FIXTURE}")
    monkeypatch.delenv("CATHEDRAL_ATTEST_ALLOW_STUB", raising=False)
    root = ArenaEngine().run(1).anchor["merkle_root"]
    qp = tmp_path / "q.json"
    qp.write_text(json.dumps({"quote_b64": _quote(root), "e2e_pubkey_b64": PUB}))
    ra = attestation.round_attest_readiness(root, quote_path=qp)
    assert ra["live_quote"] is True and ra["attested_to_this_round"] is True


def test_quote_bound_to_a_different_round_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("CATHEDRAL_ATTEST_DCAP_VERIFY_CMD", f"{sys.executable} {FIXTURE}")
    root = ArenaEngine().run(1).anchor["merkle_root"]
    qp = tmp_path / "q.json"
    qp.write_text(json.dumps({"quote_b64": _quote("ff" * 32), "e2e_pubkey_b64": PUB}))
    ra = attestation.round_attest_readiness(root, quote_path=qp)
    assert ra["attested_to_this_round"] is False        # binds a different commitment


def test_detects_real_intel_quote_on_file(tmp_path):
    p = tmp_path / "real.json"
    p.write_text(json.dumps({"intel_verified": True, "report_data_match": True,
                             "instance": "us-central1-b/attest-x", "cost_usd": 0.0033}))
    ra = attestation.round_attest_readiness("ab" * 32, real_receipt_path=p)
    assert ra["has_real_quote_on_file"] is True
    assert ra["real_quote_cost_usd"] == 0.0033
    # a quote present but not Intel-verified is not counted
    p.write_text(json.dumps({"intel_verified": False, "report_data_match": True}))
    assert attestation.round_attest_readiness("ab" * 32, real_receipt_path=p)["has_real_quote_on_file"] is False


def test_surfaced_in_operator_console():
    oc = ArenaEngine().run(1).operator_console
    assert "round_attest" in oc and oc["round_attest"]["available"] is True


def test_real_quote_reverifies_live_when_on_file():
    """The arena's live attestation is REAL + re-checkable: the on-file Intel-verified
    TDX quote re-verifies end-to-end (report_data caller-binding recomputed locally +
    Intel collateral). Skips if no real quote is present on this machine."""
    from game.arena import attestation
    v = attestation.reverify_real_quote()
    if not v.get("available"):
        return                                          # no real quote on this checkout
    assert v["ok"] is True
    assert v["binding_reverified"] is True              # report_data[0:32] re-checked locally
    assert v["intel_verified"] is True
    assert v["instance"]                                # the real GCE TDX instance


def test_live_attestation_surfaced_each_round():
    r = ArenaEngine().run(1)
    la = r.operator_console.get("live_attestation", {})
    # live attestation is computed every round (real quote re-verified, or blocked w/ reason)
    assert "available" in la
    if la.get("available"):
        assert la["ok"] is True and la["binding_reverified"] is True
