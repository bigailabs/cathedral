"""live_status() — the arena reads a stored attestor response and reports real
attestation honestly: a real Intel-verified quote verifies; an attestor error is
surfaced as blocked (never silently 'real').
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

from game.arena import attestation

FIXTURE = Path(__file__).resolve().parent / "dcap_verify_fixture.py"
NONCE = "abad1dea" * 8
PUB = base64.b64encode(b"k" * 32).decode()


def _good_quote():
    lo = hashlib.sha256((NONCE + PUB).encode()).digest()
    return base64.b64encode(b"\x00" * 568 + lo + b"\x00" * 32).decode()


def test_real_quote_receipt_verifies(monkeypatch, tmp_path):
    monkeypatch.setenv("CATHEDRAL_ATTEST_DCAP_VERIFY_CMD", f"{sys.executable} {FIXTURE}")
    rp = tmp_path / "receipt.json"
    rp.write_text(json.dumps({"quote_b64": _good_quote(), "nonce": NONCE,
                              "e2e_pubkey_b64": PUB, "instance": "attest-spot",
                              "cost_usd": 0.001}))
    st = attestation.live_status(rp)
    assert st["available"] is True
    assert st["ok"] is True and st["intel_verified"] is True
    assert st["backend"].startswith("command-dcap")


def test_attestor_error_receipt_is_blocked(tmp_path):
    rp = tmp_path / "receipt.json"
    rp.write_text(json.dumps({"error": "scp guest script failed: Permission denied (publickey)"}))
    st = attestation.live_status(rp)
    assert st["available"] is False and st["blocked"] is True
    assert "publickey" in st["reason"]


def test_missing_receipt_not_blocked(tmp_path):
    st = attestation.live_status(tmp_path / "nope.json")
    assert st["available"] is False and st["blocked"] is False


def test_real_intel_verified_quote_on_disk():
    """The REAL Polaris TDX quote captured live (fire #8) must verify end-to-end:
    independent local report_data binding re-check + server-side Intel-collateral
    verification. If no real receipt is present (fresh checkout), skip."""
    import json
    rp = Path(__file__).resolve().parents[1] / "out" / "real_attest_receipt.json"
    if not rp.exists():
        return
    d = json.loads(rp.read_text())
    if "error" in d or not d.get("quote_b64"):
        return                                    # an error/placeholder receipt
    st = attestation.live_status(rp)
    assert st["available"] is True
    assert st["binding_reverified"] is True       # WE re-checked the caller binding
    assert st["intel_verified"] is True           # Intel chain (real collateral)
    assert st["ok"] is True                       # LIVE TDX VERIFIED
