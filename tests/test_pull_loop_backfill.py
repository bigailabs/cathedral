"""Regression tests for the pull-loop backfill window.

cathedralai/cathedral#105 changed `weight_loop` to compute a 7-day
rolling mean per hotkey, but `pull_loop` seeded its initial cursor at
``now - 1 hour``. A fresh or restarted validator never hydrated the rest
of the 7-day window before its first ``set_weights`` call, so Cathedral
validators on SN39 produced different ``weights_pre_burn`` vectors
depending on how long they'd been running.

A first attempt at the fix (PR #109 first pass) used `max(ran_at)` from
local DB to pick the cursor. That was wrong for the upgrade path:
existing validators with recent rows from the old `now - 1h` cursor
would resume from those recent rows and never walk the older end of the
new 7-day window. So Rizzo/Kraken/RT21 stayed divergent from TAO.com.

The current fix uses a **durable marker** in a new ``pull_loop_meta``
table: until a validator records that it has completed a 7-day backfill
under the current code version, every startup forces a seed at
``now - 7 days`` regardless of what's in ``pulled_eval_runs``. After
the first drained catch-up pass the marker is written, so subsequent
restarts use the cheap resume-from-local-max path.

This module pins:

1. Fresh DB → cursor seeds at ``now - 7 days``.
2. **Upgrade case**: DB has recent rows but no marker → still seed at
   ``now - 7 days``. This is the load-bearing case.
3. Marker present and fresh row inside window → resume from
   ``max(ran_at)``.
4. Marker present but max row older than window → reseed at
   ``now - 7 days``.
5. Repeated startup with marker present does not duplicate rows or
   skew score aggregates.
6. End-to-end: fresh validator backfill hydrates the weight window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cathedral.validator import pull_loop
from cathedral.validator.db import connect
from cathedral.validator.pull_loop import (
    _INITIAL_BACKFILL_DAYS,
    _backfill_completed_under_current_code,
    _initial_cursor,
    _mark_backfill_complete,
    latest_pulled_score_per_hotkey,
    upsert_pulled_eval,
)


@pytest.mark.asyncio
async def test_initial_cursor_on_fresh_db_returns_backfill_floor(tmp_path) -> None:
    """Case 1: empty DB, no marker → seed at ``now - 7 days``.

    A first-boot validator needs to walk the full scoring window before
    its first weight set, otherwise its vector under-counts masons vs.
    a validator that's been running for days.
    """
    conn = await connect(str(tmp_path / "v.db"))
    try:
        before = datetime.now(UTC) - timedelta(days=_INITIAL_BACKFILL_DAYS)
        cursor_ran_at, cursor_id = await _initial_cursor(conn)
        after = datetime.now(UTC) - timedelta(days=_INITIAL_BACKFILL_DAYS)

        assert cursor_id == "", (
            "fresh DB must seed an empty id so the tuple cursor's "
            "strict `>` comparison includes the boundary timestamp"
        )
        parsed = datetime.fromisoformat(cursor_ran_at)
        assert before <= parsed <= after, (
            f"fresh DB must seed at now - {_INITIAL_BACKFILL_DAYS} days; "
            f"got {parsed}, expected between {before} and {after}"
        )
        assert not await _backfill_completed_under_current_code(conn), (
            "fresh DB must NOT report backfill complete before any pulls"
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_upgrade_path_forces_backfill_despite_recent_rows(tmp_path) -> None:
    """**Load-bearing case** for the production upgrade story.

    Existing validators already have recent rows in ``pulled_eval_runs``
    from the old ``now - 1h`` cursor. After auto-update + restart they
    must still walk the older end of the new 7-day window — otherwise
    they stay divergent from TAO.com forever.

    Pre-fix (PR #109 first pass): max(ran_at) was 30 min ago, so the
    cursor resumed there and never backfilled days 1-7. Post-fix: no
    backfill marker present → force seed at now - 7d regardless of
    what's in pulled_eval_runs.
    """
    conn = await connect(str(tmp_path / "v.db"))
    try:
        recent = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
        await upsert_pulled_eval(
            conn,
            eval_run={
                "id": "eval-pre-upgrade",
                "weighted_score": 0.8,
                "ran_at": recent,
            },
            miner_hotkey="hk-from-old-cursor",
        )

        # Sanity: no backfill marker yet (upgrade just happened).
        assert not await _backfill_completed_under_current_code(conn)

        cursor_ran_at, cursor_id = await _initial_cursor(conn)
        floor_dt = datetime.now(UTC) - timedelta(days=_INITIAL_BACKFILL_DAYS)
        # Allow a 30-second jitter window — clock between assertion and
        # the helper's `datetime.now(UTC)` ticks slightly.
        lower = floor_dt - timedelta(seconds=30)
        upper = floor_dt + timedelta(seconds=30)
        parsed = datetime.fromisoformat(cursor_ran_at)
        assert lower <= parsed <= upper, (
            "upgrade path must seed at now - 7 days even with recent "
            f"rows present; got {parsed} (recent row was at {recent})"
        )
        assert cursor_id == "", (
            "upgrade path must reset cursor id to empty; tuple cursor "
            "needs to include rows older than the recent local one"
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_marker_present_resumes_from_max_ran_at(tmp_path) -> None:
    """Case 3: backfill completed earlier → cheap resume path.

    Steady-state restart of a validator that already did the 7-day
    backfill at least once under the current code. The marker is
    present, so we trust local state and resume from max(ran_at).
    """
    conn = await connect(str(tmp_path / "v.db"))
    try:
        recent = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        await upsert_pulled_eval(
            conn,
            eval_run={
                "id": "eval-recent",
                "weighted_score": 0.7,
                "ran_at": recent,
            },
            miner_hotkey="hk-1",
        )
        await _mark_backfill_complete(conn)
        assert await _backfill_completed_under_current_code(conn)

        cursor_ran_at, cursor_id = await _initial_cursor(conn)
        assert cursor_ran_at == recent, (
            "marker-present restart must resume from max(ran_at), got "
            f"{cursor_ran_at} expected {recent}"
        )
        assert cursor_id == "eval-recent"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_marker_present_but_stale_max_resets_to_floor(tmp_path) -> None:
    """Case 4: marker present but local max is older than 7d → reseed.

    A validator down for >7d has no useful state in its
    ``pulled_eval_runs`` table for the current scoring window. Even
    with the marker present, the resume-from-max path would re-pull
    nothing useful and miss the new 7-day window. Reset to the floor.
    """
    conn = await connect(str(tmp_path / "v.db"))
    try:
        stale = (datetime.now(UTC) - timedelta(days=_INITIAL_BACKFILL_DAYS + 5)).isoformat()
        await upsert_pulled_eval(
            conn,
            eval_run={
                "id": "eval-stale",
                "weighted_score": 0.9,
                "ran_at": stale,
            },
            miner_hotkey="hk-stale",
        )
        await _mark_backfill_complete(conn)

        cursor_ran_at, cursor_id = await _initial_cursor(conn)
        assert cursor_id == "", (
            "long-downtime reseed must clear the id portion; saved id "
            "is older than the backfill floor"
        )
        parsed = datetime.fromisoformat(cursor_ran_at)
        floor = datetime.now(UTC) - timedelta(days=_INITIAL_BACKFILL_DAYS + 1)
        assert parsed > floor, (
            f"long-downtime restart must seed at now - {_INITIAL_BACKFILL_DAYS} days; got {parsed}"
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_repeated_startup_backfill_is_idempotent(tmp_path) -> None:
    """Repeated startups (with the marker) do not duplicate rows or skew scores.

    Even if the publisher re-serves the same row across multiple
    backfills (e.g. operator nukes the marker by hand to force a
    re-walk), ``upsert_pulled_eval``'s ON CONFLICT clause absorbs it.
    """
    conn = await connect(str(tmp_path / "v.db"))
    try:
        eval_run = {
            "id": "eval-rep",
            "task_type": "synthetic_boolean_v1",
            "eval_output_schema_version": 5,
            "weighted_score": 0.42,
            "ran_at": datetime.now(UTC).isoformat(),
        }
        # Simulate three startups each pulling the same row.
        for _ in range(3):
            await upsert_pulled_eval(conn, eval_run=eval_run, miner_hotkey="hk-1")
            await _mark_backfill_complete(conn)

        cur = await conn.execute(
            "SELECT COUNT(*) FROM pulled_eval_runs WHERE eval_run_id = ?",
            ("eval-rep",),
        )
        row = await cur.fetchone()
        assert row is not None and row[0] == 1, (
            f"upsert must dedupe by eval_run_id; got {row[0]} rows after 3 repeated startups"
        )

        scores = await latest_pulled_score_per_hotkey(
            conn, since_days=7, task_family_weights={"synthetic_boolean_v1": 1.0}
        )
        assert scores.get("hk-1") == pytest.approx(0.42), (
            f"repeated startup re-pulls must not skew the mean — got {scores.get('hk-1')}"
        )

        # Marker row count stays at 1 too (single key, repeated upsert).
        cur = await conn.execute("SELECT COUNT(*) FROM pull_loop_meta")
        row = await cur.fetchone()
        assert row is not None and row[0] == 1, (
            f"pull_loop_meta must keep one row per key; got {row[0]}"
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_production_upgrade_failure_pattern(tmp_path) -> None:
    """The exact failure observed in production:

    - Validator DB contains a recent eval from 30 minutes ago (left over
      from the pre-#109 ``now - 1h`` cursor era).
    - Publisher has an older positive eval from 4 days ago that the
      validator never saw.
    - On startup, pull_loop must request from ``now - 7d``, not from
      the recent local row.
    - After pull, ``latest_pulled_score_per_hotkey(..., since_days=7)``
      includes both hotkeys.
    """
    conn = await connect(str(tmp_path / "v.db"))
    try:
        # Pre-existing recent row from old code era (no backfill marker).
        recent = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
        await upsert_pulled_eval(
            conn,
            eval_run={
                "id": "eval-recent",
                "task_type": "synthetic_boolean_v1",
                "eval_output_schema_version": 5,
                "weighted_score": 0.5,
                "ran_at": recent,
            },
            miner_hotkey="hk-existing",
        )

        # Confirm the cursor _initial_cursor returns is the 7-day floor,
        # NOT the recent row. This is the load-bearing assertion.
        cursor_ran_at, cursor_id = await _initial_cursor(conn)
        floor_dt = datetime.now(UTC) - timedelta(days=_INITIAL_BACKFILL_DAYS)
        parsed = datetime.fromisoformat(cursor_ran_at)
        assert parsed < datetime.fromisoformat(recent), (
            "cursor must point BEFORE the recent local row so the older "
            f"window gets pulled; got cursor={parsed}, recent={recent}"
        )
        assert abs((parsed - floor_dt).total_seconds()) < 30, (
            f"cursor must be at the 7-day floor (~{floor_dt}); got {parsed}"
        )
        assert cursor_id == ""

        # Simulate the publisher serving an older positive eval that
        # the pull loop now picks up because the cursor was reset.
        four_days_ago = (datetime.now(UTC) - timedelta(days=4)).isoformat()
        await upsert_pulled_eval(
            conn,
            eval_run={
                "id": "eval-older-backfilled",
                "task_type": "synthetic_boolean_v1",
                "eval_output_schema_version": 5,
                "weighted_score": 0.9,
                "ran_at": four_days_ago,
            },
            miner_hotkey="hk-backfilled",
        )

        scores = await latest_pulled_score_per_hotkey(
            conn, since_days=7, task_family_weights={"synthetic_boolean_v1": 1.0}
        )
        assert "hk-existing" in scores, "recent row's hotkey must survive"
        assert "hk-backfilled" in scores, (
            "backfilled older hotkey must appear in the 7-day score aggregator after backfill"
        )
        assert scores["hk-existing"] == pytest.approx(0.5)
        assert scores["hk-backfilled"] == pytest.approx(0.9)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_fresh_validator_backfill_hydrates_weight_window(tmp_path) -> None:
    """End-to-end: fresh DB pulls a 6-day-old row and weight_loop sees it.

    Pre-fix: pull_loop seeded cursor at ``now - 1h``, so any eval older
    than 1 hour was invisible to ``latest_pulled_score_per_hotkey``'s
    7-day window even though the publisher still served it.

    Post-fix: the cursor seeds at ``now - 7d``, the saturation-driven
    inner loop drains the page, and the hotkey appears in the weight
    aggregator's output.
    """
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    from cathedral.v1_types import canonical_json

    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()

    six_days_ago = (datetime.now(UTC) - timedelta(days=6)).isoformat()
    miner_hotkey = "5HotkeyOfMasonBackfilledByFreshValidator"
    # v5 (SAT lane) signed row. Only the v5 keyset is in the signed bytes
    # (see pull_loop._SIGNED_EVAL_OUTPUT_KEYS_V5); the schema-version field
    # and signature/merkle envelope are not.
    signed = {
        "id": "00000000-0000-4000-8000-000000000099",
        "agent_id": "11111111-1111-4111-8111-000000000099",
        "agent_display_name": "Backfilled Agent",
        "miner_hotkey": miner_hotkey,
        "task_type": "synthetic_boolean_v1",
        "task_id_public": "task-pub-099",
        "epoch_salt": "salt-099",
        "difficulty_tier": 0,
        "weighted_score": 0.66,
        "score_parts": {},
        "answer_hash": "a" * 64,
        "verifier_details_hash": "b" * 64,
        "rejection_reason": None,
        "ran_at": six_days_ago,
    }
    blob = canonical_json(signed)
    payload_entry = dict(signed)
    payload_entry["eval_output_schema_version"] = 5
    payload_entry["cathedral_signature"] = base64.b64encode(sk.sign(blob)).decode("ascii")
    payload_entry["merkle_epoch"] = None

    conn = await connect(str(tmp_path / "v.db"))

    seed_ran_at, _seed_id = await _initial_cursor(conn)
    parsed = datetime.fromisoformat(seed_ran_at)
    floor = datetime.now(UTC) - timedelta(days=_INITIAL_BACKFILL_DAYS + 1)
    assert parsed > floor

    pull_loop.verify_eval_output_signature(payload_entry, pk)
    await upsert_pulled_eval(
        conn,
        eval_run=payload_entry,
        miner_hotkey=miner_hotkey,
    )

    scores = await latest_pulled_score_per_hotkey(
        conn, since_days=7, task_family_weights={"synthetic_boolean_v1": 1.0}
    )
    assert scores.get("5HotkeyOfMasonBackfilledByFreshValidator") == pytest.approx(0.66)

    await conn.close()


@pytest.mark.asyncio
async def test_v3_bug_isolation_weight_blends_with_sat_rows(tmp_path) -> None:
    """bug_isolation_v1 contributes only the configured slice.

    SAT (synthetic_boolean_v1) rows are the primary lane. A mixed miner
    gets the configured blend, and a v3-only miner cannot take a
    full-weight slot when the blend is 5 percent.
    """
    conn = await connect(str(tmp_path / "v.db"))
    try:
        now = datetime.now(UTC).isoformat()
        await upsert_pulled_eval(
            conn,
            eval_run={
                "id": "eval-mixed-sat",
                "task_type": "synthetic_boolean_v1",
                "eval_output_schema_version": 5,
                "weighted_score": 0.80,
                "ran_at": now,
            },
            miner_hotkey="hk-mixed",
        )
        await upsert_pulled_eval(
            conn,
            eval_run={
                "id": "eval-mixed-v3",
                "task_type": "bug_isolation_v1",
                "eval_output_schema_version": 3,
                "weighted_score": 0.20,
                "ran_at": now,
            },
            miner_hotkey="hk-mixed",
        )
        await upsert_pulled_eval(
            conn,
            eval_run={
                "id": "eval-v3-only",
                "task_type": "bug_isolation_v1",
                "eval_output_schema_version": 3,
                "weighted_score": 0.60,
                "ran_at": now,
            },
            miner_hotkey="hk-v3-only",
        )

        scores = await latest_pulled_score_per_hotkey(
            conn,
            since_days=7,
            v3_bug_isolation_weight=0.05,
            task_family_weights={"synthetic_boolean_v1": 0.95},
        )
        assert scores["hk-mixed"] == pytest.approx((0.80 * 0.95) + (0.20 * 0.05))
        assert scores["hk-v3-only"] == pytest.approx(0.60 * 0.05)

        scores_disabled = await latest_pulled_score_per_hotkey(
            conn,
            since_days=7,
            v3_bug_isolation_weight=0.0,
            task_family_weights={"synthetic_boolean_v1": 0.95},
        )
        assert scores_disabled["hk-mixed"] == pytest.approx(0.80 * 0.95)
        assert "hk-v3-only" not in scores_disabled
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_legacy_card_rows_no_longer_earn_incentive(tmp_path) -> None:
    """Non-SAT, non-bug-isolation (legacy EU-AI-Act card) rows are dropped.

    Replaces the old v1-pool averaging test: the v1 card bucket has been
    removed, so card-era rows contribute nothing to the weight vector.
    """
    conn = await connect(str(tmp_path / "v.db"))
    try:
        now = datetime.now(UTC).isoformat()
        for idx in range(4):
            await upsert_pulled_eval(
                conn,
                eval_run={
                    "id": f"eval-historical-{idx}",
                    "card_id": "eu-ai-act",
                    "weighted_score": 1.0,
                    "ran_at": now,
                },
                miner_hotkey="hk-card-only",
            )
            await conn.execute(
                "UPDATE pulled_eval_runs SET task_type='unknown' WHERE eval_run_id=?",
                (f"eval-historical-{idx}",),
            )
        await upsert_pulled_eval(
            conn,
            eval_run={
                "id": "eval-new-eu-card",
                "card_id": "eu-ai-act",
                "weighted_score": 1.0,
                "ran_at": now,
            },
            miner_hotkey="hk-card-only",
        )
        await conn.commit()

        scores = await latest_pulled_score_per_hotkey(
            conn,
            since_days=7,
            v3_bug_isolation_weight=0.05,
            task_family_weights={"synthetic_boolean_v1": 1.0},
        )
        assert "hk-card-only" not in scores, "legacy card rows must not earn incentive"
    finally:
        await conn.close()


# --------------------------------------------------------------------------
# C1 regression: marker write must gate on a successfully drained tick.
#
# PR #109 first-merge review caught: the marker was being written on every
# outer tick that exited the inner loop, regardless of WHY it exited.
# Transport error, malformed payload, and saturation-cap exhaustion all
# break the inner loop without proving the cursor reached the head of the
# publisher's feed. Writing the marker on those paths locks the validator
# out of ever re-backfilling on restart, recreating the exact divergence
# PR #109 is supposed to fix.
#
# These tests pin the fix: marker is written ONLY after an outer tick that
# saw `len(items) < limit` (a non-saturated page = caught up).
# --------------------------------------------------------------------------


async def _run_one_pull_tick(
    conn,
    monkeypatch,
    fake_transport_handler,
    *,
    initial_backfill_event: object | None = None,
    limit: int = 500,
) -> object:
    """Run exactly one outer tick of `run_pull_loop` against a fake httpx
    transport and return the asyncio.Event that was passed for backfill
    signalling (so tests can assert on `is_set()`).

    Uses ``stop`` set immediately after the first outer tick to avoid
    hanging on the 30s sleep.
    """
    import asyncio

    import httpx
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    from cathedral.validator.health import Health

    # Wrap the AsyncClient constructor so the loop hits our MockTransport.
    real_init = httpx.AsyncClient.__init__

    def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["transport"] = httpx.MockTransport(fake_transport_handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)

    stop = asyncio.Event()
    backfill_event = initial_backfill_event or asyncio.Event()

    # Sentinel: after the first `pull_loop_tick`, set stop so the outer
    # loop exits before the 30s sleep blocks the test.
    from cathedral.validator import pull_loop as pull_loop_mod

    original_info = pull_loop_mod.logger.info

    def _maybe_stop_after_tick(event: str, **fields):  # type: ignore[no-untyped-def]
        original_info(event, **fields)
        if event == "pull_loop_tick":
            stop.set()

    monkeypatch.setattr(pull_loop_mod.logger, "info", _maybe_stop_after_tick)

    sk = Ed25519PrivateKey.generate()
    task = asyncio.create_task(
        pull_loop_mod.run_pull_loop(
            conn=conn,
            publisher_url="https://fake.cathedral.test",
            cathedral_public_key=sk.public_key(),
            health=Health(),
            interval_secs=0.01,
            stop=stop,
            limit=limit,
            initial_backfill_complete=backfill_event,
        )
    )
    await asyncio.wait_for(task, timeout=3)
    return backfill_event


@pytest.mark.asyncio
async def test_marker_not_written_on_transport_error(tmp_path, monkeypatch) -> None:
    """C1.a — HTTP error must not write the durable marker.

    A first-tick transport failure used to write `backfill_complete`
    anyway, locking the validator out of ever re-backfilling. Now the
    `drained` flag gates the marker write.
    """
    import httpx

    def fail_with_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated network failure")

    conn = await connect(str(tmp_path / "v.db"))
    try:
        event = await _run_one_pull_tick(conn, monkeypatch, fail_with_timeout)
        assert not await _backfill_completed_under_current_code(conn), (
            "C1.a: backfill marker must NOT be written when the first publisher call errors out"
        )
        assert not event.is_set(), (
            "C1.a: initial_backfill_complete event must NOT be set when "
            "the first publisher call errors out"
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_marker_not_written_on_malformed_payload(tmp_path, monkeypatch) -> None:
    """C1.b — malformed payload (`items` not a list) must not write the marker.

    A misbehaving / corrupt publisher response used to count as a
    drained tick, locking the validator out of re-backfill.
    """
    import httpx

    def malformed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": "not-a-list"})

    conn = await connect(str(tmp_path / "v.db"))
    try:
        event = await _run_one_pull_tick(conn, monkeypatch, malformed)
        assert not await _backfill_completed_under_current_code(conn), (
            "C1.b: backfill marker must NOT be written when the publisher "
            "returns a malformed `items` payload"
        )
        assert not event.is_set()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_marker_not_written_on_empty_payload_object(tmp_path, monkeypatch) -> None:
    """C1.b.i — payload missing the `items` key entirely (e.g. `{}`)
    must not write the marker.

    Caught in PR #110 re-review: ``payload.get("items") or []`` coerced
    a missing key into an empty list, which then looked identical to
    "publisher caught up, drained tick" and wrote the marker. Production
    failure: malformed 200 response on the first post-upgrade poll
    records "backfill complete" without walking the feed, then the next
    restart resumes from old local max and misses days 1-7 again.
    """
    import httpx

    def empty_object(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    conn = await connect(str(tmp_path / "v.db"))
    try:
        event = await _run_one_pull_tick(conn, monkeypatch, empty_object)
        assert not await _backfill_completed_under_current_code(conn), (
            "C1.b.i: backfill marker must NOT be written when the "
            "publisher returns a 200 with no `items` key — that's a "
            "malformed payload, not a caught-up cursor"
        )
        assert not event.is_set(), "C1.b.i: event must NOT be set on missing-key payload"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_marker_not_written_on_null_items(tmp_path, monkeypatch) -> None:
    """C1.b.ii — payload with `items: null` must not write the marker.

    Same C1 surface as the empty-object case: ``payload.get("items") or []``
    coerced `None` into `[]`, which then triggered the drained-tick
    path. The fix uses an explicit `not isinstance(payload["items"], list)`
    check so None falls through to the malformed-payload branch.
    """
    import httpx

    def null_items(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": None})

    conn = await connect(str(tmp_path / "v.db"))
    try:
        event = await _run_one_pull_tick(conn, monkeypatch, null_items)
        assert not await _backfill_completed_under_current_code(conn), (
            "C1.b.ii: backfill marker must NOT be written when the "
            "publisher returns `items: null` — that's malformed, not "
            "a caught-up cursor"
        )
        assert not event.is_set(), "C1.b.ii: event must NOT be set on null-items payload"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_marker_not_written_on_saturation_cap_exhaustion(tmp_path, monkeypatch) -> None:
    """C1.c — hitting _MAX_INNER_PULLS without ever draining must not
    write the marker.

    A publisher with a giant backlog could feed `_MAX_INNER_PULLS`
    saturated pages without ever returning `len(items) < limit`. That
    means there are still rows behind the cursor. Pre-fix this still
    wrote the marker; post-fix it doesn't.
    """
    import secrets

    import httpx

    page_size = 2  # tiny `limit` makes saturation cheap to simulate

    def always_saturated(request: httpx.Request) -> httpx.Response:
        # Return exactly `page_size` items every time so the inner loop
        # always considers itself saturated and hits the cap.
        items = []
        for _ in range(page_size):
            # Use fully-formed but signature-invalid entries — the
            # verification step will reject them, but that's fine for
            # this test: we want to assert the marker logic gates on
            # `drained`, not on `persisted`.
            items.append(
                {
                    "id": secrets.token_hex(8),
                    "agent_id": secrets.token_hex(8),
                    "agent_display_name": "x",
                    "card_id": "eu-ai-act",
                    "output_card": {"id": "eu-ai-act"},
                    "output_card_hash": "0" * 64,
                    "weighted_score": 0.0,
                    "polaris_verified": False,
                    "ran_at": datetime.now(UTC).isoformat(),
                    "cathedral_signature": "AAAA",
                    "merkle_epoch": None,
                }
            )
        return httpx.Response(
            200,
            json={
                "items": items,
                # Advance the cursor so the next saturated page comes back.
                "next_since_ran_at": items[-1]["ran_at"],
                "next_since_id": items[-1]["id"],
            },
        )

    conn = await connect(str(tmp_path / "v.db"))
    try:
        event = await _run_one_pull_tick(conn, monkeypatch, always_saturated, limit=page_size)
        assert not await _backfill_completed_under_current_code(conn), (
            "C1.c: backfill marker must NOT be written when the inner "
            "loop exits via saturation cap exhaustion — there are still "
            "rows behind the cursor"
        )
        assert not event.is_set(), "C1.c: event must NOT be set on saturation-cap exit"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_marker_is_written_after_drained_tick(tmp_path, monkeypatch) -> None:
    """Positive control: a tick that returns `len(items) < limit` DOES
    write the marker and set the event. Pairs with the three negative
    cases above.
    """
    import httpx

    def empty_page(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [],
                "next_since_ran_at": None,
                "next_since_id": None,
            },
        )

    conn = await connect(str(tmp_path / "v.db"))
    try:
        event = await _run_one_pull_tick(conn, monkeypatch, empty_page)
        assert await _backfill_completed_under_current_code(conn), (
            "positive control: drained tick (len(items)=0 < limit) MUST write the marker"
        )
        assert event.is_set(), "positive control: drained tick MUST set the backfill event"
    finally:
        await conn.close()
