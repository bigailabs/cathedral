"""Refresh artifact-tier mechanism scores into the router store, then (optionally)
compose + publish the preview weight vector.

Background. The signed-score intake (`POST /mechanisms/{id}/scores`) feeds
``tier="signed"`` mechanisms. ``tier="artifact"`` mechanisms (SAT, CyberGym) are
*computed* from local verified-work tables by their adapters
(``mechanism_sat_adapter``, ``mechanism_cybergym_adapter``) — but nothing ever called
those adapters *into* the store, so ``mechanism_router.compose`` saw no artifact-tier
scores. This is that missing step, the "scored -> weights" wiring the router contract
leaves to a caller.

``refresh_artifact_scores(store)`` — for each **enabled**, ``tier=="artifact"`` spec,
resolve its adapter, compute ``(ScoreVector, ScoreVectorMeta)``, and ``put_scores``.
Adapters are resolved **lazily** by ``mechanism_id``, so a mechanism whose adapter
module isn't present yet (e.g. ``cybergym_v0`` before
``scaffold/publisher/mechanism_cybergym_adapter`` merges) is skipped and logged — the
router's documented "contributes nothing this cycle" shape, never an exception.

``compose_and_publish(store, ...)`` — refresh, compose the eligible vector
(``mechanism_eligibility.compose_eligible``), then hand it to
``mechanism_weightset.set_weights``, which builds + signs + publishes the NEXT preview
artifact (served by ``GET /mechanisms/weights/next``), stays permanently DRY-RUN, and
**hard-refuses mainnet / finney / SN39**. So this can never touch real SN39 weights —
the immutable cathedral-validator release remains the sole path that submits weights.

Guardrails (mirroring the SAT adapter): read-only except ``put_scores`` of the
mechanism's own row; **default-off** (nothing calls this until an operator/cron does);
deterministic (the only time dependency is ``ScoreVectorMeta.signed_at_ms``, set by the
adapter); no secrets beyond the ``network``/``netuid`` env vars ``weights.py`` uses; an
unresolved or failing adapter is skipped + logged, never zeroed into another mechanism.
"""
from __future__ import annotations

import importlib
import inspect
import logging
import os
import time
from typing import Any, Callable, Mapping

from . import mechanism_eligibility, mechanism_weightset
from .mechanism_router import MechanismStore, ScoreVector, ScoreVectorMeta

logger = logging.getLogger(__name__)

# mechanism_id -> (module_path, function_name), resolved LAZILY so a not-yet-merged
# adapter is tolerated. Both adapters share the shape
# ``adapter(store, *, epoch=None, ...) -> (ScoreVector, ScoreVectorMeta)``.
ARTIFACT_ADAPTERS: dict[str, tuple[str, str]] = {
    "sat_v2": ("scaffold.publisher.mechanism_sat_adapter", "sat_mechanism_scores"),
    "cybergym_v0": ("scaffold.publisher.mechanism_cybergym_adapter", "cybergym_mechanism_scores"),
}

ArtifactAdapter = Callable[..., "tuple[ScoreVector, ScoreVectorMeta]"]


def _resolve(entry: Any) -> ArtifactAdapter | None:
    """Resolve a registry entry to a callable. Accepts a ready callable (tests) or a
    ``(module_path, function_name)`` pair imported lazily; a missing module/attr is
    tolerated (returns None) so an unmerged adapter never breaks the cycle."""
    if entry is None:
        return None
    if callable(entry):
        return entry
    module_path, fn_name = entry
    try:
        return getattr(importlib.import_module(module_path), fn_name)
    except (ImportError, AttributeError) as exc:
        logger.info("artifact refresh: adapter %s.%s unavailable (%s)", module_path, fn_name, exc)
        return None


def _call(fn: ArtifactAdapter, store: MechanismStore, *, epoch: int | None):
    params = inspect.signature(fn).parameters.values()
    accepts_epoch = any(p.name == "epoch" or p.kind == p.VAR_KEYWORD for p in params)
    return fn(store, epoch=epoch) if accepts_epoch else fn(store)


def refresh_artifact_scores(
    store: MechanismStore, *, epoch: int | None = None,
    adapters: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    """Run every enabled artifact-tier mechanism's adapter and persist its scores.

    Returns ``{mechanism_id: n_uids_scored}`` for mechanisms refreshed this cycle. A
    mechanism with no resolvable adapter (or whose adapter raises) is skipped + logged
    and its stored row is left untouched — never overwritten with an empty/zero vector.
    An adapter that legitimately returns ``({}, meta)`` *is* persisted (the router's
    "contributes nothing this cycle" state), so a mechanism that earned nothing this
    cycle correctly stops contributing.
    """
    registry = ARTIFACT_ADAPTERS if adapters is None else adapters
    refreshed: dict[str, int] = {}
    for spec in store.list_specs():
        if not spec.enabled or spec.tier != "artifact":
            continue
        fn = _resolve(registry.get(spec.mechanism_id))
        if fn is None:
            logger.info("artifact refresh: no adapter for enabled mechanism %r; skipping",
                        spec.mechanism_id)
            continue
        try:
            scores, meta = _call(fn, store, epoch=epoch)
        except Exception as exc:  # noqa: BLE001 — one bad adapter must not sink the cycle
            logger.warning("artifact refresh: adapter for %r raised (%s); skipping",
                           spec.mechanism_id, exc)
            continue
        store.put_scores(spec.mechanism_id, scores, meta)
        refreshed[spec.mechanism_id] = len(scores)
        logger.info("artifact refresh: %r -> %d uids (source=%s)",
                    spec.mechanism_id, len(scores), meta.source)
    return refreshed


def _now_ms() -> int:
    return int(time.time() * 1000)


def compose_and_publish(
    store: MechanismStore, *, netuid: int, network: str, signing_key_hex: str,
    epoch: int | None = None, now_ms: int | None = None,
    adapters: Mapping[str, Any] | None = None, **set_weights_kwargs: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``refresh_artifact_scores`` -> ``compose_eligible`` -> ``set_weights``.

    ``set_weights`` builds + signs + publishes the NEXT preview artifact, stays
    permanently DRY-RUN, and **raises ``UnsafeNetworkError`` on mainnet / finney /
    SN39 before building anything** — so this composes previews for a testnet target
    and never writes real SN39 weights. Returns ``(set_weights_result, compose_debug)``.
    """
    refresh_artifact_scores(store, epoch=epoch, adapters=adapters)
    specs = store.list_specs()
    scores: dict[str, tuple[ScoreVector, ScoreVectorMeta]] = {}
    for spec in specs:
        got = store.get_scores(spec.mechanism_id)
        if got is not None:
            scores[spec.mechanism_id] = got
    composed, debug = mechanism_eligibility.compose_eligible(
        store, specs, scores, now_ms=now_ms or _now_ms())
    result = mechanism_weightset.set_weights(
        composed, netuid=netuid, network=network, signing_key_hex=signing_key_hex,
        **set_weights_kwargs)
    return result, debug


REFRESH_INTERVAL_ENV = "CATHEDRAL_MECH_REFRESH_INTERVAL_SECONDS"


def _interval_seconds() -> float:
    try:
        return float(os.environ.get(REFRESH_INTERVAL_ENV, "") or 0)
    except ValueError:
        return 0.0


def refresh_enabled() -> bool:
    """The periodic in-process refresh runs only when
    ``CATHEDRAL_MECH_REFRESH_INTERVAL_SECONDS`` is a positive number (default-off) —
    the same env-gated pattern as ``arena_eval_loop`` / ``refill.refill_loop``."""
    return _interval_seconds() > 0


async def refresh_loop(
    store: MechanismStore, *, interval_seconds: float | None = None,
    stop_event: Any | None = None, log: Callable[..., None] = lambda *a, **k: None,
) -> None:
    """Periodic asyncio task: run ``refresh_artifact_scores`` every interval seconds.

    Gated by the caller (start only when ``refresh_enabled()``). The refresh runs in a
    worker thread so the event loop is never blocked; one bad cycle is logged, never
    fatal; the task cancels cleanly. **Refresh only** — it never composes or writes
    weights. Mirrors ``arena_eval_loop``.
    """
    import asyncio

    interval = interval_seconds if interval_seconds is not None else _interval_seconds()
    interval = interval or 60.0
    log("mech_refresh_loop_start", interval=interval)
    try:
        while not (stop_event and stop_event.is_set()):
            try:
                refreshed = await asyncio.to_thread(refresh_artifact_scores, store)
                log("mech_refresh_tick", refreshed=refreshed)
            except Exception as exc:  # noqa: BLE001 — one bad tick must not kill the loop
                log("mech_refresh_error", error=str(exc))
            try:
                await asyncio.wait_for(
                    stop_event.wait() if stop_event else asyncio.sleep(interval),
                    timeout=interval)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        log("mech_refresh_loop_cancelled")
        raise


__all__ = [
    "ARTIFACT_ADAPTERS", "refresh_artifact_scores", "compose_and_publish",
    "REFRESH_INTERVAL_ENV", "refresh_enabled", "refresh_loop",
]
