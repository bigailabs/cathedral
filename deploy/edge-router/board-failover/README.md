# Cathedral board-failover edge worker

Purpose: protect the cheap miner **board** reads (Tier-1) the same way the
validator weight feed (Tier-0) was protected — by serving them from a healthy
publisher origin that is independent of the slow leaderboard read service —
**while honouring the read/submit role split**.

## The problem this closes

The cheap miner board reads are cheap snapshot queries served by the **read**
role:

- `GET /v1/synthetic-boolean/active-challenges`
- `GET /v1/synthetic-boolean/challenge-broadcast`
- `GET /v1/synthetic-boolean/current-challenge`
- `GET /v1/synthetic-boolean/per-miner/status`
- `GET /v1/synthetic-boolean/per-miner/summary`

They were returning `504` because they shared one read service with the
expensive `GET /v1/leaderboard/recent` cursor scan (and `/v1/leaderboard/top`).
A single slow leaderboard query saturating that service starved the cheap board
reads. This maps to the plan's read tiering:

| Tier | Surface | Requirement |
| --- | --- | --- |
| 0 critical | `/v1/validator/weights/next` | must always answer (already protected) |
| **1 important** | **active-board / challenge-broadcast / current-challenge / per-miner status+summary** | **should work; this worker protects it** |
| 2 non-critical | `/v1/leaderboard/recent`, `/v1/leaderboard/top` | best-effort; stays on the existing read service |

## Read vs submit role split (critical)

The board reads above are **read-role** and live on the read origin. A separate
set of paths are **submit-role** and are served in prod by the **submit**
origin:

- `GET /v1/synthetic-boolean/active-cnf`
- `GET /v1/synthetic-boolean/per-miner/challenges`
- `GET /v1/synthetic-boolean/per-miner/cnf`
- `GET /v1/challenges/*` (private challenge fetch)
- `POST /v1/agents/submit`

These are **not** board reads. A read origin returns `404` for them. Because
board-failover, when attached, owns the `api.cathedral.computer` hot paths, it
must route the submit-role paths to `SUBMIT_ORIGIN`
(`https://submit.cathedral.computer`) — **never** to `BOARD_ORIGIN` /
`LEADERBOARD_ORIGIN`. This matches the role split enforced by the main edge
router (`../worker.mjs`, `SUBMIT_GET_PATHS` / `SUBMIT_GET_PREFIXES` /
`SUBMIT_POST_PATHS`).

## What this worker does

It splits reads by **origin**, not just by cache, and routes by role:

```text
BOARD (Tier-1 read) GETs       -> BOARD_ORIGIN        (healthy read/publisher origin)
/v1/leaderboard/recent         -> LEADERBOARD_ORIGIN  (existing read service, isolated)
/v1/leaderboard/top            -> LEADERBOARD_ORIGIN  (existing read service, isolated)
SUBMIT-role paths              -> SUBMIT_ORIGIN       (submit origin; never a read origin)
```

The slow leaderboard stays on its own origin and can saturate only itself. It
can no longer take the board down with it. The board origin is the publisher
service that already serves these snapshots from in-memory caches. The submit
origin keeps serving CNF/submit so board failover does not break per-miner CNF
delivery.

This worker is **separate** from `../worker.mjs` on purpose: it can be attached
to only the board/submit hot paths during an incident and detached on rollback
without touching the main edge router. It does **not** cache — the main router
already owns edge caching for these paths; this worker's single job is origin
isolation.

This worker does **not** claim `/v1/leaderboard/recent` or `/v1/leaderboard/top`
as routes. Those stay on the main edge router / read service. Do not add them
here, or the isolation is defeated.

`OPTIONS` preflight is answered locally (`204` + CORS headers) and never hits an
origin, mirroring the main router so browser submit keeps working under failover.

## NEVER attach as catch-all (default-deny is path-scoped on purpose)

This worker **default-denies**: any path it does not explicitly classify
(`worker.js` `classifyBoardRequest` → tier `"none"`) returns `404
route_not_served_by_board_failover`. That behaviour is **safe only because the
routes in `wrangler.toml` are exact hot paths** (board reads, leaderboard,
submit-role). Each attached route hands this worker exactly one known path.

> **Do NOT attach this worker to a catch-all route** such as
> `api.cathedral.computer/*` (or any broad `*` pattern). A catch-all would make
> the default-deny `404` **every unrelated API path** that flows through it —
> `/health`, `/.well-known/cathedral-jwks.json`, the Tier-0 validator weight feed
> `/v1/validator/weights/next`, `/v1/admin/*`, `/v1/leaderboard/explain`, and any
> future endpoint — taking the whole surface down. The blast radius of a misattach
> here is the entire API, not just the board.

Only ever uncomment the **per-path** patterns listed in `wrangler.toml`. If you
later need this worker to front a broader prefix, change the default-deny branch
to pass unknown paths through to a configured origin first — do not point a
catch-all at the current default-deny worker.

## Environment

| Var | Default | Meaning |
| --- | --- | --- |
| `BOARD_ORIGIN` | `https://read.cathedral.computer` | Healthy publisher (read) origin for Tier-1 board reads. Point at a publisher/read service that is **not** carrying the slow leaderboard load. |
| `LEADERBOARD_ORIGIN` | `https://read.cathedral.computer` | Existing read service. The slow leaderboard hog stays here, isolated. |
| `SUBMIT_ORIGIN` | `https://submit.cathedral.computer` | Submit service. Submit-role paths (active-cnf, per-miner/challenges, per-miner/cnf, `/v1/challenges/*`, POST submit) go here. A read origin 404s them. |
| `BOARD_ORIGIN_HOST` | (empty) | Optional `Host` override for `BOARD_ORIGIN`. |
| `LEADERBOARD_ORIGIN_HOST` | (empty) | Optional `Host` override for `LEADERBOARD_ORIGIN`. |
| `SUBMIT_ORIGIN_HOST` | (empty) | Optional `Host` override for `SUBMIT_ORIGIN`. |
| `BOARD_ORIGIN_RESOLVE_OVERRIDE` | (empty) | Optional Cloudflare `resolveOverride` for `BOARD_ORIGIN`. |
| `LEADERBOARD_ORIGIN_RESOLVE_OVERRIDE` | (empty) | Optional `resolveOverride` for `LEADERBOARD_ORIGIN`. |
| `SUBMIT_ORIGIN_RESOLVE_OVERRIDE` | (empty) | Optional `resolveOverride` for `SUBMIT_ORIGIN`. |
| `BOARD_ORIGIN_TIMEOUT_MS` | `4500` | Tight timeout: board reads are cheap and must answer fast. |
| `LEADERBOARD_ORIGIN_TIMEOUT_MS` | `9000` | Looser timeout for the slow leaderboard path. |
| `SUBMIT_ORIGIN_TIMEOUT_MS` | `10000` | Submit timeout (verification can be slower than a snapshot). |

The board and leaderboard origins only buy isolation if they are **different
services** (or at least the leaderboard load has been moved off `BOARD_ORIGIN`).
If both point at the same saturated service, nothing improves. The durable fix
is a dedicated board/publisher origin distinct from whatever serves the
leaderboard.

## Exact routes to repoint (cutover)

Do this only after `BOARD_ORIGIN` (and, if you route submit here,
`SUBMIT_ORIGIN`) is confirmed green:

```text
https://read.cathedral.computer/v1/synthetic-boolean/current-challenge   -> 200
https://read.cathedral.computer/v1/synthetic-boolean/active-challenges   -> 200
https://submit.cathedral.computer/v1/synthetic-boolean/per-miner/cnf?challenge_id=... -> 200/422
```

1. Set the worker vars (or edit `[vars]` in `wrangler.toml`), pointing
   `BOARD_ORIGIN`/`LEADERBOARD_ORIGIN` at the read service(s) and `SUBMIT_ORIGIN`
   at the submit service.
2. Uncomment the routes in `wrangler.toml`. These are the **only** routes this
   worker should ever own — board reads go to the read origin, submit-role paths
   to the submit origin:

   ```text
   # read-role -> BOARD_ORIGIN
   api.cathedral.computer/v1/synthetic-boolean/active-challenges*
   api.cathedral.computer/v1/synthetic-boolean/challenge-broadcast*
   api.cathedral.computer/v1/synthetic-boolean/current-challenge*
   api.cathedral.computer/v1/synthetic-boolean/per-miner/status*
   api.cathedral.computer/v1/synthetic-boolean/per-miner/summary*

   # submit-role -> SUBMIT_ORIGIN
   api.cathedral.computer/v1/synthetic-boolean/active-cnf*
   api.cathedral.computer/v1/synthetic-boolean/per-miner/challenges*
   api.cathedral.computer/v1/synthetic-boolean/per-miner/cnf*
   api.cathedral.computer/v1/challenges/*
   api.cathedral.computer/v1/agents/submit*
   ```

   Plus the legacy `/api/cathedral/...` form of the synthetic-boolean paths
   (also in `wrangler.toml`).
3. Deploy:

   ```bash
   npx wrangler deploy
   ```

   A Cloudflare route is most-specific-match: attaching these exact paths to
   `cathedral-board-failover` takes them off `cathedral-edge-router` for those
   paths only. `/v1/leaderboard/recent`, `/v1/leaderboard/top`,
   `/v1/validator/weights/next`, and everything else stay on the main router
   untouched.
4. Verify the split is live:

   ```bash
   curl -sS -D - -o /dev/null https://api.cathedral.computer/v1/synthetic-boolean/current-challenge | grep -i x-cathedral-board-tier
   # expect: x-cathedral-board-tier: board

   curl -sS -D - -o /dev/null "https://api.cathedral.computer/v1/synthetic-boolean/per-miner/cnf?challenge_id=pm-test" | grep -i x-cathedral-board-tier
   # expect: x-cathedral-board-tier: submit   (served by SUBMIT_ORIGIN, NOT 404)
   ```

   The leaderboard must NOT carry the board tier header (it is not routed here):

   ```bash
   curl -sS -D - -o /dev/null https://api.cathedral.computer/v1/leaderboard/recent | grep -i x-cathedral-board-tier || echo "leaderboard not on board-failover (correct)"
   ```

   > Live read-origin smoke matrix (every routed path returns its expected
   > status from the real origins, and no submit-role path 404s) is a go-live
   > step — see "Deferred to live" below.

## Rollback (exact)

Board-failover is purely additive; rolling back returns the paths to the main
edge router with no other change.

1. Re-comment the routes in `wrangler.toml` (or delete the routes from the
   `cathedral-board-failover` worker in the Cloudflare dashboard:
   Workers & Pages -> cathedral-board-failover -> Triggers -> Routes).
2. Redeploy or delete:

   ```bash
   npx wrangler deploy        # with routes commented out, OR
   npx wrangler delete        # remove the worker entirely
   ```

3. With the routes detached, `cathedral-edge-router` reclaims them (its
   `wrangler.toml` already lists the board + submit hot paths). Verify:

   ```bash
   curl -sS -D - -o /dev/null https://api.cathedral.computer/v1/synthetic-boolean/current-challenge | grep -i x-cathedral-edge-cache
   # expect the main router's edge-cache header (HIT/MISS/STALE-REFRESH/BYPASS), NOT x-cathedral-board-tier
   ```

The Railway split domains (`read.cathedral.computer`, `submit.cathedral.computer`)
remain usable directly as the manual bypass throughout.

## Deferred to live

- **Live read-origin smoke matrix.** Hit every routed path against the real
  origins and confirm: board reads `200` from `BOARD_ORIGIN`, leaderboard `200`
  from `LEADERBOARD_ORIGIN`, and **submit-role paths reach `SUBMIT_ORIGIN` (not a
  `404` from a read origin)**. This requires the live Railway services and is a
  go-live step, not verifiable in CI. The in-code role classification is covered
  by `worker.test.mjs`.

## Alternative / complementary mitigation: raise read-service worker count

Edge origin-splitting is the primary fix. A second, **guarded** lever is to give
the read service more in-process workers so one slow leaderboard request does
not block the single worker that also serves board reads.

The split deploy runs the read service at `WEB_CONCURRENCY=2`. Raising it gives
the board reads a worker to run on while a leaderboard query is in flight:

```text
# Read service (Railway), only if the box has CPU/RAM headroom:
WEB_CONCURRENCY=4
```

Guardrails before raising it:

- It is **off by default**. The committed value stays at `2`; raise it only as a
  deliberate, observed change.
- Each extra worker is a full app replica in the same container. Confirm the
  read service has CPU and memory headroom first, or workers will OOM/thrash and
  make things worse.
- Each worker opens its own DB connections. `CATHEDRAL_PG_POOL_MAX` is per
  worker, so total connections scale with `WEB_CONCURRENCY`. Confirm
  `WEB_CONCURRENCY * CATHEDRAL_PG_POOL_MAX` stays within the Postgres connection
  ceiling before raising it.
- This only **reduces** head-of-line blocking; it does not isolate the board.
  The leaderboard can still consume all workers under enough load. Prefer the
  edge origin-split above for true isolation; use worker-count as a fast,
  reversible stopgap or in combination.

To roll back, set `WEB_CONCURRENCY=2` on the read service and redeploy.

## Test

```bash
node worker.test.mjs
```

The tests assert that board reads route to `BOARD_ORIGIN`, that
`/v1/leaderboard/{recent,top}` route to `LEADERBOARD_ORIGIN` (never the board
origin), that **submit-role paths route to `SUBMIT_ORIGIN` and never a read
origin** (a submit path aimed at a read origin fails the test), that `OPTIONS`
preflight returns `204` without touching an origin, that origin failures return
the tier-specific `504 *_origin_unavailable`, and that unknown routes
default-deny.
