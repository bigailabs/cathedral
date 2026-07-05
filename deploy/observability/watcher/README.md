# cathedral-alert-watcher (Cloudflare Worker, cron)

Off-box watcher for the ramp-critical health signals. Polls the public and
admin health surfaces every minute from Cloudflare's edge and pushes alerts to
a webhook. It closes the gap RELIABILITY_UPGRADE_PLAN P7 left open ("alerts and
dashboards live"): `/v1/admin/validator-health` already classifies problems as
`level=page`, but until now nothing polled it.

Why a Cloudflare cron worker and not an in-process loop: an in-process watcher
dies with the origin it is watching. Both recent incidents (the pm-read 429 bug
and the submit outage) were reported by miners before any internal signal. This
follows the same pattern as the existing cron workers (cathedral-sat-poster,
cathedral-sat-broadcast) and the edge-router/failover workers committed under
`deploy/edge-router/`.

## What it watches

Thresholds come from `../ALERTS.md` and `scaffold/publisher/health_thresholds.py`.
If a number here disagrees with ALERTS.md, ALERTS.md wins.

### Tier 0: validator weight feed (all three URLs)

`GET /v1/validator/weights/next` on the canonical, legacy-prefixed, and
read-service-direct URLs (`WEIGHTS_URLS`).

| Alert id | Condition | Level |
|---|---|---|
| `weights_5xx` | any 5xx response | PAGE |
| `weights_bad_status` | any non-200, non-5xx response | PAGE |
| `weights_stale_fallback` | body `source: stale_fallback` or `x-cathedral-vector-source: stale_fallback` header | PAGE |
| `weights_vector_age` | `now - generated_at` > 600s / > 300s | PAGE / WARN |
| `weights_unreachable` | fetch failure on 2 consecutive polls | PAGE |

First response: check Railway service status and the weights refresh thread
(`[weights]` log lines). If the origin is hard-down, the weights-failover
worker (`deploy/edge-router/weights-failover/`) is the mitigation: it serves
the last-known-good signed vector; make sure its routes are attached.

### Publisher origin health (`/v1/admin/validator-health`, admin token)

| Alert id | Condition | Level |
|---|---|---|
| `origin_vector_freshness` | origin classifies its own vector `level=page` or over the 72 min hard ceiling / `level=warn` or `unknown` | PAGE / WARN |
| `weights_feed_5xx_counted` | the origin-side 5xx counter for the weight feed advanced since the last poll | PAGE |
| `http_5xx_rate` | global 5xx rate > 2% over the poll window (min 20 requests) | WARN |
| `submit_429_rate` | `submit_busy_retry` + `per_miner_busy_retry` > 120/min | WARN |
| `submit_path_down` | 5xx-class submit rejections (`async_worker_unavailable` / `pm_async_worker_unavailable_sync` / `submit_queue_backpressure`) rose since the last poll | PAGE |
| `ratelimit_fail_open` | `ratelimit.unresolved_ip_count` rose since the last poll | WARN |
| `validator_health_auth` | admin token rejected (401/403) | WARN |
| `validator_health_unreachable` | fetch failure / non-200 | WARN |

First response for `submit_path_down` (PAGE): the submit route is returning
5xx, not merely throttling — this is the outage class a miner reported once
already. The v2 backlog can look *healthier* during this (nothing gets
admitted), so this direct probe is the real signal. Check the verify worker is
alive (`CATHEDRAL_SUBMIT_ASYNC_REQUIRE_WORKER` gates 503s on worker liveness)
and the submit/pool config; correlate with `v2_worker_stall`.

First response for `submit_429_rate`: this is the submit or pm-read gate
saturating (the 429-bug incident signature). Check submit-metrics
(`/v1/admin/synthetic-boolean/submit-metrics`) for which reason dominates and
whether concurrency caps need a bump. For `ratelimit_fail_open`: client IPs are
failing to resolve, so the abuse limiter is effectively bypassable; check the
proxy header path (#333).

Counter-based checks (5xx rate, 429 rate, fail-open) compare against the
previous poll's counters stored in KV and reset their baseline automatically
when `http_status.started_at_iso` changes (process restart).

### V2 verify pipeline (`/v2/verify/metrics`, public)

| Alert id | Condition | Level |
|---|---|---|
| `v2_backlog_depth` | `pending_count` >= 20000 / >= 5000 | PAGE / WARN |
| `v2_backlog_trend` | pending rose 5 consecutive polls and is above 500 | WARN |
| `v2_oldest_pending` | oldest pending submit >= 900s / >= 300s (SLO: 99% verified in 5m) | PAGE / WARN |
| `v2_worker_stalled` | pending >= 10 with `processed_last_60s == 0` on 2 consecutive polls | PAGE |
| `v2_worker_disabled` | metrics on but `enabled: false` | WARN |
| `v2_tick_errors` | `tick_errors_last_60s > 0` | WARN |
| `v2_metrics_unreachable` | fetch failure (page on 2nd consecutive) | WARN then PAGE |
| `v2_metrics_missing` | 404 while `V2_EXPECTED=1` | WARN |

First response for backlog alerts: miners are submitting faster than the single
verify worker drains (~2000/min ceiling; the 98k-backlog incident). Confirm the
worker holds the advisory lock (`lock_held_by_self`), check `last_worker_error`,
and slow the ramp or raise the worker batch rate. `v2_worker_stalled` means work
exists and nothing drained for 2 minutes: restart the v2 worker service.

Worker liveness is detected via backlog-vs-drain on purpose: `last_batch_at`
only updates on non-empty batches, so it goes stale on an idle-but-healthy
worker. A true heartbeat age (the DB row written by `write_v2_worker_heartbeat`
every loop tick) is not exposed on `/v2/verify/metrics` yet; exposing it is a
small follow-up on the v2 branch, and the watcher can then add an explicit
heartbeat-age check.

## Alert delivery

One webhook message per cron run, bundling every change:

```
cathedral watcher
[PAGE] weights feed HTTP 503: https://api.cathedral.computer/v1/validator/weights/next
[WARN] v2 verify backlog rising 5 consecutive polls: 620 -> 1240 (intake outpacing verify)
[RESOLVED] busy-retry 429 rate 300/min (was warn, active 45m)
```

Lifecycle: an alert fires once when it appears, escalates immediately on
warn -> page, re-fires every `REALERT_MINS` (default 30) while still active,
and sends a `[RESOLVED]` line when it clears. State lives in the `ALERT_STATE`
KV namespace; if the binding is missing the watcher still runs, but stateless
(no dedup, no trend, no counter deltas) and says so in the message.

`WEBHOOK_FORMAT`: `discord` (default, posts `{content}`), `slack` (`{text}`),
or `json` (posts the raw events array to any custom receiver).

## Deploy

From `deploy/observability/watcher/`:

```sh
# 1. one-time: create the state KV namespace, paste the id into wrangler.toml
npx wrangler kv namespace create ALERT_STATE

# 2. secrets
npx wrangler secret put ALERT_WEBHOOK_URL       # Discord webhook URL (required)
npx wrangler secret put CATHEDRAL_ADMIN_TOKEN   # publisher admin token (recommended)
npx wrangler secret put STATUS_TOKEN            # optional: gates GET /status

# 3. deploy (registers the every-minute cron)
npx wrangler deploy
```

Without `CATHEDRAL_ADMIN_TOKEN` the validator-health probe is skipped (the
Tier 0 and v2 checks still run) and each alert message notes the skip.

Manual check without waiting for cron (read-only, sends nothing):

```sh
curl -s https://cathedral-alert-watcher.<account>.workers.dev/status \
  -H "Authorization: Bearer $STATUS_TOKEN" | jq .findings
```

Or tail the cron runs live: `npx wrangler tail cathedral-alert-watcher`.

## Tests

```sh
node checks.test.mjs
```

All threshold, trend, counter-delta, and alert-lifecycle logic is pure and
lives in `checks.mjs`; `worker.mjs` only does fetch/KV/webhook I/O.

## Known limits

- Subrequests from a worker to a host on the same Cloudflare zone can bypass
  other workers' routes and hit the origin directly. If that applies here, the
  watcher tests origin health rather than the exact edge path miners see; a
  hard origin outage still alerts either way (origin 5xx instead of
  `stale_fallback`).
- `ratelimit.unresolved_ip_count` reads null until the v2 ingress hardening
  (#333) is deployed on the polled revision; the watcher treats null as
  "not wired" and stays quiet rather than alerting on a missing field.
- The UID200 on-chain update-age alert from ALERTS.md needs a chain read and is
  out of scope here; `scripts/validator_release_gate.py` still covers it
  pre-deploy.
