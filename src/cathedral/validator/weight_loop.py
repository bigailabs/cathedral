"""Weight-set loop - joins scores to uids and pushes to chain on a timer."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable

import aiosqlite
import structlog

from cathedral.chain import Chain, apply_burn, normalize
from cathedral.chain.client import WeightStatus
from cathedral.validator.chain_bridge import publish_weight_vector
from cathedral.validator.health import Health
from cathedral.validator.pull_loop import (
    latest_pulled_score_per_hotkey,
    par2_merit_per_operator,
)
from cathedral.validator.shadow_weights import (
    compute_shadow_weight_diff,
    record_shadow_weight_diff,
)

logger = structlog.get_logger(__name__)


def _resolve_v3_bug_isolation_weight(configured: float | None) -> float | None:
    import os

    raw = os.environ.get("CATHEDRAL_V3_BUG_ISOLATION_WEIGHT")
    if raw is None:
        return configured
    try:
        return float(raw)
    except ValueError:
        logger.warning("v3_bug_isolation_weight_invalid", value=raw)
        return configured


def _resolve_task_family_weights(configured: dict[str, float] | None) -> dict[str, float]:
    import json
    import os

    out = dict(configured or {})
    raw_json = os.environ.get("CATHEDRAL_TASK_FAMILY_WEIGHTS_JSON")
    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError:
            logger.warning("task_family_weights_json_invalid")
        else:
            if isinstance(parsed, dict):
                out.update(parsed)
            else:
                logger.warning("task_family_weights_json_invalid", reason="not_object")

    raw_boolean = os.environ.get("CATHEDRAL_SYNTHETIC_BOOLEAN_V1_WEIGHT")
    if raw_boolean is not None:
        try:
            out["synthetic_boolean_v1"] = float(raw_boolean)
        except ValueError:
            logger.warning("synthetic_boolean_v1_weight_invalid", value=raw_boolean)

    return out


def _par2_weights_enabled() -> bool:
    """True iff the PAR-2 vector should DRIVE set_weights (the flip).

    Default off: PAR-2 is computed in shadow and diffed against the legacy
    vector, but the legacy vector still drives weights until this flip — which
    is gated on shadow convergence + an operator deploy.
    """
    raw = os.environ.get("CATHEDRAL_PAR2_WEIGHTS_ENABLED", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _par2_alpha() -> float:
    raw = os.environ.get("CATHEDRAL_PAR2_ALPHA", "").strip()
    try:
        return max(0.0, float(raw)) if raw else 0.5
    except ValueError:
        return 0.5


async def run_weight_loop(
    conn: aiosqlite.Connection,
    chain: Chain,
    health: Health,
    interval_secs: int = 1200,
    disabled: bool = False,
    burn_uid: int = 204,
    forced_burn_percentage: float = 0.0,
    v3_bug_isolation_weight: float | None = None,
    task_family_weights: dict[str, float] | None = None,
    stop: asyncio.Event | None = None,
    initial_backfill_complete: asyncio.Event | None = None,
    initial_backfill_timeout_secs: float = 120.0,
    remote_weight_apply: Callable[[], Awaitable[object]] | None = None,
) -> None:
    stop = stop or asyncio.Event()
    # Track whether the initial backfill ever signalled completion.
    # `backfill_ready` becomes True the first time
    # `initial_backfill_complete` is observed set. If the timeout
    # fallback path fires it stays False - every `weights_set` log
    # line carries this flag so operators reading the log can tell
    # whether the published vector was computed from a known-good
    # 7-day window or a possibly-thin local DB.
    backfill_ready = False
    if initial_backfill_complete is None or initial_backfill_complete.is_set():
        backfill_ready = True
    else:
        # Wait for the pull_loop's first drained catch-up pass before
        # the first weight set. Without this, a freshly-upgraded
        # validator can publish a vector computed from a half-hydrated
        # 7-day window. The event is set permanently after the first
        # complete pass, so subsequent iterations are unblocked.
        # Timeout caps the wait so a broken pull loop can't pin the
        # weight loop forever - if the backfill hasn't completed in
        # ``initial_backfill_timeout_secs``, fall through and publish
        # with whatever's in the local DB. Better to ship a thin
        # vector than no vector at all, but log loudly + set
        # `backfill_ready=False` so it's visible.
        logger.info(
            "weight_loop_waiting_for_backfill",
            timeout_secs=initial_backfill_timeout_secs,
        )
        try:
            await asyncio.wait_for(
                initial_backfill_complete.wait(),
                timeout=initial_backfill_timeout_secs,
            )
            backfill_ready = True
            logger.info("weight_loop_backfill_signal_received")
        except TimeoutError:
            logger.warning(
                "weight_loop_backfill_timeout_publishing_thin_vector",
                timeout_secs=initial_backfill_timeout_secs,
                consequence=(
                    "First weights_set will run with whatever rows the "
                    "local DB has - likely a strict subset of the "
                    "publisher's 7-day feed. Subsequent ticks recheck "
                    "the event and may upgrade to backfill_ready=True."
                ),
            )
    while not stop.is_set():
        # The event can land between ticks (e.g. timeout fell through
        # before backfill completed, then backfill completed during
        # the first set_weights). Re-check on every iteration so the
        # log flag stays accurate without restarting the loop.
        if (
            not backfill_ready
            and initial_backfill_complete is not None
            and initial_backfill_complete.is_set()
        ):
            backfill_ready = True
            logger.info("weight_loop_backfill_signal_received_late")
        try:
            if remote_weight_apply is not None:
                # Remote mode must not fall back to locally computed weights while
                # waiting for a signed publisher vector; the callback owns all
                # apply/no-op decisions for this cadence tick.
                await remote_weight_apply()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval_secs)
                except TimeoutError:
                    pass
                continue

            metagraph = await chain.metagraph()
            registered = await chain.is_registered()
            await health.update(
                current_block=metagraph.block,
                registered=registered,
            )
            await health.heartbeat("last_metagraph_at")

            # Pulled SAT scores from the publisher are the sole score source.
            # 7-day window matches the cadence of SAT challenge refresh + miner
            # iteration. A 30-day mean penalises a miner who fixed a bug
            # yesterday by averaging in last week's zero scores; in practice
            # that anti-recency bias is what's been pinning legitimate masons
            # near zero weight. 7d lets a debugged miner climb within a day of
            # shipping a real solve, while still smoothing out single-eval
            # noise. Full time-decayed mean is the next iteration.
            try:
                scores = await latest_pulled_score_per_hotkey(
                    conn,
                    since_days=7,
                    v3_bug_isolation_weight=_resolve_v3_bug_isolation_weight(
                        v3_bug_isolation_weight
                    ),
                    task_family_weights=_resolve_task_family_weights(task_family_weights),
                )
            except Exception as ex:
                logger.debug("pulled_scores_unavailable", error=str(ex))
                scores = {}
            uid_by_hotkey = metagraph.hotkey_to_uid()
            # Observability: surface which hotkeys we know vs. which the
            # metagraph drops. Without this you only see `count=N` in the
            # weights_set line and have no idea why N is smaller than the
            # number of masons producing scored cards. Drops are usually
            # test hotkeys that never registered on chain.
            unmapped = [hk for hk in scores if hk not in uid_by_hotkey]
            raw = [(uid_by_hotkey[hk], s) for hk, s in scores.items() if hk in uid_by_hotkey]
            positive = [(uid, s) for uid, s in raw if s > 0]
            logger.info(
                "weights_pre_burn",
                total_hotkeys=len(scores),
                mapped_hotkeys=len(raw),
                positive_hotkeys=len(positive),
                unmapped_count=len(unmapped),
                # 8-char prefixes keep logs scannable without leaking too much
                unmapped_sample=[hk[:8] for hk in unmapped[:5]],
                positive_sample=[(uid, round(s, 3)) for uid, s in positive[:5]],
            )
            burned = apply_burn(
                raw,
                burn_uid=burn_uid,
                forced_burn_percentage=forced_burn_percentage,
            )
            normalized = normalize(burned)

            # ----- Shadow PAR-2 vector + divergence (WS-SCORE.D/F) ----------
            # Compute the PAR-2 weight vector alongside the legacy one and
            # record the divergence into shadow_weight_diffs. Best-effort and
            # flag-gated: by default the legacy vector still drives set_weights
            # (shadow only). When CATHEDRAL_PAR2_WEIGHTS_ENABLED is set the
            # PAR-2 vector becomes authoritative — the flip, which is gated on
            # shadow convergence + an operator deploy, never a PR merge.
            final_vector = normalized
            try:
                par2_merit = await par2_merit_per_operator(
                    conn, since_days=7, alpha=_par2_alpha()
                )
                par2_raw = [
                    (uid_by_hotkey[hk], m)
                    for hk, m in par2_merit.items()
                    if hk in uid_by_hotkey
                ]
                par2_normalized = normalize(
                    apply_burn(
                        par2_raw,
                        burn_uid=burn_uid,
                        forced_burn_percentage=forced_burn_percentage,
                    )
                )
                diff = compute_shadow_weight_diff(normalized, par2_normalized)
                await record_shadow_weight_diff(
                    conn,
                    validator_hotkey=str(
                        getattr(chain, "hotkey_ss58", "")
                        or getattr(chain, "validator_hotkey", "")
                        or ""
                    ),
                    network=str(getattr(chain, "network", "") or ""),
                    netuid=int(getattr(chain, "netuid", 0) or 0),
                    diff=diff,
                )
                logger.info(
                    "par2_shadow_diff",
                    abs_diff_sum=round(diff.abs_diff_sum, 4),
                    blocker=diff.blocker,
                    par2_count=len(par2_normalized),
                    legacy_count=len(normalized),
                )
                if _par2_weights_enabled():
                    final_vector = par2_normalized
                    logger.info("par2_weights_driving", count=len(par2_normalized))
            except Exception as ex:
                logger.warning("par2_shadow_failed", error=str(ex))

            status = await publish_weight_vector(chain, final_vector, disabled=disabled)
            await health.update(weight_status=status)
            await health.heartbeat("last_weight_set_at")
            logger.info(
                "weights_set",
                count=len(final_vector),
                status=status.value,
                # Surface which uids actually shipped so operators can sanity-
                # check against the on-chain weight set without diffing logs.
                uids=[uid for uid, _ in final_vector][:20],
                # True only when this validator's pull_loop has signalled
                # that the 7-day backfill drained. A `False` here means
                # the weight vector was computed from a possibly-thin
                # local DB (timeout fallback path or no pull loop wired).
                backfill_ready=backfill_ready,
            )
            # Canonical event name for a Path A chain.set_weights completion.
            # The `status` field is authoritative - HEALTHY means the extrinsic
            # landed, DISABLED means dry-run completed, BLOCKED_BY_* means the
            # chain rejected or never accepted it. Pairs with
            # `chain_weights_set_remote` in remote_weight_loop.py for Path B.
            # Dual-emitted with the legacy `weights_set` event for one release.
            logger.info(
                "chain_weights_set_local",
                count=len(final_vector),
                status=status.value,
                uids=[uid for uid, _ in final_vector][:20],
                backfill_ready=backfill_ready,
            )
        except Exception as e:
            logger.warning("weight_loop_error", error=str(e))
            await health.update(weight_status=WeightStatus.BLOCKED_BY_TRANSACTION_ERROR)

        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_secs)
        except TimeoutError:
            pass
