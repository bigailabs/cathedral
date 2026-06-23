"""The REAL attestation verifier path — the arena uses Cathedral's verifier of
record (configurable DCAP command), not a toy. These prove the real plumbing
runs end-to-end against a quote, and that the report_data binding is enforced.
"""
from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path

import pytest

from game.arena import attestation

FIXTURE = Path(__file__).resolve().parent / "dcap_verify_fixture.py"
NONCE = "deadbeef" * 8
PUBKEY = base64.b64encode(b"x" * 32).decode()


def _quote(nonce=NONCE, pubkey=PUBKEY, tail=b"\x00" * 32):
    """A 632-byte TDX-shaped quote whose report_data[0:32] binds (nonce,pubkey)."""
    lo = hashlib.sha256((nonce + pubkey).encode()).digest()
    body = b"\x00" * 568 + lo + tail
    return base64.b64encode(body).decode()


def test_real_dcap_command_path_runs(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_ATTEST_DCAP_VERIFY_CMD", f"{sys.executable} {FIXTURE}")
    monkeypatch.delenv("CATHEDRAL_ATTEST_ALLOW_STUB", raising=False)
    assert attestation.intel_backend() == "command-dcap"
    v = attestation.verify_real_quote(_quote(), nonce_hex=NONCE, e2e_pubkey_b64=PUBKEY)
    assert v["report_data_bind_ok"] is True
    assert v["intel_verified"] is True
    assert v["ok"] is True and v["backend"] == "command-dcap"


def test_report_data_binding_enforced(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_ATTEST_DCAP_VERIFY_CMD", f"{sys.executable} {FIXTURE}")
    # a quote whose report_data binds a DIFFERENT pubkey must fail the bind check
    bad = _quote(pubkey=base64.b64encode(b"y" * 32).decode())
    v = attestation.verify_real_quote(bad, nonce_hex=NONCE, e2e_pubkey_b64=PUBKEY)
    assert v["report_data_bind_ok"] is False
    assert v["ok"] is False


def test_stub_path_is_labeled(monkeypatch):
    monkeypatch.delenv("CATHEDRAL_ATTEST_DCAP_VERIFY_CMD", raising=False)
    monkeypatch.delenv("CATHEDRAL_DCAP_VERIFY_CMD", raising=False)
    monkeypatch.setenv("CATHEDRAL_ATTEST_ALLOW_STUB", "1")
    assert attestation.intel_backend() == "stub-intel-collateral"
    v = attestation.verify_real_quote(_quote(), nonce_hex=NONCE, e2e_pubkey_b64=PUBKEY)
    assert v["ok"] is True and v["backend"] == "stub-intel-collateral"


def test_short_quote_rejected(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_ATTEST_DCAP_VERIFY_CMD", f"{sys.executable} {FIXTURE}")
    v = attestation.verify_real_quote(base64.b64encode(b"\x00" * 100).decode(),
                                      nonce_hex=NONCE, e2e_pubkey_b64=PUBKEY)
    assert v["ok"] is False and "too_short" in v["reason"]


def test_no_verifier_configured_fails_closed(monkeypatch):
    monkeypatch.delenv("CATHEDRAL_ATTEST_DCAP_VERIFY_CMD", raising=False)
    monkeypatch.delenv("CATHEDRAL_DCAP_VERIFY_CMD", raising=False)
    monkeypatch.delenv("CATHEDRAL_ATTEST_ALLOW_STUB", raising=False)
    assert attestation.intel_backend() == "none-configured"
    v = attestation.verify_real_quote(_quote(), nonce_hex=NONCE, e2e_pubkey_b64=PUBKEY)
    assert v["ok"] is False                    # no verifier => not trusted
