"""Signed weight policy surface (issue #155).

The publisher builds a deterministic weight vector keyed by miner hotkey
(not uid), signs it with a Cathedral-controlled Ed25519 key, and serves
it from ``GET /v1/validator/weights/next``. Validators that opt in fetch
this vector, verify the signature, map hotkeys to live uids against the
local metagraph, drop missing hotkeys, renormalize, and relay through
the existing chain.set_weights path.

Local validator behaviour is unchanged unless the operator flips the
opt-in. The wire shape and signing rules are pinned here so the
publisher and validator agree on bytes without round-tripping through
storage representations.
"""

from cathedral.policy.signing import (
    BurnSnapshot,
    SignedWeightVector,
    WeightEntry,
    canonical_bytes,
    sign_vector,
    verify_vector,
)

__all__ = [
    "BurnSnapshot",
    "SignedWeightVector",
    "WeightEntry",
    "canonical_bytes",
    "sign_vector",
    "verify_vector",
]
