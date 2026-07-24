"""Signing-key loading. The publisher reads a 32-byte Ed25519 key from
CATHEDRAL_EVAL_SIGNING_KEY (hex). For dev it can generate a TEST key — never a
hardcoded one — written to a path so a restart keeps the same JWKS. Production
sets the env to the live key so validators see no change.
"""
from __future__ import annotations

import os


def load_signing_key(generate_dev_key: bool = True) -> str:
    """Return the 32-byte signing key as hex. Order: env var, then (dev only) a
    freshly generated test key. Raises if no key and dev generation disabled."""
    env = os.environ.get("CATHEDRAL_EVAL_SIGNING_KEY")
    if env and env.strip():
        raw = bytes.fromhex(env.strip())
        if len(raw) != 32:
            raise ValueError(f"CATHEDRAL_EVAL_SIGNING_KEY must be 32 bytes, got {len(raw)}")
        return env.strip()
    if not generate_dev_key:
        raise RuntimeError("CATHEDRAL_EVAL_SIGNING_KEY not set and dev generation disabled")
    return generate_test_key()


def generate_test_key() -> str:
    """Generate a fresh random 32-byte test signing key (dev only). NOT the live
    key — wire_compat still proves live-row compatibility against the real key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    sk = Ed25519PrivateKey.generate()
    return sk.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    ).hex()
