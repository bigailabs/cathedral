"""CyberGym mechanism adapter — plugs verified PoC scores into the router.

A sibling of ``mechanism_sat_adapter.py``. The router is workload-agnostic
(``deploy/MECHANISM_ROUTER_CONTRACT.md``: "SAT now; Hermes/Secure-Compute
later"), so CyberGym integrates as a *new mechanism*, not a router change. This
adapter turns the CyberGym validator's verified per-miner scores into a router
``ScoreVector`` keyed by miner UID.

Tier is ``artifact`` (proof-backed), not ``signed`` (a claim): a CyberGym score
is the level-weighted sum of *verified* PoC solves — each a differential crash
test (the PoC crashes the vulnerable build and not the patched one), which the
validator re-derives and never trusts a worker to report. That is the same
proof-backed posture as the SAT mechanism.

Score semantics mirror SAT: emission is **proportional to verified work**, not
winner-take-all. The CyberGym king-of-the-hill frontier is a separate
leaderboard/corpus-lineage concept (who is the reference model); emission
rewards every miner in proportion to the verified solves it produced this cycle.
A deployment that wants winner-take-all can write only the champion's row.

The verified scores are read from the ``cybergym_scores`` table, which the
CyberGym validator writes after verification (one row per miner_hotkey per
epoch, ``score`` = level-weighted verified solves). ``hotkey -> uid`` mapping
comes from the ``metagraph_hotkeys`` snapshot table, exactly as the SAT adapter
and ``mechanism_eligibility`` use it.

Guardrails (identical to the SAT adapter):
  - Read-only: writes no table, modifies no write path.
  - Default off: nothing calls this until a ``MechanismSpec`` wires it into the
    router; it has no effect on any live weight vector on its own.
  - Deterministic: no randomness; the only time dependency is
    ``ScoreVectorMeta.signed_at_ms``, recording when the vector was built.
  - No secrets: reads the same two network/netuid env vars weights.py uses.
  - Unmapped hotkeys are dropped (logged), never zeroed into another UID; an
    empty result returns ``({}, meta)`` — the router's documented "contributes
    nothing this cycle" shape, never an exception.
"""
from __future__ import annotations

import logging
import os
import time

from .mechanism_router import ScoreVector, ScoreVectorMeta
from .store import Store
from .weights import NETUID_ENV, NETWORK_ENV

logger = logging.getLogger(__name__)

MECHANISM_ID = "cybergym_v0"
SOURCE = "cybergym_adapter"
# Tier to register the MechanismSpec under (ScoreVectorMeta carries no tier
# field per contract). Proof-backed, like SAT.
TIER = "artifact"


def _load_hotkey_to_uid(store: Store, *, network: str, netuid: int) -> dict[str, int]:
    """hotkey -> uid from the metagraph_hotkeys snapshot table.

    A plain mapping read (no freshness filtering) — the router's ``compose``
    decides staleness for the whole vector via ``meta.signed_at_ms`` /
    ``max_score_age_ms``, mirroring how weights.py and the SAT adapter treat the
    same table.
    """
    rows = store.query(
        "SELECT hotkey, uid FROM metagraph_hotkeys WHERE network=? AND netuid=?",
        (network, netuid),
    )
    mapping: dict[str, int] = {}
    for row in rows:
        uid = row["uid"]
        if uid is None:
            continue
        mapping[str(row["hotkey"])] = int(uid)
    return mapping


def _verified_scores(store: Store, *, epoch: int | None) -> dict[str, float]:
    """Verified per-miner CyberGym scores from the ``cybergym_scores`` table.

    One row per (miner_hotkey, epoch) with ``score`` = the level-weighted sum of
    verified solves the CyberGym validator derived for that miner. When ``epoch``
    is given, only that epoch's rows are summed; otherwise the latest score per
    hotkey is used. Negative or non-numeric scores are ignored defensively — the
    router expects a non-negative vector.
    """
    if epoch is None:
        rows = store.query(
            "SELECT miner_hotkey, score FROM cybergym_scores "
            "WHERE (miner_hotkey, epoch) IN "
            "(SELECT miner_hotkey, MAX(epoch) FROM cybergym_scores "
            " GROUP BY miner_hotkey)"
        )
    else:
        rows = store.query(
            "SELECT miner_hotkey, score FROM cybergym_scores WHERE epoch=?",
            (int(epoch),),
        )
    totals: dict[str, float] = {}
    for row in rows:
        try:
            value = float(row["score"])
        except (TypeError, ValueError):
            continue
        if value <= 0.0:
            continue
        hotkey = str(row["miner_hotkey"])
        totals[hotkey] = totals.get(hotkey, 0.0) + value
    return totals


def cybergym_mechanism_scores(
    store: Store,
    *,
    epoch: int | None = None,
) -> tuple[ScoreVector, ScoreVectorMeta]:
    """Verified CyberGym scores remapped from miner_hotkey to miner uid.

    Returns ``({}, meta)`` when there are no verified scores, or none of the
    scored hotkeys map to a UID — the router's documented fallback, never an
    exception.
    """
    network = os.environ.get(NETWORK_ENV, "finney")
    netuid = int(os.environ.get(NETUID_ENV, "39"))

    totals = _verified_scores(store, epoch=epoch)
    hotkey_to_uid = _load_hotkey_to_uid(store, network=network, netuid=netuid)

    vector: ScoreVector = {}
    dropped = 0
    for hotkey, score in totals.items():
        uid = hotkey_to_uid.get(hotkey)
        if uid is None:
            dropped += 1
            continue
        vector[uid] = vector.get(uid, 0.0) + float(score)

    if dropped:
        logger.info(
            "cybergym_mechanism_scores: dropped %d/%d verified hotkeys with no "
            "uid mapping in metagraph_hotkeys (network=%s netuid=%s)",
            dropped, len(totals), network, netuid,
        )

    meta = ScoreVectorMeta(
        mechanism_id=MECHANISM_ID,
        signed_at_ms=int(time.time() * 1000),
        sig_ok=True,
        source=SOURCE,
    )
    return vector, meta
