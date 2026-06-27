# Cathedral Edge Router

Purpose: move miner read pressure off Railway while preserving the current split
services.

This Worker is intentionally small:

- read routes go to `https://read.cathedral.computer`
- submit and private-CNF routes go to `https://submit.cathedral.computer`
- safe public read routes are cached at the Cloudflare edge
- submit/private routes are never cached
- unknown routes are default-denied by the Worker; add an explicit route before
  exposing a new mechanism through `api.cathedral.computer`
- legacy `/api/cathedral/...` paths are normalized before routing

## Why this exists

The Railway split made read and submit services independent, but miners can
still overload Railway by polling read endpoints directly. The next step is to
place Cloudflare in front of the read routes so the common active-board,
weights, and leaderboard reads are served from edge cache.

## Cache policy

The Worker uses short fresh TTLs and a longer edge fallback window:

| Route | Fresh | Edge fallback |
| --- | ---: | ---: |
| `/v1/synthetic-boolean/active-challenges` | 5s | 900s |
| `/v1/synthetic-boolean/challenge-broadcast` | 5s | 900s |
| `/v1/synthetic-boolean/current-challenge` | 5s | 300s |
| `/v1/synthetic-boolean/per-miner/summary` | 5s | 30s |
| `/v1/validator/weights/next` | 15s | 30s |
| `/v1/leaderboard/recent` | 2s | 20s |
| `/v1/leaderboard/top` | 15s | 90s |
| `/v1/leaderboard/explain` | 10s | 60s |

Private or signed requests bypass cache. Cache keys whitelist known query
parameters per route. For routes with known query parameters, unsupported
parameters are rejected instead of being forwarded to origin, so random
`?x=...` cache-busting cannot amplify Railway load. For zero-parameter public
snapshot routes, extra query parameters are ignored and collapse to the same
cache key.

Cached read routes serve stale data while refreshing in the background, except
`/v1/validator/weights/next`, which refreshes synchronously so validators do not
receive an intentionally stale signed vector unless the origin errors.

## Origin timeouts

Default Worker origin timeouts:

| Route class | Env var | Default |
| --- | --- | ---: |
| Cached read miss/refresh and non-health read bypass | `READ_ORIGIN_TIMEOUT_MS` | 4500ms |
| Health/readiness/JWKS read bypass | `READ_HEALTH_ORIGIN_TIMEOUT_MS` | 9000ms |
| Submit/private-CNF routes | `SUBMIT_ORIGIN_TIMEOUT_MS` | 10000ms |

Health/readiness routes are not cached. They intentionally get a longer timeout
than cacheable board reads so transient Railway stalls do not make the edge
look dead while cached board traffic is still healthy.

## Deploy

Deploy from this directory after uncommenting the route in `wrangler.toml`.

```bash
npx wrangler deploy
```

Production routes are exact hot paths, not broad prefixes:

```text
api.cathedral.computer/v1/synthetic-boolean/active-challenges*
api.cathedral.computer/v1/synthetic-boolean/challenge-broadcast*
api.cathedral.computer/v1/synthetic-boolean/current-challenge*
api.cathedral.computer/v1/synthetic-boolean/active-cnf*
api.cathedral.computer/v1/synthetic-boolean/per-miner/challenges*
api.cathedral.computer/v1/synthetic-boolean/per-miner/cnf*
api.cathedral.computer/v1/synthetic-boolean/per-miner/status*
api.cathedral.computer/v1/synthetic-boolean/per-miner/summary*
api.cathedral.computer/v1/agents/submit*
api.cathedral.computer/v1/leaderboard/recent*
api.cathedral.computer/v1/leaderboard/top*
api.cathedral.computer/v1/leaderboard/explain*
api.cathedral.computer/v1/validator/weights/next*
api.cathedral.computer/.well-known/*
api.cathedral.computer/health*
```

Legacy `/api/cathedral/...` forms are also routed for the same hot paths.
`/v1/challenges/{challenge_id}/cnf` stays on the existing monolith during this
cutover because Cloudflare route syntax cannot target only that leaf without
catching unrelated `/v1/challenges/*` generator lease endpoints.

Do not start with `api.cathedral.computer/*` or
`api.cathedral.computer/v1/synthetic-boolean/*`. This Worker default-denies
unknown routes, and readiness/non-SAT/compute endpoints may still live on the
monolith during migration.

Do this only after split endpoints are green:

```text
https://read.cathedral.computer/health/ready
https://submit.cathedral.computer/health/ready
```

## Test

```bash
node worker.test.mjs
node smoke.mjs
node route-map.mjs
node soak.mjs
node diagnose.mjs
```

Override the smoke target with:

```bash
CATHEDRAL_EDGE_BASE_URL=https://api.cathedral.computer node smoke.mjs
```

For `api.cathedral.computer`, smoke expects the Worker to be in path by default.
Before the DNS proxy is flipped, use explicit bypass mode for an origin
availability check:

```bash
CATHEDRAL_EDGE_BASE_URL=https://api.cathedral.computer CATHEDRAL_EDGE_ALLOW_BYPASS=1 node smoke.mjs
```

After `api.cathedral.computer` is orange-cloud proxied through Cloudflare, run
without bypass mode, or equivalently:

```bash
CATHEDRAL_EDGE_BASE_URL=https://api.cathedral.computer CATHEDRAL_EDGE_EXPECT_WORKER=1 node smoke.mjs
```

That mode proves routed hot paths are actually hitting the Worker while sampled
non-routed monolith paths still pass through.

## Diagnose

Use this when miners report timeouts or 429s and you need to distinguish
Railway origin trouble from Cloudflare edge trouble:

```bash
node diagnose.mjs
```

Expected healthy shape:

```text
api      200 ... edge=BYPASS server=cloudflare body=json
read     200 ... server=railway-hikari body=json
submit   200 ... server=railway-hikari body=json
status=healthy
```

If `api` returns Cloudflare HTML with `status=429` while `read` and `submit`
return `200`, the split Railway services are healthy and
`api.cathedral.computer` is being rate-limited before Cathedral/Worker code can
respond. That is a Cloudflare zone policy issue, not a Railway outage.

If the diagnostic returns `status=cloudflare_worker_plan_limit`, Cloudflare is
serving Error 1027 because the Worker reached its request plan limit. A WAF or
rate-limit bypass does not fix that. Either upgrade the Cloudflare Workers plan
or disable the Worker routes and keep miners on the direct split endpoints.

If the diagnostic returns `status=network_probe_error`, rerun it from another
network before restarting services. That status means the diagnostic machine
could not complete one or more endpoint probes.

Immediate mitigation:

- tell miners to use `https://read.cathedral.computer` for reads
- tell miners to use `https://submit.cathedral.computer` for submits/private CNF

Do not assume the Workers.dev hostname is a safe emergency API. It is also
served by Cloudflare and can show the same edge-side rate limiting. The direct
split Railway hostnames above are the intended bypass while the branded API
hostname is being fixed.

Permanent fix:

- add a targeted Cloudflare rate-limit/WAF bypass or per-path override for the
  Cathedral API hot paths listed above
- keep the Worker routes attached for `api.cathedral.computer`
- rerun `node diagnose.mjs`, then `node route-map.mjs`, then `node soak.mjs`

The repeatable path is:

```bash
node cloudflare-api-bypass.mjs
CLOUDFLARE_API_TOKEN=... node cloudflare-api-bypass.mjs --apply
```

The token must be able to read the `cathedral.computer` zone and edit zone
rulesets/WAF/rate-limit rules. `Account API Tokens Write` alone is not enough;
that permission can create tokens but cannot inspect or fix the zone policy.

The bypass is intentionally limited to known Cathedral API hot paths. It skips
Cloudflare WAF/rate-limit products for those paths, so origin-side auth,
signature checks, nonce/replay protection, submit backpressure, and explicit
allowlisting remain load-bearing.

For a fuller cutover gate, use:

```bash
CATHEDRAL_EDGE_BASE_URL=https://api.cathedral.computer node route-map.mjs
```

Before DNS proxying, this command should fail because the production host still
bypasses the Worker. To run a direct-origin availability check before cutover:

```bash
CATHEDRAL_EDGE_BASE_URL=https://api.cathedral.computer CATHEDRAL_EDGE_ALLOW_BYPASS=1 node route-map.mjs
```

For a bounded low-load soak after cutover:

```bash
CATHEDRAL_EDGE_BASE_URL=https://api.cathedral.computer CATHEDRAL_EDGE_SOAK_ITERATIONS=12 node soak.mjs
```

Pre-cutover origin-direct soak requires explicit bypass mode:

```bash
CATHEDRAL_EDGE_BASE_URL=https://api.cathedral.computer CATHEDRAL_EDGE_ALLOW_BYPASS=1 CATHEDRAL_EDGE_SOAK_ITERATIONS=3 node soak.mjs
```

`soak.mjs` intentionally skips `/v1/leaderboard/top` by default. Add
`CATHEDRAL_EDGE_INCLUDE_TOP=1` only when you explicitly want to include that
heavier dashboard path.

## Rollback

Remove or disable the Cloudflare Worker route. The Railway split domains remain
usable directly.
