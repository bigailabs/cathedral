"""Ed25519 signer for eval-run / task-family records.

Extracted from ``scoring_pipeline.py`` (which carries card-era scoring that is
being removed) into a neutral module so the live SAT signing path does not
depend on any card machinery. ``EvalSigner`` is imported by the publisher
signing path and the SAT attest worker.
"""

from __future__ import annotations

import base64
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cathedral.v1_types import canonical_json


class EvalSigner:
    """Wraps ``Ed25519PrivateKey`` for signing eval-run records.

    Loaded from ``CATHEDRAL_EVAL_SIGNING_KEY`` (32-byte raw private key in
    hex), matching the Polaris convention (``POLARIS_CATHEDRAL_SIGNING_KEY``).
    """

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._sk = private_key

    @classmethod
    def from_env_hex(cls, hex_str: str) -> EvalSigner:
        try:
            raw = bytes.fromhex(hex_str.strip())
        except ValueError as e:
            raise ValueError("CATHEDRAL_EVAL_SIGNING_KEY must be hex") from e
        if len(raw) != 32:
            raise ValueError(f"signing key must be 32 bytes, got {len(raw)}")
        return cls(Ed25519PrivateKey.from_private_bytes(raw))

    def sign(self, eval_run_dict: dict[str, Any]) -> str:
        payload = canonical_json(eval_run_dict)
        return base64.b64encode(self._sk.sign(payload)).decode("ascii")


__all__ = ["EvalSigner"]
