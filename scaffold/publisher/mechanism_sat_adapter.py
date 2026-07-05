"""SAT mechanism adapter — plugs V2 SAT solves into the mechanism router.

Turns the EXISTING verified V2 SAT scores (scaffold.publisher.v2_pipeline
.score_totals, which already sums verified per-miner solves in the V2 store)
into a router ScoreVector keyed by miner UID, per
deploy/MECHANISM_ROUTER_CONTRACT.md. This is the first "artifact"-tier
mechanism (proof-backed, not a signed claim) meant to plug into the router.

hotkey -> uid mapping comes from the ``metagraph_hotkeys`` table — the same
out-of-band snapshot table scaffold/publisher/weights.py already reads for
its payable-hotkeys policy (see weights._load_fresh_metagraph_hotkeys and the
NETWORK_ENV/NETUID_ENV settings). Hotkeys with no row (or a NULL uid) in that
table are dropped, not zeroed.

Guardrails:
  - Read-only: does not write to any table, does not import/modify
    v2_pipeline.py or weights.py's write paths.
  - Default off: nothing in the publisher calls this yet; it has no effect
    on any live weight vector until a MechanismSpec wires it into the router.
  - Deterministic: no randomness; the only time dependency is
    ScoreVectorMeta.signed_at_ms, which records when the vector was built
    (not used in the score math itself).
  - No secrets: reads two env vars for network/netuid (same ones weights.py
    already uses), no keys or credentials touched.
"""
from __future__ import annotations

import logging
import os
import time

from .mechanism_router import ScoreVector, ScoreVectorMeta
from .store import Store
from .v2_pipeline import score_totals
from .weights import NETUID_ENV, NETWORK_ENV

logger = logging.getLogger(__name__)

MECHANISM_ID = "sat_v2"
SOURCE = "sat_adapter"
# Tier this mechanism should be registered under when a MechanismSpec is
# created for it (ScoreVectorMeta itself carries no tier field per contract).
TIER = "artifact"


def _load_hotkey_to_uid(store: Store, *, network: str, netuid: int) -> dict[str, int]:
    """hotkey -> uid from the metagraph_hotkeys snapshot table.

    This is a plain mapping read (no freshness/age filtering) — the router's
    ``compose`` is what decides staleness for a mechanism's whole score
    vector via ``meta.signed_at_ms`` / ``max_score_age_ms``, mirroring how
    weights.py treats the same table for payable-hotkeys.
    """
    rows = store.query(
        "SELECT hotkey, uid FROM metagraph_hotkeys WHERE network=? AND netuid=?",
        (network, netuid),
    )
    mapping: dict[str, int] = {}
    for row in rows:
        hotkey = str(row["hotkey"])
        uid = row["uid"]
        if uid is None:
            continue
        mapping[hotkey] = int(uid)
    return mapping


def sat_mechanism_scores(
    store: Store,
    *,
    since_iso: str | None = None,
    epoch: int | None = None,
) -> tuple[ScoreVector, ScoreVectorMeta]:
    """Verified V2 SAT scores remapped from miner_hotkey to miner uid.

    Sums verified per-miner solves via
    ``scaffold.publisher.v2_pipeline.score_totals`` (already de-duplicated
    across the manifest/bitset submit paths and filtered to
    status="verified"), then maps each ``miner_hotkey`` to a UID using the
    publisher's existing ``metagraph_hotkeys`` snapshot.

    Hotkeys with no UID mapping are dropped (logged, not raised or zeroed
    into someone else's score). If there are no verified scores, or none of
    the verified hotkeys map to a UID, this returns ``({}, meta)`` — the
    router's documented fallback shape for "this mechanism contributes
    nothing this cycle," never an exception.
    """
    network = os.environ.get(NETWORK_ENV, "finney")
    netuid = int(os.environ.get(NETUID_ENV, "39"))

    totals = score_totals(store, since_iso=since_iso, epoch=epoch)
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
            "sat_mechanism_scores: dropped %d/%d verified hotkeys with no uid "
            "mapping in metagraph_hotkeys (network=%s netuid=%s)",
            dropped, len(totals), network, netuid,
        )

    meta = ScoreVectorMeta(
        mechanism_id=MECHANISM_ID,
        signed_at_ms=int(time.time() * 1000),
        sig_ok=True,
        source=SOURCE,
    )
    return vector, meta
