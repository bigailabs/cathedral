"""Signed final-scores vector — the orchestrator's ONE number per miner.

This is the v4 scoring interface. The orchestrator composes whatever scoring
it wants (multiple challenge types, recency, arena payouts) into a single
per-hotkey weight, signs the vector, and serves it at
``GET /v1/validator/weights/next``. A validator's whole job is: verify the
signature, sanity-check, apply burn from the same signed payload, set weights.
No row pulling, no local averaging, no 7-day window — every scoring decision
lives HERE and can change without a validator release.

Wire shape is byte-compatible with the vector deployed validators already
verify (cathedral.policy.signing.SignedWeightVector): canonical bytes = drop
``signature``, sort keys, no whitespace, UTF-8; Ed25519 over that. Env knob
names match the live publisher so config carries over on the domain swap.

Score composition (the recency gate lives here, not in validator code):
  * window: only solves in the trailing CATHEDRAL_WEIGHTS_WINDOW_HOURS count
    (default 24h). A miner who stops solving drops out of the vector when the
    window passes — this replaces the validator-side 7-day mean whose frozen
    tail let idle miners coast for a week.
  * mode `flat_recent`: every hotkey with >=1 accepted solve in the
    window gets equal weight — byte-faithful to today's economics (flat 1.0
    rows) minus the stale tail.
  * mode `proportional` (default): weight = distinct challenges solved in the window,
    multiplied by explicit tier importance weights, relative to the busiest
    solver. The dial to turn when we want harder or more important tiers to
    pay more — flipped by env, no validator involvement.
  * mode `row_score_recent` (default-off): weight = sum of positive
    eval_runs.row_json weighted_score values in the window, relative to the
    top scorer. This is the explicit mode that makes attested row score
    upgrades observable in the active signed vector.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .store import Store
# Shared verify surface lives in the dependency-light module so a validator
# install doesn't drag in FastAPI/store; re-exported here for the orchestrator's
# callers and the gates (one import surface).
from ..wire_vector import (  # noqa: F401
    MAX_VECTOR_ENTRIES,
    VectorError,
    canonical_bytes,
    invariant_check,
    verify_signature,
)

# Env knobs — SAME names as the live publisher (config carries over).
SIGNING_KEY_ENV = "CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY"   # falls back to app key
KEY_ID_ENV = "CATHEDRAL_WEIGHT_POLICY_KEY_ID"
NETWORK_ENV = "CATHEDRAL_WEIGHT_POLICY_NETWORK"
NETUID_ENV = "CATHEDRAL_WEIGHT_POLICY_NETUID"
BURN_UID_ENV = "CATHEDRAL_WEIGHT_POLICY_BURN_UID"
BURN_PERCENTAGE_ENV = "CATHEDRAL_WEIGHT_POLICY_FORCED_BURN_PERCENTAGE"
VALID_FOR_ENV = "CATHEDRAL_WEIGHT_POLICY_VALID_FOR_SECS"
# v4-only composition knobs.
WINDOW_HOURS_ENV = "CATHEDRAL_WEIGHTS_WINDOW_HOURS"
MODE_ENV = "CATHEDRAL_WEIGHTS_MODE"                       # flat_recent | proportional | row_score_recent
ROW_SCORE_TASK_TYPES_ENV = "CATHEDRAL_WEIGHTS_ROW_SCORE_TASK_TYPES"
# Difficulty-weighted scoring. CATHEDRAL_WEIGHTS_TIER_WEIGHTS accepts JSON
# {"1":1,"2":3,"3":8} or comma form "1=1,2=3,3=8". If unset, preserve the
# existing launch default: tier 1 = 1.0, tier 2 = CATHEDRAL_WEIGHTS_TIER2_MULT.
TIER_WEIGHTS_ENV = "CATHEDRAL_WEIGHTS_TIER_WEIGHTS"
TIER2_MULT_ENV = "CATHEDRAL_WEIGHTS_TIER2_MULT"
# Transitional per-miner incentive. When >0, shared-board scoring remains the
# base and verified per-miner solves add a bounded normalized bonus. This lets
# miners migrate without replacing the live scorer in one step.
PERMINER_BONUS_MULT_ENV = "CATHEDRAL_PERMINER_BONUS_MULT"
PERMINER_REQUIRE_COLDKEY_ENV = "CATHEDRAL_PERMINER_REQUIRE_COLDKEY"
PERMINER_HISTORY_FLOOR_ENV = "CATHEDRAL_PERMINER_HISTORY_FLOOR"
PERMINER_SCORING_MODE_ENV = "CATHEDRAL_PERMINER_SCORING_MODE"

_CACHE_TTL_SECS = 60.0
_vector_cache: dict[str, tuple[float, dict[str, Any]]] = {}
# Serializes the cache-miss build so concurrent misses can't each call
# next_policy_version() and emit two different vectors with the same
# policy_version (the orchestrator is single-instance — a process lock suffices).
_build_lock = threading.Lock()
# Background refresh state.  A single daemon thread rebuilds the vector every
# _CACHE_TTL_SECS; all request handlers read from _vector_cache without ever
# blocking on the DB query.  _bg_started tracks whether the thread is running
# so we only ever spawn one.
_bg_started = False
_bg_lock = threading.Lock()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def window_hours() -> float:
    return _env_float(WINDOW_HOURS_ENV, 24.0)


def mode() -> str:
    m = os.environ.get(MODE_ENV, "proportional").strip().lower()
    return m if m in ("flat_recent", "proportional", "row_score_recent") else "proportional"


def row_score_task_types() -> set[str]:
    raw = os.environ.get(
        ROW_SCORE_TASK_TYPES_ENV,
        "synthetic_boolean_v1,solver_attestation_v1,audit_replay_v1,audit_arena_v1",
    )
    return {
        item.strip()
        for item in raw.replace(";", ",").split(",")
        if item.strip()
    }


def burn_percentage() -> float:
    return min(100.0, max(0.0, _env_float(BURN_PERCENTAGE_ENV, 85.0)))


def burn_uid() -> int | None:
    raw = os.environ.get(BURN_UID_ENV, "204").strip()
    return int(raw) if raw else None


def _ms_iso(dt: datetime) -> str:
    """ISO-8601 UTC, ms precision, trailing Z — the live vector convention."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    s = dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    return s + "Z"


# -- score composition --------------------------------------------------------

def tier_from_challenge_id(cid: str) -> int:
    """Parse lane ids whose second token is t{N}, for example sat-t2-*.

    Defaults to 1 on any parse failure. This keeps arbitrary ids containing
    "-t2-" from silently changing emissions.
    """
    try:
        match = re.match(r"^(?:sat|audit|pm)[-_]t(\d+)(?:[-_]|$)", cid)
        if match:
            return int(match.group(1))
    except (TypeError, ValueError):
        pass
    return 1


def tier2_multiplier() -> float:
    """Weight multiplier applied to tier2 challenges relative to tier1.
    Default 3.0 — a tier2 solve counts 3× a tier1 solve in proportional mode.
    Set CATHEDRAL_WEIGHTS_TIER2_MULT=1.0 to disable (byte-identical to pre-AJM scoring)."""
    return _env_float(TIER2_MULT_ENV, 3.0)


def _valid_weight(value: Any) -> float | None:
    try:
        weight = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(weight) or weight <= 0.0:
        return None
    return weight


def tier_weights() -> dict[int, float]:
    """Tier importance weights used by proportional scoring.

    The default preserves the current economic intent: tier 1 is the
    participation floor and tier 2 is the harder differentiator. Operators can
    add future tiers without a code deploy by setting
    CATHEDRAL_WEIGHTS_TIER_WEIGHTS to JSON or comma form.
    """
    raw = os.environ.get(TIER_WEIGHTS_ENV, "").strip()
    default = {1: 1.0, 2: tier2_multiplier()}
    if not raw:
        return default
    parsed: dict[int, float] = {}
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            items = obj.items()
        elif isinstance(obj, list):
            items = enumerate(obj, start=1)
        else:
            items = ()
        for key, value in items:
            tier = int(str(key).strip())
            weight = _valid_weight(value)
            if tier > 0 and weight is not None:
                parsed[tier] = weight
    except Exception:
        for part in raw.split(","):
            if not part.strip():
                continue
            sep = "=" if "=" in part else ":"
            if sep not in part:
                continue
            key, value = part.split(sep, 1)
            try:
                tier = int(key.strip())
            except ValueError:
                continue
            weight = _valid_weight(value.strip())
            if tier > 0 and weight is not None:
                parsed[tier] = weight
    return parsed or default


def tier_weight(tier: int) -> float:
    weights = tier_weights()
    return weights.get(int(tier), weights.get(1, 1.0))


def perminer_bonus_multiplier() -> float:
    """Small additive bonus for miners using per-miner unique assignments."""
    return min(1.0, max(0.0, _env_float(PERMINER_BONUS_MULT_ENV, 0.2)))


def perminer_history_floor() -> float:
    """Minimum bonus share for assigned-beta miners with little recent history."""
    return min(1.0, max(0.0, _env_float(PERMINER_HISTORY_FLOOR_ENV, 0.25)))


def perminer_scoring_mode() -> str:
    """How verified per-miner solves affect the live vector.

    bonus: keep shared SAT scoring as base, then add a bounded assigned bonus.
    assigned_only: replace shared scoring with the assigned-only vector.
    """
    raw = os.environ.get(PERMINER_SCORING_MODE_ENV, "bonus").strip().lower()
    return raw if raw in {"bonus", "assigned_only"} else "bonus"


def coldkey_collapse_enabled() -> bool:
    """Opt-in Sybil hardening. OFF by default so this is byte-identical to today
    until an operator flips it AND a hotkey->coldkey map is supplied."""
    return os.environ.get("CATHEDRAL_WEIGHTS_COLDKEY_COLLAPSE", "").strip().lower() in {
        "1", "true", "yes", "on"}


def perminer_require_coldkey() -> bool:
    raw = os.environ.get(PERMINER_REQUIRE_COLDKEY_ENV, "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _perminer_scores(store: Store) -> dict[str, float]:
    """Current-epoch normalized per-miner scores, or empty when disabled/no solves."""
    from . import per_miner as pm
    if not pm.perminer_enabled():
        return {}
    return pm.compute_perminer_scores(store, pm.current_epoch())


def _perminer_compose_scores(store: Store, *, ident=lambda hk: hk) -> dict[str, float] | None:
    """Per-miner scoring path. Returns scores when CATHEDRAL_PERMINER_ENABLED is on
    AND not in shadow-only mode. Returns None when flag is off (caller falls
    through to existing scoring — byte-identical to pre-flag behaviour).

    Shadow mode: flag is on + CATHEDRAL_PERMINER_SHADOW=1 → compute the vector,
    log it, but return None so the LIVE vector stays the current scoring. This
    lets us run shadow comparisons without touching the live board.
    """
    from . import per_miner as pm
    if not pm.perminer_enabled():
        return None  # flag off: zero change
    epoch = pm.current_epoch()
    try:
        rows = store.query(
            "SELECT miner_hotkey, challenge_id, difficulty_weight "
            "FROM per_miner_solves WHERE epoch=? AND verified=1 ",
            (epoch,),
        )
    except Exception:
        rows = []
    hk_totals: dict[str, float] = {}
    hk_seen: dict[str, set[str]] = {}
    for r in rows:
        hk = str(r["miner_hotkey"])
        cid = str(r["challenge_id"])
        score = float(r["difficulty_weight"] or 0.0)
        if score <= 0.0:
            continue
        if cid in hk_seen.get(hk, set()):
            continue
        hk_seen.setdefault(hk, set()).add(cid)
        hk_totals[hk] = hk_totals.get(hk, 0.0) + score
    identity_best: dict[str, float] = {}
    hks: dict[str, set[str]] = {}
    for hk, total in hk_totals.items():
        idk = str(ident(hk))
        identity_best[idk] = max(identity_best.get(idk, 0.0), total)
        hks.setdefault(idk, set()).add(hk)
    scores: dict[str, float] = {}
    if identity_best:
        top = max(identity_best.values())
        if top > 0.0:
            for idk, total in identity_best.items():
                per = round((total / top) / len(hks[idk]), 6)
                for hk in hks[idk]:
                    scores[hk] = per
    if pm.perminer_shadow():
        # Shadow: log the vector for comparison but don't serve it.
        print(f"[per_miner] shadow_vector epoch={epoch} scores={scores}")
        return None  # fall through to live scoring
    return scores if scores else None


def _apply_perminer_bonus(
    store: Store,
    base: dict[str, float],
    coldkey_of: dict[str, str] | None = None,
) -> dict[str, float]:
    """Add a transition bonus for per-miner adopters without replacing base scoring."""
    bonus = perminer_bonus_multiplier()
    if bonus <= 0.0:
        return base
    pm_scores = _perminer_scores(store)
    if not pm_scores:
        return base
    combined = dict(base)
    if perminer_require_coldkey() and not coldkey_of:
        return base
    use_ck = bool(coldkey_of)
    if not use_ck:
        top_base = max(base.values()) if base else 0.0
        history_floor = perminer_history_floor()
        for hk, score in pm_scores.items():
            history = 1.0 if top_base <= 0.0 else combined.get(hk, 0.0) / top_base
            history_mult = history_floor + (1.0 - history_floor) * max(0.0, min(1.0, history))
            combined[hk] = combined.get(hk, 0.0) + bonus * float(score) * history_mult
    else:
        def mapped(hk: str) -> str:
            return coldkey_of[hk]  # type: ignore[index]

        members: dict[str, set[str]] = {}
        best: dict[str, float] = {}
        history: dict[str, float] = {}
        for hk in set(base) | set(pm_scores):
            if hk not in coldkey_of:  # type: ignore[operator]
                continue
            members.setdefault(mapped(hk), set()).add(hk)
        for hk, score in base.items():
            if hk not in coldkey_of:  # type: ignore[operator]
                continue
            idk = mapped(hk)
            history[idk] = max(history.get(idk, 0.0), float(score))
        for hk, score in pm_scores.items():
            if hk not in coldkey_of:  # type: ignore[operator]
                continue
            idk = mapped(hk)
            best[idk] = max(best.get(idk, 0.0), float(score))
        top_history = max(history.values()) if history else 0.0
        history_floor = perminer_history_floor()
        for idk, score in best.items():
            hks = members.get(idk) or set()
            if not hks:
                continue
            recent = 1.0 if top_history <= 0.0 else history.get(idk, 0.0) / top_history
            history_mult = history_floor + (1.0 - history_floor) * max(0.0, min(1.0, recent))
            per_hotkey_bonus = (bonus * score * history_mult) / len(hks)
            for hk in hks:
                combined[hk] = combined.get(hk, 0.0) + per_hotkey_bonus
    top = max(combined.values()) if combined else 0.0
    if top <= 0.0:
        return {}
    return {hk: round(v / top, 6) for hk, v in combined.items()}


def _load_coldkey_map(store: Store) -> dict[str, str] | None:
    """hotkey->coldkey, refreshed out-of-band into the ``coldkey_map`` table by
    a small metagraph poller (the thin publisher has no chain access of its own).
    Returns None when the table is missing/empty so scoring stays per-hotkey
    (fail-open: a missing or partial map can never zero an honest miner)."""
    try:
        rows = store.query("SELECT hotkey, coldkey FROM coldkey_map")
    except Exception:
        return None
    m = {str(r["hotkey"]): str(r["coldkey"]) for r in rows}
    return m or None


def _load_scoring_coldkey_map(store: Store) -> dict[str, str] | None:
    """Load coldkey identity when base scoring or assigned-beta needs it."""
    if (
        coldkey_collapse_enabled()
        or perminer_require_coldkey()
        or perminer_scoring_mode() == "assigned_only"
        or perminer_bonus_multiplier() > 0.0
    ):
        return _load_coldkey_map(store)
    return None


def scoring_identity_for_hotkey(store: Store, hotkey: str) -> str:
    """Return the scoring identity for hotkey-bound beta lanes.

    When coldkey collapse is enabled and a map row exists, per-miner challenge
    assignment uses the coldkey too. This makes sybil stacking pointless at the
    work-assignment layer, not just after score normalization.
    """
    if not coldkey_collapse_enabled():
        return hotkey
    try:
        rows = store.query("SELECT coldkey FROM coldkey_map WHERE hotkey=? LIMIT 1", (hotkey,))
    except Exception:
        return hotkey
    if not rows:
        return hotkey
    return str(rows[0]["coldkey"] or hotkey)


def _positive_row_weighted_score(row_json: Any) -> float | None:
    """Extract a finite, positive weighted_score from eval_runs.row_json."""
    try:
        row = json.loads(row_json) if isinstance(row_json, str) else row_json
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(row, dict):
        return None
    raw = row.get("weighted_score")
    if isinstance(raw, bool):
        return None
    try:
        score = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(score) or score <= 0.0:
        return None
    return score


def _compose_row_score_recent(
    store: Store,
    since: str,
    *,
    ident=lambda hk: hk,
) -> dict[str, float]:
    """Opt-in row-score composer.

    This is intentionally default-off: it makes attested row_json score upgrades
    visible in the signed weight vector only when CATHEDRAL_WEIGHTS_MODE is set
    to row_score_recent.
    """
    allowed_task_types = row_score_task_types()
    rows = store.query(
        "SELECT miner_hotkey, task_type, row_json FROM eval_runs "
        "WHERE ran_at > ? AND attested=1",
        (since,))
    totals: dict[str, float] = {}
    hks: dict[str, set[str]] = {}
    for r in rows:
        if str(r["task_type"]) not in allowed_task_types:
            continue
        score = _positive_row_weighted_score(r["row_json"])
        if score is None:
            continue
        hk = str(r["miner_hotkey"])
        idk = str(ident(hk))
        totals[idk] = totals.get(idk, 0.0) + score
        hks.setdefault(idk, set()).add(hk)
    if not totals:
        return {}
    top = max(totals.values())
    if top <= 0.0:
        return {}
    result: dict[str, float] = {}
    for idk, score in totals.items():
        per = round((score / top) / len(hks[idk]), 6)
        for hk in hks[idk]:
            result[hk] = per
    return result


def _proportional_ledger_has_rows(store: Store, since: str) -> bool:
    rows = store.query(
        "SELECT 1 FROM lane_challenge_solves s "
        "LEFT JOIN lane_challenges c ON c.challenge_id = s.challenge_id "
        "WHERE s.solved_at_iso > ? AND COALESCE(c.score_multiplier, 1.0) > 0 "
        "LIMIT 1",
        (since,),
    )
    return bool(rows)


def _perminer_policy_status(store: Store | None = None) -> dict[str, Any]:
    """Surface per-miner flag state so a score-source flip is never silent."""
    try:
        from . import per_miner as pm
    except Exception:
        return {
            "perminer_enabled": False,
            "perminer_shadow": False,
            "perminer_live_requested": False,
            "perminer_epoch": None,
            "perminer_has_scores": False,
            "score_source": None,
            "scoring_mode": perminer_scoring_mode(),
            "bonus_multiplier": perminer_bonus_multiplier(),
            "history_floor": perminer_history_floor(),
            "coldkey_required": perminer_require_coldkey(),
        }
    enabled = pm.perminer_enabled()
    shadow = pm.perminer_shadow()
    epoch = pm.current_epoch() if enabled else None
    has_scores = False
    if enabled and store is not None and epoch is not None:
        has_scores = bool(pm.compute_perminer_scores(store, epoch))
    live_requested = enabled and not shadow
    return {
        "perminer_enabled": enabled,
        "perminer_shadow": shadow,
        "perminer_live_requested": live_requested,
        "perminer_epoch": epoch,
        "perminer_has_scores": has_scores,
        "score_source": "per_miner" if live_requested and has_scores
        and perminer_scoring_mode() == "assigned_only" else None,
        "scoring_mode": perminer_scoring_mode(),
        "bonus_multiplier": perminer_bonus_multiplier(),
        "history_floor": perminer_history_floor(),
        "coldkey_required": perminer_require_coldkey(),
    }


def _effective_mode(store: Store, since: str) -> str:
    requested = mode()
    if requested == "proportional" and not _proportional_ledger_has_rows(store, since):
        return "flat_recent_fallback"
    return requested


def explain_miner_score(
    store: Store, miner_hotkey: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """Miner-facing score explanation for the current signed-vector policy.

    This endpoint companion is deliberately read-only: it explains the current
    composer inputs and never affects the signed vector.
    """
    now = now or datetime.now(timezone.utc)
    since = _ms_iso(now - timedelta(hours=window_hours()))
    coldkey_of = _load_scoring_coldkey_map(store)
    scores = compose_scores(store, now=now, coldkey_of=coldkey_of)
    pm_status = _perminer_policy_status(store)
    requested = mode()
    effective = _effective_mode(store, since)
    source = pm_status["score_source"] or effective
    hotkey = str(miner_hotkey)
    base: dict[str, Any] = {
        "miner_hotkey": hotkey,
        "window_hours": window_hours(),
        "since": since,
        "requested_mode": requested,
        "effective_mode": effective,
        "score_source": source,
        "normalized_weight": float(scores.get(hotkey, 0.0)),
        "top_weight": max(scores.values()) if scores else 0.0,
        "miner_count": len(scores),
        "tier_weights": tier_weights(),
        "perminer": {
            "enabled": pm_status["perminer_enabled"],
            "shadow": pm_status["perminer_shadow"],
            "live_requested": pm_status["perminer_live_requested"],
            "epoch": pm_status["perminer_epoch"],
            "has_scores": pm_status["perminer_has_scores"],
            "scoring_mode": pm_status["scoring_mode"],
            "bonus_multiplier": pm_status["bonus_multiplier"],
            "history_floor": pm_status["history_floor"],
            "coldkey_required": pm_status["coldkey_required"],
        },
    }

    if source == "per_miner":
        try:
            from . import per_miner as pm
            rows = store.query(
                "SELECT tier, COUNT(*) AS solves, SUM(difficulty_weight) AS units "
                "FROM per_miner_solves WHERE epoch=? AND miner_hotkey=? AND verified=1 "
                "GROUP BY tier ORDER BY tier",
                (pm.current_epoch(), hotkey),
            )
            top_rows = store.query(
                "SELECT miner_hotkey, SUM(difficulty_weight) AS units "
                "FROM per_miner_solves WHERE epoch=? AND verified=1 "
                "GROUP BY miner_hotkey",
                (pm.current_epoch(),),
            )
            raw_units = sum(float(r["units"] or 0.0) for r in rows)
            top_units = max((float(r["units"] or 0.0) for r in top_rows), default=0.0)
            base.update({
                "raw_units": round(raw_units, 6),
                "top_units": round(top_units, 6),
                "distinct_challenges": int(sum(int(r["solves"] or 0) for r in rows)),
                "tiers": [
                    {
                        "tier": int(r["tier"]),
                        "solves": int(r["solves"] or 0),
                        "weighted_units": round(float(r["units"] or 0.0), 6),
                    }
                    for r in rows
                ],
            })
            return base
        except Exception as exc:
            base["explain_error"] = f"per_miner_explain_failed:{type(exc).__name__}"
            return base

    if effective == "proportional":
        rows = store.query(
            "SELECT DISTINCT s.miner_hotkey, s.challenge_id "
            "FROM lane_challenge_solves s "
            "LEFT JOIN lane_challenges c ON c.challenge_id = s.challenge_id "
            "WHERE s.solved_at_iso > ? AND COALESCE(c.score_multiplier, 1.0) > 0",
            (since,),
        )
        by_miner: dict[str, dict[str, Any]] = {}
        weights_by_tier = tier_weights()
        for r in rows:
            hk = str(r["miner_hotkey"])
            cid = str(r["challenge_id"])
            tier = tier_from_challenge_id(cid)
            weight = float(weights_by_tier.get(tier, weights_by_tier.get(1, 1.0)))
            entry = by_miner.setdefault(hk, {"units": 0.0, "seen": set(), "tiers": {}})
            if cid in entry["seen"]:
                continue
            entry["seen"].add(cid)
            entry["units"] += weight
            tier_entry = entry["tiers"].setdefault(tier, {"solves": 0, "units": 0.0})
            tier_entry["solves"] += 1
            tier_entry["units"] += weight
        own = by_miner.get(hotkey, {"units": 0.0, "seen": set(), "tiers": {}})
        top_units = max((float(v["units"]) for v in by_miner.values()), default=0.0)
        base.update({
            "raw_units": round(float(own["units"]), 6),
            "top_units": round(top_units, 6),
            "distinct_challenges": len(own["seen"]),
            "tiers": [
                {
                    "tier": tier,
                    "solves": int(v["solves"]),
                    "weighted_units": round(float(v["units"]), 6),
                    "score_weight": float(weights_by_tier.get(tier, weights_by_tier.get(1, 1.0))),
                }
                for tier, v in sorted(own["tiers"].items())
            ],
        })
        return base

    if effective == "row_score_recent":
        rows = store.query(
            "SELECT task_type, row_json FROM eval_runs "
            "WHERE ran_at > ? AND miner_hotkey=? AND attested=1",
            (since, hotkey),
        )
        total = 0.0
        accepted = 0
        for r in rows:
            if str(r["task_type"]) not in row_score_task_types():
                continue
            score = _positive_row_weighted_score(r["row_json"])
            if score is None:
                continue
            accepted += 1
            total += score
        base.update({
            "raw_units": round(total, 6),
            "accepted_rows": accepted,
            "distinct_challenges": accepted,
            "tiers": [],
        })
        return base

    feed = store.query(
        "SELECT COUNT(DISTINCT id) AS n FROM eval_runs WHERE ran_at > ? AND miner_hotkey=?",
        (since, hotkey),
    )
    accepted = int(feed[0]["n"] or 0) if feed else 0
    base.update({
        "raw_units": 1.0 if accepted else 0.0,
        "accepted_rows": accepted,
        "distinct_challenges": accepted,
        "tiers": [],
    })
    return base


def compose_scores(
    store: Store, *, now: datetime | None = None,
    coldkey_of: dict[str, str] | None = None,
) -> dict[str, float]:
    """One final number per hotkey, from solves inside the trailing window.

    This is where multi-challenge scoring composes: community solves today;
    arena/champion payouts and future challenge types add their term here and
    the validator interface never changes.

    IDENTITY-AWARE SCORING (the Sybil fix). When coldkey collapse is enabled AND
    a hotkey->coldkey map is supplied, a distinct challenge is credited ONCE PER
    COLDKEY -- the union of solves across all of that coldkey's hotkeys -- and
    the coldkey's score is then split across its solving hotkeys. So:
      * mirroring one solve onto k hotkeys adds NOTHING (same challenge_id, one
        entry in the coldkey's set) -> cloning earns zero extra;
      * solving MORE distinct challenges earns more, even across many hotkeys ->
        honest volume is fully rewarded, not punished.
    With no map (default) identity == hotkey, so this is byte-identical to the
    prior per-hotkey proportional scoring.

    flat_recent reads the signed feed (eval_runs) -- seeded history keeps the
    vector populated from the first second after a cutover. proportional needs
    the per-challenge claim ledger; it falls back to flat until that ledger has
    in-window data.
    """
    # Per-miner path (flag-gated). When the flag is off this is a no-op and
    # the rest of the function runs unchanged — byte-identical to pre-flag.
    now = now or datetime.now(timezone.utc)
    since = _ms_iso(now - timedelta(hours=window_hours()))
    use_ck = coldkey_collapse_enabled() and bool(coldkey_of)

    if (
        perminer_scoring_mode() == "assigned_only"
        and perminer_require_coldkey()
        and not coldkey_of
    ):
        from . import per_miner as pm
        if pm.perminer_enabled() and not pm.perminer_shadow():
            return {}

    def ident(hk: str) -> str:
        return coldkey_of.get(hk, hk) if use_ck else hk

    pm_scores = _perminer_compose_scores(store, ident=ident)
    if pm_scores is not None and perminer_scoring_mode() == "assigned_only":
        return pm_scores

    if mode() == "row_score_recent":
        return _apply_perminer_bonus(
            store,
            _compose_row_score_recent(store, since, ident=ident),
            coldkey_of,
        )

    if mode() == "proportional":
        rows = store.query(
            "SELECT DISTINCT s.miner_hotkey, s.challenge_id "
            "FROM lane_challenge_solves s "
            "LEFT JOIN lane_challenges c ON c.challenge_id = s.challenge_id "
            "WHERE s.solved_at_iso > ? AND COALESCE(c.score_multiplier, 1.0) > 0",
            (since,))
        # identity -> weighted score (sum of per-challenge tier weights, deduped)
        scores_w: dict[str, float] = {}
        # identity -> set of distinct challenge_ids (for dedup)
        seen: dict[str, set] = {}
        hks: dict[str, set] = {}     # identity -> set of its solving hotkeys
        weights_by_tier = tier_weights()
        for r in rows:
            hk = str(r["miner_hotkey"]); idk = ident(hk)
            cid = str(r["challenge_id"])
            if cid not in seen.get(idk, set()):
                seen.setdefault(idk, set()).add(cid)
                tier = tier_from_challenge_id(cid)
                weight = weights_by_tier.get(tier, weights_by_tier.get(1, 1.0))
                scores_w[idk] = scores_w.get(idk, 0.0) + weight
            hks.setdefault(idk, set()).add(hk)
        if scores_w:
            top = max(scores_w.values())
            base: dict[str, float] = {}
            for idk, w in scores_w.items():
                per = round((w / top) / len(hks[idk]), 6)
                for hk in hks[idk]:
                    base[hk] = per
            return _apply_perminer_bonus(store, base, coldkey_of)
        # no in-window claim rows -> fall through to flat

    feed = store.query(
        "SELECT DISTINCT miner_hotkey FROM eval_runs WHERE ran_at > ?", (since,))
    hotkeys = {str(r["miner_hotkey"]) for r in feed}
    if not use_ck:
        return _apply_perminer_bonus(store, {hk: 1.0 for hk in hotkeys}, coldkey_of)
    # flat, identity-deduped: each coldkey's hotkeys share a single 1.0
    groups: dict[str, list[str]] = {}
    for hk in hotkeys:
        groups.setdefault(ident(hk), []).append(hk)
    out: dict[str, float] = {}
    for members in groups.values():
        per = round(1.0 / len(members), 6)
        for hk in members:
            out[hk] = per
    return _apply_perminer_bonus(store, out, coldkey_of)


# -- monotonic policy_version (validator rollback fence) -----------------------

def next_policy_version(store: Store) -> int:
    """Monotonic AND continuous with the live orchestrator: the deployed
    validators' rollback fences hold the live emitter's epoch-ms versions
    (~1.78e12), so a counter restarting at 1 would be rejected as a rollback
    by every fence. Epoch-ms keeps any successor emitter automatically ahead;
    max(stored+1, now_ms) keeps it strictly monotonic even within one ms."""
    now_ms = int(time.time() * 1000)

    def _bump(conn):
        row = conn.execute(
            "SELECT last_policy_version FROM weight_policy_state WHERE id = 1"
        ).fetchone()
        nxt = max((int(row[0]) if row else 0) + 1, now_ms)
        conn.execute(
            "INSERT OR REPLACE INTO weight_policy_state(id, last_policy_version, updated_at_iso) "
            "VALUES (1, ?, ?)", (nxt, _ms_iso(datetime.now(timezone.utc))))
        return nxt
    return store.write(_bump)


# -- sign -----------------------------------------------------------------------

def build_signed_vector(store: Store, *, signing_key_hex: str,
                        now: datetime | None = None) -> dict[str, Any]:
    """Compose scores, assemble the wire payload, sign. Returns the dict
    served verbatim by /v1/validator/weights/next."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    now = now or datetime.now(timezone.utc)
    coldkey_of = _load_scoring_coldkey_map(store)
    since = _ms_iso(now - timedelta(hours=window_hours()))
    scores = compose_scores(store, now=now, coldkey_of=coldkey_of)
    requested_mode = mode()
    effective_mode = _effective_mode(store, since)
    proportional_ledger_empty = requested_mode == "proportional" and effective_mode == "flat_recent_fallback"
    pm_status = _perminer_policy_status(store)
    score_source = pm_status["score_source"] or effective_mode
    valid_for = _env_float(VALID_FOR_ENV, 1800.0)
    policy_inputs = {
        "mode": requested_mode, "effective_mode": effective_mode,
        "score_source": score_source,
        "window_hours": window_hours(),
        "burn": burn_percentage(), "burn_uid": burn_uid(),
        "tier_weights": tier_weights(),
        "hotkeys": sorted(scores), "scores": [scores[k] for k in sorted(scores)],
    }
    payload: dict[str, Any] = {
        "vector_id": str(uuid.uuid4()),
        "policy_version": next_policy_version(store),
        "network": os.environ.get(NETWORK_ENV, "finney"),
        "netuid": int(os.environ.get(NETUID_ENV, "39")),
        "generated_at": _ms_iso(now),
        "expires_at": _ms_iso(now + timedelta(seconds=valid_for)),
        "burn_snapshot": {
            "burn_uid": burn_uid(),
            "forced_burn_percentage": burn_percentage(),
        },
        "policy_hash": "sha256:" + hashlib.sha256(
            json.dumps(policy_inputs, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "key_id": os.environ.get(KEY_ID_ENV, "cathedral-weight-policy"),
        "policy_reason": f"v4_{effective_mode}_{window_hours():g}h_window",
        "policy_metadata": {
            "miner_count": len(scores),
            "composer": "scaffold.weights",
            "tier_weights": tier_weights(),
            "requested_mode": requested_mode,
            "effective_mode": effective_mode,
            "score_source": score_source,
            "proportional_ledger_empty": proportional_ledger_empty,
            "coldkey_map_loaded": bool(coldkey_of),
            "perminer_scoring_mode": pm_status["scoring_mode"],
            "perminer": {
                "enabled": pm_status["perminer_enabled"],
                "shadow": pm_status["perminer_shadow"],
                "live_requested": pm_status["perminer_live_requested"],
                "epoch": pm_status["perminer_epoch"],
                "has_scores": pm_status["perminer_has_scores"],
                "scoring_mode": pm_status["scoring_mode"],
                "bonus_multiplier": pm_status["bonus_multiplier"],
                "history_floor": pm_status["history_floor"],
                "coldkey_required": pm_status["coldkey_required"],
            },
        },
        "weights": [
            {"miner_hotkey": hk, "weight": scores[hk]} for hk in sorted(scores)
        ],
    }
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(signing_key_hex.strip()))
    payload["signature"] = base64.b64encode(sk.sign(canonical_bytes(payload))).decode()
    return payload


def _bg_refresh_loop(store: Store, signing_key_hex: str) -> None:
    """Background daemon thread: rebuild the vector every _CACHE_TTL_SECS.

    Never raises — a transient DB error is logged and retried next cycle.
    Runs forever; the process exiting is the only exit condition (daemon=True).
    """
    while True:
        try:
            vec = build_signed_vector(store, signing_key_hex=signing_key_hex)
            with _build_lock:
                _vector_cache["v"] = (time.time(), vec)
        except Exception as exc:
            print(f"[weights] bg_refresh error (will retry): {exc!r}")
        time.sleep(_CACHE_TTL_SECS)


def _ensure_bg_started(store: Store, signing_key_hex: str) -> None:
    """Lazily start the background refresh thread (idempotent)."""
    global _bg_started
    if _bg_started:
        return
    with _bg_lock:
        if _bg_started:
            return
        t = threading.Thread(
            target=_bg_refresh_loop, args=(store, signing_key_hex),
            name="weights-bg-refresh", daemon=True,
        )
        t.start()
        _bg_started = True


def current_vector(store: Store, *, signing_key_hex: str) -> dict[str, Any]:
    """Serve the latest signed vector from the in-memory cache.

    The background refresh thread (started on first call) rebuilds the vector
    every _CACHE_TTL_SECS without ever blocking the request path.  Only the
    very first call (empty cache) waits for a build — after that every request
    returns in microseconds.

    IMPORTANT: the synchronous first-build path does NOT hold _build_lock
    during the DB query.  Holding the lock during a slow (5-30s) DB build
    would block every concurrent request handler that tries to read the cache,
    causing a cascading stall.  Instead we build outside the lock and acquire
    only briefly to write the result.  If two threads both hit an empty cache
    simultaneously, both build (at most twice at startup), and the first writer
    wins; the second's result is discarded.  This wastes one extra build at
    most once at startup and is far better than starving all callers.
    """
    # Ensure the background thread is running so the cache stays fresh.
    _ensure_bg_started(store, signing_key_hex)

    with _build_lock:
        hit = _vector_cache.get("v")
    if hit is not None:
        return hit[1]

    # First call with an empty cache: build synchronously WITHOUT holding the
    # lock (a slow build inside the lock starves every concurrent request).
    vec = build_signed_vector(store, signing_key_hex=signing_key_hex)
    with _build_lock:
        # Another thread may have written the cache while we built.
        # Prefer the existing entry (avoids a duplicate policy_version bump),
        # but if it is still empty write ours.
        existing = _vector_cache.get("v")
        if existing is None:
            _vector_cache["v"] = (time.time(), vec)
        else:
            vec = existing[1]
    return vec


def _reset_vector_cache() -> None:
    """Test hook."""
    global _bg_started
    _vector_cache.clear()
    _bg_started = False
