"""Background loop: keep the SAT lane pending pool topped up.

For each ``(tier, kind)`` advertised by the generator's
``/v1/pool/health``, count Cathedral-side ``pending + active`` rows
for that combo. When ``pending`` is below the configured target, lease
one CNF and import it as ``pending``.

Promotion (``pending -> active``) is intentionally OUT OF SCOPE for
this loop. Promotion is owned by:
  - the existing ``cathedral-sn39-watch`` hourly routine, which
    promotes pending->active when a tier slot is empty;
  - in-process direct-submit rotation in ``publisher/submit.py``
    (PR #235), which promotes the next pending after a winner.

Single source of truth for "which row is currently announced" stays
with those promoters; this loop only widens the bench.

Run inside the publisher's lifespan via ``asyncio.create_task``;
cancellation is observed through the shared ``stop`` Event.

Concurrency contract: ONE publisher instance per environment. Two
publishers running this loop in parallel can each count the same
deficit and lease independently — generator-side leasing prevents
duplicate CNFs (each instance gets a different ``generator_run_id``)
but Cathedral's pending depth would overshoot by roughly
``instances * max_imports_per_tick`` per cycle. The Railway deploy
is single-instance, so this stays a documented constraint rather
than a gating leader lock.
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from cathedral.lanes.challenge_source import (
    SqliteChallengeSource,
)
from cathedral.publisher.sat_generator_client import (
    SatGeneratorClient,
    SatGeneratorError,
)
from cathedral.publisher.sat_generator_import import (
    SAT_FAMILY_ID,
    import_challenge_from_generator,
)

logger = structlog.get_logger(__name__)


_DEFAULT_INTERVAL_SECONDS = 300
_DEFAULT_TARGET_PENDING = 3
_DEFAULT_MAX_IMPORTS_PER_TICK = 2
_DEFAULT_STORAGE_ROOT = "/data/sat-challenges"
_PENDING = "pending"
_ACTIVE = "active"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutopilotConfig:
    """Resolved autopilot configuration.

    All knobs come from env. Defaults assume one publisher instance and
    a generator producing for a small number of (tier, kind) combos.
    """

    interval_seconds: int = _DEFAULT_INTERVAL_SECONDS
    default_target_pending: int = _DEFAULT_TARGET_PENDING
    max_imports_per_tick: int = _DEFAULT_MAX_IMPORTS_PER_TICK
    storage_root: Path = Path(_DEFAULT_STORAGE_ROOT)
    # Overrides keyed by "<tier>:<kind>" -> target pending depth.
    target_overrides: dict[str, int] = None  # type: ignore[assignment]

    def target_for(self, tier: int, kind: str) -> int:
        key = f"{int(tier)}:{kind}"
        if self.target_overrides and key in self.target_overrides:
            return int(self.target_overrides[key])
        return self.default_target_pending


def autopilot_enabled() -> bool:
    """True iff CATHEDRAL_SAT_AUTOPILOT_ENABLED is set to a truthy value."""
    raw = os.environ.get("CATHEDRAL_SAT_AUTOPILOT_ENABLED", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def config_from_env() -> AutopilotConfig:
    """Build an AutopilotConfig from environment variables.

    Recognised env vars (all CATHEDRAL_SAT_AUTOPILOT_*):
      - ENABLED               (read separately via ``autopilot_enabled()``)
      - INTERVAL_SECONDS      default 300
      - TARGET_PENDING        default 3 (per (tier, kind))
      - MAX_IMPORTS_PER_TICK  default 2
      - STORAGE_ROOT          default /data/sat-challenges
      - TARGETS               optional JSON object,
                              e.g. {"1:sha256_preimage": 5, "3:sha256_preimage": 1}
    """
    interval = int(
        os.environ.get(
            "CATHEDRAL_SAT_AUTOPILOT_INTERVAL_SECONDS", str(_DEFAULT_INTERVAL_SECONDS)
        )
    )
    target = int(
        os.environ.get(
            "CATHEDRAL_SAT_AUTOPILOT_TARGET_PENDING", str(_DEFAULT_TARGET_PENDING)
        )
    )
    max_imports = int(
        os.environ.get(
            "CATHEDRAL_SAT_AUTOPILOT_MAX_IMPORTS_PER_TICK",
            str(_DEFAULT_MAX_IMPORTS_PER_TICK),
        )
    )
    storage_root = Path(
        os.environ.get("CATHEDRAL_SAT_AUTOPILOT_STORAGE_ROOT", _DEFAULT_STORAGE_ROOT)
    )
    overrides_raw = os.environ.get("CATHEDRAL_SAT_AUTOPILOT_TARGETS", "").strip()
    overrides: dict[str, int] = {}
    if overrides_raw:
        try:
            parsed = json.loads(overrides_raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"CATHEDRAL_SAT_AUTOPILOT_TARGETS is not valid JSON: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError(
                "CATHEDRAL_SAT_AUTOPILOT_TARGETS must be a JSON object of "
                '"<tier>:<kind>" -> int'
            )
        for k, v in parsed.items():
            key = str(k)
            # Keys must be "<int_tier>:<non-empty_kind>". A typo would
            # silently fall back to the default target — bad enough to
            # justify rejecting at startup.
            if ":" not in key:
                raise ValueError(
                    f"CATHEDRAL_SAT_AUTOPILOT_TARGETS key {key!r} must be "
                    '"<tier>:<kind>"'
                )
            tier_part, kind_part = key.split(":", 1)
            if not tier_part.isdigit() or not kind_part:
                raise ValueError(
                    f"CATHEDRAL_SAT_AUTOPILOT_TARGETS key {key!r} must be "
                    '"<tier>:<kind>" with tier as non-negative integer and '
                    "non-empty kind"
                )
            value = int(v)
            if value < 0:
                raise ValueError(
                    f"CATHEDRAL_SAT_AUTOPILOT_TARGETS[{key!r}] must be >= 0 "
                    f"(got {value})"
                )
            overrides[key] = value
    # Bounds-check the loop knobs so a bad env var can't burn the
    # generator (interval=0) or stall it forever (max_imports<=0).
    if interval < 10:
        raise ValueError(
            f"CATHEDRAL_SAT_AUTOPILOT_INTERVAL_SECONDS must be >= 10 (got {interval})"
        )
    if max_imports < 1:
        raise ValueError(
            "CATHEDRAL_SAT_AUTOPILOT_MAX_IMPORTS_PER_TICK must be >= 1"
        )
    if target < 0:
        raise ValueError("CATHEDRAL_SAT_AUTOPILOT_TARGET_PENDING must be >= 0")
    return AutopilotConfig(
        interval_seconds=interval,
        default_target_pending=target,
        max_imports_per_tick=max_imports,
        storage_root=storage_root,
        target_overrides=overrides,
    )


# ---------------------------------------------------------------------------
# Counting + planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ComboNeed:
    tier: int
    kind: str
    pending: int
    active: int
    target: int

    @property
    def deficit(self) -> int:
        return max(0, self.target - self.pending)


async def _count_cathedral_pending_per_combo(
    source: SqliteChallengeSource, family: str
) -> dict[tuple[int, str], tuple[int, int]]:
    """Return ``{(tier, kind): (pending_count, active_count)}`` for one family.

    ``kind`` is read from ``audit_metadata['kind']``. Rows missing a
    ``kind`` are bucketed under an empty string so they are visible
    in logs but never match a generator-advertised combo.
    """
    rows = await source.list_for_family(family)
    counts: dict[tuple[int, str], list[int]] = {}
    for row in rows:
        if row.status not in {_PENDING, _ACTIVE}:
            continue
        audit = row.audit_metadata
        # audit_metadata is typed dict[str, Any] but the underlying
        # SQLite column holds arbitrary JSON; a row written by an
        # older code path could legitimately surface as a list or
        # string here. Skip those rather than crash the whole tick.
        if not isinstance(audit, dict):
            logger.warning(
                "sat_autopilot_skipping_row_with_non_dict_audit",
                challenge_id=row.challenge_id,
                audit_type=type(audit).__name__,
            )
            continue
        raw_kind = audit.get("kind")
        if not isinstance(raw_kind, str):
            # A non-string kind can't match any generator-advertised
            # combo. Bucket as "" so it stays visible in logs but
            # never satisfies a deficit.
            kind = ""
        else:
            kind = raw_kind
        key = (int(row.tier), kind)
        bucket = counts.setdefault(key, [0, 0])
        if row.status == _PENDING:
            bucket[0] += 1
        else:
            bucket[1] += 1
    return {k: (v[0], v[1]) for k, v in counts.items()}


def _plan_imports(
    *,
    pool_families: list[dict[str, Any]],
    cathedral_counts: dict[tuple[int, str], tuple[int, int]],
    config: AutopilotConfig,
) -> list[_ComboNeed]:
    """Pick the (tier, kind) combos with the largest deficits, up to
    ``max_imports_per_tick`` total leases planned.

    Only consider combos the generator actually advertises and that
    currently have at least one ready CNF in the generator pool. A
    combo with ``ready_depth == 0`` is skipped this tick — leasing
    would just hand us a 404/409 and waste a request.
    """
    needs: list[_ComboNeed] = []
    for entry in pool_families:
        if (entry.get("family") or "") != SAT_FAMILY_ID:
            continue
        tier_raw = entry.get("tier")
        kind = entry.get("kind") or ""
        if tier_raw is None or not kind:
            continue
        tier = int(tier_raw)
        ready_depth = int(entry.get("ready_depth") or 0)
        if ready_depth <= 0:
            continue
        pending, active = cathedral_counts.get((tier, kind), (0, 0))
        target = config.target_for(tier, kind)
        need = _ComboNeed(
            tier=tier, kind=kind, pending=pending, active=active, target=target
        )
        if need.deficit > 0:
            # Cap the per-combo plan by what the generator can actually
            # serve this tick. Asking for 5 from a pool of 2 just wastes
            # the next 3 lease calls.
            capped = _ComboNeed(
                tier=tier,
                kind=kind,
                pending=pending,
                active=active,
                target=min(target, pending + ready_depth),
            )
            if capped.deficit > 0:
                needs.append(capped)
    # Largest deficit first. Stable ordering by (tier, kind) on ties.
    needs.sort(key=lambda n: (-n.deficit, n.tier, n.kind))
    # Total budget across all combos: never lease more than
    # max_imports_per_tick in one cycle, so a misconfiguration can't
    # stampede the generator.
    selected: list[_ComboNeed] = []
    budget = config.max_imports_per_tick
    for need in needs:
        if budget <= 0:
            break
        take = min(need.deficit, budget)
        # Re-express as a single-import need OR keep multi-import?
        # Keep multi: caller loops `for _ in range(take)`.
        selected.append(
            _ComboNeed(
                tier=need.tier,
                kind=need.kind,
                pending=need.pending,
                active=need.active,
                target=need.pending + take,
            )
        )
        budget -= take
    return selected


# ---------------------------------------------------------------------------
# Loop body
# ---------------------------------------------------------------------------


async def run_one_tick(
    *,
    client: SatGeneratorClient,
    source: SqliteChallengeSource,
    config: AutopilotConfig,
    stop: asyncio.Event | None = None,
    db_write_lock: AbstractAsyncContextManager | None = None,
) -> dict[str, Any]:
    """Run a single autopilot tick. Returns a small summary dict.

    Errors from a single import are logged and do NOT propagate, so a
    transient generator hiccup never kills the loop. Errors from the
    initial pool_health call also degrade to a no-op tick.

    ``stop`` is honored mid-tick: if set, the tick exits cleanly before
    starting the next lease so shutdown does not wait for the full
    per-combo plan to drain.
    """
    summary: dict[str, Any] = {
        "imports_attempted": 0,
        "imports_succeeded": 0,
        "errors": 0,
        "skipped_at_target": 0,
        "stopped_early": False,
    }
    try:
        pool = await client.pool_health()
    except SatGeneratorError as exc:
        logger.warning("sat_autopilot_pool_health_failed", error=str(exc))
        summary["errors"] += 1
        return summary

    counts = await _count_cathedral_pending_per_combo(source, SAT_FAMILY_ID)
    plan = _plan_imports(
        pool_families=pool.families, cathedral_counts=counts, config=config
    )
    if not plan:
        logger.info(
            "sat_autopilot_tick_no_imports_needed",
            advertised_combos=len(pool.families),
            tracked_combos=len(counts),
        )
        summary["skipped_at_target"] = len(pool.families)
        return summary

    for need in plan:
        if stop is not None and stop.is_set():
            summary["stopped_early"] = True
            return summary
        to_take = need.target - need.pending  # number of leases for this combo
        for _ in range(to_take):
            if stop is not None and stop.is_set():
                summary["stopped_early"] = True
                return summary
            summary["imports_attempted"] += 1
            try:
                # Pass the write gate INTO the import so it serializes only the
                # shared-connection writes (upsert/activate) against the
                # winner-write path and the fill loop — NOT the lease/fetch/
                # confirm network I/O. Holding the process-wide lock across the
                # generator round-trips would block winner writes for the HTTP
                # timeout; gating only the DB write avoids that.
                result = await import_challenge_from_generator(
                    client=client,
                    source=source,
                    storage_root=config.storage_root,
                    tier=need.tier,
                    kind=need.kind,
                    activate=False,
                    db_write_lock=db_write_lock,
                )
            except SatGeneratorError as exc:
                # Generator-side issue (empty pool, 409, etc). Log and
                # move on; the next tick will retry.
                logger.warning(
                    "sat_autopilot_import_failed",
                    tier=need.tier,
                    kind=need.kind,
                    error=str(exc),
                )
                summary["errors"] += 1
                break  # next combo
            except Exception as exc:  # pragma: no cover - safety net
                logger.exception(
                    "sat_autopilot_import_unexpected_error",
                    tier=need.tier,
                    kind=need.kind,
                    error=str(exc),
                )
                summary["errors"] += 1
                break
            else:
                summary["imports_succeeded"] += 1
                logger.info(
                    "sat_autopilot_imported_pending",
                    cathedral_challenge_id=result.cathedral_challenge_id,
                    tier=result.tier,
                    kind=result.kind,
                    pending_before=need.pending,
                    target=need.target,
                )
    return summary


async def run_autopilot_supervisor(
    *,
    client: SatGeneratorClient,
    source: SqliteChallengeSource,
    config: AutopilotConfig,
    stop: asyncio.Event,
    db_write_lock: AbstractAsyncContextManager | None = None,
) -> None:
    """Lifespan-friendly wrapper that owns the client's aclose.

    Use this from the publisher's lifespan: it enters the client's
    async context (so the underlying httpx.AsyncClient is closed on
    shutdown / task cancellation) and runs ``run_autopilot_loop``
    inside it. Tests that drive the loop directly with a mock-transport
    client should call ``run_autopilot_loop`` and manage the client
    themselves.
    """
    async with client:
        await run_autopilot_loop(
            client=client,
            source=source,
            config=config,
            stop=stop,
            db_write_lock=db_write_lock,
        )


async def run_autopilot_loop(
    *,
    client: SatGeneratorClient,
    source: SqliteChallengeSource,
    config: AutopilotConfig,
    stop: asyncio.Event,
    db_write_lock: AbstractAsyncContextManager | None = None,
) -> None:
    """Cooperative loop. Exits cleanly when ``stop`` is set.

    Sleeps with ``wait_for(stop.wait(), timeout=interval)`` so a
    shutdown signal interrupts the sleep instead of waiting up to a
    full interval.
    """
    logger.info(
        "sat_autopilot_loop_started",
        interval_seconds=config.interval_seconds,
        default_target_pending=config.default_target_pending,
        max_imports_per_tick=config.max_imports_per_tick,
        storage_root=str(config.storage_root),
    )
    while not stop.is_set():
        try:
            await run_one_tick(
                client=client,
                source=source,
                config=config,
                stop=stop,
                db_write_lock=db_write_lock,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - safety net
            logger.exception("sat_autopilot_tick_crashed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.interval_seconds)
        except TimeoutError:
            continue
    logger.info("sat_autopilot_loop_stopped")
