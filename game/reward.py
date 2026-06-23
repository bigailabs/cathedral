"""The reward rule — Const's structure, made concrete.

    reward = linear_metric_to_maximize x boolean_gate

For each credited solve c by miner i:

    contrib(c) = verified(c) x attest_gate(c) x tier_weight(t_c) x speed(c)
                 \___ booleans (0/1) ___/      \___ continuous, linear ___/

    metric_i   = Σ_c contrib(c)                 # the linear metric to maximize
    reward_i   = liveness_gate_i x metric_i     # one more boolean gate, miner-level

Final weight = reward_i normalized to the leader, with Sybil hotkeys collapsed by
coldkey BEFORE normalizing (so splitting one operator across hotkeys gains
nothing — mirrors scaffold/publisher/weights.py). The vector is Ed25519-signed,
the same emission shape a validator relays.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class SolveCredit:
    """One (miner, challenge) outcome, with every factor of the rule explicit."""
    hotkey: str
    challenge_id: str
    tier: int
    verified: bool          # boolean gate: witness satisfies THIS miner's CNF
    attest_gate: bool       # boolean gate: attested compute when the tier needs it
    tier_weight: float      # linear: harder tier pays more
    speed: float            # linear: fastest-wins curve in (0,1)
    reason: str = ""        # why a gate tripped (for the scoreboard)

    @property
    def contrib(self) -> float:
        gate = (1.0 if self.verified else 0.0) * (1.0 if self.attest_gate else 0.0)
        return gate * self.tier_weight * self.speed


@dataclass
class MinerResult:
    hotkey: str
    coldkey: str
    archetype: str
    live: bool
    metric: float = 0.0
    reward: float = 0.0
    weight: float = 0.0
    credits: list[SolveCredit] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def compose(
    miners: list[tuple[str, str, str, bool]],   # (hotkey, coldkey, archetype, live)
    credits: list[SolveCredit],
) -> dict[str, MinerResult]:
    """Apply the rule and the Sybil-collapsed normalization. Returns hotkey->result."""
    by_hk: dict[str, MinerResult] = {}
    for hk, ck, arch, live in miners:
        by_hk[hk] = MinerResult(hk, ck, arch, live)

    for c in credits:
        r = by_hk.get(c.hotkey)
        if r is None:
            continue
        r.credits.append(c)
        r.metric += c.contrib

    # reward = liveness_gate x metric
    for r in by_hk.values():
        r.reward = (1.0 if r.live else 0.0) * r.metric

    # Sybil collapse: sum reward at the coldkey, normalize to the top coldkey,
    # then split a coldkey's weight evenly across its hotkeys.
    ck_reward: dict[str, float] = {}
    ck_hotkeys: dict[str, list[str]] = {}
    for r in by_hk.values():
        ck_reward[r.coldkey] = ck_reward.get(r.coldkey, 0.0) + r.reward
        ck_hotkeys.setdefault(r.coldkey, []).append(r.hotkey)

    top = max(ck_reward.values()) if ck_reward else 0.0
    if top > 0:
        for ck, total in ck_reward.items():
            share = (total / top) / len(ck_hotkeys[ck])
            for hk in ck_hotkeys[ck]:
                by_hk[hk].weight = round(share, 6)
    return by_hk


def naive_weights(results: dict[str, MinerResult]) -> dict[str, float]:
    """The Sybil-VULNERABLE normalization: normalize each hotkey to the top
    hotkey reward, with NO coldkey collapse. Used only to SHOW what the collapse
    prevents — a coldkey with k hotkeys would collect up to k x its fair share."""
    top = max((r.reward for r in results.values()), default=0.0)
    if top <= 0:
        return {hk: 0.0 for hk in results}
    return {hk: round(r.reward / top, 6) for hk, r in results.items()}


def coldkey_totals(weights_by_hotkey: dict[str, float],
                   coldkey_of: dict[str, str]) -> dict[str, float]:
    """Sum a per-hotkey weight vector up to coldkeys."""
    totals: dict[str, float] = {}
    for hk, w in weights_by_hotkey.items():
        ck = coldkey_of.get(hk, hk)
        totals[ck] = totals.get(ck, 0.0) + w
    return totals


# -- emission: the signed weight vector ---------------------------------------

def _canonical_bytes(payload: dict) -> bytes:
    body = {k: v for k, v in payload.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_vector(results: dict[str, MinerResult], *, policy_version: int) -> dict:
    """Ed25519-sign the normalized per-miner weight vector (the emission)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    weights = [
        {"miner_hotkey": hk, "weight": r.weight}
        for hk, r in sorted(results.items()) if r.weight > 0
    ]
    sk = Ed25519PrivateKey.generate()
    pub = sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    payload = {
        "policy_version": policy_version,
        "netuid": 39,
        "weights": weights,
        "burn_uid": 204,
        "pubkey": pub.hex(),     # bound into the signed bytes (verifier reads it back)
    }
    payload["signature"] = sk.sign(_canonical_bytes(payload)).hex()
    return payload


def verify_vector(payload: dict) -> bool:
    """Independently verify the signed vector (what a validator does)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(payload["pubkey"]))
        pk.verify(bytes.fromhex(payload["signature"]), _canonical_bytes(payload))
        return True
    except Exception:
        return False
