# Cathedral Reliability — Session Summary & Go-Live Checklist

**Date:** 2026-06-27 · **Branch:** `reliability-integrated` (off `reliability-upgrade-plan` tip `6516257`, HEAD `79a1a29`)
**Status:** Code-complete, adversarially reviewed, full suite green for all reliability changes. **Nothing deployed.** Needs Fred's review + explicit OK before any live impact.

This session had two halves: (1) a **live incident** that was diagnosed and fixed, and (2) the **full reliability plan**, implemented on a branch and gated off. The canonical plan is `deploy/RELIABILITY_UPGRADE_PLAN.md`.

---

## Part 1 — Live incident (RESOLVED today)

**Symptom:** `api.cathedral.computer/v1/validator/weights/next` returned `504` for ~4.5h. On-chain, **6 of 11 SN39 validators went stale** (including our UID200, ~3h dark).

**Root cause:** `/v1/leaderboard/recent` ran 30–46s per call (no statement timeout), and with the read service on `WEB_CONCURRENCY=1` it exhausted the Postgres pool. The Cloudflare edge routed the **validator weight feed** to that jammed read service, so validators couldn't fetch the signed vector and stopped setting weights. The vector itself was healthy the whole time (publisher served it in 0.27s).

**Fix applied live (already in production):**
- Set `CATHEDRAL_PG_STATEMENT_TIMEOUT_MS=4000` on the read service (Railway) — bounds the slow query (this env *is* wired: `store.py:909`).
- Deployed a **Cloudflare worker `cathedral-weights-failover`** serving the weight feed from the healthy **publisher** origin, with a **KV last-known-good** fallback (`source: stale_fallback`). Repointed the two weight-feed routes to it; left `cathedral-edge-router` untouched.

**Outcome:** UID200 resumed setting weights ~1 min after the fix; **10/11 validators fresh** within ~25 min. (UID204 is a long-dead validator, unrelated.)

> **Operational fact worth keeping:** most SN39 validators consume the Cathedral weight feed, so a feed outage stalls most of the subnet's weight-setting — not just ours. The feed is a single point of failure, now backed by the KV last-known-good.

---

## Part 2 — Reliability plan implemented (on `reliability-integrated`, NOT deployed)

**25 files, +4761/−24.** Plan doc intact, no CRLF churn, merge-base == plan tip.

**Test status:** full suite = **461 passed, 8 failed**. All 8 failures are pre-existing, under `game/arena/tests/` (corpus-data assertions), reproduce identically on the bare plan base, and the integration touches **zero** `game/arena/` files. **All 88 reliability tests pass**; both Cloudflare worker test suites pass.

**Default-off proven empirically:** built the app with all `CATHEDRAL_*` env vars stripped and submitted a real signed solution → **byte-for-byte legacy synchronous `200`** (no `202`, no behavior change). Every new behavior is gated.

### Per-slice: what changed · gating · how to take it live

| Slice | What it adds | Gating (off by default) |
|---|---|---|
| **weights-failover** (`deploy/edge-router/weights-failover/`) | Versioned copy of the live failover worker + KV last-known-good + edge cache + tests + `wrangler.toml` | Routes commented out in `wrangler.toml`; the live worker is already deployed (this captures it into the repo) |
| **submit-redesign** (`app.py`, `store.py` DDL, `submit_admission.py`, `verify_worker.py`) | Durable `202` admission + idempotency + async verification worker + bounded backpressure (Phases 3/4/5) | `CATHEDRAL_SUBMIT_ASYNC_ENABLED` / `CATHEDRAL_ASYNC_VERIFY_ENABLED` default **False**; legacy sync `200` unchanged when unset; startup WARN if async-on but no drain worker |
| **observability + release-gate** (`app.py` 5xx middleware, `health_thresholds.py`, `scripts/validator_release_gate.py`, `deploy/observability/ALERTS.md`) | Real 5xx counting, validator-health endpoint, mainnet release-gate script, alert/SLO defs (Phase 7) | Read-only/additive; no behavior change |
| **board-tiering** (`deploy/edge-router/board-failover/`) | Worker to protect cheap board reads from the slow leaderboard, honoring the read/submit role split | Routes commented out; nothing binds until cutover |
| **deploy-config-hardening** (`railway-split.ps1`, `ROLE_SPLIT_RUNBOOK.md`, `app.py` guard) | Safe defaults incl. `CATHEDRAL_PG_STATEMENT_TIMEOUT_MS`; startup warning if a read service boots without it (Phase 1) | Config + warning only |
| **retention-storage** (`retention.py`) | Retention/compaction job, dry-run by default (Phase 6) | Dry-run; no destructive deletes without explicit enable |
| **miner-error-contract** (`docs/MINER_ERROR_CONTRACT.md`) | Published miner-facing error table + alignment test pinned to source | Doc only |

---

## Deferred-to-live checklist (needs Postgres / Cloudflare / finney — your call, gated)

These could not be verified in-session and must happen at go-live, in order:

1. **Submit-async (highest care):** validate the admission/claim SQL against a **real Postgres** (`FOR UPDATE SKIP LOCKED`, `ON CONFLICT DO NOTHING`); deploy a **worker-role service** running the verify worker and confirm it drains pending → ranked **before** flipping `CATHEDRAL_SUBMIT_ASYNC_ENABLED` (otherwise `202` receipts never pay out).
2. **weights-failover:** byte-match the repo `worker.js` vs the live deployed Cloudflare script (`shasum -a 256`); record the hash in the README. Confirm the restored edge cache behaves as intended on the live zone.
3. **board-tiering:** run a live read-origin smoke matrix (every routed path returns 200 from its configured origin) before un-commenting routes.
4. **release-gate:** run `scripts/validator_release_gate.py` against **live finney** once to validate the chain mapping; pin the bittensor version.

## Rollback
- **Live failover worker (Part 1):** repoint the two weight-feed routes back to `cathedral-edge-router` and delete the worker (KV can stay).
- **Branch:** nothing is merged to `main`; the integration branch can simply be abandoned. Each slice's live toggle is a single env var / route revert.

## What I need from you
- Review `reliability-integrated` (this branch). It is **not** merged to `main` and **nothing is deployed**.
- Give explicit OK before any go-live step above. I will not flip a flag, repoint a route, or merge to main without it.
