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
from . import external_scores

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
SIGNING_KEY_ENV = "CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY"  # falls back to app key
KEY_ID_ENV = "CATHEDRAL_WEIGHT_POLICY_KEY_ID"
NETWORK_ENV = "CATHEDRAL_WEIGHT_POLICY_NETWORK"
NETUID_ENV = "CATHEDRAL_WEIGHT_POLICY_NETUID"
BURN_UID_ENV = "CATHEDRAL_WEIGHT_POLICY_BURN_UID"
BURN_HOTKEY_ENV = "CATHEDRAL_WEIGHT_POLICY_BURN_HOTKEY"
BURN_PERCENTAGE_ENV = "CATHEDRAL_WEIGHT_POLICY_FORCED_BURN_PERCENTAGE_V2"
VALID_FOR_ENV = "CATHEDRAL_WEIGHT_POLICY_VALID_FOR_SECS"
VALIDATED_SUPPLY_ENABLED_ENV = "CATHEDRAL_VALIDATED_SUPPLY_ENABLED"
# Selects which signed allocation contract validated_supply_metadata emits.
# Default "v2" keeps the launch-locked 90% Intel TDX / 10% fixed burn contract
# byte-identical. "v3" opts into 70% Intel TDX / 30% CyberGym / 0% fixed burn.
# Any other value fails closed (see allocation_contract()).
ALLOCATION_CONTRACT_ENV = "CATHEDRAL_ALLOCATION_CONTRACT"
# v3 allocation split (fractions of total emission). Burn remains the sink for
# forfeited/ineligible lane mass even though the FIXED burn allocation is 0.
V3_TDX_ALLOCATION = 0.70
V3_CYBERGYM_ALLOCATION = 0.30
# v4-only composition knobs.
WINDOW_HOURS_ENV = "CATHEDRAL_WEIGHTS_WINDOW_HOURS"
MODE_ENV = "CATHEDRAL_WEIGHTS_MODE"  # flat_recent | proportional | row_score_recent
ROW_SCORE_TASK_TYPES_ENV = "CATHEDRAL_WEIGHTS_ROW_SCORE_TASK_TYPES"
# Difficulty-weighted scoring. CATHEDRAL_WEIGHTS_TIER_WEIGHTS accepts JSON
# {"1":1,"2":3,"3":8} or comma form "1=1,2=3,3=8". If unset, preserve the
# existing launch default: tier 1 = 1.0, tier 2 = CATHEDRAL_WEIGHTS_TIER2_MULT.
TIER_WEIGHTS_ENV = "CATHEDRAL_WEIGHTS_TIER_WEIGHTS"
TIER2_MULT_ENV = "CATHEDRAL_WEIGHTS_TIER2_MULT"
# Transitional per-miner incentive controls. In pm_primary / assigned_only the
# shared public board is compatibility/debug only and contributes zero score.
# Bonus mode remains available for staged rollouts, but is not the paying lane.
PERMINER_BONUS_MULT_ENV = "CATHEDRAL_PERMINER_BONUS_MULT"
PERMINER_REQUIRE_COLDKEY_ENV = "CATHEDRAL_PERMINER_REQUIRE_COLDKEY"
PERMINER_HISTORY_FLOOR_ENV = "CATHEDRAL_PERMINER_HISTORY_FLOOR"
PERMINER_SCORING_MODE_ENV = "CATHEDRAL_PERMINER_SCORING_MODE"
PERMINER_PUBLIC_BASELINE_ENV = "CATHEDRAL_PERMINER_PUBLIC_BASELINE"
# Off by default. "mark" signs metagraph membership gaps in metadata; "filter"
# removes non-payable hotkeys from the signed weights when a fresh snapshot exists.
PAYABLE_HOTKEYS_ENV = "CATHEDRAL_WEIGHTS_PAYABLE_HOTKEYS"  # off | mark | filter
PAYABLE_HOTKEYS_MAX_AGE_SECS_ENV = "CATHEDRAL_WEIGHTS_PAYABLE_HOTKEYS_MAX_AGE_SECS"
# External score-source controls. Disabled by default; when enabled, source
# scores are blended publisher-side before Cathedral signs the one validator feed.
EXTERNAL_SCORES_ENABLED_ENV = "CATHEDRAL_EXTERNAL_SCORES_ENABLED"
EXTERNAL_SCORES_SOURCE_ENV = "CATHEDRAL_EXTERNAL_SCORES_SOURCE"
EXTERNAL_SCORES_MODE_ENV = "CATHEDRAL_EXTERNAL_SCORES_MODE"  # blend | external_primary
EXTERNAL_SCORES_WEIGHT_ENV = "CATHEDRAL_EXTERNAL_SCORES_WEIGHT"
EXTERNAL_SCORES_BASE_WEIGHT_ENV = "CATHEDRAL_EXTERNAL_SCORES_BASE_WEIGHT"
EXTERNAL_SCORES_WINDOW_SECS_ENV = "CATHEDRAL_EXTERNAL_SCORES_WINDOW_SECS"
# Real-money safety knobs (see docs/VIOLET_EXTERNAL_SCORES.md):
#  FRACTION       — explicit external share (0..1); if set it wins over the
#                   base/external weights (e.g. 0.10 = 10% external, 90% base).
#  MAX_FRACTION   — hard cap on the external share (default 0.5) so a misconfig
#                   cannot silently hand the vector to the external source.
#  REQUIRE_REGISTERED — external scores may only pay hotkeys in the fresh
#                   metagraph snapshot; fail-closed if the snapshot is missing.
#  PRIMARY_CONFIRM — external_primary (100% external) requires this explicit ack.
EXTERNAL_SCORES_FRACTION_ENV = "CATHEDRAL_EXTERNAL_SCORES_FRACTION"
EXTERNAL_SCORES_MAX_FRACTION_ENV = "CATHEDRAL_EXTERNAL_SCORES_MAX_FRACTION"
EXTERNAL_SCORES_REQUIRE_REGISTERED_ENV = "CATHEDRAL_EXTERNAL_SCORES_REQUIRE_REGISTERED"
EXTERNAL_SCORES_PRIMARY_CONFIRM_ENV = "CATHEDRAL_EXTERNAL_SCORES_PRIMARY_CONFIRM"

# (#5) Sources whose live composition must never silently inherit the legacy
# 50% BASE_WEIGHT/WEIGHT default. An explicit CATHEDRAL_EXTERNAL_SCORES_FRACTION
# is required for these; without one the blend fails closed to base-only.
EXTERNAL_SCORES_FRACTION_REQUIRED_SOURCES = {"cathedral_confidential_tdx"}
# (#6) Sources that must never run in external_primary (100% external intent)
# mode, confirmed or not. A confidential/attested source stays capped-blend
# only; external_scores_mode() enforces this centrally.
EXTERNAL_SCORES_NO_PRIMARY_SOURCES = {"cathedral_confidential_tdx"}
# Sources subject to the final-attribution accounting control.
EXTERNAL_SCORES_GLOBAL_CAP_SOURCES = {"cathedral_confidential_tdx"}
CONFIDENTIAL_TDX_HARD_CAP: float = 0.10
# Sources eligible for the explicit 100%-confidential-compute scoring mode
# (CATHEDRAL_EXTERNAL_SCORES_MODE=confidential_primary). The mode is valid ONLY
# for these sources; for any other source it resolves back to blend so this env
# can never hand a public source the whole vector.
CONFIDENTIAL_PRIMARY_SOURCES = {"cathedral_confidential_tdx"}
CONFIDENTIAL_PRIMARY_CONTRACT_VERSION = "v1"

_CACHE_TTL_SECS = 60.0
_LEGACY_PERSISTED_VECTOR_ID = "latest"
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
_bg_generation = 0

# Self-heal watchdog. Each background refresh is wall-clock bounded so one hung
# DB call can no longer freeze a publisher replica's served vector (previously a
# stuck rebuild left a replica serving a 68-min-stale vector). On timeout the
# cycle is abandoned and retried next tick; a healthy leader's persisted vector
# is then picked up via _load_persisted_vector. Set <=0 to disable the watchdog
# (direct call) — used by tests. Default 90s is well above a normal 5-30s build
# and far below the multi-minute freeze we are guarding against.
_REFRESH_TIMEOUT_SECS = float(
    os.environ.get("CATHEDRAL_WEIGHTS_REFRESH_TIMEOUT_SECS", "90") or "90"
)
# At most one in-flight refresh attempt at a time, so sustained hangs cannot leak
# an unbounded number of threads/DB connections (one attempt per 60s tick).
_refresh_attempt_lock = threading.Lock()
_refresh_attempt: "threading.Thread | None" = None
# Refresh liveness, for observability + future /health wiring.
_refresh_health_lock = threading.Lock()
_refresh_health: dict[str, Any] = {
    "last_ok_ts": 0.0,
    "last_status": "init",
    "last_error": None,
    "last_timeout_ts": 0.0,
    "consecutive_failures": 0,
}


class _RefreshTimeout(Exception):
    """Raised when a single background refresh exceeds _REFRESH_TIMEOUT_SECS."""


class VectorNotReady(RuntimeError):
    """No signed vector exists yet and another publisher owns the build lock."""


def _cache_write(vec: dict[str, Any]) -> bool:
    """Adopt only a non-regressing signed vector in this process."""
    incoming_version = int(vec.get("policy_version") or 0)
    with _build_lock:
        current = _vector_cache.get("v")
        if current is not None:
            current_version = int(current[1].get("policy_version") or 0)
            if incoming_version < current_version:
                return False
            if incoming_version == current_version and current[1] != vec:
                return False
        _vector_cache["v"] = (time.time(), vec)
    return True


def _vector_scope() -> tuple[str, int]:
    """Return the signed subnet identity used to isolate shared scorer state."""
    network = os.environ.get(NETWORK_ENV, "finney").strip() or "finney"
    try:
        netuid = int(os.environ.get(NETUID_ENV, "39") or "39")
    except ValueError as exc:
        raise ValueError(f"invalid {NETUID_ENV}") from exc
    return network, netuid


def _persisted_vector_id() -> str:
    network, netuid = _vector_scope()
    return f"latest:{network}:{netuid}"


def _refresh_lock_name() -> str:
    network, netuid = _vector_scope()
    return f"cathedral:weights:refresh:{network}:{netuid}"


def _load_persisted_vector(store: Store) -> dict[str, Any] | None:
    rows = store.query(
        "SELECT vector_json FROM signed_weight_vectors WHERE id = ?",
        (_persisted_vector_id(),),
    )
    if not rows:
        # One-time compatibility with the pre-scope singleton. Never adopt a
        # legacy row for a different subnet; that was the cross-subnet race.
        rows = store.query(
            "SELECT vector_json FROM signed_weight_vectors WHERE id = ?",
            (_LEGACY_PERSISTED_VECTOR_ID,),
        )
        if not rows:
            return None
        legacy = json.loads(rows[0]["vector_json"])
        network, netuid = _vector_scope()
        if legacy.get("network") != network or legacy.get("netuid") != netuid:
            return None
        return legacy
    return json.loads(rows[0]["vector_json"])


def _persist_vector(store: Store, vec: dict[str, Any]) -> dict[str, Any]:
    """Persist monotonically and return the vector that won the durable race."""
    generated_at = str(vec.get("generated_at") or "")
    policy_version = int(vec.get("policy_version") or 0)
    payload = json.dumps(vec, sort_keys=True, separators=(",", ":"))
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    )

    def _write(conn):
        conn.execute(
            "INSERT INTO signed_weight_vectors"
            "(id, generated_at_iso, policy_version, vector_json, updated_at_iso) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "generated_at_iso=excluded.generated_at_iso, "
            "policy_version=excluded.policy_version, "
            "vector_json=excluded.vector_json, "
            "updated_at_iso=excluded.updated_at_iso "
            "WHERE excluded.policy_version > signed_weight_vectors.policy_version",
            (_persisted_vector_id(), generated_at, policy_version, payload, updated_at),
        )
        row = conn.execute(
            "SELECT vector_json FROM signed_weight_vectors WHERE id = ?",
            (_persisted_vector_id(),),
        ).fetchone()
        if row is None:
            raise RuntimeError("signed vector persistence returned no winning row")
        return json.loads(row[0])

    return store.write(_write)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def window_hours() -> float:
    return _env_float(WINDOW_HOURS_ENV, 24.0)


def mode() -> str:
    m = os.environ.get(MODE_ENV, "proportional").strip().lower()
    return (
        m
        if m in ("flat_recent", "proportional", "row_score_recent")
        else "proportional"
    )


def row_score_task_types() -> set[str]:
    raw = os.environ.get(
        ROW_SCORE_TASK_TYPES_ENV,
        "synthetic_boolean_v1,solver_attestation_v1,audit_replay_v1,audit_arena_v1",
    )
    return {item.strip() for item in raw.replace(";", ",").split(",") if item.strip()}


def burn_percentage() -> float:
    return min(100.0, max(0.0, _env_float(BURN_PERCENTAGE_ENV, 0.0)))


def burn_uid() -> int | None:
    raw = os.environ.get(BURN_UID_ENV, "204").strip()
    return int(raw) if raw else None


def burn_hotkey() -> str | None:
    raw = os.environ.get(BURN_HOTKEY_ENV, "").strip()
    return raw or None


def allocation_contract() -> str:
    """The selected signed allocation contract: "v2" (default) or "v3".

    Fail closed: an unrecognized value is a misconfiguration of live economic
    policy, so it raises rather than silently falling back to a default split.
    """
    raw = (os.environ.get(ALLOCATION_CONTRACT_ENV, "v2") or "v2").strip().lower()
    if raw not in ("v2", "v3"):
        raise VectorError(
            f"unknown allocation contract {raw!r}; expected 'v2' or 'v3'"
        )
    return raw


def validated_supply_metadata() -> dict[str, Any] | None:
    """Return the signed allocation policy or fail closed on drift.

    Default (v2) is the launch-locked 90% Intel TDX / 10% fixed-burn contract and
    is byte-identical to the pre-v3 behavior. v3 (opt-in via
    CATHEDRAL_ALLOCATION_CONTRACT=v3) is 70% Intel TDX / 30% CyberGym / 0% fixed
    burn. In BOTH contracts the burn hotkey remains the SINK for forfeited or
    ineligible lane mass, so an explicit burn hotkey is required either way; v3
    differs only in that the FIXED burn allocation is 0 rather than 10%.
    """
    if not _env_bool(VALIDATED_SUPPLY_ENABLED_ENV, False):
        return None
    if external_scores_mode() != "confidential_primary":
        raise VectorError("validated_supply requires confidential_primary mode")
    destination = burn_hotkey()
    if destination is None:
        raise VectorError("validated_supply requires an explicit burn hotkey")
    if burn_uid() is not None:
        raise VectorError("validated_supply must resolve burn by hotkey, not UID")
    contract = allocation_contract()
    if contract == "v3":
        # v3 burns NO fixed share; forfeited/ineligible lane mass still sinks to
        # the burn hotkey, which is why an explicit destination is still demanded.
        if not math.isclose(burn_percentage(), 0.0, rel_tol=0.0, abs_tol=1e-12):
            raise VectorError("validated_supply v3 requires exactly 0% fixed burn")
        return {
            "contract_version": "v3",
            "intel_tdx_allocation": V3_TDX_ALLOCATION,
            "cybergym_allocation": V3_CYBERGYM_ALLOCATION,
            "fixed_burn_allocation": 0.0,
            "burn_hotkey": destination,
        }
    if not math.isclose(burn_percentage(), 10.0, rel_tol=0.0, abs_tol=1e-12):
        raise VectorError("validated_supply requires exactly 10% forced burn")
    return {
        "contract_version": "v2",
        "intel_tdx_allocation": 0.90,
        "fixed_burn_allocation": 0.10,
        "burn_hotkey": destination,
    }


def _ms_iso(dt: datetime) -> str:
    """ISO-8601 UTC, ms precision, trailing Z — the live vector convention."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    s = (
        dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{dt.microsecond // 1000:03d}"
    )
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
    pm_primary: make assigned solves primary. Public-board baseline is zero.
    assigned_only: replace shared scoring with the assigned-only vector.
    """
    raw = os.environ.get(PERMINER_SCORING_MODE_ENV, "bonus").strip().lower()
    return raw if raw in {"bonus", "pm_primary", "assigned_only"} else "bonus"


def perminer_public_baseline() -> float:
    """Shared-board score share when per-miner work is the primary lane.

    The public-board lane is legacy compatibility/debug only. Keep the function
    and metadata field for API compatibility, but hard-zero the value so stale
    env cannot accidentally keep paying public-board solves.
    """
    return 0.0


def coldkey_collapse_enabled() -> bool:
    """Opt-in Sybil hardening. OFF by default so this is byte-identical to today
    until an operator flips it AND a hotkey->coldkey map is supplied."""
    return os.environ.get("CATHEDRAL_WEIGHTS_COLDKEY_COLLAPSE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def perminer_require_coldkey() -> bool:
    raw = os.environ.get(PERMINER_REQUIRE_COLDKEY_ENV, "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _perminer_scoring_enabled() -> bool:
    """True when per-miner solve rows are allowed to drive scoring.

    The legacy V1 miner surface used CATHEDRAL_PERMINER_ENABLED for both route
    enablement and scoring. The converged V2 profile keeps that V1 route flag
    unset so old miner routes stay retired and startup env pinning can be
    lock-free, but verified V2 bitset submits still bridge into the same
    per_miner_solves ledger and must feed the signed weight vector.
    """
    from . import launch_profile
    from . import per_miner as pm

    return pm.perminer_enabled() or launch_profile.converged()


def _perminer_scoring_shadow() -> bool:
    from . import per_miner as pm

    return pm.perminer_shadow()


def payable_hotkeys_mode() -> str:
    raw = os.environ.get(PAYABLE_HOTKEYS_ENV, "off").strip().lower()
    return raw if raw in {"off", "mark", "filter"} else "off"


def payable_hotkeys_max_age_secs() -> float:
    return max(0.0, _env_float(PAYABLE_HOTKEYS_MAX_AGE_SECS_ENV, 600.0))


def _load_fresh_metagraph_hotkeys(
    store: Store,
    *,
    now: datetime,
) -> tuple[set[str] | None, dict[str, Any]]:
    """Load a fresh out-of-band metagraph membership snapshot.

    The publisher still does not need chain credentials. An external poller can
    refresh metagraph_hotkeys; stale rows age out and are not treated payable.
    """
    network = os.environ.get(NETWORK_ENV, "finney")
    netuid = int(os.environ.get(NETUID_ENV, "39"))
    max_age = payable_hotkeys_max_age_secs()
    cutoff = _ms_iso(now - timedelta(seconds=max_age))
    meta: dict[str, Any] = {
        "network": network,
        "netuid": netuid,
        "max_age_secs": max_age,
        "cutoff": cutoff,
        "snapshot_fresh": False,
        "snapshot_hotkey_count": 0,
        "snapshot_updated_at": None,
    }
    try:
        rows = store.query(
            "SELECT hotkey, updated_at_iso FROM metagraph_hotkeys "
            "WHERE network=? AND netuid=? AND updated_at_iso > ?",
            (network, netuid, cutoff),
        )
    except Exception as exc:
        meta["snapshot_error"] = str(exc)
        return None, meta
    hotkeys = {str(r["hotkey"]) for r in rows}
    if not hotkeys:
        return None, meta
    meta["snapshot_fresh"] = True
    meta["snapshot_hotkey_count"] = len(hotkeys)
    meta["snapshot_updated_at"] = max(str(r["updated_at_iso"]) for r in rows)
    return hotkeys, meta


def _apply_payable_hotkey_policy(
    store: Store,
    scores: dict[str, float],
    *,
    now: datetime,
) -> tuple[dict[str, float], dict[str, Any]]:
    mode_value = payable_hotkeys_mode()
    meta: dict[str, Any] = {
        "mode": mode_value,
        "enabled": mode_value != "off",
        "enforced": False,
        "status": "off",
        "raw_miner_count": len(scores),
        "final_miner_count": len(scores),
        "missing_count": 0,
        "missing_hotkeys": [],
    }
    if mode_value == "off":
        return scores, meta

    payable, snapshot_meta = _load_fresh_metagraph_hotkeys(store, now=now)
    meta.update(snapshot_meta)
    if payable is None:
        meta["status"] = "no_fresh_snapshot"
        return scores, meta

    missing = sorted(set(scores) - payable)
    meta["missing_hotkeys"] = missing
    meta["missing_count"] = len(missing)
    if mode_value == "filter":
        filtered = {hk: score for hk, score in scores.items() if hk in payable}
        meta["enforced"] = True
        meta["final_miner_count"] = len(filtered)
        meta["status"] = "filtered" if missing else "all_payable"
        return filtered, meta

    meta["status"] = "marked_missing" if missing else "all_payable"
    return scores, meta


def _perminer_window_scores(
    store: Store,
    *,
    since: str,
    ident=lambda hk: hk,
) -> dict[str, float]:
    """Trailing-window normalized per-miner scores."""
    rows = store.query(
        "SELECT miner_hotkey, SUM(difficulty_weight) AS total "
        "FROM per_miner_solves "
        "WHERE solved_at_iso > ? AND verified=1 AND difficulty_weight > 0 "
        "GROUP BY miner_hotkey",
        (since,),
    )
    hk_totals: dict[str, float] = {}
    for r in rows:
        hk = str(r["miner_hotkey"])
        total = float(r["total"] or 0.0)
        if total <= 0.0:
            continue
        hk_totals[hk] = total
    identity_best: dict[str, float] = {}
    hks: dict[str, set[str]] = {}
    for hk, total in hk_totals.items():
        ident_value = ident(hk)
        if ident_value is None:
            continue
        idk = str(ident_value)
        identity_best[idk] = max(identity_best.get(idk, 0.0), total)
        hks.setdefault(idk, set()).add(hk)
    if not identity_best:
        return {}
    top = max(identity_best.values())
    if top <= 0.0:
        return {}
    scores: dict[str, float] = {}
    for idk, total in identity_best.items():
        per = round((total / top) / len(hks[idk]), 6)
        for hk in hks[idk]:
            scores[hk] = per
    return scores


def _perminer_scores(store: Store, *, now: datetime | None = None) -> dict[str, float]:
    """Trailing-window normalized per-miner scores, or empty when disabled/no solves."""
    if not _perminer_scoring_enabled() or _perminer_scoring_shadow():
        return {}
    now = now or datetime.now(timezone.utc)
    since = _ms_iso(now - timedelta(hours=window_hours()))
    return _perminer_window_scores(store, since=since)


def _perminer_compose_scores(
    store: Store,
    *,
    ident=lambda hk: hk,
    since: str | None = None,
) -> dict[str, float] | None:
    """Per-miner scoring path. Returns scores when the legacy V1 PM surface is
    on OR the v2-converged profile is active, and not in shadow-only mode.
    Returns None when scoring is off (caller falls through to existing scoring
    — byte-identical to pre-flag behaviour).

    Shadow mode: flag is on + CATHEDRAL_PERMINER_SHADOW=1 → compute the vector,
    log it, but return None so the LIVE vector stays the current scoring. This
    lets us run shadow comparisons without touching the live board.
    """
    if not _perminer_scoring_enabled():
        return None  # flag off: zero change
    since = since or _ms_iso(
        datetime.now(timezone.utc) - timedelta(hours=window_hours())
    )
    try:
        scores = _perminer_window_scores(store, since=since, ident=ident)
    except Exception:
        if perminer_scoring_mode() in {"pm_primary", "assigned_only"}:
            raise
        print("[per_miner] bonus score query failed; continuing with base vector")
        return None
    if _perminer_scoring_shadow():
        # Shadow: log the vector for comparison but don't serve it.
        print(
            f"[per_miner] shadow_vector window_hours={window_hours()} scores={scores}"
        )
        return None  # fall through to live scoring
    if scores:
        return scores
    if perminer_scoring_mode() in {"pm_primary", "assigned_only"}:
        return {}
    return None


def _apply_perminer_primary(
    base: dict[str, float],
    pm_scores: dict[str, float] | None,
) -> dict[str, float]:
    """Make PM solves primary; public board pays zero."""
    baseline = perminer_public_baseline()
    pm_share = 1.0 - baseline

    def budgeted(scores: dict[str, float], budget: float) -> dict[str, float]:
        if budget <= 0.0:
            return {}
        total = sum(max(0.0, float(v)) for v in scores.values())
        if total <= 0.0:
            return {}
        return {
            hk: budget * max(0.0, float(score)) / total for hk, score in scores.items()
        }

    combined: dict[str, float] = {}
    for part in (budgeted(base, baseline), budgeted(pm_scores or {}, pm_share)):
        for hk, score in part.items():
            combined[hk] = combined.get(hk, 0.0) + score
    top = max(combined.values()) if combined else 0.0
    if top <= 0.0:
        return {}
    return {hk: round(v / top, 6) for hk, v in combined.items()}


def _apply_perminer_bonus(
    store: Store,
    base: dict[str, float],
    coldkey_of: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, float]:
    """Add a transition bonus for per-miner adopters without replacing base scoring."""
    bonus = perminer_bonus_multiplier()
    if bonus <= 0.0:
        return base
    pm_scores = _perminer_scores(store, now=now)
    if not pm_scores:
        return base
    combined = dict(base)
    use_ck = bool(coldkey_of)
    if not use_ck:
        top_base = max(base.values()) if base else 0.0
        history_floor = perminer_history_floor()
        for hk, score in pm_scores.items():
            history = 1.0 if top_base <= 0.0 else combined.get(hk, 0.0) / top_base
            history_mult = history_floor + (1.0 - history_floor) * max(
                0.0, min(1.0, history)
            )
            combined[hk] = combined.get(hk, 0.0) + bonus * float(score) * history_mult
    else:

        def mapped(hk: str) -> str:
            return coldkey_of.get(hk, hk)  # type: ignore[union-attr]

        members: dict[str, set[str]] = {}
        best: dict[str, float] = {}
        history: dict[str, float] = {}
        for hk in set(base) | set(pm_scores):
            members.setdefault(mapped(hk), set()).add(hk)
        for hk, score in base.items():
            idk = mapped(hk)
            history[idk] = max(history.get(idk, 0.0), float(score))
        for hk, score in pm_scores.items():
            idk = mapped(hk)
            best[idk] = max(best.get(idk, 0.0), float(score))
        top_history = max(history.values()) if history else 0.0
        history_floor = perminer_history_floor()
        for idk, score in best.items():
            hks = members.get(idk) or set()
            if not hks:
                continue
            recent = 1.0 if top_history <= 0.0 else history.get(idk, 0.0) / top_history
            history_mult = history_floor + (1.0 - history_floor) * max(
                0.0, min(1.0, recent)
            )
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
        or perminer_scoring_mode() in {"assigned_only", "pm_primary"}
        or perminer_bonus_multiplier() > 0.0
    ):
        return _load_coldkey_map(store)
    return None


def scoring_identity_for_hotkey(
    store: Store, hotkey: str, *, require_mapped: bool = False
) -> str | None:
    """Return the scoring identity for hotkey-bound beta lanes.

    When coldkey collapse is enabled and a map row exists, per-miner challenge
    assignment uses the coldkey too. This makes sybil stacking pointless at the
    work-assignment layer, not just after score normalization.
    """
    if not coldkey_collapse_enabled() and not require_mapped:
        return hotkey
    try:
        rows = store.query(
            "SELECT coldkey FROM coldkey_map WHERE hotkey=? LIMIT 1", (hotkey,)
        )
    except Exception:
        return None if require_mapped else hotkey
    if not rows:
        return None if require_mapped else hotkey
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
        (since,),
    )
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


def external_scores_enabled() -> bool:
    return _env_bool(EXTERNAL_SCORES_ENABLED_ENV, False)


def external_scores_source() -> str:
    raw = (
        os.environ.get(EXTERNAL_SCORES_SOURCE_ENV, "violet_audio") or "violet_audio"
    ).strip()
    return raw or "violet_audio"


def external_scores_mode() -> str:
    """blend (default), external_primary, or confidential_primary.

    (#6) Confirmed/preserved behavior: external_primary has always been a
    request-for-100%-external signal gated by
    CATHEDRAL_EXTERNAL_SCORES_PRIMARY_CONFIRM -- it does not by itself change
    the blend math in _apply_external_scores, which still allocates via
    _external_blend_weights()'s fraction (explicit FRACTION or the capped
    legacy weights). That composition is unchanged here. What IS enforced
    here: a confidential/attested source (EXTERNAL_SCORES_NO_PRIMARY_SOURCES)
    must never resolve to external_primary, confirmed or not -- it always
    reports as "blend" so every downstream external_primary branch (the ack
    warning, the score_source label) treats it as a capped blend.
    """
    raw = os.environ.get(EXTERNAL_SCORES_MODE_ENV, "").strip().lower()
    if raw in ("", "blend"):
        return "blend"
    if raw == "confidential_primary":
        # Explicit 100% confidential-compute intent. Preserved regardless of
        # source; source validity is enforced fail-closed (degrade to burn,
        # never base) in _apply_confidential_primary. NEVER silently blend.
        return "confidential_primary"
    if raw == "external_primary":
        if external_scores_source() in EXTERNAL_SCORES_NO_PRIMARY_SOURCES:
            return "blend"
        return "external_primary"
    # Unknown nonempty mode: fail closed with a clear error instead of
    # silently resolving to blend.
    raise VectorError(
        f"unknown {EXTERNAL_SCORES_MODE_ENV}={raw!r}; expected one of "
        "blend, external_primary, confidential_primary"
    )


def external_scores_window_secs() -> float:
    return max(1.0, _env_float(EXTERNAL_SCORES_WINDOW_SECS_ENV, 3600.0))


def _normalize_positive_scores(scores: dict[str, float]) -> dict[str, float]:
    clean: dict[str, float] = {}
    for hk, raw in scores.items():
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0.0:
            clean[str(hk)] = value
    if not clean:
        return {}
    top = max(clean.values())
    if top <= 0.0:
        return {}
    return {hk: round(v / top, 6) for hk, v in clean.items()}


def _identity_collapse_scores(
    raw: dict[str, float], *, ident=lambda hk: hk, strict_unit_interval: bool = False
) -> dict[str, float]:
    """Normalize hotkey scores and split one identity's score across its hotkeys."""
    groups: dict[str, set[str]] = {}
    group_scores: dict[str, float] = {}
    for hk, raw_score in raw.items():
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            if strict_unit_interval:
                raise VectorError(f"confidential score for {hk!r} is not numeric")
            continue
        if strict_unit_interval and (
            not math.isfinite(score) or not 0.0 <= score <= 1.0
        ):
            raise VectorError(
                f"confidential score for {hk!r} must be finite and in [0, 1]: {score!r}"
            )
        if not math.isfinite(score) or score <= 0.0:
            continue
        idk = str(ident(str(hk)))
        groups.setdefault(idk, set()).add(str(hk))
        # External reports are score vectors, not event ledgers. Use max per
        # identity so cloning the same coldkey across hotkeys cannot sum rewards.
        group_scores[idk] = max(group_scores.get(idk, 0.0), score)
    if not group_scores:
        return {}
    top = max(group_scores.values())
    if top <= 0.0:
        return {}
    out: dict[str, float] = {}
    for idk, score in group_scores.items():
        members = groups[idk]
        per = round((score / top) / len(members), 6)
        for hk in members:
            out[hk] = per
    return out


def _compose_external_scores(
    store: Store,
    *,
    now: datetime,
    ident=lambda hk: hk,
) -> dict[str, float]:
    """Fetch the latest complete external snapshot and identity-collapse it.

    Returns an empty dict when external scoring is disabled or no fresh
    complete snapshot exists (fail-closed).
    """
    if not external_scores_enabled():
        return {}
    window = external_scores_window_secs()
    try:
        raw = external_scores.latest_snapshot_scores(
            store,
            source=external_scores_source(),
            max_age_secs=window,
            now=now,
        )
    except Exception as exc:
        print(f"[weights] external_scores compose failed: {exc!r}")
        return {}
    if raw is None:
        return {}
    return _identity_collapse_scores(
        raw,
        ident=ident,
        strict_unit_interval=external_scores_source()
        in EXTERNAL_SCORES_GLOBAL_CAP_SOURCES,
    )


def _confidential_tdx_fraction() -> float | None:
    """Parse the explicit confidential fraction without clamping bad config."""
    raw = os.environ.get(EXTERNAL_SCORES_FRACTION_ENV, "").strip()
    if not raw:
        return None
    try:
        fraction = float(raw)
    except ValueError as exc:
        raise VectorError(f"confidential_tdx fraction is not numeric: {raw!r}") from exc
    if not math.isfinite(fraction) or not 0.0 < fraction <= CONFIDENTIAL_TDX_HARD_CAP:
        raise VectorError(
            f"confidential_tdx fraction must be finite and in (0, 0.10]: {fraction!r}"
        )
    return fraction


def _external_blend_weights() -> tuple[float, float, float]:
    """Return (base_weight, external_weight, effective_external_share).

    An explicit CATHEDRAL_EXTERNAL_SCORES_FRACTION wins (share == fraction).
    Otherwise the legacy base/external weights are used, but the effective
    external share is HARD-CAPPED at CATHEDRAL_EXTERNAL_SCORES_MAX_FRACTION
    (default 0.5) so a fat external_weight cannot silently take the vector."""
    max_frac = min(1.0, max(0.0, _env_float(EXTERNAL_SCORES_MAX_FRACTION_ENV, 0.5)))
    frac_raw = os.environ.get(EXTERNAL_SCORES_FRACTION_ENV, "").strip()
    if frac_raw:
        frac = min(max_frac, max(0.0, _env_float(EXTERNAL_SCORES_FRACTION_ENV, 0.0)))
        return (1.0 - frac), frac, frac
    if external_scores_source() in EXTERNAL_SCORES_FRACTION_REQUIRED_SOURCES:
        # (#5) Fail closed: this source must never inherit the legacy 1.0/1.0
        # (50%) default. No explicit fraction => zero external share.
        return 1.0, 0.0, 0.0
    base_weight = max(0.0, _env_float(EXTERNAL_SCORES_BASE_WEIGHT_ENV, 1.0))
    external_weight = max(0.0, _env_float(EXTERNAL_SCORES_WEIGHT_ENV, 1.0))
    tot = base_weight + external_weight
    if tot <= 0.0:
        return 0.0, 0.0, 0.0
    share = external_weight / tot
    if share > max_frac and max_frac < 1.0:
        external_weight = base_weight * max_frac / (1.0 - max_frac)
        share = max_frac
    return base_weight, external_weight, share


def _apply_confidential_tdx_global_cap(
    base_norm: dict[str, float],
    ext_norm: dict[str, float],
    fraction: float,
) -> "tuple[dict[str, float], dict[str, float], dict[str, float], dict[str, Any]]":
    """Build global-cap components over the union of both normalized vectors."""
    if not math.isfinite(fraction) or not 0.0 < fraction <= CONFIDENTIAL_TDX_HARD_CAP:
        raise VectorError(f"confidential_tdx fraction invalid: {fraction!r}")

    base_comp: dict[str, float] = {}
    ext_comp: dict[str, float] = {}
    blended: dict[str, float] = {}

    for hk in sorted(set(base_norm) | set(ext_norm)):
        a_i = (1.0 - fraction) * base_norm.get(hk, 0.0)
        c_i = fraction * ext_norm.get(hk, 0.0)
        if not math.isfinite(a_i) or a_i < 0.0 or not math.isfinite(c_i) or c_i < 0.0:
            raise VectorError(f"{hk}: invalid component a_i={a_i!r}, c_i={c_i!r}")

        w_i = a_i + c_i
        if w_i > 0.0:
            blended[hk] = w_i
        base_comp[hk] = a_i
        ext_comp[hk] = c_i

    totals = _validate_confidential_tdx_components(
        blended, base_comp, ext_comp, fraction, context="blend"
    )

    cap_meta: dict[str, Any] = {
        "configured_cap": CONFIDENTIAL_TDX_HARD_CAP,
        "configured_fraction": fraction,
        "actual_base_mass": totals["base_mass"],
        "actual_external_mass": totals["external_mass"],
        "realized_external_fraction": totals["external_fraction"],
        "withheld_external_mass": 0.0,
        "cap_version": "v3",
        "global_cap_assertion_ok": True,
    }
    return blended, base_comp, ext_comp, cap_meta


def _machine_precision_equal(left: float, right: float) -> bool:
    if left == right:
        return True
    return abs(left - right) <= max(math.ulp(left), math.ulp(right))


def _validate_confidential_tdx_components(
    scores: dict[str, float],
    base_comp: dict[str, float],
    ext_comp: dict[str, float],
    fraction: float,
    *,
    context: str,
) -> dict[str, float]:
    """Validate signed attribution rows and the global confidential fraction."""
    if not math.isfinite(fraction) or not 0.0 < fraction <= CONFIDENTIAL_TDX_HARD_CAP:
        raise VectorError(f"{context}: invalid confidential fraction {fraction!r}")
    base_mass = 0.0
    ext_mass = 0.0
    component_keys = set(base_comp) | set(ext_comp)
    for hk in component_keys | set(scores):
        if hk not in scores:
            for label, raw_value in (
                ("base", base_comp.get(hk, 0.0)),
                ("external", ext_comp.get(hk, 0.0)),
            ):
                try:
                    value = float(raw_value)
                except (TypeError, ValueError) as exc:
                    raise VectorError(
                        f"{context}: {hk} has non-numeric {label} component"
                    ) from exc
                if not math.isfinite(value) or value < 0.0:
                    raise VectorError(f"{context}: {hk} has invalid {label} component")
                if value != 0.0:
                    raise VectorError(
                        f"{context}: {hk} attribution has no signed weight"
                    )
            continue
        if hk not in base_comp or hk not in ext_comp:
            raise VectorError(f"{context}: {hk} missing signed attribution component")
        raw_weight = scores[hk]
        weight = float(raw_weight)
        a_i = float(base_comp[hk])
        c_i = float(ext_comp[hk])
        if not all(math.isfinite(value) for value in (weight, a_i, c_i)):
            raise VectorError(f"{context}: {hk} has non-finite weight/component")
        if weight < 0.0 or a_i < 0.0 or c_i < 0.0:
            raise VectorError(f"{context}: {hk} has negative weight/component")
        component_sum = a_i + c_i
        if not _machine_precision_equal(weight, component_sum):
            raise VectorError(
                f"{context}: {hk} weight {weight!r} != components {component_sum!r}"
            )
        base_mass = math.fsum((base_mass, a_i))
        ext_mass = math.fsum((ext_mass, c_i))

    total_mass = base_mass + ext_mass
    external_fraction = ext_mass / total_mass if total_mass > 0.0 else 0.0
    if total_mass == 0.0 and ext_mass != 0.0:
        raise VectorError(f"{context}: zero total mass has nonzero confidential mass")
    score_mass = math.fsum(float(value) for value in scores.values())
    if not math.isclose(score_mass, total_mass, rel_tol=0.0, abs_tol=1e-12):
        raise VectorError(
            f"{context}: score mass {score_mass!r} != component mass {total_mass!r}"
        )
    if total_mass > 0.0 and abs(external_fraction - fraction) > 1e-12:
        raise VectorError(
            f"{context}: confidential aggregate {external_fraction!r} != {fraction!r}"
        )
    return {
        "base_mass": base_mass,
        "external_mass": ext_mass,
        "external_fraction": external_fraction,
    }


def _l1_normalize(scores: dict[str, float]) -> dict[str, float]:
    """L1 (sum) normalize a positive score vector so entries sum to 1.0.

    Drops non-positive entries.  Returns empty dict on zero mass.
    """
    clean: dict[str, float] = {}
    for hk, raw in scores.items():
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v) and v > 0.0:
            clean[str(hk)] = v
    total = sum(clean.values())
    if total <= 0.0:
        return {}
    return {hk: v / total for hk, v in clean.items()}


def _apply_confidential_primary(
    store: Store,
    base: dict[str, float],
    *,
    now: datetime,
    ident=lambda hk: hk,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Explicit 100% confidential-compute composition (confidential_primary).

    The latest fresh COMPLETE confidential snapshot is the ONLY positive score
    source. Base scores are ignored entirely (base_mass == 0). Positive
    confidential scores are L1-normalized to exactly all miner mass (sum == 1.0
    before the signed burn). Explicit zero / omission in the complete snapshot
    stays a revocation. If the report, confirmation, freshness, or eligibility
    is absent/invalid, return an empty vector so the signed burn fallback
    applies downstream. Never blends, never falls back to base scores.
    """
    src = external_scores_source()
    cp_meta: dict[str, Any] = {
        "contract_version": CONFIDENTIAL_PRIMARY_CONTRACT_VERSION,
        "mode": "confidential_primary",
        "source": src,
        "base_mass": 0.0,
        "confidential_mass": 0.0,
        "complete": False,
        "fresh": False,
        "confirmed": _env_bool(EXTERNAL_SCORES_PRIMARY_CONFIRM_ENV, False),
        "require_registered": _env_bool(EXTERNAL_SCORES_REQUIRE_REGISTERED_ENV, True),
        "external_miner_count": 0,
        "degradation_reason": None,
    }
    blend_meta: dict[str, Any] = {
        "base_mass": 0.0,
        "external_mass": 0.0,
        "base_miner_count": 0,
        "external_miner_count": 0,
        "blended": False,
        "confidential_primary": cp_meta,
    }

    def _degrade(reason: str) -> tuple[dict[str, float], dict[str, Any]]:
        cp_meta["degradation_reason"] = reason
        print(
            f"[weights] confidential_primary degraded: {reason} "
            "-> empty vector (signed burn fallback)"
        )
        return {}, blend_meta

    # Source validity is fail-closed here, NOT in external_scores_mode(): an
    # absent/wrong source under confidential_primary intent must degrade to a
    # signed burn vector, never resolve to blend/base. Only the confidential
    # source can receive 100% of the vector via this mode.
    if src not in CONFIDENTIAL_PRIMARY_SOURCES:
        return _degrade("invalid_source")

    # Snapshot status for the signed metadata (complete/fresh). Best-effort.
    try:
        since = _ms_iso(now - timedelta(seconds=external_scores_window_secs()))
        st = external_scores.status(store, source=src, since_iso=since)
        cp_meta["complete"] = bool(st.get("latest_complete"))
        cp_meta["fresh"] = bool(st.get("latest_fresh"))
    except Exception as exc:
        cp_meta["status_error"] = repr(exc)

    # Explicit confirmation is mandatory for 100% confidential intent.
    if not cp_meta["confirmed"]:
        return _degrade("primary_confirm_missing")

    # The only positive source: the latest fresh complete confidential snapshot.
    ext = _compose_external_scores(store, now=now, ident=ident)
    if not ext:
        if cp_meta["complete"] and cp_meta["fresh"]:
            return _degrade("confidential_snapshot_revoked_all")
        return _degrade("confidential_snapshot_unavailable")

    # Scorer-side metagraph filtering stays configurable. When disabled, the
    # thin validator is the authoritative registration check.
    if cp_meta["require_registered"]:
        registered, _snap = _load_fresh_metagraph_hotkeys(store, now=now)
        if registered is None:
            return _degrade("registration_snapshot_unavailable")
        ext = {hk: v for hk, v in ext.items() if hk in registered}
        if not ext:
            return _degrade("no_registered_confidential_scores")

    # Payable-hotkey filter (configurable) applied pre-normalization so the
    # normalized mass stays exactly 1.0 after any downstream no-op re-filter.
    if payable_hotkeys_mode() == "filter":
        ext, payable_meta = _apply_payable_hotkey_policy(store, ext, now=now)
        blend_meta["external_payable_filter"] = payable_meta
        if not ext:
            return _degrade("no_payable_confidential_scores")

    normalized = _l1_normalize(ext)
    if not normalized:
        return _degrade("confidential_norm_zero")

    cp_meta["confidential_mass"] = 1.0
    cp_meta["complete"] = True
    cp_meta["fresh"] = True
    cp_meta["external_miner_count"] = len(normalized)
    blend_meta["external_mass"] = 1.0
    blend_meta["external_miner_count"] = len(normalized)
    print(f"[weights] confidential_primary vector composed: miners={len(normalized)}")
    return normalized, blend_meta


def _apply_external_scores(
    store: Store,
    base: dict[str, float],
    *,
    now: datetime,
    ident=lambda hk: hk,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Blend base and external score vectors.

    Returns (final_scores, blend_metadata).  The metadata exposes realized
    base and external contribution mass for signed-vector policy logging.

    Contract:
    - Identity-collapse before allocation.
    - L1/sum-normalize base and external vectors independently.
    - When both have positive mass:
        output = (1 - fraction) * base_norm + fraction * ext_norm
    - Base only: preserve 100% base behavior.
    - External only: fail closed to base (empty) result.  External must
      never expand to 100% on its own.
    - Neither: preserve existing downstream burn behavior (empty dict).
    """
    blend_meta: dict[str, Any] = {
        "base_mass": 0.0,
        "external_mass": 0.0,
        "base_miner_count": 0,
        "external_miner_count": 0,
        "blended": False,
    }
    # Explicit 100% confidential-compute mode: base is never a positive source,
    # even when ingestion is disabled. Disabled ingestion in primary mode must
    # produce empty/burn, never fall through to base.
    if external_scores_mode() == "confidential_primary":
        return _apply_confidential_primary(store, base, now=now, ident=ident)

    if not external_scores_enabled():
        blend_meta["base_mass"] = 1.0 if base else 0.0
        blend_meta["base_miner_count"] = len(base)
        return base, blend_meta

    src = external_scores_source()
    confidential_fraction = None
    if src in EXTERNAL_SCORES_GLOBAL_CAP_SOURCES:
        confidential_fraction = _confidential_tdx_fraction()
        if confidential_fraction is None:
            blend_meta["base_mass"] = 1.0 if base else 0.0
            blend_meta["base_miner_count"] = len(base)
            blend_meta["degraded"] = "confidential_fraction_missing"
            return base, blend_meta

    ext = _compose_external_scores(store, now=now, ident=ident)

    # Confidential TDX must always have a fresh metagraph snapshot before any
    # external mass is admitted, regardless of the legacy registration flag.
    require_registered = src in EXTERNAL_SCORES_GLOBAL_CAP_SOURCES or _env_bool(
        EXTERNAL_SCORES_REQUIRE_REGISTERED_ENV, True
    )
    if ext and require_registered:
        registered, _meta = _load_fresh_metagraph_hotkeys(store, now=now)
        if registered is None:
            print(
                "[weights] external_scores: registration snapshot unavailable "
                "-> NOT blending external scores (fail-closed)"
            )
            ext = {}
            if src in EXTERNAL_SCORES_GLOBAL_CAP_SOURCES:
                blend_meta["degraded"] = (
                    "confidential_registration_snapshot_unavailable"
                )
        else:
            ext = {hk: v for hk, v in ext.items() if hk in registered}

    # external_primary mode: still require explicit ack.
    if ext and external_scores_mode() == "external_primary":
        if not _env_bool(EXTERNAL_SCORES_PRIMARY_CONFIRM_ENV, False):
            print(
                "[weights] external_scores: external_primary requested WITHOUT "
                f"{EXTERNAL_SCORES_PRIMARY_CONFIRM_ENV}=true -> falling back to capped blend"
            )

    # (#4) Payability/eligibility filtering MUST happen to each mechanism
    # BEFORE it is L1-normalized and allocated, not after the blend. If a
    # single post-blend filter pass (e.g. build_signed_vector's final
    # _apply_payable_hotkey_policy call) removed hotkeys after allocation, it
    # could strip base-only entries disproportionately and push the realized
    # external share of the SURVIVING vector above the configured fraction.
    # Filtering both mechanisms here, pre-normalization, makes the (1-f)/f
    # split exact by construction on the final, already-filtered hotkey set;
    # the later call in build_signed_vector is then a no-op for anything
    # already filtered here (still run there so base-only vectors, and the
    # off/mark modes, keep their existing policy-metadata surface).
    if payable_hotkeys_mode() == "filter":
        base, base_payable_meta = _apply_payable_hotkey_policy(store, base, now=now)
        blend_meta["base_payable_filter"] = base_payable_meta
        if ext:
            ext, ext_payable_meta = _apply_payable_hotkey_policy(store, ext, now=now)
            blend_meta["external_payable_filter"] = ext_payable_meta

    has_base = bool(base) and any(v > 0 for v in base.values())
    has_ext = bool(ext) and any(v > 0 for v in ext.values())

    # Neither: return empty (downstream burn handles this).
    if not has_base and not has_ext:
        return {}, blend_meta

    # External only: fail closed. Do NOT let external expand to 100%.
    if not has_base and has_ext:
        blend_meta["external_miner_count"] = len(ext)
        blend_meta["degraded"] = "external_only_fail_closed"
        return {}, blend_meta

    # Base only: 100% base.
    if has_base and not has_ext:
        blend_meta["base_mass"] = 1.0
        blend_meta["base_miner_count"] = len(base)
        return base, blend_meta

    # Both have positive mass: blend.
    _bw, _ew, fraction = _external_blend_weights()
    if confidential_fraction is not None:
        fraction = confidential_fraction
    if fraction <= 0.0:
        blend_meta["base_mass"] = 1.0
        blend_meta["base_miner_count"] = len(base)
        return base, blend_meta

    base_norm = _l1_normalize(base)
    ext_norm = _l1_normalize(ext)

    if not base_norm:
        blend_meta["degraded"] = "base_norm_zero"
        return base, blend_meta
    if not ext_norm:
        blend_meta["base_mass"] = 1.0
        blend_meta["base_miner_count"] = len(base)
        return base, blend_meta

    # Confidential TDX uses a global union composition with auditable row parts.
    if src in EXTERNAL_SCORES_GLOBAL_CAP_SOURCES:
        blended, base_comp, ext_comp, cap_meta = _apply_confidential_tdx_global_cap(
            base_norm, ext_norm, fraction
        )
        realized_base = cap_meta["actual_base_mass"]
        realized_ext = cap_meta["actual_external_mass"]
        blend_meta["blended"] = True
        blend_meta["base_mass"] = round(realized_base, 9)
        blend_meta["external_mass"] = round(realized_ext, 9)
        blend_meta["base_miner_count"] = len(base_norm)
        blend_meta["external_miner_count"] = len(ext_norm)
        blend_meta["fraction"] = fraction
        blend_meta["confidential_tdx_cap"] = cap_meta
        # Per-hotkey auditable components (§5) stored ONLY in signed weight entries,
        # not in policy_metadata. Keep as internal state for _build_weights_list to use.
        blend_meta["_internal_base_components"] = base_comp
        blend_meta["_internal_ext_components"] = ext_comp
        print("[weights] confidential_tdx blend applied")
        return blended, blend_meta

    base_coeff = 1.0 - fraction
    ext_coeff = fraction

    hotkeys = set(base_norm) | set(ext_norm)
    blended: dict[str, float] = {}
    for hk in hotkeys:
        blended[hk] = base_coeff * base_norm.get(hk, 0.0) + ext_coeff * ext_norm.get(
            hk, 0.0
        )

    blend_meta["blended"] = True
    blend_meta["base_mass"] = round(base_coeff, 9)
    blend_meta["external_mass"] = round(ext_coeff, 9)
    blend_meta["base_miner_count"] = len(base_norm)
    blend_meta["external_miner_count"] = len(ext_norm)
    blend_meta["fraction"] = fraction

    print("[weights] external_scores blend applied")

    return blended, blend_meta


def _external_scores_policy_status(
    store: Store | None,
    *,
    now: datetime,
) -> dict[str, Any]:
    enabled = external_scores_enabled()
    source = external_scores_source()
    window_secs = external_scores_window_secs()
    status: dict[str, Any] = {
        "enabled": enabled,
        "source": source,
        "mode": external_scores_mode(),
        "window_secs": window_secs,
        "base_weight": max(0.0, _env_float(EXTERNAL_SCORES_BASE_WEIGHT_ENV, 1.0)),
        "external_weight": max(0.0, _env_float(EXTERNAL_SCORES_WEIGHT_ENV, 1.0)),
        "effective_external_share": 1.0
        if external_scores_mode() == "confidential_primary"
        else round(_external_blend_weights()[2], 6),
        "require_registered": _env_bool(EXTERNAL_SCORES_REQUIRE_REGISTERED_ENV, True),
        "primary_confirmed": _env_bool(EXTERNAL_SCORES_PRIMARY_CONFIRM_ENV, False),
        "has_scores": False,
    }
    if not enabled or store is None:
        return status
    since = _ms_iso(now - timedelta(seconds=window_secs))
    try:
        status.update(external_scores.status(store, source=source, since_iso=since))
        status["has_scores"] = int(status.get("active_score_count") or 0) > 0
    except Exception as exc:
        status["error"] = repr(exc)
    return status


def _proportional_ledger_has_rows(store: Store, since: str) -> bool:
    rows = store.query(
        "SELECT 1 FROM lane_challenge_solves s "
        "LEFT JOIN lane_challenges c ON c.challenge_id = s.challenge_id "
        "WHERE s.solved_at_iso > ? AND COALESCE(c.score_multiplier, 1.0) > 0 "
        "LIMIT 1",
        (since,),
    )
    return bool(rows)


def _perminer_policy_status(
    store: Store | None = None,
    *,
    now: datetime | None = None,
    coldkey_of: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Surface per-miner flag state so a score-source flip is never silent."""
    try:
        from . import per_miner as pm
    except Exception:
        return {
            "perminer_enabled": False,
            "perminer_shadow": False,
            "perminer_live_requested": False,
            "perminer_bonus_live": False,
            "perminer_primary_live": False,
            "perminer_epoch": None,
            "perminer_has_scores": False,
            "score_source": None,
            "scoring_mode": perminer_scoring_mode(),
            "bonus_multiplier": perminer_bonus_multiplier(),
            "history_floor": perminer_history_floor(),
            "public_baseline": perminer_public_baseline(),
            "coldkey_required": perminer_require_coldkey(),
            "identity_ready": not perminer_require_coldkey(),
            "degraded_reason": "per_miner_import_failed",
        }
    enabled = _perminer_scoring_enabled()
    shadow = _perminer_scoring_shadow()
    epoch = pm.current_epoch() if enabled else None
    has_scores = False
    coldkey_loaded = bool(coldkey_of)
    unmapped_hotkeys_24h: int | None = None
    if enabled and store is not None and epoch is not None:
        now = now or datetime.now(timezone.utc)
        since = _ms_iso(now - timedelta(hours=window_hours()))

        def mapped_identity(hk: str) -> str:
            return coldkey_of.get(hk, hk) if coldkey_of else hk

        has_scores = bool(
            _perminer_window_scores(store, since=since, ident=mapped_identity)
        )
        try:
            rows = store.query(
                "SELECT DISTINCT miner_hotkey FROM per_miner_solves "
                "WHERE solved_at_iso > ? AND verified=1",
                (since,),
            )
            unmapped_hotkeys_24h = sum(
                1
                for r in rows
                if not coldkey_of or str(r["miner_hotkey"]) not in coldkey_of
            )
        except Exception:
            unmapped_hotkeys_24h = None
    live_requested = enabled and not shadow
    scoring_mode = perminer_scoring_mode()
    identity_ready = True
    degraded_reason = None
    if live_requested and scoring_mode in {"pm_primary", "assigned_only"}:
        if not has_scores:
            degraded_reason = "no_verified_per_miner_scores"
    bonus_live = (
        live_requested
        and scoring_mode == "bonus"
        and perminer_bonus_multiplier() > 0.0
        and has_scores
        and identity_ready
    )
    primary_live = (
        live_requested
        and scoring_mode == "pm_primary"
        and has_scores
        and identity_ready
    )
    return {
        "perminer_enabled": enabled,
        "perminer_shadow": shadow,
        "perminer_live_requested": live_requested,
        "perminer_bonus_live": bonus_live,
        "perminer_primary_live": primary_live,
        "perminer_epoch": epoch,
        "perminer_has_scores": has_scores,
        "score_source": "per_miner"
        if live_requested and has_scores and scoring_mode == "assigned_only"
        else "pm_primary"
        if primary_live
        else None,
        "scoring_mode": scoring_mode,
        "bonus_multiplier": perminer_bonus_multiplier(),
        "history_floor": perminer_history_floor(),
        "public_baseline": perminer_public_baseline(),
        "coldkey_required": perminer_require_coldkey(),
        "identity_ready": identity_ready,
        "identity_mode": (
            "coldkey_map_with_hotkey_fallback" if coldkey_loaded else "hotkey_fallback"
        ),
        "unmapped_hotkeys_24h": unmapped_hotkeys_24h,
        "degraded_reason": degraded_reason,
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
    pm_status = _perminer_policy_status(store, now=now, coldkey_of=coldkey_of)
    requested = mode()
    effective = _effective_mode(store, since)
    source = pm_status["score_source"] or effective
    hotkey = str(miner_hotkey)
    scores: dict[str, float] = {}
    if source in {"per_miner", "pm_primary"}:
        scores = compose_scores(store, now=now, coldkey_of=coldkey_of)
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
            "bonus_live": pm_status.get("perminer_bonus_live", False),
            "primary_live": pm_status.get("perminer_primary_live", False),
            "epoch": pm_status["perminer_epoch"],
            "has_scores": pm_status["perminer_has_scores"],
            "scoring_mode": pm_status["scoring_mode"],
            "bonus_multiplier": pm_status["bonus_multiplier"],
            "history_floor": pm_status["history_floor"],
            "public_baseline": pm_status["public_baseline"],
            "coldkey_required": pm_status["coldkey_required"],
        },
    }

    if source in {"per_miner", "pm_primary"}:
        try:
            rows = store.query(
                "SELECT tier, COUNT(*) AS solves, SUM(difficulty_weight) AS units "
                "FROM per_miner_solves WHERE solved_at_iso > ? AND miner_hotkey=? AND verified=1 "
                "GROUP BY tier ORDER BY tier",
                (since, hotkey),
            )
            top_rows = store.query(
                "SELECT miner_hotkey, SUM(difficulty_weight) AS units "
                "FROM per_miner_solves WHERE solved_at_iso > ? AND verified=1 "
                "GROUP BY miner_hotkey",
                (since,),
            )
            raw_units = sum(float(r["units"] or 0.0) for r in rows)
            top_units = max((float(r["units"] or 0.0) for r in top_rows), default=0.0)
            base.update(
                {
                    "raw_units": round(raw_units, 6),
                    "top_units": round(top_units, 6),
                    "distinct_challenges": int(
                        sum(int(r["solves"] or 0) for r in rows)
                    ),
                    "tiers": [
                        {
                            "tier": int(r["tier"]),
                            "solves": int(r["solves"] or 0),
                            "weighted_units": round(float(r["units"] or 0.0), 6),
                        }
                        for r in rows
                    ],
                }
            )
            return base
        except Exception as exc:
            base["explain_error"] = f"per_miner_explain_failed:{type(exc).__name__}"
            return base

    if effective == "proportional":
        rows = store.query(
            "SELECT DISTINCT s.challenge_id "
            "FROM lane_challenge_solves s "
            "LEFT JOIN lane_challenges c ON c.challenge_id = s.challenge_id "
            "WHERE s.solved_at_iso > ? AND s.miner_hotkey=? "
            "AND COALESCE(c.score_multiplier, 1.0) > 0",
            (since, hotkey),
        )
        weights_by_tier = tier_weights()
        own: dict[str, Any] = {"units": 0.0, "seen": set(), "tiers": {}}
        for r in rows:
            cid = str(r["challenge_id"])
            tier = tier_from_challenge_id(cid)
            weight = float(weights_by_tier.get(tier, weights_by_tier.get(1, 1.0)))
            if cid in own["seen"]:
                continue
            own["seen"].add(cid)
            own["units"] += weight
            tier_entry = own["tiers"].setdefault(tier, {"solves": 0, "units": 0.0})
            tier_entry["solves"] += 1
            tier_entry["units"] += weight
        raw_units = float(own["units"])
        top_units = raw_units
        if raw_units > 0.0:
            try:
                case_parts = []
                params: list[float | int | str] = []
                for tier, weight in sorted(weights_by_tier.items()):
                    case_parts.append("WHEN ? THEN ?")
                    params.extend([int(tier), float(weight)])
                default_weight = float(weights_by_tier.get(1, 1.0))
                case_sql = (
                    "CASE COALESCE(c.tier, 1) " + " ".join(case_parts) + " ELSE ? END"
                )
                params.extend([default_weight, since])
                top_rows = store.query(
                    "SELECT MAX(units) AS top_units FROM ("
                    "SELECT d.miner_hotkey, SUM(" + case_sql + ") AS units "
                    "FROM (SELECT DISTINCT miner_hotkey, challenge_id "
                    "FROM lane_challenge_solves WHERE solved_at_iso > ?) d "
                    "LEFT JOIN lane_challenges c ON c.challenge_id = d.challenge_id "
                    "WHERE COALESCE(c.score_multiplier, 1.0) > 0 "
                    "GROUP BY d.miner_hotkey"
                    ") x",
                    tuple(params),
                )
                top_units = max(
                    raw_units,
                    float(top_rows[0]["top_units"] or 0.0) if top_rows else 0.0,
                )
            except Exception as exc:
                base["top_units_error"] = f"{type(exc).__name__}"
        normalized_weight = raw_units / top_units if top_units > 0 else 0.0
        base.update(
            {
                "normalized_weight": round(normalized_weight, 6),
                "top_weight": 1.0 if top_units > 0 else 0.0,
                "raw_units": round(raw_units, 6),
                "top_units": round(top_units, 6),
                "distinct_challenges": len(own["seen"]),
                "tiers": [
                    {
                        "tier": tier,
                        "solves": int(v["solves"]),
                        "weighted_units": round(float(v["units"]), 6),
                        "score_weight": float(
                            weights_by_tier.get(tier, weights_by_tier.get(1, 1.0))
                        ),
                    }
                    for tier, v in sorted(own["tiers"].items())
                ],
            }
        )
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
        base.update(
            {
                "raw_units": round(total, 6),
                "accepted_rows": accepted,
                "distinct_challenges": accepted,
                "tiers": [],
            }
        )
        return base

    feed = store.query(
        "SELECT COUNT(DISTINCT id) AS n FROM eval_runs WHERE ran_at > ? AND miner_hotkey=?",
        (since, hotkey),
    )
    accepted = int(feed[0]["n"] or 0) if feed else 0
    base.update(
        {
            "raw_units": 1.0 if accepted else 0.0,
            "accepted_rows": accepted,
            "distinct_challenges": accepted,
            "tiers": [],
        }
    )
    return base


def _compose_proportional_hotkey_sql(store: Store, since: str) -> dict[str, float]:
    """Fast proportional scorer when identity is the hotkey.

    The old path selected every distinct solve into Python, then looped and
    tier-weighted in process. On the live board that can hold the GIL during the
    background weight refresh and stall even `/health`. Let Postgres/SQLite do
    the distinct/group/sum work and only normalize the small hotkey aggregate.
    """
    weights_by_tier = tier_weights()
    tier_expr_parts = ["CASE WHEN c.tier IS NOT NULL THEN c.tier"]
    params: list[float | int | str] = []
    for tier in sorted(weights_by_tier):
        if int(tier) == 1:
            continue
        tier_expr_parts.append("WHEN d.challenge_id LIKE ? THEN ?")
        params.extend([f"%-t{int(tier)}-%", int(tier)])
    tier_expr_parts.append("ELSE 1 END")
    tier_expr = " ".join(tier_expr_parts)

    case_parts: list[str] = []
    for tier, weight in sorted(weights_by_tier.items()):
        case_parts.append("WHEN ? THEN ?")
        params.extend([int(tier), float(weight)])
    default_weight = float(weights_by_tier.get(1, 1.0))
    case_sql = "CASE (" + tier_expr + ") " + " ".join(case_parts) + " ELSE ? END"
    params.extend([default_weight, since])
    rows = store.query(
        "SELECT d.miner_hotkey, SUM(" + case_sql + ") AS units "
        "FROM ("
        "  SELECT DISTINCT miner_hotkey, challenge_id "
        "  FROM lane_challenge_solves WHERE solved_at_iso > ?"
        ") d "
        "LEFT JOIN lane_challenges c ON c.challenge_id = d.challenge_id "
        "WHERE COALESCE(c.score_multiplier, 1.0) > 0 "
        "GROUP BY d.miner_hotkey",
        tuple(params),
    )
    units = {
        str(r["miner_hotkey"]): float(r["units"] or 0.0)
        for r in rows
        if float(r["units"] or 0.0) > 0.0
    }
    if not units:
        return {}
    top = max(units.values())
    if top <= 0.0:
        return {}
    return {hk: round(score / top, 6) for hk, score in units.items()}


def compose_scores(
    store: Store,
    *,
    now: datetime | None = None,
    coldkey_of: dict[str, str] | None = None,
    blend_meta_out: dict[str, Any] | None = None,
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
    use_pm_ck = bool(coldkey_of)

    def ident(hk: str) -> str:
        return coldkey_of.get(hk, hk) if use_ck else hk

    def pm_ident(hk: str) -> str | None:
        return coldkey_of.get(hk, hk) if use_pm_ck else ident(hk)

    # Container for blend metadata; populated by _apply_external_scores.
    _blend_meta_box: list[dict[str, Any]] = []

    def _finish_external(base: dict[str, float]) -> dict[str, float]:
        result, meta = _apply_external_scores(store, base, now=now, ident=ident)
        _blend_meta_box.clear()
        _blend_meta_box.append(meta)
        return result

    pm_scores = _perminer_compose_scores(store, ident=pm_ident, since=since)
    if pm_scores is not None and perminer_scoring_mode() == "assigned_only":
        scores = _finish_external(pm_scores)
        if blend_meta_out is not None and _blend_meta_box:
            blend_meta_out.update(_blend_meta_box[0])
        return scores

    def finish_base(base: dict[str, float]) -> dict[str, float]:
        if pm_scores is not None and perminer_scoring_mode() == "pm_primary":
            base = _apply_perminer_primary(base, pm_scores)
        else:
            base = _apply_perminer_bonus(store, base, coldkey_of, now=now)
        scores = _finish_external(base)
        if blend_meta_out is not None and _blend_meta_box:
            blend_meta_out.update(_blend_meta_box[0])
        return scores

    if mode() == "row_score_recent":
        return finish_base(_compose_row_score_recent(store, since, ident=ident))

    if mode() == "proportional":
        if not use_ck:
            base = _compose_proportional_hotkey_sql(store, since)
            if base:
                return finish_base(base)
        rows = store.query(
            "SELECT DISTINCT s.miner_hotkey, s.challenge_id "
            "FROM lane_challenge_solves s "
            "LEFT JOIN lane_challenges c ON c.challenge_id = s.challenge_id "
            "WHERE s.solved_at_iso > ? AND COALESCE(c.score_multiplier, 1.0) > 0",
            (since,),
        )
        # identity -> weighted score (sum of per-challenge tier weights, deduped)
        scores_w: dict[str, float] = {}
        # identity -> set of distinct challenge_ids (for dedup)
        seen: dict[str, set] = {}
        hks: dict[str, set] = {}  # identity -> set of its solving hotkeys
        weights_by_tier = tier_weights()
        for r in rows:
            hk = str(r["miner_hotkey"])
            idk = ident(hk)
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
            return finish_base(base)
        # no in-window claim rows -> fall through to flat

    feed = store.query(
        "SELECT DISTINCT miner_hotkey FROM eval_runs WHERE ran_at > ?", (since,)
    )
    hotkeys = {str(r["miner_hotkey"]) for r in feed}
    if not use_ck:
        return finish_base({hk: 1.0 for hk in hotkeys})
    # flat, identity-deduped: each coldkey's hotkeys share a single 1.0
    groups: dict[str, list[str]] = {}
    for hk in hotkeys:
        groups.setdefault(ident(hk), []).append(hk)
    out: dict[str, float] = {}
    for members in groups.values():
        per = round(1.0 / len(members), 6)
        for hk in members:
            out[hk] = per
    return finish_base(out)


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
            "VALUES (1, ?, ?)",
            (nxt, _ms_iso(datetime.now(timezone.utc))),
        )
        return nxt

    return store.write(_bump)


# -- sign -----------------------------------------------------------------------


def _compose_cybergym_lane_v3(store: Store, *, now: datetime) -> dict[str, Any]:
    """Compose the CyberGym lane for the v3 allocation contract. Fail closed.

    Returns the signed, uid-keyed lane block written to
    ``policy_metadata["cybergym_lane"]``. It is exactly what
    ``cybergym_bridge.cybergym_allocation`` produces: contributing miners split
    their share and any forfeited / ineligible mass is allocated to the burn UID
    resolved from the burn hotkey through the same fresh metagraph snapshot the
    eligibility gate uses. The combined lane mass equals the configured v3
    CyberGym allocation (``V3_CYBERGYM_ALLOCATION`` = 0.30). Any condition that
    leaves the 30% lane unprovable — mechanism disabled, wrong configured
    fraction, or an unresolved burn destination — raises so the ENTIRE v3 vector
    refuses to sign rather than emit a partial or ambiguous split.
    """
    # Imported lazily: cybergym_bridge imports from this module.
    from . import cybergym_bridge

    if not cybergym_bridge.mechanism_enabled():
        raise VectorError(
            "allocation contract v3 requires the CyberGym mechanism enabled via "
            f"{cybergym_bridge.MECHANISM_ENABLED_ENV}"
        )
    fraction = cybergym_bridge.weight_fraction()
    if not math.isclose(
        fraction, V3_CYBERGYM_ALLOCATION, rel_tol=0.0, abs_tol=1e-12
    ):
        raise VectorError(
            "allocation contract v3 requires CyberGym weight fraction "
            f"{V3_CYBERGYM_ALLOCATION} via {cybergym_bridge.WEIGHT_FRACTION_ENV}; "
            f"got {fraction!r}"
        )
    lane = cybergym_bridge.cybergym_allocation(store, now=now)
    status = lane.get("status")
    if status != "ok":
        # disabled / no_contribution / burn_destination_unresolved all mean the
        # 30% lane cannot be proven; refuse rather than emit a partial vector.
        burn_reason = (lane.get("burn") or {}).get("reason")
        cyber_reason = (lane.get("cybergym") or {}).get("reason")
        raise VectorError(
            "allocation contract v3 CyberGym lane not composable: "
            f"status={status!r} cybergym={cyber_reason!r} burn={burn_reason!r}"
        )
    weights = lane.get("weights") or {}
    lane_mass = math.fsum(float(v) for v in weights.values())
    if not math.isclose(lane_mass, fraction, rel_tol=0.0, abs_tol=1e-9):
        raise VectorError(
            f"allocation contract v3 CyberGym lane mass {lane_mass!r} != {fraction!r}"
        )
    burn_uid_val = lane.get("burn_uid")
    forfeited = float(lane.get("forfeited_fraction") or 0.0)
    if forfeited > 0.0 and burn_uid_val is None:
        raise VectorError(
            "allocation contract v3 CyberGym lane has forfeited mass but no burn uid"
        )
    return {
        "fraction": fraction,
        "weights": {str(int(uid)): float(w) for uid, w in weights.items()},
        "contributing_fraction": float(lane.get("contributing_fraction") or 0.0),
        "forfeited_fraction": forfeited,
        "burn_uid": None if burn_uid_val is None else int(burn_uid_val),
        "cybergym": lane.get("cybergym") or {},
    }


def build_signed_vector(
    store: Store, *, signing_key_hex: str, now: datetime | None = None
) -> dict[str, Any]:
    """Compose scores, assemble the wire payload, sign. Returns the dict
    served verbatim by /v1/validator/weights/next.

    After the final payable filter, recomputes aggregate component masses and
    fractions for global-cap sources and reasserts the configured fraction.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    now = now or datetime.now(timezone.utc)
    supply_policy = validated_supply_metadata()
    coldkey_of = _load_scoring_coldkey_map(store)
    since = _ms_iso(now - timedelta(hours=window_hours()))
    blend_meta: dict[str, Any] = {}
    scores = compose_scores(
        store, now=now, coldkey_of=coldkey_of, blend_meta_out=blend_meta
    )
    scores, payable_meta = _apply_payable_hotkey_policy(store, scores, now=now)
    cap_meta = blend_meta.get("confidential_tdx_cap")
    if cap_meta:
        base_comp = blend_meta.get("_internal_base_components") or {}
        ext_comp = blend_meta.get("_internal_ext_components") or {}
        filtered_base = {hk: base_comp[hk] for hk in scores if hk in base_comp}
        filtered_ext = {hk: ext_comp[hk] for hk in scores if hk in ext_comp}
        blend_meta["_internal_base_components"] = filtered_base
        blend_meta["_internal_ext_components"] = filtered_ext
        totals = _validate_confidential_tdx_components(
            scores,
            filtered_base,
            filtered_ext,
            float(blend_meta["fraction"]),
            context="post-payable-filter",
        )
        cap_meta.update(
            {
                "actual_base_mass": totals["base_mass"],
                "actual_external_mass": totals["external_mass"],
                "realized_external_fraction": totals["external_fraction"],
            }
        )
        blend_meta["base_miner_count"] = len(filtered_base)
        blend_meta["external_miner_count"] = sum(
            1 for value in filtered_ext.values() if value > 0.0
        )
    requested_mode = mode()
    effective_mode = _effective_mode(store, since)
    proportional_ledger_empty = (
        requested_mode == "proportional" and effective_mode == "flat_recent_fallback"
    )
    pm_status = _perminer_policy_status(store, now=now, coldkey_of=coldkey_of)
    external_status = _external_scores_policy_status(store, now=now)
    score_source = pm_status["score_source"] or effective_mode
    if external_scores_mode() == "confidential_primary":
        score_source = f"confidential_primary:{external_scores_source()}"
    elif external_status.get("enabled") and external_status.get("has_scores"):
        ext_source = f"external:{external_status.get('source')}"
        score_source = (
            ext_source
            if external_scores_mode() == "external_primary"
            else f"{score_source}+{ext_source}"
        )
    valid_for = _env_float(VALID_FOR_ENV, 1800.0)
    policy_inputs = {
        "mode": requested_mode,
        "effective_mode": effective_mode,
        "score_source": score_source,
        "external_scores": external_status,
        "window_hours": window_hours(),
        "burn": burn_percentage(),
        "burn_uid": burn_uid(),
        "burn_hotkey": burn_hotkey(),
        "validated_supply": supply_policy,
        "tier_weights": tier_weights(),
        "payable_hotkeys": payable_meta,
        "hotkeys": sorted(scores),
        "scores": [scores[k] for k in sorted(scores)],
    }
    burn_snapshot = {
        "burn_uid": burn_uid(),
        "forced_burn_percentage": burn_percentage(),
    }
    configured_burn_hotkey = burn_hotkey()
    if configured_burn_hotkey is not None:
        burn_snapshot["burn_hotkey"] = configured_burn_hotkey
    payload: dict[str, Any] = {
        "vector_id": str(uuid.uuid4()),
        "policy_version": next_policy_version(store),
        "network": os.environ.get(NETWORK_ENV, "finney"),
        "netuid": int(os.environ.get(NETUID_ENV, "39")),
        "generated_at": _ms_iso(now),
        "expires_at": _ms_iso(now + timedelta(seconds=valid_for)),
        "burn_snapshot": burn_snapshot,
        "policy_hash": "sha256:"
        + hashlib.sha256(
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
            "payable_hotkeys": payable_meta,
            "external_scores": external_status,
            "blend": {
                k: v for k, v in blend_meta.items() if not k.startswith("_internal")
            },
            "confidential_tdx_cap": blend_meta.get("confidential_tdx_cap"),
            "perminer_scoring_mode": pm_status["scoring_mode"],
            "perminer": {
                "enabled": pm_status["perminer_enabled"],
                "shadow": pm_status["perminer_shadow"],
                "live_requested": pm_status["perminer_live_requested"],
                "bonus_live": pm_status.get("perminer_bonus_live", False),
                "primary_live": pm_status.get("perminer_primary_live", False),
                "epoch": pm_status["perminer_epoch"],
                "has_scores": pm_status["perminer_has_scores"],
                "scoring_mode": pm_status["scoring_mode"],
                "bonus_multiplier": pm_status["bonus_multiplier"],
                "history_floor": pm_status["history_floor"],
                "public_baseline": pm_status["public_baseline"],
                "coldkey_required": pm_status["coldkey_required"],
                "identity_ready": pm_status.get("identity_ready", False),
                "degraded_reason": pm_status.get("degraded_reason"),
            },
        },
        "weights": _build_weights_list(scores, blend_meta),
    }
    # Signed confidential-primary contract metadata is added ONLY when the mode
    # is selected, so default and 10% blend vectors stay byte-compatible.
    cp_policy = blend_meta.get("confidential_primary")
    if cp_policy is not None:
        payload["policy_metadata"]["confidential_primary"] = cp_policy
    if supply_policy is not None:
        if cp_policy is None:
            raise VectorError(
                "validated_supply requires signed confidential_primary metadata"
            )
        payload["policy_metadata"]["validated_supply"] = supply_policy
        # v3 wires in the CyberGym lane (30%) alongside the confidential Intel TDX
        # lane (70%). The signed weight rows stay the confidential-primary rows
        # (mass 1.0, base_component 0 / external_component weight) — byte-identical
        # to v2 — and the validator scales the TDX lane to 70% at mapping time.
        # The CyberGym lane travels uid-keyed in policy_metadata so the validator
        # re-derives it exactly as this publisher composed it.
        if supply_policy.get("contract_version") == "v3":
            payload["policy_metadata"]["cybergym_lane"] = _compose_cybergym_lane_v3(
                store, now=now
            )
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(signing_key_hex.strip()))
    payload["signature"] = base64.b64encode(sk.sign(canonical_bytes(payload))).decode()
    return payload


def _build_weights_list(
    scores: dict[str, float],
    blend_meta: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build the weights list, annotating each entry with per-hotkey components
    when a confidential_tdx global blend was performed.
    Components are emitted ONLY in each signed weight entry, not in policy_metadata.
    Existing thin validators ignore the extra fields.

    Components are NOT rounded independently; w_i = a_i + c_i is preserved exactly.
    """
    base_comp: dict[str, float] = blend_meta.get("_internal_base_components") or {}
    ext_comp: dict[str, float] = blend_meta.get("_internal_ext_components") or {}
    capped = bool(blend_meta.get("confidential_tdx_cap"))
    cp_meta = blend_meta.get("confidential_primary")
    confidential_primary = (
        bool(cp_meta) and float(cp_meta.get("confidential_mass") or 0.0) > 0.0
    )
    if capped:
        _validate_confidential_tdx_components(
            scores,
            base_comp,
            ext_comp,
            float(blend_meta["fraction"]),
            context="pre-sign",
        )
    entries: list[dict[str, Any]] = []
    for hk in sorted(scores):
        entry: dict[str, Any] = {"miner_hotkey": hk, "weight": scores[hk]}
        if capped:
            entry["base_component"] = base_comp[hk]
            entry["external_component"] = ext_comp[hk]
        elif confidential_primary:
            # 100% confidential: base share is exactly 0, external == weight.
            entry["base_component"] = 0.0
            entry["external_component"] = scores[hk]
        entries.append(entry)
    return entries


def _mark_refresh(status: str, error: "BaseException | None" = None) -> None:
    with _refresh_health_lock:
        _refresh_health["last_status"] = status
        if status == "ok":
            _refresh_health["last_ok_ts"] = time.time()
            _refresh_health["last_error"] = None
            _refresh_health["consecutive_failures"] = 0
        else:
            if status == "timeout":
                _refresh_health["last_timeout_ts"] = time.time()
            if error is not None:
                _refresh_health["last_error"] = repr(error)
            _refresh_health["consecutive_failures"] += 1


def refresh_health() -> dict[str, Any]:
    """Snapshot of background-refresh liveness (for /health wiring and tests).

    ``age_seconds`` is time since the last *successful* refresh, or None if one
    has never completed in this process.
    """
    with _refresh_health_lock:
        snap = dict(_refresh_health)
    last_ok = snap.get("last_ok_ts") or 0.0
    snap["age_seconds"] = (time.time() - last_ok) if last_ok else None
    snap["timeout_secs"] = _REFRESH_TIMEOUT_SECS
    return snap


def _refresh_once_with_timeout(
    store: Store, *, signing_key_hex: str, timeout: float
) -> dict[str, Any] | None:
    """Run _refresh_once in a worker thread, bounded by ``timeout`` seconds.

    Returns the vector (or None) on completion; raises _RefreshTimeout if the
    attempt is still running after ``timeout``, or if a prior attempt is still
    wedged (we never run two at once). The orphaned attempt is a daemon thread
    and cannot block process exit; it is abandoned and the next cycle retries —
    picking up a healthy leader's persisted vector if this build is stuck. Any
    exception inside the attempt is re-raised to the caller.
    """
    global _refresh_attempt
    with _refresh_attempt_lock:
        prev = _refresh_attempt
        if prev is not None and prev.is_alive():
            raise _RefreshTimeout("previous refresh attempt still running")

    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["vec"] = _refresh_once(store, signing_key_hex=signing_key_hex)
        except BaseException as exc:  # surfaced to the loop's handler below
            box["err"] = exc

    t = threading.Thread(target=_run, name="weights-refresh-attempt", daemon=True)
    with _refresh_attempt_lock:
        _refresh_attempt = t
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise _RefreshTimeout(f"weight refresh exceeded {timeout}s")
    if "err" in box:
        raise box["err"]
    return box.get("vec")


def _try_adopt_persisted(store: Store, generation: int, timeout: float = 10.0) -> None:
    """Best-effort freshness recovery after a refresh timeout.

    Adopt the persisted ``latest`` vector (a cheap PK lookup) that a healthy
    sibling leader may have written, so a replica whose own build is wedged stops
    serving a stale cache. Bounded by ``timeout`` so it can never itself re-wedge
    the background loop. Never raises.
    """
    box: dict[str, Any] = {}

    def _run() -> None:
        try:
            box["vec"] = _load_persisted_vector(store)
        except BaseException as exc:
            box["err"] = exc

    t = threading.Thread(target=_run, name="weights-adopt-persisted", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        print("[weights] persisted adopt timed out; skipping")
        return
    if "err" in box:
        print(f"[weights] persisted adopt failed: {box['err']!r}")
        return
    vec = box.get("vec")
    if vec is not None and generation == _bg_generation:
        _cache_write(vec)


def _run_refresh_cycle(store: Store, signing_key_hex: str, generation: int) -> str:
    """One refresh iteration. Never raises; returns a status string.

    Identical behavior to the historical loop body on the happy path (build →
    cache); the only additions are the wall-clock bound and liveness marking.
    """
    try:
        if _REFRESH_TIMEOUT_SECS > 0:
            vec = _refresh_once_with_timeout(
                store, signing_key_hex=signing_key_hex, timeout=_REFRESH_TIMEOUT_SECS
            )
        else:
            vec = _refresh_once(store, signing_key_hex=signing_key_hex)
        if vec is not None and generation == _bg_generation:
            _cache_write(vec)
        _mark_refresh("ok")
        return "ok"
    except _RefreshTimeout as exc:
        _mark_refresh("timeout", exc)
        print(
            f"[weights] bg_refresh TIMED OUT after {_REFRESH_TIMEOUT_SECS}s; "
            "abandoning cycle, will retry next tick"
        )
        # Freshness recovery: adopt a sibling leader's persisted vector (bounded).
        _try_adopt_persisted(store, generation)
        return "timeout"
    except Exception as exc:
        _mark_refresh("error", exc)
        print(f"[weights] bg_refresh error (will retry): {exc!r}")
        return "error"


def _bg_refresh_loop(store: Store, signing_key_hex: str, generation: int) -> None:
    """Background daemon thread: rebuild the vector every _CACHE_TTL_SECS.

    Never raises — a transient DB error OR a hung rebuild is logged and retried
    next cycle. Each attempt is wall-clock bounded (_REFRESH_TIMEOUT_SECS) so a
    single stuck DB call can no longer freeze this replica's served vector.
    Runs forever; the process exiting is the only exit condition (daemon=True).
    """
    while True:
        if generation != _bg_generation:
            return
        _run_refresh_cycle(store, signing_key_hex, generation)
        time.sleep(_CACHE_TTL_SECS)


def _ensure_bg_started(store: Store, signing_key_hex: str) -> None:
    """Lazily start the background refresh thread (idempotent)."""
    global _bg_started
    if _bg_started:
        return
    with _bg_lock:
        if _bg_started:
            return
        generation = _bg_generation
        t = threading.Thread(
            target=_bg_refresh_loop,
            args=(store, signing_key_hex, generation),
            name="weights-bg-refresh",
            daemon=True,
        )
        t.start()
        _bg_started = True


def start_background_refresh(store: Store, *, signing_key_hex: str) -> None:
    """Public startup hook: begin refreshing the signed vector in the background."""
    _ensure_bg_started(store, signing_key_hex)


def _refresh_once(store: Store, *, signing_key_hex: str) -> dict[str, Any] | None:
    """Refresh once without allowing every process to rebuild at once."""
    with store.advisory_lock(_refresh_lock_name()) as acquired:
        if acquired:
            vec = build_signed_vector(store, signing_key_hex=signing_key_hex)
            return _persist_vector(store, vec)
    return _load_persisted_vector(store)


def cached_vector(store: Store, *, signing_key_hex: str) -> dict[str, Any] | None:
    """Return the latest signed vector if warm; never build on the request path."""
    _ensure_bg_started(store, signing_key_hex)
    with _build_lock:
        hit = _vector_cache.get("v")
    if hit is None:
        try:
            vec = _load_persisted_vector(store)
        except Exception as exc:
            print(f"[weights] persisted_vector_load_failed error={exc!r}")
            return None
        if vec is not None:
            _cache_write(vec)
        return vec
    return hit[1]


# Public read endpoints should prefer cached_vector(). This synchronous helper
# is for explicit callers that intentionally accept a cold-build DB cost.
def current_vector(
    store: Store,
    *,
    signing_key_hex: str,
    force_rebuild: bool = False,
) -> dict[str, Any]:
    """Serve the latest signed vector from the in-memory cache.

    The background refresh thread (started on first call) rebuilds the vector
    every _CACHE_TTL_SECS without ever blocking the request path.  Only the
    very first call (empty cache) waits for a build — after that every request
    returns in microseconds.

    The synchronous cold-build path uses the same cluster advisory lock as the
    background refresher. If another process owns that lock and no durable
    vector exists yet, this call fails as warming rather than starting an
    unlocked competing build.
    """
    if not force_rebuild:
        hit = cached_vector(store, signing_key_hex=signing_key_hex)
        if hit is not None:
            return hit

    # Explicit sync callers accept a DB cost, but still use the cluster-wide
    # advisory lock so only one process rebuilds the vector.
    vec = _refresh_once(store, signing_key_hex=signing_key_hex)
    if vec is None:
        raise VectorNotReady(
            "signed weight vector is warming; the cluster build lock is held"
        )
    _cache_write(vec)
    return vec


def _reset_vector_cache() -> None:
    """Test hook."""
    global _bg_started, _bg_generation
    _vector_cache.clear()
    _bg_started = False
    _bg_generation += 1
