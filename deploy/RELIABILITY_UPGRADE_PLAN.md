# Cathedral SAT — Reliability Upgrade Plan (canonical)

**Status:** Draft for action · **Owner:** Fred · **Scope:** live SAT lane (`api.cathedral.computer`) only — no Bittensor mainnet, no SN39 validator (UID200) changes.
**Last verified against code + live probes:** 2026-06-27.

This is the single source of truth for fixing SAT-lane reliability. It supersedes ad-hoc fixes (PRs #302–#308). Every claim below was checked against the code or a live probe; items not yet confirmed are marked **(hypothesis)**.

---

## TL;DR

Two confirmed problems are hurting live miners:

1. **Read origin is fully down right now** — every read path returns `504` after ~10s (the edge timeout firing because the origin never answers). Miners can't even fetch a challenge. **This is an outage and is the top priority.**
2. **Submit concurrency is hard-capped at 1** in the split deploy script — so the moment two miner submits overlap, the second gets an instant `429 submit_busy_retry`. This is the "submit is unreliable" complaint.

Fix order: **restore reads → raise submit cap → make the bad config un-repeatable in the repo → root-cause the read origin → add real backpressure + alerting.** Raising the submit cap alone does nothing while reads are 504 (you can't submit a solution to a challenge you can't read).

---

## Confirmed problems & evidence

### P1 — Read origin outage (ACUTE)
Live probes, 2026-06-27 (safe GETs only):

| Path | Result |
|---|---|
| `GET /health/ready` | `504` after **9.4s** |
| `GET /v1/synthetic-boolean/current-challenge` | `504` after **10.2s** |
| `GET /v1/synthetic-boolean/active-challenges` | `504` after **11.7s** (earlier reports got a stale `200` from edge cache; now also 504) |

`/health/ready` timing out means the read origin is **not answering at all** (down / crash-looping / OOM / unreachable), not merely slow. Edge timeouts that turn this into a 504 (`deploy/edge-router/worker.mjs:228-238`):
- submit route: `SUBMIT_ORIGIN_TIMEOUT_MS` = **10000**
- read cacheable: `READ_ORIGIN_TIMEOUT_MS` = **4500**
- read health: `READ_HEALTH_ORIGIN_TIMEOUT_MS` = **9000**

PRs #302–#308 ("route edge reads around broken read origin", "extend active board edge cache", "restore edge read origin to split service") were repeated attempts at exactly this and **did not resolve it** — the origin is still down. Root cause is not yet identified (see Phase 2).

### P2 — Submit concurrency hard-capped at 1
The effective submit concurrency (`scaffold/publisher/app.py:233-247`):
```
submit_max_concurrency = min(CATHEDRAL_SUBMIT_MAX_CONCURRENCY, CATHEDRAL_SUBMIT_HARD_CAP)
```
Code defaults: `MAX_CONCURRENCY=24`, `HARD_CAP=8`. **The split deploy overrides `HARD_CAP=1` and `WEB_CONCURRENCY=1`:**
- `deploy/railway-split.ps1:178` → `CATHEDRAL_SUBMIT_HARD_CAP=1`
- `deploy/railway-split.ps1:180` → `WEB_CONCURRENCY=1`
- `deploy/ROLE_SPLIT_RUNBOOK.md:78` → documents the same `=1`

There are **two** non-blocking gates a submit must pass, each a `BoundedSemaphore(submit_max_concurrency)`:
1. ASGI pre-body backpressure middleware — `scaffold/publisher/app.py:631-700` (own `_submit_gate`, app.py:646)
2. FastAPI dependency `_submit_slot` — `scaffold/publisher/app.py:324-345`, wired at `agents_submit` `app.py:3369-3381`

Both call `gate.acquire(blocking=False)` and **immediately** raise `429 submit_busy_retry` (header `X-Cathedral-Rejection-Reason: submit_busy_retry`) when full — they do **not** queue. With cap 1, one in-flight submit blocks all others. This reproduces even for an unauthenticated dummy POST because the gate runs *before* auth/solution validation. Confirmed cause of the complaint.

### Background — why both gates exist
The non-blocking gates protect the Postgres origin from being swamped by heavy submit/CNF work. The design is correct; the **value (1) is wrong**. The goal is "shed load past capacity," not "serialize all miners to one at a time."

---

## The plan

### Phase 0 — Immediate triage (live; requires Railway access)
> Cannot be done from the dev WSL environment — no `railway` CLI / token there. Must run wherever Railway deploys are issued.

**0a. Restore the read origin (do this first — it's the outage).**
- Inspect the read service in Railway: is it crashed, OOM-killed, health-failing, or undeployed? `/health/ready` → 504 points to the process not answering.
- Confirm the read service domain (`read.cathedral.computer`, `railway-split.ps1:173`) is actually attached and the edge worker `READ_ORIGIN` points at it.
- Restart / redeploy the read service. Re-probe `/health/ready` until it returns `200`.

**0b. Raise submit concurrency** on the submit service:
```
CATHEDRAL_SUBMIT_HARD_CAP=8
CATHEDRAL_SUBMIT_MAX_CONCURRENCY=24
WEB_CONCURRENCY=2
CATHEDRAL_PM_READ_HARD_CAP=128
CATHEDRAL_THREADPOOL_TOKENS=32
CATHEDRAL_PG_POOL_MAX=32
```
Then confirm via `GET /v1/admin/synthetic-boolean/submit-metrics` (`app.py:306-322`): `hard_cap` should read 8, and `by_reason.submit_busy_retry` should fall. Do **not** jump to unlimited — step to `HARD_CAP=16` only if DB/CPU headroom is confirmed.

### Phase 1 — Make the bad config un-repeatable (repo; side branch, NOT main)
The root of P2 is that the deploy script bakes in `=1`. Until that's fixed, any re-run of the split script silently re-introduces the choke.
- Edit `deploy/railway-split.ps1`: submit service → `HARD_CAP=8`, add `MAX_CONCURRENCY=24`, `WEB_CONCURRENCY=2`, `PM_READ_HARD_CAP=128`, `THREADPOOL_TOKENS=32`, `PG_POOL_MAX=32`.
- Edit `deploy/ROLE_SPLIT_RUNBOOK.md` to match and explain *why* (link this doc).
- Land on a side branch, reviewed; do not merge to `main` without Fred's sign-off.

### Phase 2 — Root-cause the read origin (the actually-unsolved bug)
P0a restarts it; this prevents recurrence. Investigate, in order:
- **Is it OOM / crash-looping?** Check Railway memory/restart counts on the read service. `PG_POOL_MAX=16` × `WEB_CONCURRENCY=2` and threadpool sizing vs container memory.
- **Is Postgres the bottleneck?** Slow/again-saturated DB makes `/health/ready` hang. Check connection count vs `PG_POOL_MAX`, slow queries on the active-board read path.
- **Is the cursor scan starving the hot path?** PR b55ac1e ("stop recent-feed cursor scans from starving the read hot path") suggests a known offender — verify it's deployed and effective.
- **Edge cache fallback:** ensure `active-challenges` serves stale-while-revalidate so a brief origin blip degrades to stale data, not 504 (it already does for that route; extend to `current-challenge` if safe).

### Phase 3 — Replace instant-429 with bounded queueing (design hardening)
Non-blocking gates convert *any* overlap into a reject. Soften:
- Give `_submit_slot` / ASGI gate a short bounded wait (e.g. `acquire(timeout=0.25–0.5s)`) before 429, so brief bursts queue instead of bounce. Keep a hard ceiling so true overload still sheds.
- Ensure `Retry-After` (already set to `1`) plus client jitter/backoff is documented for miners.
- Consider separating the ASGI gate and dependency gate sizes so a submit doesn't consume two slots for one request.

### Phase 4 — Observability & SLOs
- **Alerts:** page when `/health/ready` ≠ 200 for >60s, or `submit_busy_retry` rate > X/min, or read p95 > edge timeout.
- **Dashboard:** surface `/v1/admin/synthetic-boolean/submit-metrics` (`max_concurrency`, `hard_cap`, `by_reason`, `recent`) and read-origin health continuously.
- **SLOs (proposed):** read availability ≥ 99.5%; submit `429` rate < 1% under normal load; challenge-fetch p95 < 2s.

### Phase 5 — Miner-facing error contract
Publish what each error means and the correct client action, so complaints separate "server degraded" from "client bug":

| HTTP / reason | Meaning | Miner action |
|---|---|---|
| `429 submit_busy_retry` | submit service saturated | retry with jitter/backoff (honor `Retry-After`) |
| `409 challenge_not_active` | stale/retired challenge | refetch active challenge + CNF |
| `409 challenge_already_locked` | locked/retired during a race | refetch |
| `409 already_solved` | this hotkey already solved it | move to next challenge |
| `401 invalid hotkey signature` | signing payload mismatch | fix client signing |
| `400 solution_*` | invalid DIMACS/solver output | fix solution format |
| `404` on CNF URL | CNF token expired/invalid | refetch active-cnf |
| `504 *_origin_unavailable` | origin/edge timeout | **server-side** — not a miner bug |

---

## Target configuration reference

| Variable | Submit svc (now) | Submit svc (target) | Read svc | Notes |
|---|---|---|---|---|
| `CATHEDRAL_SUBMIT_HARD_CAP` | **1** | **8** (→16 if headroom) | n/a | the choke |
| `CATHEDRAL_SUBMIT_MAX_CONCURRENCY` | (24 default) | 24 | n/a | ceiling; effective = min(this, hard_cap) |
| `WEB_CONCURRENCY` | **1** | **2** | 2 | uvicorn workers |
| `CATHEDRAL_PM_READ_HARD_CAP` | **1** | 128 | 128 (default) | per-miner read gate |
| `CATHEDRAL_THREADPOOL_TOKENS` | 16 | 32 | 32 | |
| `CATHEDRAL_PG_POOL_MAX` | 16 | 32 | 16 | watch total DB connections across replicas |
| `CATHEDRAL_CNF_TOKEN_SECRET` | shared | shared (identical every replica) | shared | required for CNF token validation |
| `CATHEDRAL_SERVICE_ROLE` | submit | submit | read | |

Edge timeouts (`worker.mjs`): submit 10s, read 4.5s, read-health 9s — tune only after origin is healthy.

---

## Verification & rollback

**Verify after Phase 0:**
1. `GET /health/ready` → `200`.
2. `GET /v1/synthetic-boolean/current-challenge` → `200` with a live challenge.
3. `GET /v1/admin/synthetic-boolean/submit-metrics` → `hard_cap: 8`, `submit_busy_retry` trending down.
4. A real miner round completes submit without `429` under normal load.

**Rollback:** every Phase 0 change is a single env-var revert + redeploy. If raising the cap stresses Postgres (connection exhaustion, CPU), step `HARD_CAP` back to 4 and `PG_POOL_MAX` down before reverting fully. Read-origin restart is non-destructive.

---

## Risks & non-goals
- **Don't unbounded the gate.** Removing the cap trades 429s for an origin meltdown. The fix is "right-sized + queue briefly," not "off."
- **Watch DB connections** when raising `PG_POOL_MAX` × replicas × `WEB_CONCURRENCY` — keep total under Postgres `max_connections`.
- **Non-goals:** no Bittensor mainnet changes; no SN39 validator (UID200) changes; no merge to `main` without sign-off; this plan does not touch the Compute/Agent lanes.

---

## Status checklist
- [ ] P0a — read origin restored (`/health/ready` = 200)
- [ ] P0b — submit cap raised to 8, verified via submit-metrics
- [ ] P1 — deploy script + runbook fixed on a side branch
- [ ] P2 — read-origin root cause identified & documented
- [ ] P3 — bounded-queue backpressure shipped
- [ ] P4 — alerts + dashboard live
- [ ] P5 — miner error contract published
