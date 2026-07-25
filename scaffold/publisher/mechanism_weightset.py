"""Artifact-only weight-set + receipt stage for the mechanism router.

This is the CHAIN-ADJACENT piece: it takes the composed ``{uid: weight}`` table
from ``mechanism_router.compose(...)`` and turns it into a *signed weight
artifact* (schema ``cathedral.mechanism_weights.v1``) that we can inspect
and audit.  This legacy helper has no chain-writing capability.

Safety posture (mirrors ``deploy/MECHANISM_ROUTER_CONTRACT.md`` §Isolation):

  * ALWAYS DRY-RUN. ``set_weights`` computes + signs + logs + records the
    artifact but never invokes the caller-provided callback.  An arbitrary
    callback cannot prove which chain it writes, so accepting one would let a
    nominal testnet request launder an SN39/Finney submission.
  * HARD REFUSE on ``network == "finney"`` / mainnet / a known mainnet netuid —
    ``set_weights`` raises before doing anything.
  * No secrets in code or logs; deterministic output (sorted, rounded).

Reuses the signing + audit-bundle style from ``v2_pipeline.py`` (Ed25519 over
``canonical_bytes`` + a merkle root over per-leaf hashes) and the canonical-bytes
helper from ``wire_vector`` so the emission is byte-stable.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..wire_vector import canonical_bytes

log = logging.getLogger(__name__)

SCHEMA = "cathedral.mechanism_weights.v1"
# Retained as a compatibility symbol for callers/tests. It no longer enables
# any broadcast path.
LIVE_ENV = "CATHEDRAL_MECH_WEIGHTSET_LIVE"

# --- network / netuid classification (the hard safety boundary) -------------
# Anything not explicitly testnet is treated as unsafe and refused.
MAINNET_NETWORKS = frozenset({"finney", "mainnet", "main"})
TESTNET_NETWORKS = frozenset({"test", "testnet", "local", "mock"})
# SN39 is Cathedral's mainnet subnet; never emit for it here regardless of net.
MAINNET_NETUIDS = frozenset({39})


class UnsafeNetworkError(RuntimeError):
    """Raised when a weight-set is requested against mainnet / finney."""


def _normalize_network(network: str) -> str:
    return str(network or "").strip().lower()


def is_mainnet(network: str, netuid: int) -> bool:
    net = _normalize_network(network)
    return net in MAINNET_NETWORKS or int(netuid) in MAINNET_NETUIDS


def is_testnet(network: str, netuid: int) -> bool:
    net = _normalize_network(network)
    return net in TESTNET_NETWORKS and int(netuid) not in MAINNET_NETUIDS


def _assert_target_allowed(network: str, netuid: int) -> None:
    """Fail closed unless this is a recognized non-SN39 testnet target."""
    if is_mainnet(network, netuid):
        raise UnsafeNetworkError(
            "refusing legacy mechanism weight-set on mainnet/SN39: "
            f"network={network!r} netuid={netuid}; use the immutable "
            "cathedral-validator release"
        )
    if not is_testnet(network, netuid):
        raise UnsafeNetworkError(
            f"refusing mechanism weight-set on unrecognised network={network!r} netuid={netuid} "
            f"(allowed testnet networks: {sorted(TESTNET_NETWORKS)})"
        )


def _now_iso_ms() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _leaf_hash(uid: int, weight: float) -> str:
    body = {"uid": int(uid), "weight": round(float(weight), 9)}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def build_weight_artifact(
    composed: dict[int, float],
    *,
    netuid: int,
    network: str,
    signing_key_hex: str,
    allocation: Any | None = None,
    key_id: str | None = None,
) -> dict[str, Any]:
    """Build a signed ``cathedral.mechanism_weights.v1`` artifact.

    ``composed`` is the ``{uid: weight}`` table from ``mechanism_router.compose``.
    Output is deterministic: uids sorted, weights rounded, merkle root over the
    per-uid leaf hashes, Ed25519 signature over ``canonical_bytes(payload)``.

    NOTE: this only *builds and signs*. It never broadcasts and imposes no
    network gate on its own — ``set_weights`` owns the mainnet refusal. Callers
    that build directly are responsible for not shipping the result to mainnet.
    """
    # deterministic, non-negative, finite weights only
    clean: dict[int, float] = {}
    for uid, w in composed.items():
        fw = float(w)
        if not math.isfinite(fw):
            continue
        if fw <= 0:
            continue
        clean[int(uid)] = round(fw, 9)

    weights = [{"uid": uid, "weight": clean[uid]} for uid in sorted(clean)]
    leaves = [_leaf_hash(w["uid"], w["weight"]) for w in weights]
    root = (
        hashlib.sha256("".join(sorted(leaves)).encode()).hexdigest()
        if leaves
        else hashlib.sha256(b"").hexdigest()
    )
    now = _now_iso_ms()
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "artifact_id": str(uuid.uuid4()),
        "testnet": True,
        "network": _normalize_network(network),
        "netuid": int(netuid),
        "generated_at": now,
        "policy_version": int(datetime.now(timezone.utc).timestamp() * 1000),
        "n_uids": len(weights),
        "weights": weights,
        "allocation_snapshot": allocation,
        "merkle_root": "sha256:" + root,
        "leaves": leaves,
        "receipt_id": "sha256:" + hashlib.sha256(root.encode()).hexdigest(),
        "key_id": key_id
        or os.environ.get("CATHEDRAL_WEIGHT_POLICY_KEY_ID", "cathedral-weight-policy"),
    }
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(signing_key_hex.strip()))
    payload["signature"] = base64.b64encode(sk.sign(canonical_bytes(payload))).decode()
    return payload


def _live_env() -> bool:
    return str(os.environ.get(LIVE_ENV, "")).strip().lower() == "true"


def set_weights(
    composed: dict[int, float],
    *,
    netuid: int,
    network: str,
    signing_key_hex: str,
    allocation: Any | None = None,
    confirm: bool = False,
    broadcast_fn: Callable[[dict[str, Any]], Any] | None = None,
    key_id: str | None = None,
) -> dict[str, Any]:
    """Compute, sign, and record an artifact without any chain write.

    HARD REFUSE: raises ``UnsafeNetworkError`` on mainnet/finney/mainnet netuid
    before building anything.

    ``confirm`` and ``broadcast_fn`` remain accepted only for source
    compatibility. They are deliberately ignored and the callback is never
    invoked.
    """
    # (0) hard boundary — do this first, before we build or sign anything.
    _assert_target_allowed(network, netuid)

    artifact = build_weight_artifact(
        composed,
        netuid=netuid,
        network=network,
        signing_key_hex=signing_key_hex,
        allocation=allocation,
        key_id=key_id,
    )
    would_set = {w["uid"]: w["weight"] for w in artifact["weights"]}

    gates = {
        "live_env": _live_env(),
        "target_allowed": is_testnet(network, netuid),
        "confirm": bool(confirm),
        "broadcast_fn": broadcast_fn is not None,
    }

    # Record the artifact for the read endpoint. This module is permanently
    # artifact-only; the immutable cathedral-validator release is the sole
    # repository path allowed to submit weights.
    publish_next(artifact)
    requested = any((gates["live_env"], gates["confirm"], gates["broadcast_fn"]))
    reason = (
        "dry_run: legacy mechanism broadcaster permanently disabled; "
        "artifact published only"
    )
    log.info(
        "mechanism weight-set ARTIFACT-ONLY "
        "(network=%s netuid=%s n_uids=%s live_requested=%s)",
        network,
        netuid,
        artifact["n_uids"],
        requested,
    )
    return {
        "mode": "dry_run",
        "broadcast": False,
        "would_set": would_set,
        "artifact": artifact,
        "reason": reason,
    }


# --- read endpoint: latest artifact -----------------------------------------
# In-process holder for the most recently built weight-set artifact. This is a
# read-only convenience for operators to inspect the NEXT (testnet) weight-set;
# it is deliberately separate from the real /v1 vector path.
_LATEST: dict[str, Any] | None = None


def publish_next(artifact: dict[str, Any]) -> None:
    global _LATEST
    _LATEST = artifact


def latest_next() -> dict[str, Any] | None:
    return _LATEST


def build_router():
    """APIRouter serving ``GET /mechanisms/weights/next``.

    Kept out of ``app.py`` on purpose — a caller opts in via
    ``app.include_router(mechanism_weightset.build_router())``. The response is
    always stamped ``testnet: true`` so it can never be mistaken for the real
    signed /v1 weight vector.
    """
    from fastapi import APIRouter, HTTPException

    router = APIRouter(tags=["mechanisms"])

    @router.get("/mechanisms/weights/next")
    def get_next_weightset() -> dict[str, Any]:
        art = latest_next()
        if art is None:
            raise HTTPException(
                status_code=404, detail="no mechanism weight-set built yet"
            )
        # defensive re-stamp: never let this look like the real /v1 vector
        return {**art, "testnet": True}

    return router
