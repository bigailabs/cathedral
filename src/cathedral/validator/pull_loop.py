"""Validator pull loop — `GET /v1/leaderboard/recent` from the publisher.

Per CONTRACTS.md Section 6 (validator side):
- Validator binary polls publisher's leaderboard/recent every 30s
- For each new entry: verify cathedral signature, verify against last
  known Merkle root from on-chain
- Persist to validator's local DB
- Existing weight_loop reads from validator's local DB and sets weights

For v1 the validator's "local DB" is the existing sqlite — we add a
`scores` row keyed by `miner_hotkey` so the existing weight_loop keeps
working without modification. The publisher signs each `EvalRun`
record; the validator verifies with the Cathedral public key (loaded
from the publisher's `/.well-known/cathedral-jwks.json`, or directly
from `CATHEDRAL_PUBLIC_KEY_HEX` for now).
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite
import httpx
import structlog
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from cathedral.v1_types import canonical_json
from cathedral.validator.health import Health

logger = structlog.get_logger(__name__)


_PULL_INTERVAL_SECS = 30.0

# Default page size on the wire. The publisher endpoint accepts up to
# 1000; we ship 500 by default to leave headroom and keep per-tick work
# bounded. See 2026-05-12-track-3-pull-cursor-audit.md Risk 1.
_DEFAULT_PULL_LIMIT = 500

# Saturation cap: when a page returns exactly `limit` rows, pull again
# without sleeping (we're behind). Cap inner iterations so a misbehaving
# publisher can't pin the validator at 100% CPU. 4 * 500 = 2000 rows per
# outer tick — ~5.7M rows/day ceiling.
_MAX_INNER_PULLS = 4

# Initial-cursor window. weight_loop computes a 7-day rolling mean per
# hotkey (cathedralai/cathedral#105). A fresh validator that seeds its
# cursor at "now - 1 hour" never hydrates the rest of the 7-day window,
# so its weights_pre_burn vector under-counts masons compared to a
# validator that's been running for days. Seed at "now - 7 days" so a
# first-boot validator catches up to the scoring window before its
# first weight set. Re-pulling rows is safe — `upsert_pulled_eval`'s
# ON CONFLICT clause idempotently updates by eval_run_id.
_INITIAL_BACKFILL_DAYS = 7


class PullVerificationError(Exception):
    """An eval-run record from the publisher failed cathedral signature check."""


def verify_eval_run_signature(eval_run: dict[str, Any], public_key: Ed25519PublicKey) -> None:
    """Verify cathedral_signature over the canonical EvalOutput projection.

    CRIT-7: prior versions verified over a storage-shaped dict that the
    publisher does not actually sign. The publisher signs the public
    EvalOutput projection (CONTRACTS.md §1.10 + L8); this function
    tolerates being called with either:

    * the public EvalOutput projection (preferred / wire shape), or
    * a legacy storage-shaped dict that has a nested ``output_card_json``,

    and in both cases verifies against the canonical wire bytes the
    publisher signed. Any extra/storage-only keys are dropped before
    verification so the byte-exact projection is reconstructed.

    Raises ``PullVerificationError`` on failure.
    """
    projection = _to_eval_output_projection(eval_run)
    if projection is None:
        raise PullVerificationError("eval-run dict missing fields needed to reconstruct EvalOutput")
    verify_eval_output_signature(projection, public_key)


async def upsert_pulled_eval(
    conn: aiosqlite.Connection,
    *,
    eval_run: dict[str, Any],
    miner_hotkey: str,
) -> None:
    """Persist a pulled eval result to the validator's local DB.

    We reuse the existing `scores` table — it's keyed by `claim_id` for
    the legacy path, but we extend to the v1 path by inserting one row
    per pulled eval, using a synthetic negative claim_id derived from
    the eval-run UUID hash so it cannot collide with the legacy
    AUTOINCREMENT positive ids.

    The weight_loop reads `latest_score_per_hotkey()` which picks the
    most recent score per hotkey across ALL claim_ids — so legacy and
    pulled rows blend cleanly.
    """
    eval_run_id = str(eval_run["id"])
    weighted = float(eval_run["weighted_score"])
    ran_at = eval_run["ran_at"]
    task_type = str(eval_run.get("task_type") or eval_run.get("card_id") or "unknown")
    schema_version_raw = eval_run.get("eval_output_schema_version", 1)
    try:
        schema_version = int(schema_version_raw)
    except (TypeError, ValueError):
        schema_version = 1
    if isinstance(ran_at, datetime):
        ran_at = ran_at.isoformat()

    # Synthetic claim_id: hash of eval_run_id mod 2^31 negated, so always
    # negative + collision-resistant within a single validator instance.
    import zlib

    synth_id = -(zlib.crc32(eval_run_id.encode("utf-8")) & 0x7FFFFFFF) - 1

    # Insert into a new pull-side table to avoid FK violations against `claims`.
    await _ensure_pulled_eval_runs_table(conn)
    await conn.execute(
        """
        INSERT INTO pulled_eval_runs (
            eval_run_id, miner_hotkey, weighted_score,
            ran_at, pulled_at, synth_claim_id, task_type, schema_version
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(eval_run_id) DO UPDATE SET
            weighted_score=excluded.weighted_score,
            ran_at=excluded.ran_at,
            pulled_at=excluded.pulled_at,
            task_type=excluded.task_type,
            schema_version=excluded.schema_version
        """,
        (
            eval_run_id,
            miner_hotkey,
            weighted,
            ran_at,
            datetime.now(UTC).isoformat(),
            synth_id,
            task_type,
            schema_version,
        ),
    )
    await conn.commit()


async def latest_pulled_score_per_hotkey(
    conn: aiosqlite.Connection,
    *,
    since_days: int = 30,
    v3_bug_isolation_weight: float | None = None,
    task_family_weights: dict[str, float] | None = None,
) -> dict[str, float]:
    """Rolling mean per hotkey from the pull-side table.

    v1 card scores remain primary. bug_isolation_v1 can contribute a
    small configurable share once enabled. Generic Task Family rows
    follow the same rule: they contribute only their configured slice
    and default to zero weight.
    """
    await _ensure_pulled_eval_runs_table(conn)
    v3_weight = max(0.0, min(1.0, float(v3_bug_isolation_weight or 0.0)))
    lane_weights = _normalized_task_family_weights(task_family_weights)

    since = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()
    cur = await conn.execute(
        """
        SELECT miner_hotkey, task_type, weighted_score, schema_version
        FROM pulled_eval_runs
        WHERE ran_at >= ?
        """,
        (since,),
    )
    rows = await cur.fetchall()
    samples_by_hotkey: dict[str, dict[str, list[float]]] = {}
    for row in rows:
        hotkey = str(row[0])
        task_type = str(row[1] or "unknown")
        try:
            schema_version = int(row[3])
        except (TypeError, ValueError):
            schema_version = 1
        if schema_version in (5, 6):
            # v6 (PAR-2 facts) routes to the same SAT lane bucket as v5.
            # The mean-of-weighted_score blend below is the legacy path; the
            # PAR-2 aggregation (solve_rank/challenge_value/operator) replaces
            # it once open-window scoring is live (no v6 rows exist until then).
            if task_type not in lane_weights:
                continue
            score_bucket = task_type
        elif task_type == "bug_isolation_v1":
            score_bucket = "v3"
        else:
            score_bucket = "v1"
        score = float(row[2])
        samples_by_hotkey.setdefault(hotkey, {}).setdefault(score_bucket, []).append(score)

    by_hotkey: dict[str, dict[str, float]] = {}
    for hotkey, buckets in samples_by_hotkey.items():
        by_hotkey[hotkey] = {
            bucket: sum(values) / len(values) for bucket, values in buckets.items() if values
        }

    out: dict[str, float] = {}
    for hotkey, scores in by_hotkey.items():
        v1_score = scores.get("v1")
        weighted_total = 0.0
        active_special_weight = 0.0
        v3_score = scores.get("v3")
        if v3_score is not None and v3_weight > 0.0:
            weighted_total += v3_score * v3_weight
            active_special_weight += v3_weight
        for task_type, lane_weight in lane_weights.items():
            lane_score = scores.get(task_type)
            if lane_score is not None and lane_weight > 0.0:
                weighted_total += lane_score * lane_weight
                active_special_weight += lane_weight

        active_special_weight = min(active_special_weight, 1.0)
        weighted_total = min(weighted_total, 1.0)
        if v1_score is not None:
            if active_special_weight == 0.0:
                out[hotkey] = v1_score
            else:
                out[hotkey] = (v1_score * (1.0 - active_special_weight)) + weighted_total
        elif weighted_total > 0.0:
            out[hotkey] = weighted_total
    return out


def _normalized_task_family_weights(
    configured: dict[str, float] | None,
) -> dict[str, float]:
    weights = {"synthetic_boolean_v1": 0.0}
    for key, value in (configured or {}).items():
        try:
            weights[str(key)] = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            continue
    return weights


async def _ensure_pulled_eval_runs_table(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pulled_eval_runs (
            eval_run_id TEXT PRIMARY KEY,
            miner_hotkey TEXT NOT NULL,
            weighted_score REAL NOT NULL,
            ran_at TEXT NOT NULL,
            pulled_at TEXT NOT NULL,
            synth_claim_id INTEGER NOT NULL,
            task_type TEXT NOT NULL DEFAULT 'unknown',
            schema_version INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    cur = await conn.execute("PRAGMA table_info(pulled_eval_runs)")
    cols = {str(row[1]) for row in await cur.fetchall()}
    if "task_type" not in cols:
        try:
            await conn.execute(
                "ALTER TABLE pulled_eval_runs ADD COLUMN task_type TEXT NOT NULL DEFAULT 'unknown'"
            )
        except aiosqlite.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    if "schema_version" not in cols:
        try:
            await conn.execute(
                "ALTER TABLE pulled_eval_runs ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1"
            )
        except aiosqlite.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    await conn.commit()


# Durable marker recording that a 7-day backfill has completed under the
# current pull_loop code. Bumped whenever the backfill-window semantics
# change so existing validators redo the backfill after auto-update.
_BACKFILL_MARKER_KEY = "initial_backfill_completed_at"
_BACKFILL_CODE_VERSION = "v1"


async def _ensure_meta_table(conn: aiosqlite.Connection) -> None:
    """Create the durable pull-loop metadata table if it doesn't exist.

    Single-row-per-key store. We use it for the backfill-completion
    marker; any future durable pull_loop state goes here too.
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pull_loop_meta (
            key           TEXT PRIMARY KEY,
            value         TEXT NOT NULL,
            code_version  TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        )
        """
    )
    await conn.commit()


async def _backfill_completed_under_current_code(conn: aiosqlite.Connection) -> bool:
    """True iff a 7-day backfill has finished under this code version.

    Without this marker, an upgrade-and-restart on an existing validator
    DB would see "recent rows exist, resume from max(ran_at)" and never
    backfill the older end of the new 7-day window. That's the exact
    failure that left Rizzo/Kraken/RT21 weighting fewer masons than
    TAO.com even after PR #105 shipped.
    """
    await _ensure_meta_table(conn)
    cur = await conn.execute(
        "SELECT code_version FROM pull_loop_meta WHERE key = ?",
        (_BACKFILL_MARKER_KEY,),
    )
    row = await cur.fetchone()
    if row is None:
        return False
    return str(row[0]) == _BACKFILL_CODE_VERSION


async def _mark_backfill_complete(conn: aiosqlite.Connection) -> None:
    """Persist the "we have walked the full 7-day window" marker.

    Called by ``run_pull_loop`` after the first drained catch-up pass.
    Subsequent restarts will see the marker and skip the forced
    backfill — they resume from local max(ran_at) like the steady-state
    case. If we ever change the backfill-window semantics, bump
    ``_BACKFILL_CODE_VERSION`` to invalidate every existing marker and
    force a re-backfill across the fleet.
    """
    await _ensure_meta_table(conn)
    now = datetime.now(UTC).isoformat()
    await conn.execute(
        """
        INSERT INTO pull_loop_meta (key, value, code_version, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            code_version=excluded.code_version,
            updated_at=excluded.updated_at
        """,
        (_BACKFILL_MARKER_KEY, now, _BACKFILL_CODE_VERSION, now),
    )
    await conn.commit()


async def _initial_cursor(
    conn: aiosqlite.Connection, *, backfill_days: int = _INITIAL_BACKFILL_DAYS
) -> tuple[str, str]:
    """Pick the (since_ran_at, since_id) tuple cursor for the first pull.

    Two-mode logic guarded by the durable backfill marker:

    A. **Backfill not yet completed under this code version** (no marker,
       or marker written by older code). Seed at ``now - backfill_days``
       with an empty id, regardless of what rows are already in
       ``pulled_eval_runs``. This is the load-bearing case for the
       upgrade path: validators that auto-updated with recent rows
       already in their DB MUST re-walk the older end of the window so
       their ``weights_pre_burn`` vector converges with everyone else's.
       ``upsert_pulled_eval``'s ON CONFLICT clause absorbs re-served
       rows safely.

    B. **Backfill marker is current.** Steady-state restart. Resume from
       the highest persisted ``(ran_at, eval_run_id)``. The tuple
       cursor's strict ``>`` ensures we never re-pull the boundary row
       (cheap optimization on top of the upsert's idempotence).

       Sub-case B': if the highest persisted ran_at is older than the
       backfill window, the validator was down longer than the scoring
       window cares about. Reseed at the backfill floor — pulling rows
       older than 7d wouldn't change
       ``latest_pulled_score_per_hotkey``'s answer.

    The table is created lazily by ``upsert_pulled_eval`` on the first
    successful pull, so handle the absence-of-table case (sqlite raises
    OperationalError on SELECT FROM nonexistent table) by falling back
    to the backfill floor.
    """
    backfill_floor_dt = datetime.now(UTC) - timedelta(days=backfill_days)
    backfill_floor = backfill_floor_dt.isoformat()

    # Mode A: force backfill if marker is absent or stale.
    if not await _backfill_completed_under_current_code(conn):
        return backfill_floor, ""

    # Mode B: marker present and current — resume from max(ran_at).
    try:
        cur = await conn.execute(
            "SELECT ran_at, eval_run_id FROM pulled_eval_runs "
            "ORDER BY ran_at DESC, eval_run_id DESC LIMIT 1"
        )
        row = await cur.fetchone()
    except aiosqlite.OperationalError:
        row = None

    if row is None:
        return backfill_floor, ""

    last_ran_at = str(row[0])
    last_id = str(row[1])
    # Compare ISO-8601 strings lexicographically — the publisher's
    # ran_at format is fixed-width ISO with `Z` or `+00:00`, so
    # lexicographic comparison matches chronological comparison for
    # rows within the same timezone (UTC). Mismatched zones would
    # require parsing; we don't ship those.
    if last_ran_at < backfill_floor:
        return backfill_floor, ""
    return last_ran_at, last_id


async def run_pull_loop(
    *,
    conn: aiosqlite.Connection,
    publisher_url: str,
    cathedral_public_key: Ed25519PublicKey,
    health: Health,
    interval_secs: float = _PULL_INTERVAL_SECS,
    api_token: str | None = None,
    stop: asyncio.Event | None = None,
    limit: int = _DEFAULT_PULL_LIMIT,
    initial_backfill_complete: asyncio.Event | None = None,
) -> None:
    """Long-running pull loop. Polls publisher every ``interval_secs``.

    v1.1.0 cursor: tuple ``(ran_at, id)`` with strict `>` comparison —
    matches the publisher's ``ORDER BY ran_at ASC, id ASC`` total order
    and the row-value comparison ``WHERE (ran_at, id) > (?, ?)``. See
    ``2026-05-12-track-3-pull-cursor-audit.md`` Risk 2 for why the prior
    ``since = max(ran_at)`` with ``>=`` could leak rows at millisecond
    collisions under cadence eval load.

    Saturation pull: when a page returns exactly ``limit`` rows, pull
    again immediately without sleeping. Capped at ``_MAX_INNER_PULLS``
    inner iterations per outer tick so a misbehaving publisher can't
    starve the loop.

    Initial-backfill signal: ``initial_backfill_complete`` is set after
    the first outer tick that fully drains the cursor (i.e. returns a
    non-saturated page). ``run_weight_loop`` awaits this event before
    its first ``set_weights`` so a freshly-upgraded validator does not
    publish a weight vector computed from a half-hydrated 7-day window.
    Subsequent ticks leave the event set; only the first set matters.
    """
    stop = stop or asyncio.Event()
    base = publisher_url.rstrip("/")
    # Initial cursor: force backfill from `now - 7d` if the durable
    # backfill marker is absent or stale (existing validators that
    # auto-updated keep recent rows in pulled_eval_runs from the old
    # 1-hour-seed code; without forcing a backfill they would never
    # hydrate the older end of the new 7-day window). After backfill
    # we write the marker so subsequent restarts resume from
    # max(ran_at). See ``_initial_cursor`` for the full rules.
    cursor_ran_at, cursor_id = await _initial_cursor(conn)
    backfill_already_done = await _backfill_completed_under_current_code(conn)
    logger.info(
        "pull_loop_initial_cursor",
        since_ran_at=cursor_ran_at,
        since_id=cursor_id or "<empty>",
        backfill_mode="resume" if backfill_already_done else "backfill",
    )

    headers: dict[str, str] = {}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        while not stop.is_set():
            inner_pulls = 0
            total_fetched = 0
            total_persisted = 0
            # `drained` is True iff this outer tick exited the inner
            # loop by hitting a non-saturated page (len(items) < limit).
            # Transport errors, malformed payloads, and saturation-cap
            # exhaustion all leave it False — none of those prove the
            # backfill window has been walked end-to-end, and we MUST
            # NOT write the durable marker on any of them. Doing so on
            # a first-tick transport error would lock the validator
            # out of ever re-backfilling on restart (PR #109 review
            # C1 — would recreate the exact divergence #109 fixes).
            drained = False
            while inner_pulls < _MAX_INNER_PULLS:
                inner_pulls += 1
                try:
                    resp = await client.get(
                        f"{base}/v1/leaderboard/recent",
                        params={
                            # v1.1.0 tuple cursor.
                            "since_ran_at": cursor_ran_at,
                            "since_id": cursor_id,
                            # Legacy field for back-compat with v1.0.x
                            # publishers that ignore the new params. A
                            # v1.1.0 publisher prefers the tuple; a v1.0.x
                            # one falls back to `since`.
                            "since": cursor_ran_at,
                            "limit": limit,
                        },
                    )
                    resp.raise_for_status()
                    payload = resp.json()
                except httpx.HTTPError as e:
                    logger.warning("pull_transport_error", error=str(e))
                    break

                # Strict shape check: missing-key, null, and non-list
                # values are all malformed payloads — none of them prove
                # the cursor reached the head of the feed, so they MUST
                # NOT trigger the drained-tick path that writes the
                # backfill marker. The pre-fix `payload.get("items") or []`
                # coerced `{}`, `{"items": null}`, and `{"items": []}`
                # into the same empty-list code path, and an empty-list
                # response (legitimately "caught up") would then look
                # identical to the malformed cases. See review C1 on
                # PR #110.
                if "items" not in payload or not isinstance(payload["items"], list):
                    logger.warning(
                        "pull_payload_malformed",
                        has_items_key="items" in payload,
                        items_type=type(payload.get("items")).__name__,
                    )
                    break
                items = payload["items"]

                persisted = 0
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    # CRIT-7: verify the cathedral signature directly
                    # against the public EvalOutput projection — the
                    # publisher signs the projection (see
                    # scoring_pipeline.score_and_sign), so rebuilding a
                    # different storage-shaped dict and verifying that
                    # always fails. `merkle_epoch` is excluded from the
                    # signed bytes via canonical_json. The verifier is
                    # version-aware (see _SIGNED_KEYS_BY_VERSION) so it
                    # can dispatch to a future v2 key set without code
                    # changes here.
                    try:
                        verify_eval_output_signature(item, cathedral_public_key)
                    except PullVerificationError as e:
                        logger.warning(
                            "pull_eval_signature_invalid",
                            id=item.get("id"),
                            error=str(e),
                        )
                        continue

                    hotkey = _hotkey_for(item)
                    if not hotkey:
                        continue
                    await upsert_pulled_eval(conn, eval_run=item, miner_hotkey=hotkey)
                    persisted += 1

                total_fetched += len(items)
                total_persisted += persisted

                # Advance the cursor. Prefer the publisher's signalled
                # next_since_ran_at + next_since_id (v1.1.0) so a fully
                # drained page short-circuits cleanly. Fall back to the
                # last item's (ran_at, id) tuple.
                next_ran_at = payload.get("next_since_ran_at")
                next_id = payload.get("next_since_id")
                if isinstance(next_ran_at, str) and isinstance(next_id, str):
                    cursor_ran_at = next_ran_at
                    cursor_id = next_id
                elif items:
                    last = items[-1]
                    if isinstance(last, dict):
                        last_ran = last.get("ran_at")
                        last_id = last.get("id")
                        if isinstance(last_ran, str):
                            cursor_ran_at = last_ran
                        if isinstance(last_id, str):
                            cursor_id = last_id

                # If the page wasn't saturated we're caught up; break the
                # inner loop and sleep until the next outer tick. This is
                # the ONLY path that proves the cursor reached the head
                # of the publisher's feed — anything else (cap, error,
                # malformed payload) means there are still rows behind
                # the cursor we have not pulled.
                if len(items) < limit:
                    drained = True
                    break

            await health.heartbeat("last_evidence_pass_at")
            logger.info(
                "pull_loop_tick",
                fetched=total_fetched,
                persisted=total_persisted,
                inner_pulls=inner_pulls,
                drained=drained,
            )

            # Only persist the durable backfill marker after a
            # successfully drained outer tick. See `drained` comment
            # above for the failure modes this guards against.
            if drained and not backfill_already_done:
                await _mark_backfill_complete(conn)
                backfill_already_done = True
                logger.info(
                    "pull_loop_backfill_complete",
                    cursor_ran_at=cursor_ran_at,
                    cursor_id=cursor_id or "<empty>",
                    fetched=total_fetched,
                    persisted=total_persisted,
                )
            if (
                drained
                and initial_backfill_complete is not None
                and not initial_backfill_complete.is_set()
            ):
                initial_backfill_complete.set()

            await _sleep_or_stop(stop, interval_secs)


def _to_eval_output_projection(record: dict[str, Any]) -> dict[str, Any] | None:
    """Coerce a record into the canonical wire EvalOutput projection.

    CRIT-7: the publisher signs the public EvalOutput shape
    (CONTRACTS.md §1.10 + locked decision L8). The validator must
    verify against the SAME byte-exact projection that was signed.

    Accepts either:
    * a public EvalOutput dict — passed through as-is (only required
      keys are kept; ``merkle_epoch`` is excluded from canonical bytes
      via ``cathedral.v1_types.canonical_json`` regardless), or
    * a legacy storage-shaped dict with ``output_card_json`` + the
      flat agent fields — fields are renamed to the wire shape.

    Returns ``None`` if neither shape can be reconstructed (caller
    should reject the record).
    """
    # Wire shape carries `output_card`; storage shape carries `output_card_json`.
    if "output_card" in record and isinstance(record["output_card"], dict):
        output_card = record["output_card"]
    elif "output_card_json" in record and isinstance(record["output_card_json"], dict):
        output_card = record["output_card_json"]
    else:
        return None

    needed = ("id", "card_id", "weighted_score", "ran_at", "cathedral_signature")
    for k in needed:
        if k not in record:
            return None

    # `agent_id` lives at the top level on the wire; in storage it's `submission_id`.
    agent_id = record.get("agent_id")
    if agent_id is None:
        agent_id = record.get("submission_id")
    if agent_id is None:
        return None

    projection = {
        "id": record["id"],
        "agent_id": str(agent_id),
        "agent_display_name": record.get("agent_display_name", ""),
        "card_id": record["card_id"],
        "output_card": output_card,
        "output_card_hash": record.get("output_card_hash", ""),
        "weighted_score": record["weighted_score"],
        "polaris_verified": record.get("polaris_verified"),
        "ran_at": record["ran_at"],
        "cathedral_signature": record["cathedral_signature"],
        "merkle_epoch": record.get("merkle_epoch"),
    }
    return projection


def _rebuild_signed_payload(eval_output: dict[str, Any]) -> dict[str, Any] | None:
    """Re-derive the dict that was signed (wire EvalOutput projection).

    Backward-compat shim — delegates to :func:`_to_eval_output_projection`.
    See CRIT-7 note in :func:`verify_eval_run_signature`.
    """
    return _to_eval_output_projection(eval_output)


def _hotkey_for(eval_output: dict[str, Any]) -> str | None:
    version_raw = eval_output.get("eval_output_schema_version", 1)
    try:
        version = int(version_raw)
    except (TypeError, ValueError):
        version = 1
    if version in {3, 5}:
        hk = eval_output.get("miner_hotkey")
        return hk if isinstance(hk, str) and hk else None

    raw = eval_output.get("output_card") or {}
    if isinstance(raw, dict):
        hk = raw.get("worker_owner_hotkey")
        if isinstance(hk, str) and hk:
            return hk
    return None


async def _sleep_or_stop(stop: asyncio.Event, secs: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=secs)
    except TimeoutError:
        pass


__all__ = [
    "PullVerificationError",
    "_backfill_completed_under_current_code",
    "_initial_cursor",
    "_mark_backfill_complete",
    "latest_pulled_score_per_hotkey",
    "pull_once",
    "run_once",
    "run_pull_loop",
    "upsert_pulled_eval",
    "verify_eval_run_signature",
]


# --------------------------------------------------------------------------
# Test-friendly entry point
# --------------------------------------------------------------------------

# Module-level cursor for the simple `pull_once(fetcher, sink, public_key)`
# entry point. Real production deploys carry their cursor in the validator
# DB or a config file; this is sufficient for the contract test that only
# verifies cursor threading across two consecutive `pull_once` invocations.
#
# v1.1.0: cursor is a tuple ``(ran_at, id)`` to match the publisher's
# total-order ``ORDER BY ran_at ASC, id ASC`` scan. See
# ``2026-05-12-track-3-pull-cursor-audit.md``.
_LAST_SINCE: dict[str, str | None] = {"ran_at": None, "id": None}


def reset_pull_cursor() -> None:
    """Reset the module-level cursor — test convenience."""
    _LAST_SINCE["ran_at"] = None
    _LAST_SINCE["id"] = None


# v1 signed-payload key set. Matches scoring_pipeline.score_and_sign's
# public EvalOutput projection (CONTRACTS.md §1.10 + L8).
_SIGNED_EVAL_OUTPUT_KEYS_V1 = frozenset(
    {
        "id",
        "agent_id",
        "agent_display_name",
        "card_id",
        "output_card",
        "output_card_hash",
        "weighted_score",
        "polaris_verified",
        "ran_at",
    }
)

# v2 — cathedralai/cathedral#75 PR 4. Drops output_card, output_card_hash,
# polaris_verified. Adds eval_card_excerpt + eval_artifact_manifest_hash.
# `eval_output_schema_version` is NOT in this set: it's a routing hint
# validators read to pick the dispatcher entry, not part of the signed
# bytes. Must stay byte-for-byte identical to
# src/cathedral/eval/v2_payload.py:_SIGNED_KEYS_BY_VERSION on the
# miner-side branch — cross-branch contract.
_SIGNED_EVAL_OUTPUT_KEYS_V2 = frozenset(
    {
        "id",
        "agent_id",
        "agent_display_name",
        "card_id",
        "eval_card_excerpt",
        "eval_artifact_manifest_hash",
        "weighted_score",
        "ran_at",
    }
)

# v3, v3.0 bug_isolation_v1 benchmark lane. Publisher prompts the
# miner via SSH Hermes, miner returns a structured isolation claim,
# publisher scores statically on Railway against a hidden oracle and
# signs the result. No card_id in the signed bytes: v3 rows are not
# regulatory cards and do not route through the v1 card registry.
# Must stay byte-for-byte identical to
# src/cathedral/eval/v2_payload.py:_SIGNED_KEYS_BY_VERSION[3].
_SIGNED_EVAL_OUTPUT_KEYS_V3 = frozenset(
    {
        "id",
        "agent_id",
        "agent_display_name",
        "miner_hotkey",
        "task_type",
        "challenge_id",
        "challenge_id_public",
        "epoch_salt",
        "weighted_score",
        "score_parts",
        "claim",
        "ran_at",
    }
)

# v5, generic Task Family lane row. The publisher signs score and hashes
# only. Raw problem payloads and submitted answers stay publisher-private
# until an explicit reveal/export path exists.
_SIGNED_EVAL_OUTPUT_KEYS_V5 = frozenset(
    {
        "id",
        "agent_id",
        "agent_display_name",
        "miner_hotkey",
        "task_type",
        "task_id_public",
        "epoch_salt",
        "difficulty_tier",
        "weighted_score",
        "score_parts",
        "answer_hash",
        "verifier_details_hash",
        "rejection_reason",
        "ran_at",
    }
)

# v6, PAR-2 facts. v5 + challenge_value / solve_rank / solved / operator(coldkey)
# for the open-window scoring. Must stay byte-identical to
# src/cathedral/eval/v2_payload.py:_SIGNED_KEYS_BY_VERSION[6] and
# src/cathedral/lanes/sign.py:TASK_FAMILY_SIGNED_KEYS_V6. Validators verify v6
# (forward-compatible) before the publisher emits it.
_SIGNED_EVAL_OUTPUT_KEYS_V6 = _SIGNED_EVAL_OUTPUT_KEYS_V5 | frozenset(
    {
        "challenge_value",
        "solve_rank",
        "solved",
        "operator",
    }
)

# Version-keyed dispatcher. A record's signed payload schema is selected
# by ``eval_output_schema_version`` (defaulting to 1 when the field is
# absent — the v1.0.x wire shape).
_SIGNED_KEYS_BY_VERSION: dict[int, frozenset[str]] = {
    1: _SIGNED_EVAL_OUTPUT_KEYS_V1,
    2: _SIGNED_EVAL_OUTPUT_KEYS_V2,
    3: _SIGNED_EVAL_OUTPUT_KEYS_V3,
    5: _SIGNED_EVAL_OUTPUT_KEYS_V5,
    6: _SIGNED_EVAL_OUTPUT_KEYS_V6,
}

# Back-compat alias — the prior constant name. Tests and downstream code
# may still reference this; it points at the v1 key set.
_SIGNED_EVAL_OUTPUT_KEYS = _SIGNED_KEYS_BY_VERSION[1]


def verify_eval_output_signature(eval_output: dict[str, Any], public_key: Ed25519PublicKey) -> None:
    """Verify the cathedral signature over the publisher's signed payload.

    Version-aware dispatcher: reads ``eval_output_schema_version`` from
    the record (defaulting to 1 when absent — the v1.0.x wire shape),
    looks up the matching key set in ``_SIGNED_KEYS_BY_VERSION``, and
    verifies against that subset. Unknown versions raise
    ``PullVerificationError`` — we never silently treat an unknown
    version as v1.

    The publisher's ``score_and_sign`` signs a fixed-key payload (see
    ``scoring_pipeline.py``). The wire response carries extra fields
    (``cathedral_signature``, ``merkle_epoch``, ``polaris_attestation``,
    ``eval_output_schema_version``) that are NOT part of the signed
    bytes; we must strip them before canonicalizing or verify fails.

    The version field itself is not in the signed bytes — it's a routing
    hint. Tampering with it just routes verification to the wrong key
    set and fails the signature check anyway.
    """
    sig_b64 = eval_output.get("cathedral_signature")
    if not sig_b64:
        raise PullVerificationError("missing cathedral_signature")
    try:
        sig = base64.b64decode(sig_b64)
    except (ValueError, TypeError) as e:
        raise PullVerificationError(f"signature base64 invalid: {e}") from e

    version_raw = eval_output.get("eval_output_schema_version", 1)
    try:
        version = int(version_raw)
    except (TypeError, ValueError) as e:
        raise PullVerificationError(
            f"eval_output_schema_version not an int: {version_raw!r}"
        ) from e
    keys = _SIGNED_KEYS_BY_VERSION.get(version)
    if keys is None:
        raise PullVerificationError(f"unknown_schema_version: {version}")

    payload_dict = {k: v for k, v in eval_output.items() if k in keys}
    payload = canonical_json(payload_dict)
    try:
        public_key.verify(sig, payload)
    except InvalidSignature as e:
        raise PullVerificationError("invalid cathedral signature") from e


async def _pull_once_async(
    fetcher: Any,
    sink: Any,
    public_key: Ed25519PublicKey,
    *,
    limit: int = _DEFAULT_PULL_LIMIT,
) -> int:
    """One cursor-advancing cycle. Drains saturated pages.

    Mirrors :func:`run_pull_loop` so the contract test and production
    loop share semantics. When a page returns exactly ``limit`` rows,
    pull again immediately (we're behind). Cap at ``_MAX_INNER_PULLS``
    iterations per outer call so a stub fetcher that always returns a
    saturated page cannot loop forever.
    """
    persisted = 0
    inner_pulls = 0
    while inner_pulls < _MAX_INNER_PULLS:
        inner_pulls += 1
        since_ran_at = _LAST_SINCE["ran_at"]
        since_id = _LAST_SINCE["id"]
        payload = await _invoke_fetcher(
            fetcher, since_ran_at=since_ran_at, since_id=since_id, limit=limit
        )
        if not isinstance(payload, dict):
            return persisted
        items = payload.get("items") or []
        if not isinstance(items, list):
            return persisted

        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                verify_eval_output_signature(item, public_key)
            except PullVerificationError as e:
                logger.warning("pull_eval_signature_invalid", id=item.get("id"), error=str(e))
                continue
            result = sink(item)
            if asyncio.iscoroutine(result):
                await result
            persisted += 1

        # Advance the cursor — prefer the publisher's tuple cursor;
        # fall back to legacy next_since (ran_at only); fall back to
        # last item's (ran_at, id).
        next_ran_at = payload.get("next_since_ran_at")
        next_id = payload.get("next_since_id")
        if isinstance(next_ran_at, str) and isinstance(next_id, str):
            _LAST_SINCE["ran_at"] = next_ran_at
            _LAST_SINCE["id"] = next_id
        elif isinstance(next_ran_at, str):
            _LAST_SINCE["ran_at"] = next_ran_at
        else:
            legacy = payload.get("next_since")
            if isinstance(legacy, str):
                # Legacy publisher cursor — only ran_at. Leave id alone
                # (UPSERT dedupes any boundary overlap).
                _LAST_SINCE["ran_at"] = legacy
            elif items:
                last = items[-1] if isinstance(items[-1], dict) else None
                if isinstance(last, dict):
                    last_ran = last.get("ran_at")
                    last_id = last.get("id")
                    if isinstance(last_ran, str):
                        _LAST_SINCE["ran_at"] = last_ran
                    if isinstance(last_id, str):
                        _LAST_SINCE["id"] = last_id

        # Caught up — no need for another inner pull.
        if len(items) < limit:
            break

    return persisted


async def _invoke_fetcher(
    fetcher: Any,
    *,
    since_ran_at: str | None,
    since_id: str | None,
    limit: int,
) -> Any:
    """Call ``fetcher`` with a best-effort signature.

    Production fetchers accept the v1.1.0 tuple ``(since_ran_at,
    since_id)``. The existing contract tests in
    ``tests/v1/test_validator_pull_loop.py`` use a stub fetcher with the
    legacy signature ``fetcher(since=..., limit=...)``. We try the new
    shape first, fall back to the legacy shape with ``since=since_ran_at``
    if the call raises ``TypeError`` (unknown kwarg).
    """
    try:
        return await fetcher(
            since_ran_at=since_ran_at,
            since_id=since_id,
            limit=limit,
        )
    except TypeError:
        return await fetcher(since=since_ran_at, limit=limit)


def pull_once(
    fetcher: Any,
    sink: Any,
    public_key: Ed25519PublicKey,
    *,
    limit: int = _DEFAULT_PULL_LIMIT,
) -> int:
    """One cursor-advancing pull cycle.

    Fetcher signature (v1.1.0):
    ``fetcher(since_ran_at, since_id, limit) -> {items, next_since_ran_at,
    next_since_id, next_since, merkle_epoch_latest}``

    Legacy fallback (v1.0.x stubs): ``fetcher(since, limit)`` — the loop
    transparently falls back when ``fetcher`` raises ``TypeError`` on
    the new kwargs.

    ``sink(eval_output) -> None`` is called per verified entry.

    Returns the number of entries handed to ``sink``. Synchronous wrapper
    so test harnesses can call without managing an event loop.
    """
    coro = _pull_once_async(fetcher, sink, public_key, limit=limit)
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Caller is inside an event loop — schedule and wait.
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=30)
    except RuntimeError:
        pass
    return asyncio.run(coro)


# Aliases the contract test probes for.
run_once = pull_once
sync_once = pull_once
tick = pull_once
pull = pull_once


def _unused(_: json.JSONDecodeError) -> None:
    """Keep `json` import warm for readability of error paths above."""
    return None
