""""Who can earn" eligibility gate for the Mechanism Router.

``mechanism_router.compose(...)`` already drops any uid not present in the
``registered_uids`` set it is given — the gate is entirely about building that
set correctly and never letting a caller forget to pass it. This module is
that set-builder plus a wrapper that makes the gate mandatory.

Source of truth: the SAME out-of-band ``metagraph_hotkeys`` snapshot V1
already reads. We deliberately reuse ``weights._load_fresh_metagraph_hotkeys``
rather than inventing a second registration source — one snapshot, one
freshness policy (``CATHEDRAL_WEIGHTS_PAYABLE_HOTKEYS_MAX_AGE_SECS``, default
600s), one NETWORK_ENV/NETUID_ENV pair. hotkey -> uid mapping is the same
``SELECT hotkey, uid FROM metagraph_hotkeys`` query
``mechanism_sat_adapter._load_hotkey_to_uid`` uses.

FAIL CLOSED: if the snapshot is missing, empty, or stale, ``eligible_uids``
returns an EMPTY set, never "all uids" / no filter. Paying nobody beats paying
an unverified set. Every real weight-set caller in this codebase MUST build
its ``registered_uids`` via ``eligible_uids`` (or the ``compose_eligible``
wrapper below) rather than calling ``mechanism_router.compose`` directly with
a hand-rolled uid set — as of this writing there is no live weight-set loop
that calls ``compose`` yet (``mechanism_weightset.py`` only signs/dry-runs an
already-composed ``{uid: weight}`` table it is handed), so this module has no
call site to wire in the router today. When one is added, it must go through
``compose_eligible``.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from . import mechanism_router
from .store import Store
from .weights import NETUID_ENV, NETWORK_ENV, _load_fresh_metagraph_hotkeys

logger = logging.getLogger(__name__)

MIN_STAKE_ENV = "CATHEDRAL_MECH_MIN_STAKE"


def _min_stake() -> float:
    try:
        return float(os.environ.get(MIN_STAKE_ENV, "") or 0.0)
    except ValueError:
        return 0.0


def _load_hotkey_to_uid(store: Store, *, network: str, netuid: int) -> dict[str, int]:
    """hotkey -> uid from the metagraph_hotkeys snapshot table.

    Same query as ``mechanism_sat_adapter._load_hotkey_to_uid`` — deliberately
    not imported from there to avoid a cross-module dependency for one query;
    kept byte-identical in shape so both surfaces agree on what "has a uid"
    means.
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


def eligible_uids(
    store: Store,
    *,
    now: datetime | None = None,
    min_stake: float | None = None,
) -> tuple[set[int], dict[str, Any]]:
    """The set of uids currently eligible to earn mechanism weight.

    Fail-closed: any failure to establish a FRESH, non-empty registered-hotkey
    snapshot returns an EMPTY set (never "all uids", never "skip the filter").

    Registration source: ``weights._load_fresh_metagraph_hotkeys`` — the exact
    freshness cutoff (``payable_hotkeys_max_age_secs``, default 600s) and
    network/netuid V1 already uses for its own payable-hotkeys policy.

    min_stake: metagraph_hotkeys has NO stake column today, so this is
    registration-only. If a positive min_stake is requested we log a WARNING
    that it is a no-op (not silently ignored) rather than fabricate a stake
    value. TODO(stake-gate): once the metagraph poller captures stake into
    metagraph_hotkeys (or a companion table), wire real stake filtering here.
    """
    now = now or datetime.now(timezone.utc)
    stake_floor = _min_stake() if min_stake is None else float(min_stake)

    network = os.environ.get(NETWORK_ENV, "finney")
    netuid = int(os.environ.get(NETUID_ENV, "39"))

    hotkeys, snapshot_meta = _load_fresh_metagraph_hotkeys(store, now=now)

    meta: dict[str, Any] = {
        "network": network,
        "netuid": netuid,
        "registered_count": 0,
        "eligible_uid_count": 0,
        "fail_closed": False,
        "reason": "ok",
        "min_stake_requested": stake_floor,
        "min_stake_enforced": False,
    }
    meta.update(snapshot_meta)

    if hotkeys is None:
        meta["fail_closed"] = True
        meta["reason"] = "snapshot_unavailable"
        meta["registered_count"] = 0
        meta["eligible_uid_count"] = 0
        return set(), meta

    meta["registered_count"] = len(hotkeys)

    if stake_floor > 0.0:
        logger.warning(
            "mechanism_eligibility: %s=%s requested but metagraph_hotkeys has no "
            "stake column yet — stake filtering is a NO-OP (registration-only "
            "gate applied instead). See TODO(stake-gate) in mechanism_eligibility.py.",
            MIN_STAKE_ENV, stake_floor,
        )
        meta["min_stake_enforced"] = False
    else:
        meta["min_stake_enforced"] = True  # trivially satisfied: floor is 0

    hotkey_to_uid = _load_hotkey_to_uid(store, network=network, netuid=netuid)
    uids: set[int] = set()
    unmapped = 0
    for hotkey in hotkeys:
        uid = hotkey_to_uid.get(hotkey)
        if uid is None:
            unmapped += 1
            continue
        uids.add(uid)

    if unmapped:
        meta["unmapped_hotkeys"] = unmapped
        logger.info(
            "mechanism_eligibility: %d/%d fresh registered hotkeys had no uid in "
            "metagraph_hotkeys (network=%s netuid=%s)",
            unmapped, len(hotkeys), network, netuid,
        )

    meta["eligible_uid_count"] = len(uids)
    return uids, meta


def compose_eligible(
    store: Store,
    specs: list[mechanism_router.MechanismSpec],
    scores: dict[str, tuple[mechanism_router.ScoreVector, mechanism_router.ScoreVectorMeta]],
    *,
    now_ms: int,
    now: datetime | None = None,
    **compose_kwargs: Any,
) -> tuple[dict[int, float], dict[str, Any]]:
    """``mechanism_router.compose`` wrapped so the eligibility gate cannot be
    skipped — this is the ONLY sanctioned entry point for real weight-set
    callers. It builds ``registered_uids`` via ``eligible_uids`` (fail-closed)
    and passes it into ``compose`` for the caller; there is no parameter to
    override it with a hand-rolled set.
    """
    uids, elig_meta = eligible_uids(store, now=now)
    weights, debug = mechanism_router.compose(
        specs,
        scores,
        registered_uids=uids,
        now_ms=now_ms,
        **compose_kwargs,
    )
    debug["eligibility"] = elig_meta
    return weights, debug
