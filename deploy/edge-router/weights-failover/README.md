# weights-failover (Cloudflare Worker)

Durable last-known-good (LKG) failover for the validator weight feed. This worker
is the Tier-0 edge in front of the publisher origin for the weight-feed routes on
`api.cathedral.computer`. It is committed here so the deployed worker is
version-controlled and reproducible (RELIABILITY_UPGRADE_PLAN P0 #2 "Durable
last-known-good signed vector").

> **CAPTURE / RECONSTRUCTION — not byte-verified.** `worker.js` is a
> version-controlled *reconstruction* of the intended failover behavior, not a
> proven byte-for-byte mirror of the worker currently deployed on Cloudflare.
> Until the live script's source hash is recorded and matched (see
> "Recording the live script hash" below), treat this as the spec of what the
> worker should do, not evidence of what is running.

## What it does

- Serves `/v1/validator/weights/next` (the validator weight feed) by proxying GET
  requests to the publisher origin (`PUBLISHER_ORIGIN`).
- On a **successful** origin response (HTTP 2xx, non-trivial body), it stores the
  body in a KV namespace (`WEIGHTS_LKG`) under key `lkg:weights_next`, recording
  `stored_at` in the entry metadata. This write is non-blocking via `ctx.waitUntil`.
- On origin failure (timeout after `WEIGHTS_ORIGIN_TIMEOUT_MS` = 8000ms by
  default, network error, or non-2xx status), it serves the last stored value
  from KV instead, so validators keep receiving a usable vector during a
  publisher outage.
- If a path is prefixed with `/api/cathedral`, that prefix is stripped before the
  request is forwarded to the origin.

### Edge caching (decision: keep short fresh-vector edge cache)

The first capture used `cf: { cacheTtl: 0 }` + `cache-control: no-store`, which
dropped the edge cache that `cathedral-edge-router` had on this route and sent
**every** healthy validator read to the origin. That is the opposite of what we
want for the most-protected route.

This worker **restores short fresh-vector edge caching** for healthy (`origin`)
responses:

```
cache-control: public, max-age=2, s-maxage=15, stale-while-revalidate=1200
```

- `s-maxage=15` lets Cloudflare's shared edge cache serve a fresh signed vector
  for up to 15s, absorbing the bulk of healthy validator polling without hitting
  the origin. 15s is far inside the 5-minute signed-vector freshness budget in
  the plan, and matches the `freshTtl: 15` the main edge router already used for
  this route.
- `stale-while-revalidate=1200` lets the edge keep answering from a slightly
  stale-but-still-signed copy for up to 20 min while it revalidates in the
  background — a second layer of protection on top of the KV LKG.
- The subrequest to the origin uses `cf: { cacheTtl: 0 }` so the worker always
  revalidates against the origin itself (and refreshes KV) rather than reading a
  stale Cloudflare-cached subrequest. The **client/edge** caching is driven by
  the response `Cache-Control` above, not by the subrequest.

Stale **fallback** responses (`x-cathedral-vector-source: stale_fallback`) are
returned with `cache-control: no-store` so a degraded vector is never promoted
into the edge cache as if it were fresh.

### Staleness is detected from the BODY, not headers

The signed vector body carries `generated_at` and `expires_at`
(`scaffold/publisher/weights.py`). Validators (and the on-chain consumer) decide
whether a vector is too old by reading those signed fields from the body — they
do **not** rely on the worker's headers for freshness. Because of that:

- The worker serves the **original signed bytes verbatim**. It never re-signs,
  re-times, or mutates the vector. A stale copy is still cryptographically
  verifiable; age is a body-level decision the validator makes.
- The `x-cathedral-vector-source` / `x-cathedral-vector-stored-at` headers are
  **operator hints only** (which path served it, when the LKG was captured). They
  are not the freshness signal of record.

### Response headers (how to tell where a vector came from)

- `x-cathedral-vector-source`: `origin` (fresh from publisher) or `stale_fallback`
  (served from KV LKG).
- `x-cathedral-fallback-reason`: present only on fallback; one of
  `origin_error`, `origin_status_<code>` (e.g. `origin_status_500`).
- `x-cathedral-vector-stored-at`: present on fallback; ISO timestamp of when the
  served LKG value was captured.
- `cache-control`: short fresh-vector cache on `origin`, `no-store` on
  `stale_fallback`.
- `access-control-allow-origin: *`.

If the origin is down AND KV has never been populated, the worker returns
`503` with `{ "error": "no vector available", "reason": ... }`.

## Bindings (configure via `wrangler.toml` / Cloudflare dashboard)

- **KV namespace** `WEIGHTS_LKG` — durable store for the last-known-good vector.
  Create once with `wrangler kv:namespace create WEIGHTS_LKG` and paste the id
  into `wrangler.toml`.
- **Plain text** `PUBLISHER_ORIGIN` — base URL of the publisher origin
  (e.g. `https://read.cathedral.computer`). Trailing slashes are trimmed.
- **Plain text** `WEIGHTS_ORIGIN_TIMEOUT_MS` — optional origin timeout override
  (default 8000ms).

## Routing

`wrangler.toml` declares (commented, leave off until cutover) the two weight-feed
routes — `/v1/validator/weights/next` and its `/api/cathedral` legacy alias — on
`api.cathedral.computer`. Only the weight feed should hit this worker.

## Test

```
node --test deploy/edge-router/weights-failover/worker.test.mjs
# or
node deploy/edge-router/weights-failover/worker.test.mjs
```

`worker.test.mjs` exercises origin-2xx-stores-KV, origin-5xx-serves-stale,
origin-timeout/abort, no-LKG→503, `/api/cathedral` prefix strip, and the
`body.length > 2` guard, using a mock KV and a mocked `globalThis.fetch`.

## Recording the live script hash (DEFERRED-TO-LIVE)

To upgrade this file from "reconstruction" to "verified mirror", at go-live:

1. Pull the deployed script:
   `wrangler deployments view` / Cloudflare dashboard → download the live
   `cathedral-weights-failover` script.
2. Hash both:
   `shasum -a 256 worker.js` and `shasum -a 256 <downloaded live script>`.
3. If they match, record the hash + date here and drop the "reconstruction"
   caveat. If they differ, reconcile (the repo is the source of truth; redeploy
   from here).

This byte-match against live Cloudflare cannot be done in-session and is left for
the human at go-live.

## Deploy

1. `wrangler kv:namespace create WEIGHTS_LKG` and paste the id into `wrangler.toml`.
2. Set `PUBLISHER_ORIGIN` (and optionally `WEIGHTS_ORIGIN_TIMEOUT_MS`) in `[vars]`.
3. `node --check worker.js` to syntax-check; run the tests above.
4. Publish (`wrangler deploy`), then uncomment the two routes in `wrangler.toml`
   (or repoint them in the dashboard) to move the weight feed onto this worker.

## Rollback

Repoint the two `api.cathedral.computer` weight-feed routes back to
`cathedral-edge-router`, then delete this worker. KV (`WEIGHTS_LKG`) can be left in
place; it is only read on fallback and is harmless when unrouted.
