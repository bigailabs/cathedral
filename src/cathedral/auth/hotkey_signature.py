"""sr25519 hotkey signature helpers.

Submissions to `/v1/agents/submit` carry an `X-Cathedral-Signature` HTTP
header — base64 sr25519 over canonical_json of the locked payload shape:

    {
      "bundle_hash":  "<blake3 lowercase hex of plaintext zip>",
      "card_id":      "<card_definitions.id>",
      "miner_hotkey": "<ss58 ascii>",
      "submitted_at": "<iso 8601 UTC, ms precision, trailing Z>"
    }

Reference: CONTRACTS.md Section 4.1.

PR5 (`solve-on-submit`) extends the canonical shape with two optional
fields used when a miner POSTs a SAT solution alongside registration:

    {
      "bundle_hash":            "...",
      "card_id":                "synthetic_boolean_v1",
      "challenge_id":           "<challenge being solved>",
      "dimacs_solution_sha256": "<sha256 hex of dimacs body>",
      "miner_hotkey":           "...",
      "submitted_at":           "..."
    }

The `dimacs_solution_sha256` binds the signature to the specific solution
body the miner sent (prevents a MITM from swapping in another valid
solution to claim the win on someone else's behalf). Both new fields are
empty strings when the miner is only registering (no solve POST) so the
extended shape stays backwards-compatible with the 4-field signed payload
when callers explicitly request the legacy form.

We use `substrateinterface.Keypair` for verification (the standard
Bittensor / Substrate sr25519 implementation). Signing helpers exist
mostly for tests; production miners use `bittensor.wallet.hotkey.sign`.
"""

from __future__ import annotations

import base64
from typing import Any

from cathedral.v1_types import canonical_json


class InvalidSignatureError(Exception):
    """Hotkey signature failed to verify against canonical claim bytes."""


def _load_keypair_class() -> Any:
    """Resolve a sr25519 Keypair implementation.

    Production deploys ship `bittensor` (which bundles `bittensor_wallet`
    and/or `substrateinterface`). We try both so the verifier works in
    every environment without forcing a particular dependency.
    """
    last_err: Exception | None = None
    for module_name in ("bittensor_wallet", "substrateinterface"):
        try:
            mod = __import__(module_name, fromlist=["Keypair"])
        except ImportError as e:
            last_err = e
            continue
        keypair_cls = getattr(mod, "Keypair", None)
        if keypair_cls is not None:
            return keypair_cls
    raise InvalidSignatureError(
        f"no sr25519 Keypair implementation available: {last_err}"
    )


def canonical_claim_bytes(
    *,
    bundle_hash: str,
    card_id: str,
    miner_hotkey: str,
    submitted_at: str,
    challenge_id: str | None = None,
    dimacs_solution_sha256: str | None = None,
) -> bytes:
    """Return the exact bytes the miner signs.

    The dict shape is the locked payload from CONTRACTS.md Section 4.1.
    Built deterministically here so callers cannot accidentally drift
    from the canonicalization rule.

    PR5 extension: when ``challenge_id`` AND ``dimacs_solution_sha256``
    are both provided (i.e. not ``None``), they are added to the signed
    payload. Pass them as empty strings explicitly to indicate "I'm
    using the 6-field shape but I'm only registering" — most callers
    should just leave both ``None`` (legacy 4-field shape) unless the
    caller is in the solve-on-submit path.
    """
    payload: dict[str, Any] = {
        "bundle_hash": bundle_hash,
        "card_id": card_id,
        "miner_hotkey": miner_hotkey,
        "submitted_at": submitted_at,
    }
    if challenge_id is not None or dimacs_solution_sha256 is not None:
        # Both fields move together to keep the payload shape predictable.
        # Each defaults to empty string when only one was provided so the
        # signed dict always carries both keys when either is in play.
        payload["challenge_id"] = challenge_id or ""
        payload["dimacs_solution_sha256"] = dimacs_solution_sha256 or ""
    return canonical_json(payload)


def verify_hotkey_signature(
    *,
    hotkey_ss58: str,
    signature_b64: str,
    bundle_hash: str,
    card_id: str,
    submitted_at: str,
    challenge_id: str | None = None,
    dimacs_solution_sha256: str | None = None,
) -> None:
    """Verify the sr25519 signature; raise `InvalidSignatureError` on failure.

    The hotkey passed in MUST match the `miner_hotkey` field that the
    miner included when signing. We re-derive the canonical bytes from
    the trusted server-side values and require the signature to match
    those exactly. This means a miner cannot sign a payload claiming a
    different bundle_hash than the one cathedral computed from the
    uploaded bytes (Section 6 step 1).

    PR5 extension: if ``challenge_id`` or ``dimacs_solution_sha256`` is
    provided, verification uses the 6-field canonical shape. Callers in
    the solve-on-submit path always pass both (using empty strings to
    mean "no challenge / no solution"); legacy callers omit both.
    """
    keypair_cls = _load_keypair_class()

    try:
        sig_bytes = base64.b64decode(signature_b64, validate=True)
    except (ValueError, TypeError) as e:
        raise InvalidSignatureError(f"signature is not valid base64: {e}") from e

    payload = canonical_claim_bytes(
        bundle_hash=bundle_hash,
        card_id=card_id,
        miner_hotkey=hotkey_ss58,
        submitted_at=submitted_at,
        challenge_id=challenge_id,
        dimacs_solution_sha256=dimacs_solution_sha256,
    )

    try:
        kp = keypair_cls(ss58_address=hotkey_ss58)
    except (ValueError, TypeError) as e:
        raise InvalidSignatureError(f"invalid ss58 hotkey: {e}") from e

    try:
        ok = kp.verify(payload, sig_bytes)
    except Exception as e:
        raise InvalidSignatureError(f"verify raised: {e}") from e

    if not ok:
        raise InvalidSignatureError("invalid hotkey signature")


def sign_claim(
    *,
    seed_hex: str,
    bundle_hash: str,
    card_id: str,
    miner_hotkey: str,
    submitted_at: str,
    challenge_id: str | None = None,
    dimacs_solution_sha256: str | None = None,
) -> str:
    """Sign a claim payload with a raw sr25519 seed (hex). Test/CLI helper.

    Production miners should use `bittensor.wallet.hotkey.sign(...)` to
    keep keys in their wallet. The output is base64 (standard, padded).

    PR5 extension: pass ``challenge_id`` and ``dimacs_solution_sha256``
    to sign the 6-field solve-on-submit payload.
    """
    keypair_cls = _load_keypair_class()
    kp = keypair_cls.create_from_seed(seed_hex, crypto_type=1)  # 1 == sr25519
    if kp.ss58_address != miner_hotkey:
        raise ValueError(
            f"seed does not derive the requested hotkey "
            f"(seed -> {kp.ss58_address}, asked for {miner_hotkey})"
        )
    payload = canonical_claim_bytes(
        bundle_hash=bundle_hash,
        card_id=card_id,
        miner_hotkey=miner_hotkey,
        submitted_at=submitted_at,
        challenge_id=challenge_id,
        dimacs_solution_sha256=dimacs_solution_sha256,
    )
    sig = kp.sign(payload)
    return base64.b64encode(sig).decode("ascii")
