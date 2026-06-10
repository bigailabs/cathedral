"""Hotkey (sr25519) authentication for the thin publisher's miner surface.

The miner-facing endpoints (active-cnf, /v1/agents/submit) are authenticated by
an sr25519 signature over a canonical-JSON claim. This is the EXACT contract the
monolith uses (CONTRACTS.md §4.1, ported from
~/code/cathedral/src/cathedral/auth/hotkey_signature.py + tests/v1/test_hotkey_signature.py):

  * canonical bytes  = json.dumps(payload, sort_keys, compact, default=str, utf-8)
                       with the `signature` key dropped.
  * 4-field claim    = {bundle_hash, card_id, miner_hotkey, submitted_at}
  * 6-field claim    = + {challenge_id, dimacs_solution_sha256}  (solve-on-submit)
  * signature        = sr25519 over those bytes, standard base64 (88 chars).
  * headers          = X-Cathedral-Hotkey (ss58) + X-Cathedral-Signature (b64).

sr25519 verification is pluggable: the production backend uses bittensor_wallet's
Keypair (the same primitive the live publisher and Polkadot.js signer agree on).
If that import is unavailable, a StubVerifier keeps the rest of the service
testable but FAILS CLOSED (every signature rejected) with a clear TODO — it must
never silently accept. Golden test vectors (//Alice) are pinned in
publisher_verify.py so a drift in canonicalization is caught.
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Protocol

# ss58 address of //Alice — the pinned golden vector (substrate well-known key).
ALICE_SS58 = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


def canonical_claim_bytes(
    *,
    bundle_hash: str,
    card_id: str,
    miner_hotkey: str,
    submitted_at: str,
    challenge_id: str | None = None,
    dimacs_solution_sha256: str | None = None,
) -> bytes:
    """Byte-identical port of cathedral.auth.hotkey_signature.canonical_claim_bytes.

    The 6-field shape is selected when EITHER challenge_id or
    dimacs_solution_sha256 is provided (both keys then always appear, defaulting
    to ""), matching the monolith's solve-on-submit signing path exactly.
    """
    payload: dict[str, Any] = {
        "bundle_hash": bundle_hash,
        "card_id": card_id,
        "miner_hotkey": miner_hotkey,
        "submitted_at": submitted_at,
    }
    if challenge_id is not None or dimacs_solution_sha256 is not None:
        payload["challenge_id"] = challenge_id or ""
        payload["dimacs_solution_sha256"] = dimacs_solution_sha256 or ""
    body = {k: v for k, v in payload.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class HotkeyVerifier(Protocol):
    """Pluggable sr25519 verifier. verify(ss58, message, sig_b64) -> bool, total."""

    def verify(self, ss58_address: str, message: bytes, signature_b64: str) -> bool: ...


class BittensorVerifier:
    """Production sr25519 verification via bittensor_wallet.Keypair (address-only;
    the publisher holds no miner private keys). This is the same primitive the
    live publisher uses, so a signature that verifies here verifies there."""

    def __init__(self) -> None:
        from bittensor_wallet import Keypair  # noqa: F401 — import-time availability check

        self._Keypair = Keypair

    def verify(self, ss58_address: str, message: bytes, signature_b64: str) -> bool:
        try:
            sig = base64.b64decode(signature_b64, validate=True)
        except Exception:
            return False
        if len(sig) != 64:  # sr25519 signatures are exactly 64 bytes
            return False
        try:
            kp = self._Keypair(ss58_address=ss58_address)
            return bool(kp.verify(message, sig))
        except Exception:
            return False


class StubVerifier:
    """Fail-closed stand-in when no sr25519 backend is importable. Rejects every
    signature so the auth boundary can never be bypassed in a stub deployment.

    TODO(prod): install bittensor_wallet (or substrate-interface) in the runtime
    venv so BittensorVerifier is selected. The golden //Alice vectors in
    publisher_verify.py assert the real backend is the one wired in CI.
    """

    backend = "stub-fail-closed"

    def verify(self, ss58_address: str, message: bytes, signature_b64: str) -> bool:
        return False


def default_verifier() -> HotkeyVerifier:
    """Select the production sr25519 backend if importable, else fail closed."""
    try:
        return BittensorVerifier()
    except Exception:
        return StubVerifier()
