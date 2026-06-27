# Staged Go-Live: PM (`pm-*`) Durable Async Submit Admission (TRACK 1)

Goal: move the **private per-miner (`pm-*`) submit lane** — the lane that carries
the live miner traffic — onto the same durable async receipt path the public lane
already uses, **without ever risking a double payout or breaking the miner submit
contract**. Every step is reversible by flipping a flag off; nothing here changes
SAT scoring semantics or the signed weight vector.

Scoring already reads the **ledger** (`per_miner_solves` + `lane_challenge_solves`,
`verified=1`), not `per_miner_attempts`. The async path only changes **when** the
ledger row is written, never **what** it guarantees. The terminal accept is one
atomic transaction: signature burn + `INSERT OR IGNORE` distinct-solver claim +
submission/witness/feed rows. A reclaimed (crashed) attempt cannot double-pay
because the `INSERT OR IGNORE` on `per_miner_solves` returns `already_solved`.

## Flags (all DEFAULT-OFF)

| Flag | Default | Effect when true |
|---|---|---|
| `CATHEDRAL_SUBMIT_ASYNC_ENABLED` | false | Public-lane durable admission (already shipped). The pm-async chain rides on this. |
| `CATHEDRAL_ASYNC_VERIFY_ENABLED` | false | Run the drain worker loop (must be on a `worker`/`all` role). |
| `CATHEDRAL_PM_SUBMIT_ASYNC_ENABLED` | false | TRACK 1: route the `pm-*` submit lane through durable async admission. Requires `CATHEDRAL_SUBMIT_ASYNC_ENABLED` too. |
| `CATHEDRAL_PM_ASYNC_SHADOW` | false | With pm-async on, keep the **inline** result authoritative for payout; the worker re-verifies into `shadow_*` columns and logs divergence. **No payout change.** |
| `CATHEDRAL_PM_SUBMIT_MAX_SOLUTION_BYTES` | 1000000 | Cheap inline body-size guard (hard `413 solution_too_large` before persist/queue). |

Effective state matrix (what a `pm-*` submit does):

- **All off** → inline synchronous pm path, byte-for-byte legacy (200 ranked / 400
  reject with the same `X-Cathedral-Rejection-Reason`). No durable receipt row.
- **pm-async on, shadow off** → cheap inline checks, `202` + receipt, worker
  verifies + pays from the ledger. This is **cutover**.
- **pm-async on, shadow on** → inline runs authoritatively (still `200`/`400`),
  AND a shadow twin (`challenge_kind=per_miner_shadow`) is queued; the worker
  re-verifies it into `shadow_*` only and logs any async-vs-inline divergence.
- A client may force the legacy inline path per-request with header
  `X-Cathedral-Submit-Mode: sync` even when pm-async is on (escape hatch).

Instant rollback at any stage: set `CATHEDRAL_PM_SUBMIT_ASYNC_ENABLED=false`
(and `CATHEDRAL_PM_ASYNC_SHADOW=false`). The submit path reverts to inline on the
next request — no deploy, no migration, no data fixup. Already-queued `pending`
rows are harmless: a worker that is still running drains them; if no worker runs,
they sit as `pending` and miners can re-submit (idempotency key dedupes).

---

## Stage 0 — Real-Postgres SQL validation (BEFORE any flag flip)

The offline test suite runs on SQLite (one global write lock gives the same
exclusivity as `FOR UPDATE SKIP LOCKED`). Postgres claim semantics MUST be
validated on the real database, because they are deferred-to-live by definition.

1. Apply migration `0031_pm_submit_admission` (additive, nullable columns +
   one index). Confirm it is idempotent and non-blocking:
   ```sql
   -- additive + nullable: inline writers never name these columns
   \d per_miner_attempts
   -- expect: assignment_identity TEXT, shadow_status TEXT,
   --         shadow_rejection_reason TEXT  (all NULLable)
   SELECT indexname FROM pg_indexes WHERE tablename='per_miner_attempts';
   -- expect: idx_per_miner_attempts_kind_status_received
   ```
   `ADD COLUMN ... IF NOT EXISTS` with no default is a metadata-only change in
   Postgres (no table rewrite). The index build on a small/idle `per_miner_attempts`
   is fast; if the table is large, build `CONCURRENTLY` out of band first.

2. Validate the worker claim is **single-grab** under concurrency (no two workers
   claim one row, fairness by `received_at`):
   ```sql
   -- in two psql sessions simultaneously, against seeded pending rows:
   BEGIN;
   UPDATE per_miner_attempts SET status='verifying', locked_by='w1',
     locked_until_iso=:deadline, attempt_count=attempt_count+1
   WHERE id IN (
     SELECT id FROM per_miner_attempts
     WHERE status IN ('pending','failed_retryable','verifying')
       AND (next_attempt_at_iso IS NULL OR next_attempt_at_iso <= :now)
       AND (locked_until_iso IS NULL OR locked_until_iso <= :now)
     ORDER BY received_at_iso, id
     LIMIT 8 FOR UPDATE SKIP LOCKED
   ) RETURNING id;
   -- session 2 with locked_by='w2' must return a DISJOINT id set, no overlap.
   COMMIT;
   ```
   Confirm: the two `RETURNING` id sets are disjoint, and the union is in
   `received_at_iso` order.

3. Validate idempotent admission (`ON CONFLICT` / unique-index backstop):
   ```sql
   -- two identical admissions for the same (hotkey, challenge, sol_sha):
   INSERT INTO per_miner_attempts(id, idempotency_key, status, ...)
   VALUES ('a', :idem, 'pending', ...)
   ON CONFLICT (idempotency_key) DO NOTHING;   -- second one inserts 0 rows
   SELECT count(*) FROM per_miner_attempts WHERE idempotency_key=:idem;  -- = 1
   ```
   The app relies on the UNIQUE index `idx_per_miner_attempts_idem` (from 0030).
   Confirm it exists and is UNIQUE.

4. Validate the **no-double-payout** invariant against the real ledger:
   ```sql
   -- per_miner_solves PK/unique is (challenge_id, miner_hotkey); a second claim
   -- for the same pair must be a no-op:
   INSERT INTO per_miner_solves(challenge_id, miner_hotkey, epoch, tier, seq,
     difficulty_weight, verified, solved_at_iso)
   VALUES (:cid, :hk, ...) ON CONFLICT DO NOTHING;  -- second = 0 rows affected
   ```
   This is the backstop that makes a crashed-and-reclaimed attempt safe.

Exit criteria for Stage 0: migration applied + idempotent; claim is disjoint and
fairness-ordered; duplicate admission and duplicate solve are both no-ops.

---

## Stage 1 — Deploy the drain worker (no behavior change yet)

1. Ensure a `worker` (or `all`) role service has `CATHEDRAL_ASYNC_VERIFY_ENABLED=true`.
   Leave `CATHEDRAL_PM_SUBMIT_ASYNC_ENABLED=false` for now.
2. Confirm at startup there is **no** `[verify] WARNING` about an undrained queue
   on the worker role, and that the submit-serving role logs the WARNING only if
   it is the one that would 202 without a worker (it should not yet, since pm-async
   is off).
3. Confirm the drain loop is alive: `GET /v1/admin/synthetic-boolean/submit-metrics`
   → `queue.total_pending == 0`, `queue.public_async_enabled` reflects the public
   flag, `queue.pm_async_enabled == false`.

Rollback: none needed — nothing has changed for miners.

---

## Stage 2 — Shadow-compare window (the critical de-risk)

Turn on pm-async **in shadow**:

```text
CATHEDRAL_SUBMIT_ASYNC_ENABLED=true
CATHEDRAL_PM_SUBMIT_ASYNC_ENABLED=true
CATHEDRAL_PM_ASYNC_SHADOW=true
CATHEDRAL_ASYNC_VERIFY_ENABLED=true   # on the worker/all role
```

Now every `pm-*` submit still gets the **inline** authoritative verdict (miners
see the same `200`/`400` they always did, and payout is unchanged), AND a shadow
twin is queued + re-verified by the worker. Watch for parity:

- Startup logs: `[verify] pm-* async SHADOW mode ON: inline result stays
  authoritative ...`.
- Divergence log line: `[verify] pm_shadow_divergence {...}` — each one is an
  async verdict that disagreed with the inline verdict. **Target: zero.**
- Spot-check the shadow ledger:
  ```sql
  SELECT shadow_status, count(*) FROM per_miner_attempts
  WHERE challenge_kind='per_miner_shadow' GROUP BY shadow_status;
  -- ranked count should track inline accepts; rejects should match inline rejects.
  ```
- Confirm shadow is payout-neutral: the `per_miner_shadow` rows NEVER appear in
  `per_miner_solves` / `agent_submissions` / `eval_runs`, and miner-facing attempt
  /reason stats exclude them (they are filtered out of `_reason_counts_for` and the
  attempt summary).

Hold this window long enough to cover a representative traffic mix (at least one
full challenge epoch). Proceed only when divergence count is **0** over the window.

Rollback: `CATHEDRAL_PM_ASYNC_SHADOW=false` and `CATHEDRAL_PM_SUBMIT_ASYNC_ENABLED=false`.
Inline was authoritative the whole time, so there is nothing to undo.

---

## Stage 3 — Canary one tier

Cut over a single, low-blast-radius slice first. Options (pick one):

- A canary publisher/region serving a subset of miners, with:
  ```text
  CATHEDRAL_PM_ASYNC_SHADOW=false   # cutover for this canary only
  CATHEDRAL_PM_SUBMIT_ASYNC_ENABLED=true
  ```
  while the rest stay in shadow.
- Or keep one tier’s miners on shadow and watch the canary set return `202`s.

Watch on the canary:
- `queue.worker_lag_secs` stays low (the drain keeps up with inbound). If it climbs,
  raise `CATHEDRAL_ASYNC_VERIFY_BATCH` and/or add a worker replica.
- `queue.accepted_per_sec` / `queue.rejected_per_sec` look sane vs the inline rate
  measured in Stage 2.
- Receipts resolve: `GET /v1/agents/receipts/{id}` advances `pending → ranked/rejected`.
- Ledger sanity: distinct-solver counts per challenge are unchanged vs pre-cutover
  (no over- or under-counting).

Rollback: `CATHEDRAL_PM_SUBMIT_ASYNC_ENABLED=false` on the canary → instant revert
to inline. Any `pending` rows already admitted are drained by the worker (they still
pay correctly) or can be re-submitted by miners (idempotent).

---

## Stage 4 — Full cutover

When the canary is clean for a full epoch:

```text
CATHEDRAL_SUBMIT_ASYNC_ENABLED=true
CATHEDRAL_PM_SUBMIT_ASYNC_ENABLED=true
CATHEDRAL_PM_ASYNC_SHADOW=false
CATHEDRAL_ASYNC_VERIFY_ENABLED=true   # worker/all role(s)
```

Post-cutover monitoring (same signals as canary, fleet-wide):
- `queue.total_pending`, `queue.oldest_received_at`, `queue.worker_lag_secs`.
- `queue.accepted_per_sec` / `queue.rejected_per_sec`.
- No `[verify] WARNING` about an undrained queue anywhere.

---

## Rollback (any stage) — flags off => instant revert to inline

```text
CATHEDRAL_PM_SUBMIT_ASYNC_ENABLED=false
CATHEDRAL_PM_ASYNC_SHADOW=false
```

On the next request the `pm-*` submit path is the legacy inline synchronous one,
byte-for-byte. No migration rollback is required (the 0031 columns are additive +
nullable and are simply unused by the inline path). Drain any residual `pending`
rows by leaving a worker running briefly, or let miners re-submit (idempotency
prevents duplicates and the ledger prevents double payout regardless).

## Queue visibility reference

`GET /v1/admin/synthetic-boolean/submit-metrics` (admin token) → `queue`:

```json
{
  "total_pending": 0,
  "oldest_received_at": null,
  "worker_lag_secs": null,
  "by_kind": { "per_miner": { "pending": 0, "oldest_received_at": null, "worker_lag_secs": null } },
  "window_secs": 60.0,
  "accepted_per_sec": 0.0,
  "rejected_per_sec": 0.0,
  "accepted_in_window": 0,
  "rejected_in_window": 0,
  "pm_async_enabled": false,
  "pm_async_shadow": false,
  "public_async_enabled": false
}
```

`worker_lag_secs = now - oldest pending received_at` is the single number to alarm
on: if it grows without bound the drain worker is down or undersized.
