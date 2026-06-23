"""Proof anchoring — turn a whole round into ONE verifiable commitment.

Every agent's run-receipt head (its tamper-evident trace digest) plus the signed
weight vector are folded into a binary **Merkle root**. The root is Ed25519-signed
into a round commitment that COULD be written on-chain as a Bittensor commitment
(we do NOT write mainnet — the commitment is computed + stored; the chain write is
a deferred one-call step). Anyone holding the receipts can:
  * recompute the root and check it matches the commitment (round integrity), and
  * prove a single agent's proof was included, via a Merkle inclusion path
    (without revealing the other agents' proofs).

This is the "rewards proof, not claims" capstone: the round's result is itself a
checkable proof object, not an assertion.
"""
from __future__ import annotations

import hashlib
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey)


def _h(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def _leaf(s: str) -> bytes:
    """A leaf is a 32-byte node: a 64-hex digest is used directly, else hashed."""
    try:
        if len(s) == 64:
            return bytes.fromhex(s)
    except ValueError:
        pass
    return _h(s.encode())


def merkle_root(leaves: list[str]) -> str:
    if not leaves:
        return _h(b"cathedral-empty-round").hex()
    nodes = [_leaf(s) for s in leaves]
    while len(nodes) > 1:
        if len(nodes) % 2:
            nodes.append(nodes[-1])                      # duplicate last (standard)
        nodes = [_h(nodes[i] + nodes[i + 1]) for i in range(0, len(nodes), 2)]
    return nodes[0].hex()


def merkle_proof(leaves: list[str], index: int) -> list[dict]:
    """Sibling path for leaves[index] -> root. Each step: {hash, side}."""
    nodes = [_leaf(s) for s in leaves]
    path: list[dict] = []
    idx = index
    while len(nodes) > 1:
        if len(nodes) % 2:
            nodes.append(nodes[-1])
        sib = idx ^ 1
        path.append({"hash": nodes[sib].hex(), "side": "right" if idx % 2 == 0 else "left"})
        nodes = [_h(nodes[i] + nodes[i + 1]) for i in range(0, len(nodes), 2)]
        idx //= 2
    return path


def verify_merkle_proof(leaf: str, path: list[dict], root: str) -> bool:
    cur = _leaf(leaf)
    for step in path:
        sib = bytes.fromhex(step["hash"])
        cur = _h(cur + sib) if step["side"] == "right" else _h(sib + cur)
    return cur.hex() == root


# -- the round commitment -----------------------------------------------------

def round_leaves(agents, signed_vector) -> list[str]:
    """Sorted leaves: every agent's receipt head + the signed-vector digest."""
    leaves = sorted(a.run.trace_sha256 for a in agents if a.run.trace_sha256)
    sv = json.dumps(signed_vector, sort_keys=True, separators=(",", ":")).encode()
    leaves.append(hashlib.sha256(sv).hexdigest())
    return leaves


def build_anchor(agents, signed_vector, *, season: str, round_no: int,
                 epoch: int, anchored_at: str = "") -> dict:
    """Compute + sign the round commitment (the anchorable proof object)."""
    leaves = round_leaves(agents, signed_vector)
    root = merkle_root(leaves)
    sk = Ed25519PrivateKey.generate()
    pub = sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    body = {
        "season": season, "round": round_no, "epoch": epoch,
        "merkle_root": root, "n_leaves": len(leaves),
        "policy_version": signed_vector.get("policy_version"),
        "anchored_at": anchored_at, "pubkey": pub,
        "anchor_target": "bittensor commitment (DEFERRED — no mainnet write)",
    }
    canon = json.dumps({k: v for k, v in body.items() if k != "signature"},
                       sort_keys=True, separators=(",", ":")).encode()
    body["signature"] = sk.sign(canon).hex()
    return body


def verify_anchor(commitment: dict, agents, signed_vector) -> bool:
    """Independently re-derive the root from the round + check the signature."""
    if merkle_root(round_leaves(agents, signed_vector)) != commitment.get("merkle_root"):
        return False
    try:
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(commitment["pubkey"]))
        canon = json.dumps({k: v for k, v in commitment.items() if k != "signature"},
                           sort_keys=True, separators=(",", ":")).encode()
        pk.verify(bytes.fromhex(commitment["signature"]), canon)
        return True
    except Exception:
        return False
