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

Closed-epoch gate (same contract as cathedral-distill's
``CyberGymScoreStore.require_closed_epoch`` / ``compose_scores_lane``): scores
are published only for epochs marked ``closed`` in ``cybergym_epoch_status``.
An open or incomplete epoch must not publish — composing mid-epoch (or after a
restart that lost durable solver state) would emit a vector indistinguishable
from "nobody solved", capturing that mechanism's allocation as burn. When a
specific ``epoch`` is requested and is not closed, this adapter raises so the
artifact refresh loop skips and leaves the previous stored vector untouched
rather than persisting an empty overwrite. When ``epoch`` is omitted, only
rows from closed epochs are considered.

Guardrails (identical to the SAT adapter):
  - Read-only: writes no table, modifies no write path.
  - Default off: nothing calls this until a ``MechanismSpec`` wires it into the
    router; it has no effect on any live weight vector on its own.
  - Deterministic: no randomness; the only time dependency is
    ``ScoreVectorMeta.signed_at_ms``, recording when the vector was built.
  - No secrets: reads the same two network/netuid env vars weights.py uses.
  - Unmapped hotkeys are dropped (logged), never zeroed into another UID; an
    empty result returns ``({}, meta)`` — the router's documented "contributes
    nothing this cycle" shape, never an exception (except the closed-epoch
    refusal above, which is intentional fail-closed).
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

# Must match cathedral-distill.cybergym_scores.EPOCH_CLOSED.
EPOCH_CLOSED = "closed"


class CyberGymEpochNotClosed(RuntimeError):
    """Raised when scores are requested for an epoch that is not safe to publish.

    The artifact refresh loop catches adapter exceptions and skips that
    mechanism, leaving the previous ``put_scores`` row untouched — so a mid-
    epoch refresh cannot wipe a prior closed vector with an empty one.
    """


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


def _epoch_state(store: Store, epoch: int) -> str | None:
    """Return the persisted lifecycle state for ``epoch``, or None if unknown.

    None covers a missing row and a missing ``cybergym_epoch_status`` table —
    both are "not closed". The CyberGym validator creates this table beside
    ``cybergym_scores``; an adapter that cannot see the marker cannot tell
    "nobody solved" from "scoring has not finished / state was lost".
    """
    try:
        rows = store.query(
            "SELECT state FROM cybergym_epoch_status WHERE epoch=?",
            (int(epoch),),
        )
    except Exception:  # noqa: BLE001 — missing table / backend error => not closed
        return None
    if not rows:
        return None
    state = rows[0]["state"]
    return None if state is None else str(state)


def require_closed_epoch(store: Store, epoch: int) -> None:
    """Fail closed unless ``epoch`` is marked ``closed`` in the same database."""
    state = _epoch_state(store, epoch)
    if state == EPOCH_CLOSED:
        return
    detail = "no cybergym_epoch_status row" if state is None else f"state={state!r}"
    raise CyberGymEpochNotClosed(
        f"refusing to publish CyberGym epoch {int(epoch)}: {detail}. "
        "Only epochs marked closed are safe to compose; publishing now would "
        "emit a vector indistinguishable from an epoch nobody solved."
    )


def _sum_score_rows(rows) -> dict[str, float]:
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


def _verified_scores(store: Store, *, epoch: int | None) -> dict[str, float]:
    """Verified per-miner CyberGym scores from the ``cybergym_scores`` table.

    One row per (miner_hotkey, epoch) with ``score`` = the level-weighted sum of
    verified solves the CyberGym validator derived for that miner. When ``epoch``
    is given, that epoch must be ``closed`` (else ``CyberGymEpochNotClosed``) and
    only its rows are summed. When ``epoch`` is omitted, the latest **closed**
    score per hotkey is used — open/incomplete epochs never contribute.
    Negative or non-numeric scores are ignored defensively — the router expects
    a non-negative vector.
    """
    if epoch is not None:
        require_closed_epoch(store, int(epoch))
        rows = store.query(
            "SELECT miner_hotkey, score FROM cybergym_scores WHERE epoch=?",
            (int(epoch),),
        )
        return _sum_score_rows(rows)

    # Latest closed epoch per hotkey. If the status table is missing, the JOIN
    # fails closed to an empty vector (no exception — "contributes nothing").
    try:
        rows = store.query(
            "SELECT s.miner_hotkey AS miner_hotkey, s.score AS score "
            "FROM cybergym_scores AS s "
            "WHERE (s.miner_hotkey, s.epoch) IN ("
            "  SELECT s2.miner_hotkey, MAX(s2.epoch) FROM cybergym_scores AS s2 "
            "  INNER JOIN cybergym_epoch_status AS st "
            "    ON st.epoch = s2.epoch AND st.state = ? "
            "  GROUP BY s2.miner_hotkey"
            ")",
            (EPOCH_CLOSED,),
        )
    except Exception as exc:  # noqa: BLE001 — missing table => contribute nothing
        logger.info(
            "cybergym_mechanism_scores: closed-epoch lookup unavailable (%s); "
            "contributing nothing this cycle",
            exc,
        )
        return {}
    return _sum_score_rows(rows)


def cybergym_mechanism_scores(
    store: Store,
    *,
    epoch: int | None = None,
) -> tuple[ScoreVector, ScoreVectorMeta]:
    """Verified CyberGym scores remapped from miner_hotkey to miner uid.

    Returns ``({}, meta)`` when there are no verified scores from a closed
    epoch, or none of the scored hotkeys map to a UID — the router's
    documented fallback. Raises ``CyberGymEpochNotClosed`` when a specific
    ``epoch`` is requested and is not marked closed.
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
