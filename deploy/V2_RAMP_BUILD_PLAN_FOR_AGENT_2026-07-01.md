# V2 → 10% weight ramp — build plan (self-contained, for an executor agent)

**Repo:** `/mnt/c/Users/fred/code/cathedral`  ·  **Branch base:** `feat/solution-manifest-v2`
**You are an executor.** Do the work packages below in order. Each is independently testable. Do
NOT change the V1 real-reward path except W4, and W4 is additive behind a flag that defaults to 0.
Keep V2 data in the isolated V2 Postgres (`CATHEDRAL_V2_DATABASE_URL` → postgres-glua), never the V1
DB. Run `PYTHONPATH=. .venv/bin/python -m pytest` for tests. Use `.venv/bin/python`.

---

## Context you need (verified live 2026-07-01)

- Real weights come from `scaffold/publisher/weights.py` (`cached_vector`), served at
  `/v1/validator/weights/next`. Live policy `v4_proportional_24h_window`, **burn 0%**, 293 miners,
  finney netuid 39. This is authoritative and must not regress.
- A V2 verify+score pipeline already exists in `scaffold/publisher/v2_pipeline.py`
  (`process_batch`, `verify_one`, `score_totals`, `build_shadow_weight_vector`, `audit_bundle`) and
  feeds the **shadow** vector at `/v2/validator/weights/next` (stamped `shadow:true`, nothing
  consumes it). It writes to `v2_submit_events` / `solution_manifests` in the V2 DB.
- The **lean ingress** (`scaffold/publisher/v2_lean_ingress.py`, standalone app) accepts tiny signed
  bitset submits into a local SQLite (`submit_events_local`) and returns `received`. Its
  `flushed_at_iso` column is always NULL — **nothing drains it** (no flusher). It is not deployed.

## Two live problems this ramp must actually solve (don't just move them)

1. `cathedral-submit` logs show `psycopg2.pool.PoolError: connection pool exhausted` under load —
   synchronous Postgres in the submit hot path. The lean ingress removes PG from *accept*; good.
2. The V2 verify worker logs `[v2_verify] singleton_lock_held_elsewhere` continuously with ~66
   verified/24h — verify throughput is unproven and is the real bottleneck. Fixing/observing it is
   W1 and is a prerequisite for everything downstream.

**Design principle:** the lean ingress is only the cheap ACK. The load moves to flush+verify (W2/W1).
The system scales only if **verify-rate ≥ accept-rate**. W3 is the test that proves it. Do not treat
the replay-spam test as proof of scale — it exercises neither flush nor verify.

---

## W1 — Make the V2 verifier reliable and observable  *(prerequisite bottleneck)*

**Goal:** exactly one verifier runs; its rate and backlog are measurable.

**Locate:** grep `singleton_lock_held_elsewhere`, `v2_verify`, `CATHEDRAL_V2_VERIFY_WORKER_ENABLED`,
`VERIFY_LOCK`, `advisory_lock` in `scaffold/publisher/` (likely `app.py` startup + a verify loop that
calls `v2_pipeline.process_batch`).

**Do:**
1. Diagnose why the lock is perpetually "held elsewhere." Most likely cause: the verify worker is
   spawned per web worker (WEB_CONCURRENCY>1) or per replica, and one holder is idle/starved. Confirm
   the replica/worker count for `cathedral-v2-beta` and make the verifier a **single dedicated loop**
   (one holder that actually runs `process_batch`), not one-per-web-worker. If the pg advisory lock
   can be orphaned, ensure it's tied to a connection that's health-checked and re-acquired.
2. Add a metrics endpoint `GET /v2/verify/metrics` (JSON, no-store) on the v2-beta app exposing:
   `verified_last_60s`, `verify_rate_per_sec` (EWMA), `pending_count` (rows awaiting verify),
   `oldest_pending_age_secs`, `rejected_last_60s`, `lock_held_by_self` (bool), `last_batch_at`.
3. Emit one structured log line per batch: `n_verified`, `n_rejected`, `batch_ms`.

**Env:** reuse existing `CATHEDRAL_V2_VERIFY_*`. Add none unless needed.
**Tests:** unit test that with a seeded set of N pending rows, one loop verifies all N and
`verify/metrics` reports nonzero rate and zero pending after drain.
**Acceptance:** on v2-beta, `/v2/verify/metrics` shows `lock_held_by_self=true` on exactly one
worker and a nonzero `verify_rate_per_sec` when pending>0; the `singleton_lock_held_elsewhere` spam
stops or is throttled to debug level.
**Out of scope:** changing the verification algorithm.

---

## W2 — Lean ingress → V2 Postgres flusher  *(drains the local log into the existing pipeline)*

**Goal:** move `received` rows from the ingress SQLite into `v2_submit_events` so the W1 verifier
scores them. Reuse the existing verify/score pipeline — do NOT build a second verifier.

**Locate:** `LeanIngressStore` schema in `v2_lean_ingress.py` (columns: `receipt_id, idempotency_key,
miner_hotkey, challenge_id, card_id, epoch, tier, seq, cnf_sha256, assignment_encoding,
assignment_sha256, assignment_b64, status, received_at_iso, submitted_at, signature, submit_token_id,
event_json, event_sha256, flushed_at_iso`). Read the `v2_submit_events` migration (grep
`0039_v2_submit_events`) to get its exact columns + the status value the W1 verifier consumes.

**Do (new module `scaffold/publisher/v2_lean_flusher.py`):**
1. `flush_batch(ingress_store, v2_store, *, batch_size=200) -> {flushed, skipped, errors}`:
   - `SELECT * FROM submit_events_local WHERE flushed_at_iso IS NULL ORDER BY received_at_iso LIMIT ?`.
   - For each, map fields → `v2_submit_events` insert with `source='bitset'` and the status the
     verifier picks up (match the existing bitset admission path, `admit_verified_event` /
     `submit_bitset_v2` in app.py, for column parity — the verifier regenerates the CNF from
     (hotkey,epoch,tier,seq), so no CNF blob needed).
   - Insert idempotently (`ON CONFLICT (idempotency_key) DO NOTHING`); treat conflict as already-flushed.
   - On success, `UPDATE submit_events_local SET flushed_at_iso=? WHERE receipt_id=?`.
   - Never delete local rows in Phase 1 (keep for audit); a later retention pass truncates flushed rows.
2. After a successful batch, run `PRAGMA wal_checkpoint(TRUNCATE)` on the ingress DB to reclaim WAL.
3. A loop `run_flusher_loop(interval=2.0)` guarded by env `CATHEDRAL_V2_INGRESS_FLUSHER_ENABLED`
   (default false). The flusher runs as its own process/loop, single-instance (advisory lock or the
   ingress process-lock), NOT inside the ACK request path.
4. Wire the flusher metrics into `/v2/ingress/metrics`: `flushed_total`, `flush_rate_per_sec`,
   `unflushed_events` (already present), `flush_errors_last_60s`.

**Env:** `CATHEDRAL_V2_INGRESS_FLUSHER_ENABLED=false`, `CATHEDRAL_V2_INGRESS_FLUSH_BATCH=200`,
`CATHEDRAL_V2_INGRESS_FLUSH_INTERVAL_SECS=2`.
**Tests:** seed ingress SQLite with K signed rows → run `flush_batch` → assert K rows in a fake
`v2_store`, all local rows have `flushed_at_iso` set, re-running flush is a no-op (idempotent), and a
duplicate `idempotency_key` doesn't create a second row.
**Acceptance:** end-to-end local: submit → ingress `received` → flusher moves it → W1 verifier marks
it `verified|rejected` with a real `weighted_score`.
**Out of scope:** the in-place rejected-row retry rework (tracked separately as deep-review M7).

---

## W3 — Unique-submit load test harness  *(the test that actually de-risks scale)*

**Goal:** prove `verify-rate ≥ accept-rate` under realistic unique traffic, and that this traffic
does NOT reintroduce V1 pool exhaustion.

**Do (new script `scripts/v2_unique_submit_loadtest.py`):**
1. Args: `--challenge-base`, `--submit-base`, `--miners N`, `--challenges-per-miner M`,
   `--rate R` (submits/sec target), `--duration S`, `--metrics-token`.
2. Generate N synthetic keypairs (dev URIs), fetch real per-miner challenges + tokens from
   `--challenge-base` (requires those hotkeys on the mint allowlist), solve with the same solver the
   miner E2E uses, and POST **unique** bitset submits (not replays) at rate R.
3. Poll `/v2/ingress/metrics` and `/v2/verify/metrics` throughout; record time series of:
   `accept_rate`, `unflushed_events`, `flush_rate`, `verify_rate`, `oldest_pending_age`,
   ingress `/health/ready` status, and (from the V1 side) whether `submit` pool errors appear.
4. Print a PASS/FAIL summary against the acceptance criteria below and dump the series to JSON.

**Acceptance (this is the go/no-go for ramping):**
- Sustained `verify_rate ≥ accept_rate` over the run (backlog not monotonically growing).
- `unflushed_events` stays < 50% of `MAX_UNFLUSHED_EVENTS`; `oldest_pending_age` bounded.
- ingress `/health/ready` stays 200; no SQLite busy/500.
- **No new `psycopg2.pool.PoolError` on the V1 submit service** during the run (confirms the traffic
  is truly off the V1 hot path).
**Out of scope:** driving real registered miners; this uses synthetic allowlisted keys.

---

## W4 — Weight blend composer  *(the ramp mechanism; default OFF, additive)*

**Goal:** allow `final = (1-f)·V1 + f·V2_verified_registered`, `f` from env, default 0 → zero behavior
change until explicitly enabled.

**Locate:** `scaffold/publisher/weights.py` `cached_vector` (and the `/v1/validator/weights/next`
handler in `app.py`, grep `validator/weights/next`). Reuse `v2_pipeline.score_totals` /
`build_shadow_weight_vector` for the V2 side.

**Do:**
1. Read `f = clamp(float(CATHEDRAL_V2_WEIGHT_FRACTION or 0), 0, 0.5)`. If `f == 0`, return the V1
   vector unchanged (early return — proves default is a no-op).
2. Build `v2_vec` from verified V2 scores over the same 24h window, **filtered to hotkeys registered
   on SN39 with min stake** (use the metagraph the publisher already loads for V1 — grep
   `metagraph_hotkeys` in weights.py). Drop unregistered/Sybil hotkeys entirely.
3. Normalize V1 and V2 vectors over the shared UID set, combine `(1-f)*v1 + f*v2`, renormalize.
4. **Fail-safe:** if the V2 vector is missing / older than its `expires_at` / empty after the
   registration filter / raises → log a WARN and return the pure V1 vector (never zero a miner, never
   fail the weight set on V2 trouble). Add a `v2_blend` block to the vector's `policy_metadata`
   (`fraction`, `v2_miner_count`, `fallback_reason` or null) for observability.
5. Expose the effective fraction + fallback state in the served vector metadata so the ramp is auditable.

**Env:** `CATHEDRAL_V2_WEIGHT_FRACTION` (default `0`). Kill switch = set back to `0`.
**Tests:**
- `f=0` → served vector byte-identical to V1-only (no V2 read happens).
- `f=0.1` with a fake V2 vector of registered hotkeys → each blended weight equals the hand-computed
  convex combination; sum ≈ 1.
- V2 vector stale/empty/raises → falls back to pure V1, `fallback_reason` set, no exception.
- A V2 score for an UNREGISTERED hotkey is excluded from the blend.
**Out of scope:** actually setting a nonzero fraction in prod (that's an operator ramp decision, and
requires validator coordination for Yuma consensus — see the grounded plan doc).

---

## W5 — Sybil/registration gate  *(must land before any nonzero fraction)*

**Goal:** only registered SN39 hotkeys (min stake) can earn V2 score.

**Do:**
1. At mint (`app.py` `v2_per_miner_challenges`, grep it): gate token issuance behind a
   registration+min-stake check using the metagraph, controlled by
   `CATHEDRAL_V2_MINT_REQUIRE_REGISTERED` (default false so tests keep working; the existing
   `CATHEDRAL_V2_SUBMIT_TOKEN_ALLOWLIST` remains the test stopgap).
2. Belt-and-suspenders: W4 already filters the V2 vector to registered hotkeys at compose time, so a
   token leak can't pay an unregistered key. Keep both.
**Tests:** unregistered hotkey → mint refused when the flag is on; registered → allowed.
**Out of scope:** stake-threshold tuning (make it an env `CATHEDRAL_V2_MIN_STAKE`, default 0).

---

## Definition of done for "10% ready" (not "10% on")

1. W1 verifier proven single-instance with observable rate.
2. W2 flusher moving lean rows into the scored pipeline, idempotent, WAL reclaimed.
3. W3 load test PASSES its acceptance (verify ≥ accept; no V1 pool errors).
4. W4 blend merged, `f=0` a proven no-op, fail-safe fallback tested.
5. W5 registration gate available.
Only then does an operator ramp `CATHEDRAL_V2_WEIGHT_FRACTION` 0 → 0.01 → 0.05 → 0.10, watching per
epoch, with validator coordination so Yuma consensus doesn't clip the V2 slice to zero.

## Guardrails (do not violate)
- No change to V1 scoring/weights except the additive W4 blend behind `CATHEDRAL_V2_WEIGHT_FRACTION`
  default 0.
- V2 writes stay in the V2 Postgres (`CATHEDRAL_V2_DATABASE_URL`); never the V1 DB.
- New loops (verifier, flusher) are single-instance and never run inside a request handler.
- Every new default-off flag must leave existing tests green with the flag unset.
- Don't print/commit secrets; run a secret scan before committing.
