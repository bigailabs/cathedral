# Cathedral SAT — Alerts and SLOs

Status: derived from `deploy/RELIABILITY_UPGRADE_PLAN.md` (Phase 7). That plan is
the single source of truth; if a threshold here ever disagrees with the plan, the
plan wins and this file is the bug.

Delivery: `watcher/` (cathedral-alert-watcher, a Cloudflare cron worker) polls
the weight feed, `/v1/admin/validator-health`, and `/v2/verify/metrics` every
minute and pushes PAGE/WARN alerts to a webhook. See `watcher/README.md` for
thresholds, deploy steps, and first-response notes per alert.

Thresholds in code: `scaffold/publisher/health_thresholds.py` (shared by the live
`/v1/admin/validator-health` endpoint and `scripts/validator_release_gate.py`).

Chain constant (verified on finney 2026-06-27): **SN39 tempo = 360 blocks = 72 min
@ 12 s/block.** "1 tempo" below means 72 minutes.

## Severity model

- **PAGE** — wake someone now. Weight setting (the chain's payment source of
  truth) is at risk, or the read origin is hard-down.
- **WARN** — alert a channel, no page. Degrading; act before it pages.
- **TICKET** — record for follow-up; not time-critical.

## Tier 0 — Validator weight feed (watch first)

Surface: `GET /v1/validator/weights/next`
(canonical `api.cathedral.computer`, legacy-prefixed `/api/cathedral/...`,
and direct `read.cathedral.computer/...` — all three must hold).

| Alert | Condition | Severity |
|---|---|---|
| Weight feed 5xx | any `5xx` on the weight feed | **PAGE** |
| Stale fallback served | any `source: stale_fallback` response | **PAGE** |
| Stale fallback aged out | `stale_fallback` age > 1 tempo (~72 min) | **PAGE** |
| Signed-vector age (page) | `now - generated_at` > 10 min | **PAGE** |
| Signed-vector age (warn) | `now - generated_at` > 5 min | **WARN** |
| Signed-vector age (healthy) | `now - generated_at` <= 2 min | ok |
| UID200 update age (page) | UID200 on-chain update age > 20 min | **PAGE** |
| UID200 update age (warn) | UID200 on-chain update age > 10 min | **WARN** |
| UID200 update age (healthy) | UID200 on-chain update age <= 5 min | ok |
| Validators stuck | < quorum of permitted validators fresh within 1 tempo | **WARN** |
| Burn snapshot drift | `burn_snapshot` != intended policy | **PAGE** |

Notes:
- `healthy <= 2 min` tolerates one missed 60 s refresh cycle without alarm.
- Page the moment **any** `stale_fallback` is served — it means the origin is
  down — even while the stale vector is still inside the acceptable age ceiling.

## Tier 1 — Board reads

| Alert | Condition | Severity |
|---|---|---|
| Readiness down | `/health/ready` != 200 for > 60 s | **PAGE** |
| Liveness down | `/health/live` != 200 for > 60 s | **PAGE** |
| Challenge fetch slow | current-challenge / active-challenges p95 > 2 s | **WARN** |
| Origin timeout rate | `504 *_origin_unavailable` rate above baseline | **WARN** |
| Edge stale/error rate | edge serving stale/error above baseline | **TICKET** |

## Tier 2 — Submit + verification

| Alert | Condition | Severity |
|---|---|---|
| Submit busy rate | `submit_busy_retry` (429) rate above threshold | **WARN** |
| Admission emergency | `503 submit_admission_unavailable` returned | **PAGE** |
| Oldest pending age | oldest pending submit age > SLO | **WARN** |
| Receipts stuck | receipts in `verifying` past lock timeout | **WARN** |
| Worker success drop | worker success rate drops below baseline | **WARN** |
| DB write latency | DB write latency spike | **WARN** |
| Object storage writes | solution-body object writes failing | **WARN** |
| 5xx rate (global) | service-wide `5xx` rate above baseline | **WARN** |

## SLOs

```text
Validator weight feed:   99.99% availability; 0x 5xx across any 3 consecutive tempos.
Signed-vector freshness: age <= 2 min healthy; page if > 10 min; hard ceiling 1 tempo (~72 min).
Last-known-good:         feed answers even when app/DB is down (stale_fallback, signed).
Read availability:       >= 99.5%.
Challenge fetch p95:     < 2 s.
Submit admission:        99.9% of valid submits receive a receipt within 1 s.
Verification:            95% within 30 s; 99% within 5 m.
Durability:              no accepted submit receipt is lost after 202.
Retry:                   same idempotency key returns same receipt/result.
```

## Metric sources

- `GET /v1/admin/validator-health` (admin-token gated) — one pane:
  weight-feed freshness (`generated_at` age), feed 5xx count/rate, global 5xx
  rate, and the submit-metrics snapshot. `Cache-Control: no-store`.
- `GET /v1/admin/synthetic-boolean/submit-metrics` (admin-token gated) —
  submit pressure / rejection telemetry (unchanged contract).
- `scripts/validator_release_gate.py` — pre-deploy read-only gate against the
  public feed + finney metagraph (no chain writes). Run before any
  mainnet-affecting deploy; non-zero exit blocks the deploy.

## Validator release gate (must pass before/with any mainnet-affecting deploy)

```text
[ ] weights endpoint: 0x 5xx across 3 consecutive tempos
[ ] signed-vector age <= 5 min
[ ] UID200 update age <= 10 min
[ ] major validators refreshing (not stuck on a stale vector)
[ ] burn snapshot matches intended policy
[ ] last-known-good fallback verified (kill app, feed still serves signed stale vector)
[ ] all three validator URLs pass (canonical + legacy-prefixed + read-service direct)
```

`scripts/validator_release_gate.py` automates the first four (5xx, vector age,
UID200 age, validators-fresh-within-tempo). Burn-policy match, last-known-good
fallback, and the full three-URL byte-identity matrix remain partly manual today
(see the release-gate script's residual-risk note).
