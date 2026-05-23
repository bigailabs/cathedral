# Upgrading validators to v1.1.0

> Audience: operators running the Cathedral validator binary on subnet 39.
> Scope: what changes between v1.0.7 and v1.1.0, what auto-handles itself,
> what (if anything) needs operator attention, and how to roll back.

## What changed in this release

Two changes land in v1.1.0. Both are validator-compatible: a v1.0.7
validator hitting a v1.1.0 publisher continues to function, and a
v1.1.0 validator hitting a v1.0.7 publisher continues to function.

### 1. Pull cursor is now a `(ran_at, id)` tuple

The validator's pull loop polls `GET /v1/leaderboard/recent` for new
eval rows. In v1.0.7 the cursor was a single ISO timestamp `since`,
compared against `ran_at` with `>=`. Under cadence eval load (many
eval rows written in the same millisecond), the prior cursor could
silently leak rows at page boundaries because `ran_at` is not a total
order - see `2026-05-12-track-3-pull-cursor-audit.md`.

v1.1.0 introduces a composite tuple cursor `(since_ran_at, since_id)`
with strict `>` comparison. The publisher's scan is now ordered by
`(ran_at ASC, id ASC)` - a total order - so the cursor advances
strictly forward, no rows are returned twice, and no rows are skipped.

The publisher dual-emits both cursor shapes in `/v1/leaderboard/recent`:

- Legacy: `next_since` - single ISO timestamp, what v1.0.7 reads
- v1.1.0: `next_since_ran_at` + `next_since_id` - tuple, what v1.1.0 reads

Default page size also rises from 200 to 500, and the loop now drains
saturated pages within a single tick (capped at 4 inner pulls) so a
validator coming back from a brief outage catches up immediately
rather than over many 30-second ticks.

### 2. Signature verification is version-aware

The signed payload key set is now selected by
`eval_output_schema_version` on each record. v1.0.7 records do not
carry this field; the verifier defaults to version 1 and uses the
existing key set. An unknown version raises `PullVerificationError`
with `unknown_schema_version: N` - no silent fallback.

This is scaffolding for a follow-up release that introduces a v2
signed payload shape alongside the miner-side eval data model rewrite.
No wire change in v1.1.0 itself; the dispatcher is in place so that
when the publisher starts emitting v2 records, a v1.1.0 validator with
the v2 key set registered routes correctly.

## What auto-handles itself

Nothing required for operators running under PM2 (the supported
deploy mode):

- **Code rolls forward.** A PM2 restart picks up the v1.1.0 image
  (`ghcr.io/cathedralai/cathedral-runtime:v1.1.0`) and reads the new
  cursor shape on the next pull tick.
- **Validator local DB is untouched.** The pull-side schema does not
  change in v1.1.0. The publisher's `eval_runs` table gets a new
  composite index (`idx_eval_ran_at_id`), but validators do not run
  the publisher migration path - that lives on `api.cathedral.computer`.
- **Cursor advances cleanly.** First-tick cursor on v1.1.0 sends both
  `since_ran_at` and the legacy `since` for back-compat with v1.0.x
  publishers. The v1.1.0 publisher consumes the tuple; a v1.0.x
  publisher ignores the new kwargs and uses the legacy `since`.

## What needs operator attention

**Deploy sequencing is required during the rollover window** - see
"Deploy sequencing" below. The cross-version window is otherwise
binary-compatible (v1.0.7 validators continue functioning against a
v1.1.0 publisher), but cadence orchestration MUST stay gated until the
fleet has rolled forward.

Two optional considerations beyond that:

- **Observability.** After upgrading, you can confirm a validator is
  on v1.1.0 by querying
  `https://api.taostats.io/api/validator/weights/latest/v1?netuid=39`
  and checking `version_key=1001000` on the last weight-set extrinsic.
  v1.0.7 stamps `1000007`.
- **Bandwidth.** Page size 500 (up from 200) raises per-tick bandwidth
  by ~2.5x on a fully-drained loop, and up to 10x during catch-up.
  Each row is ~2-4 KB, so the steady-state wire bandwidth remains
  trivial (single-digit KB/s); the saturation cap of 2000 rows per
  tick keeps catch-up bursts bounded at ~8 MB / 30s in the worst case.

A v1.0.7 validator that has not yet upgraded continues to function
against a v1.1.0 publisher AS LONG AS cadence orchestration stays
disabled. It does NOT get the saturation-pull optimization (its loop
reads `next_since` as before), and under a cadence burst that writes
>page-size rows sharing one millisecond it silently drops the rows
past the first page boundary - which is why the Deploy sequencing
below requires the fleet to roll forward before cadence is enabled.

## Deploy sequencing

v1.1.0 introduces a tuple cursor `(ran_at, id)` on `/v1/leaderboard/recent`. v1.0.7 validators send only a single-string `since` cursor and cannot express a sub-millisecond offset. Under burst writes (>page-size rows sharing a millisecond), v1.0.7 validators will silently drop the rows past the first page boundary.

Resolved at the binary level - v1.1.0 validators always send the tuple cursor and drain bursts correctly. The constraint is only present during the rollover window when the publisher is v1.1.0 but some validators are still v1.0.7.

**Required deploy order:**

1. Deploy v1.1.0 publisher to production (Railway auto-deploys on push to main).
2. Wait 2-4 hours for the fleet to auto-cycle. PM2-driven validators pull main, restart, pick up v1.1.0. You can confirm by querying taostats: `GET https://api.taostats.io/api/validator/weights/latest/v1?netuid=39` - count rows with `version_key=1001000`.
3. Once a clear majority of validators report `version_key=1001000`, enable cadence orchestrator (env flag `CATHEDRAL_CADENCE_ENABLED=true` on the publisher).

**Why this ordering matters:** Cadence orchestrator writes batches of rows that can share millisecond timestamps. Until validators are on v1.1.0, they will silently lose rows in those bursts. If cadence is enabled before fleet rollover, miners will see successful submissions that never appear on the leaderboard.

**Rollback procedure if cadence is enabled too early:** Set `CATHEDRAL_CADENCE_ENABLED=false`. Publisher reverts to one-eval-per-submission. Validators catch up via UPSERT dedupe on subsequent polls.

## Rollback procedure

If a v1.1.0 validator misbehaves and you need to roll back, pin the
runtime image in your PM2 ecosystem file:

```js
{
  name: "cathedral-validator",
  script: "cathedral-validator",
  args: "serve --config /etc/cathedral/validator.toml",
  env: {
    CATHEDRAL_IMAGE: "ghcr.io/cathedralai/cathedral-runtime:v1.0.7"
  }
}
```

Then `pm2 reload cathedral-validator`. The validator's local sqlite
stays compatible: v1.1.0 did not add any validator-side columns or
indexes, so a downgrade is a no-op for the local DB.

## Verification after deploy

After upgrading, the publisher response should carry both cursor
shapes. `/v1/leaderboard/recent` requires a cursor parameter, so seed
one well before any real eval was written:

```bash
curl -s "https://api.cathedral.computer/v1/leaderboard/recent?since=1970-01-01T00:00:00Z&limit=1" \
  | jq '{next_since, next_since_ran_at, next_since_id}'
```

A v1.1.0 publisher returns all three fields populated (it has at least
one row to point at). A v1.0.x publisher returns only `next_since`.

On the validator side, the structured logs at the `INFO` level will
show `pull_loop_tick` with an `inner_pulls` field. v1.0.7 emitted only
`fetched` and `persisted`; the new field surfaces when the saturation
inner-pull kicks in.

## v2 signed payload - gated, not active at merge

v1.1.0 ships the v2 signed payload shape and the new eval data model
(card excerpt / artifact manifest / encrypted bundle URL) in code, but
gated behind two publisher env flags so the wire shape on merge day is
identical to v1.0.7:

- `CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD` - when `true`, `score_and_sign`
  produces v2 records (drops `output_card` + `output_card_hash` +
  `polaris_verified`, adds `eval_card_excerpt` +
  `eval_artifact_manifest_hash`). Default `false`.
- `CATHEDRAL_CADENCE_ENABLED` - when `true`, the eval orchestrator
  honors `card_definitions.refresh_cadence_hours` and re-evaluates
  submissions periodically. Default `false`.

A v1.1.0 validator already carries the version-aware verifier
dispatcher (`_SIGNED_KEYS_BY_VERSION[2]` registered with the locked v2
field set), so when the publisher flips `CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD`,
verification routes correctly with no validator action.

See "Deploy sequencing" above for the order in which these flags
should be enabled - flipping cadence before the validator fleet has
rolled forward causes silent row loss on the v1.0.7 stragglers.
