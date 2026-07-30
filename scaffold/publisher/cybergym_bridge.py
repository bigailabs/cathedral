"""CyberGym lane composition: MechanismSpec in, burn-safe allocation out.

This is the join between the ingested CyberGym reports and the ONE composed
weight vector. It is deliberately the whole lane in one place and nothing more:

  * build the CyberGym ``MechanismSpec`` (default DISABLED at fraction 0.0);
  * read the lane's score vector through
    ``mechanism_cybergym_adapter.cybergym_score_snapshot`` (one newest fresh
    complete report, freshness from the authenticated ``generated_at``);
  * compose through ``mechanism_eligibility.compose_eligible``, the sanctioned
    entry point that makes the registered-uid gate impossible to skip, with
    ``preserve_forfeited=True`` so an unproven share is never reallocated;
  * allocate the forfeited share to the burn IDENTITY, never to another miner and
    never to another lane.

Two properties make that last bullet real rather than aspirational:

  * The spec carries ``requires_forfeit_preservation=True``, so
    ``mechanism_router.compose`` RAISES ``ForfeitPreservationRequired`` if anyone
    ever composes this mechanism through the legacy renormalizing path. There is
    no silent fallback to reallocation, and this module is the single entry point
    that satisfies the requirement.
  * The burn destination is resolved from the configured burn HOTKEY through the
    same fresh ``metagraph_hotkeys`` snapshot the eligibility gate uses, with the
    UID-to-hotkey binding verified in both directions. A bare numeric UID is
    never trusted, and the pre-existing env default of UID 204 is deliberately
    not consulted, because UIDs are recycled when miners deregister and a guessed
    UID is how a forfeited share ends up in a miner's wallet. Unresolvable means
    refuse, see ``resolve_burn_destination``.

Default off, twice over: ``CATHEDRAL_CYBERGYM_MECHANISM_ENABLED`` is unset and
``CATHEDRAL_CYBERGYM_WEIGHT_FRACTION`` is 0.0, so merging this module changes no
live weight vector. Nothing in the publisher calls it yet: the live signed vector
is still built by ``weights.build_signed_vector`` from ``compose_scores`` alone.
When an owner does wire it in, it must run BEFORE the Ed25519 signature in
``weights.build_signed_vector`` so the forfeited share is inside the signed
payload rather than applied after it.

Safety posture:
  * Read-only. This module writes no table and calls no weight writer. The sole
    chain writer remains ``scaffold.validator_thin.set_weights_on_chain``.
  * Fail closed. Any condition that makes the CyberGym share unprovable, or that
    leaves the burn destination unresolved, yields an EMPTY allocation so the
    caller keeps its V1 vector. Paying nobody beats paying the wrong uid.
  * Deterministic. No randomness; the only clock input is the freshness
    comparison against the authenticated report timestamp.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from . import mechanism_eligibility
from .mechanism_cybergym_adapter import (
    MECHANISM_ID,
    TIER,
    cybergym_score_snapshot,
)
from .mechanism_router import MechanismSpec, ScoreVector, ScoreVectorMeta
from .store import Store
from .weights import (
    BURN_UID_ENV,
    NETUID_ENV,
    NETWORK_ENV,
    _load_fresh_metagraph_hotkeys,
    burn_hotkey,
)

logger = logging.getLogger(__name__)

MECHANISM_ENABLED_ENV = "CATHEDRAL_CYBERGYM_MECHANISM_ENABLED"
WEIGHT_FRACTION_ENV = "CATHEDRAL_CYBERGYM_WEIGHT_FRACTION"
OWNER_PUBKEY_ENV = "CATHEDRAL_CYBERGYM_PRODUCER_HOTKEY"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def mechanism_enabled() -> bool:
    return _env_bool(MECHANISM_ENABLED_ENV, False)


def _valid_fraction(value: Any, *, source: str) -> float:
    """A fraction is a number in [0, 1] or it is 0.0. No clamping upward.

    Clamping an out-of-range value to the maximum fails OPEN: someone writing
    ``25`` meaning 25 percent would have been given the entire vector. Anything
    unparseable, non-finite, negative, or above 1 is refused down to 0.0 and
    logged, so every wrong input fails in the same direction.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        logger.warning("%s is not a number; CyberGym fraction refused to 0.0", source)
        return 0.0
    try:
        out = float(value)
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r is not a number; CyberGym fraction refused to 0.0", source, value
        )
        return 0.0
    if not math.isfinite(out) or out < 0.0 or out > 1.0:
        logger.warning(
            "%s=%r is outside [0, 1]; CyberGym fraction refused to 0.0 (a "
            "percentage such as 25 is NOT 0.25)",
            source, value,
        )
        return 0.0
    return out


def weight_fraction() -> float:
    """The lane's share of the composed vector. Default 0.0 (contributes none).

    Anything that is not a number in [0, 1] becomes 0.0. See
    ``_valid_fraction``: out-of-range input is refused, never clamped up.
    """
    raw = (os.environ.get(WEIGHT_FRACTION_ENV) or "").strip()
    if not raw:
        return 0.0
    return _valid_fraction(raw, source=WEIGHT_FRACTION_ENV)


def cybergym_spec(mech_store: Any | None = None) -> MechanismSpec:
    """The CyberGym ``MechanismSpec``.

    A persisted spec wins when a ``MechanismStore`` is supplied, so an operator
    who registered the mechanism through the existing admin-gated
    ``PUT /mechanisms/{id}`` route keeps that as the source of truth. With no
    store the spec comes from configuration, disabled at fraction 0.0.

    A persisted spec is VALIDATED, not trusted. The admin route that writes the
    registry does not enforce the 0..1 range, so a persisted fraction of 25.0
    would otherwise bypass the same check the configuration path applies and
    allocate 25 times the whole vector. An out-of-range persisted fraction is
    refused to 0.0, which disables the lane rather than over-allocating it.

    ``requires_forfeit_preservation`` is always re-asserted, including on a
    persisted spec: the registry does not carry that column, and it is a
    code-level property of this mechanism rather than an operator setting. It is
    what makes ``mechanism_router.compose`` refuse to renormalize this lane's
    forfeited share onto other mechanisms.
    """
    if mech_store is not None:
        persisted = mech_store.get_spec(MECHANISM_ID)
        if persisted is not None:
            return replace(
                persisted,
                weight_fraction=_valid_fraction(
                    persisted.weight_fraction,
                    source=f"persisted MechanismSpec[{MECHANISM_ID}].weight_fraction",
                ),
                requires_forfeit_preservation=True,
            )
    return MechanismSpec(
        mechanism_id=MECHANISM_ID,
        owner_pubkey=(os.environ.get(OWNER_PUBKEY_ENV) or "").strip(),
        weight_fraction=weight_fraction(),
        tier=TIER,
        owner_uid=None,
        enabled=mechanism_enabled(),
        requires_forfeit_preservation=True,
    )


def resolve_burn_destination(
    store: Store, *, now: datetime
) -> tuple[int | None, dict[str, Any]]:
    """Resolve the burn UID from the CONFIGURED BURN HOTKEY, or refuse.

    A numeric burn UID is not an identity. UIDs are recycled as miners
    deregister, so paying "UID 204" because that is what an env default said is
    exactly how a forfeited share ends up in a miner's wallet. This resolves the
    destination by hotkey instead, through the SAME fresh ``metagraph_hotkeys``
    snapshot the eligibility gate uses, and verifies the binding both ways.

    Returns ``(uid, meta)``; ``uid`` is None with ``meta["reason"]`` set on any
    of: no configured burn hotkey, no fresh snapshot, the burn hotkey absent
    from the fresh snapshot, no UID for it, that UID bound to a different
    hotkey, or an explicitly configured burn UID that disagrees with the
    resolved one.
    """
    meta: dict[str, Any] = {"reason": "ok", "burn_hotkey_configured": False}
    hotkey = burn_hotkey()
    if not hotkey:
        meta["reason"] = "burn_hotkey_not_configured"
        return None, meta
    meta["burn_hotkey_configured"] = True

    network = os.environ.get(NETWORK_ENV, "finney")
    netuid = int(os.environ.get(NETUID_ENV, "39"))

    fresh, snapshot_meta = _load_fresh_metagraph_hotkeys(store, now=now)
    meta["snapshot"] = {
        k: snapshot_meta.get(k)
        for k in ("snapshot_fresh", "snapshot_hotkey_count", "snapshot_updated_at")
    }
    if fresh is None:
        meta["reason"] = "registration_snapshot_unavailable"
        return None, meta
    if hotkey not in fresh:
        meta["reason"] = "burn_hotkey_not_in_fresh_snapshot"
        return None, meta

    rows = store.query(
        "SELECT hotkey, uid FROM metagraph_hotkeys "
        "WHERE network=? AND netuid=? AND hotkey=?",
        (network, netuid, hotkey),
    )
    uid = None
    for row in rows:
        if row["uid"] is not None:
            uid = int(row["uid"])
            break
    if uid is None:
        meta["reason"] = "burn_hotkey_has_no_uid"
        return None, meta

    # Reverse binding: that UID must belong to this hotkey and no other, so a
    # stale duplicate row cannot redirect the share.
    bound = {
        str(r["hotkey"])
        for r in store.query(
            "SELECT hotkey FROM metagraph_hotkeys "
            "WHERE network=? AND netuid=? AND uid=?",
            (network, netuid, uid),
        )
    }
    if bound != {hotkey}:
        meta["reason"] = "burn_uid_hotkey_mismatch"
        meta["bound_hotkeys"] = sorted(bound)
        return None, meta

    # An explicitly configured burn UID must agree. The env default is
    # deliberately NOT consulted: only a value the operator actually set.
    configured_uid = (os.environ.get(BURN_UID_ENV) or "").strip()
    if configured_uid:
        try:
            expected = int(configured_uid)
        except ValueError:
            meta["reason"] = "burn_uid_configuration_invalid"
            return None, meta
        if expected != uid:
            meta["reason"] = "burn_uid_configuration_mismatch"
            meta["configured_burn_uid"] = expected
            return None, meta

    meta["burn_uid"] = uid
    meta["burn_hotkey"] = hotkey
    return uid, meta


def cybergym_allocation(
    store: Store,
    *,
    now: datetime | None = None,
    now_ms: int | None = None,
    mech_store: Any | None = None,
    extra_specs: list[MechanismSpec] | None = None,
    extra_scores: dict[str, tuple[ScoreVector, ScoreVectorMeta]] | None = None,
    max_score_age_ms: int | None = None,
) -> dict[str, Any]:
    """Compose the CyberGym lane and return a burn-safe uid allocation.

    ``extra_specs`` / ``extra_scores`` let a caller compose CyberGym together
    with other mechanisms (the SAT adapter, a signed-post mechanism) in the same
    single vector; the CyberGym spec is always the one built here.

    Returns a dict with:
      ``status``: ``disabled``, ``ok``, ``no_contribution``, or
                       ``burn_destination_unresolved``;
      ``weights``: ``{uid: weight}`` INCLUDING the burn uid's forfeited
                       share, or ``{}`` when the caller must keep its V1 vector;
      ``forfeited_fraction`` and ``contributing_fraction``: the explicit split;
      ``burn_uid``: where the forfeited share went, resolved by hotkey;
      ``burn``: how that destination was resolved, or why it was refused;
      ``cybergym``: the adapter's reason and provenance info;
      ``debug``: the router debug metadata, eligibility gate included.

    Never writes, never signs, never submits.
    """
    now = now or datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000) if now_ms is None else int(now_ms)
    spec = cybergym_spec(mech_store)

    result: dict[str, Any] = {
        "status": "disabled",
        "mechanism_id": spec.mechanism_id,
        "enabled": bool(spec.enabled),
        "fraction": float(spec.weight_fraction),
        "weights": {},
        "forfeited_fraction": 0.0,
        "contributing_fraction": 0.0,
        "burn_uid": None,
        "cybergym": {},
        "debug": {},
    }

    specs = list(extra_specs or [])
    specs.append(spec)
    scores: dict[str, tuple[ScoreVector, ScoreVectorMeta]] = dict(extra_scores or {})

    if not spec.enabled or float(spec.weight_fraction) <= 0.0:
        # Default path: the lane contributes nothing and allocates nothing, so
        # the caller's vector is untouched. compose() would report the whole
        # fraction as disabled/zero_fraction rather than forfeited.
        return result

    vector, meta, info = cybergym_score_snapshot(store, now=now)
    result["cybergym"] = info
    if vector:
        scores[spec.mechanism_id] = (vector, meta)

    weights, debug = mechanism_eligibility.compose_eligible(
        store,
        specs,
        scores,
        now_ms=now_ms,
        now=now,
        max_score_age_ms=max_score_age_ms,
        preserve_forfeited=True,
    )
    result["debug"] = debug
    forfeited = float(debug.get("forfeited_fraction") or 0.0)
    result["forfeited_fraction"] = forfeited
    result["contributing_fraction"] = float(debug.get("contributing_fraction") or 0.0)

    if forfeited <= 0.0:
        result["status"] = "ok" if weights else "no_contribution"
        result["weights"] = dict(weights)
        return result

    destination, burn_meta = resolve_burn_destination(store, now=now)
    result["burn"] = burn_meta
    if destination is None:
        # Fail closed: with no PROVEN burn identity, allocating anything would
        # either hand the forfeited share to other miners or pay whichever miner
        # currently holds a guessed UID.
        logger.warning(
            "cybergym_allocation: forfeited_fraction=%.6f but the burn "
            "destination is unresolved (%s); returning an empty allocation "
            "(caller keeps V1)",
            forfeited, burn_meta.get("reason"),
        )
        result["status"] = "burn_destination_unresolved"
        result["weights"] = {}
        return result

    allocated = dict(weights)
    destination = int(destination)
    if destination in allocated:
        # A burn uid that also earned mechanism weight would blur the two; keep
        # the burn share additive and record that it happened.
        result["burn_uid_also_scored"] = True
    allocated[destination] = allocated.get(destination, 0.0) + forfeited
    result["weights"] = allocated
    result["burn_uid"] = destination
    result["status"] = "ok"
    return result
