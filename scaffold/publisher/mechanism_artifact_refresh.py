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

**Two stores, not one.** This is the distinction the first cut of this module got
wrong. The ``MechanismStore`` holds specs and the latest per-mechanism score row,
in its own database (``CATHEDRAL_MECH_DB_PATH``, default ``./data/mechanisms.sqlite3``).
The *verified work* an adapter computes from — ``cybergym_scores``,
``metagraph_hotkeys`` — lives in the publisher database (``CATHEDRAL_DB_PATH``),
behind a ``store.Store``. Neither class implements the other's interface:
``SqliteMechanismStore`` has no ``query``, ``Store`` has no ``list_specs``. Handing
adapters the MechanismStore therefore raised ``AttributeError`` on every call,
which the per-adapter ``except`` below caught and logged as "skipping" — so every
artifact mechanism silently contributed nothing, forever, and the endpoint still
answered ``{"ok": true, "refreshed": {}}``. ``data_store`` is now what adapters
receive; it defaults to ``default_data_store()`` (the publisher DB), resolved
lazily so a cycle with no enabled artifact spec opens no database at all.

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

# Where the verified-work tables live. Same env var and default as server.py, so an
# operator who set CATHEDRAL_DB_PATH for the publisher does not have to set a second
# one here. Deliberately NOT CATHEDRAL_MECH_DB_PATH — see the module docstring.
DATA_DB_PATH_ENV = "CATHEDRAL_DB_PATH"
_DEFAULT_DATA_DB_PATH = "publisher.db"


def default_data_store() -> Any:
    """The publisher ``Store`` adapters read verified work from.

    Imported lazily: ``store.Store`` opens/creates a database on construction, and
    a caller that passes its own ``data_store`` (the app, the CLI, every test)
    must not pay for a stray default one.
    """
    from .store import Store

    return Store(os.environ.get(DATA_DB_PATH_ENV, "").strip() or _DEFAULT_DATA_DB_PATH)


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
    data_store: Any | None = None,
) -> dict[str, int]:
    """Run every enabled artifact-tier mechanism's adapter and persist its scores.

    ``store`` is the ``MechanismStore`` (specs in, ``put_scores`` out). ``data_store``
    is the publisher ``Store`` holding the verified-work tables adapters read; it
    defaults to ``default_data_store()``. These are two different databases — passing
    the MechanismStore to adapters is the bug this parameter exists to make
    unrepeatable, since it fails as a caught ``AttributeError`` that looks exactly
    like a mechanism legitimately having nothing to contribute.

    Returns ``{mechanism_id: n_uids_scored}`` for mechanisms refreshed this cycle. A
    mechanism with no resolvable adapter (or whose adapter raises) is skipped + logged
    and its stored row is left untouched — never overwritten with an empty/zero vector.
    An adapter that legitimately returns ``({}, meta)`` *is* persisted (the router's
    "contributes nothing this cycle" state), so a mechanism that earned nothing this
    cycle correctly stops contributing.
    """
    registry = ARTIFACT_ADAPTERS if adapters is None else adapters
    refreshed: dict[str, int] = {}
    data = data_store
    for spec in store.list_specs():
        if not spec.enabled or spec.tier != "artifact":
            continue
        fn = _resolve(registry.get(spec.mechanism_id))
        if fn is None:
            logger.info("artifact refresh: no adapter for enabled mechanism %r; skipping",
                        spec.mechanism_id)
            continue
        if data is None:
            # Lazy: a cycle with nothing enabled must not open the publisher DB.
            data = default_data_store()
        try:
            scores, meta = _call(fn, data, epoch=epoch)
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
    adapters: Mapping[str, Any] | None = None, data_store: Any | None = None,
    **set_weights_kwargs: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``refresh_artifact_scores`` -> ``compose_eligible`` -> ``set_weights``.

    ``set_weights`` builds + signs + publishes the NEXT preview artifact, stays
    permanently DRY-RUN, and **raises ``UnsafeNetworkError`` on mainnet / finney /
    SN39 before building anything** — so this composes previews for a testnet target
    and never writes real SN39 weights. Returns ``(set_weights_result, compose_debug)``.

    ``compose_eligible`` reads ``metagraph_hotkeys`` to build ``registered_uids``, so
    it needs the same publisher ``Store`` the adapters do — not the MechanismStore.
    Resolved once here and shared, so refresh and compose cannot disagree about which
    database they are reading.
    """
    data = data_store if data_store is not None else default_data_store()
    refresh_artifact_scores(store, epoch=epoch, adapters=adapters, data_store=data)
    specs = store.list_specs()
    scores: dict[str, tuple[ScoreVector, ScoreVectorMeta]] = {}
    for spec in specs:
        got = store.get_scores(spec.mechanism_id)
        if got is not None:
            scores[spec.mechanism_id] = got
    # preserve_forfeited=True: an uncredited lane's share must burn, never be
    # renormalized onto the mechanisms that did contribute. Without it this path
    # reaches mechanism_router.compose's legacy renormalizing branch, which
    # silently pays a forfeited CyberGym share to other miners; the spec's
    # requires_forfeit_preservation flag exists to make that a typed refusal.
    composed, debug = mechanism_eligibility.compose_eligible(
        data, specs, scores, now_ms=now_ms or _now_ms(), preserve_forfeited=True,
        max_score_age_ms=max_score_age_ms())
    result = mechanism_weightset.set_weights(
        composed, netuid=netuid, network=network, signing_key_hex=signing_key_hex,
        **set_weights_kwargs)
    return result, debug


REFRESH_INTERVAL_ENV = "CATHEDRAL_MECH_REFRESH_INTERVAL_SECONDS"

# Staleness ceiling applied at COMPOSE time, which is a different question from the
# adapter's own freshness check. An adapter that refuses (an open epoch, an
# unreadable marker, a tampered report) is skipped so the last published vector
# survives the cycle, which is deliberate: it stops one bad cycle from wiping a good
# epoch. The cost is that a vector whose adapter keeps refusing would otherwise be
# composed forever, paying the same miners from an epoch that is arbitrarily old.
# `compose` already implements the ceiling (a mechanism past it forfeits its share to
# burn); nothing passed one, so it was never applied.
MAX_SCORE_AGE_SECS_ENV = "CATHEDRAL_MECH_MAX_SCORE_AGE_SECS"
DEFAULT_MAX_SCORE_AGE_SECS = 3600.0


def max_score_age_ms() -> int | None:
    """The compose-time staleness ceiling in ms, or None to disable it.

    Defaults to one hour rather than to "no ceiling", because the fail-open
    direction here pays stale scores indefinitely. An explicit ``0`` disables it,
    which an operator has to choose deliberately.
    """
    raw = (os.environ.get(MAX_SCORE_AGE_SECS_ENV) or "").strip()
    if not raw:
        return int(DEFAULT_MAX_SCORE_AGE_SECS * 1000)
    try:
        seconds = float(raw)
    except ValueError:
        # A malformed ceiling must not silently become "no ceiling".
        return int(DEFAULT_MAX_SCORE_AGE_SECS * 1000)
    if seconds <= 0:
        return None
    return int(seconds * 1000)


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
    data_store: Any | None = None,
) -> None:
    """Periodic asyncio task: run ``refresh_artifact_scores`` every interval seconds.

    Gated by the caller (start only when ``refresh_enabled()``). The refresh runs in a
    worker thread so the event loop is never blocked; one bad cycle is logged, never
    fatal; the task cancels cleanly. **Refresh only** — it never composes or writes
    weights. Mirrors ``arena_eval_loop``.

    The publisher ``Store`` is resolved ONCE, before the loop, not per tick: reopening
    the database every interval for the life of the process is waste, and failing to
    open it should surface at startup rather than as a quietly empty refresh.
    """
    import asyncio

    interval = interval_seconds if interval_seconds is not None else _interval_seconds()
    interval = interval or 60.0
    data = data_store if data_store is not None else default_data_store()
    log("mech_refresh_loop_start", interval=interval)
    try:
        while not (stop_event and stop_event.is_set()):
            try:
                refreshed = await asyncio.to_thread(
                    lambda: refresh_artifact_scores(store, data_store=data))
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
    "DATA_DB_PATH_ENV", "default_data_store",
]
