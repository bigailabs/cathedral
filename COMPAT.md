# v4-live — what must not break, proven against the live network

**Snapshot:** 2026-06-09/10 UTC · branch `v4-live` · gate: `wire_compat.py`
(run with a python that has `cryptography`; everything else offline on
`fixtures/live-20260609/`).

## Live SN39 status (measured, not assumed)

| Surface | Observed |
|---|---|
| Publisher | `https://api.cathedral.computer` healthy (db/hippius/polaris ok) |
| Board | 51 active: 25× tier-1 + 25× tier-2 random_3sat (n=6000, m=25560, 7-day limit, solve-on-submit), 1× tier-3 sha256_preimage |
| Miners | 76 distinct hotkeys on the SAT leaderboard, **all** evaluated within last 24h; 242 UIDs earning on-chain |
| Wins vs solves | Lock-wins stopped 2026-06-04 (v6 open-window cutover); v6 solves flow continuously, mirrored as `-v5compat` rows |
| Scores | Flat 1.0 across the board (`score_parts.binary_correct=1.0`) — zero differentiation; on-chain every miner gets an identical ~0.05% incentive |
| Chain | 256 UIDs, **11 validators** (permit & stake>1k), burn UID 204 holds 87.6% incentive |
| Feed | v5 AND v6 rows live; cursor = `next_since` + tuple (`next_since_ran_at`,`next_since_id`) + `merkle_epoch_latest` |
| Signing | jwks kid `cathedral-eval-signing`, Ed25519 pubkey `10890a66…5e26` |

## The freeze surface (break any line → break 11 validators)

1. Feed endpoint path + dual cursor semantics (`since_ran_at`/`since`).
2. Row schema: `eval_output_schema_version ∈ {5,6}`; signed subset =
   `SIGNED_KEYS_V5` (14 keys) / `SIGNED_KEYS_V6` (+`challenge_value`,
   `solve_rank`, `solved`, `operator`) — see `scaffold/wire.py`.
3. Canonicalization: sorted keys, compact separators, `default=str`, UTF-8,
   excluding `signature`/`cathedral_signature`/`merkle_epoch` (post-signing).
4. Same Ed25519 key + jwks endpoint serving it.
5. `task_type` must stay within released validators' lane_weights vocabulary
   (`synthetic_boolean_v1` family) — unknown task types are silently dropped.
6. Burn: 85% is HARDCODED validator-side (local-weights mode; remote vector
   mode exists but is opt-in/default-off). Burn steps need the one
   "jackpot release" (see V4-DESIGN.md migration).

## The miner surface (break it → break 76 active miners; softer, they update)

`/v1/synthetic-boolean/active-challenges`, `active-cnf` (sr25519-signed,
tokenized fetch), `/v1/agents/submit` (6-field canonical-JSON signature,
solve-on-submit), read endpoints, `/skill.md`. Lane A in the thin publisher
implements THIS api first; new lanes (S/I) are additive endpoints.

## Proof artifacts on this branch

- `scaffold/wire.py` — byte-faithful port of canonical_json + v5/v6 signed
  key sets + verify/sign (needs `cryptography` at call time only).
- `wire_compat.py` — **8/8 PASS**: all 50 sampled live rows (17×v5, 33×v6)
  verify against the live public key using OUR canonicalization; tampering
  any signed field breaks verification; mutating post-signing fields doesn't;
  cursor contract present. Re-run with `--refresh` to resample the feed.
- `fixtures/live-20260609/` — jwks, 50 rows, board snapshot (golden vectors).

## Why v4 won't break the live network (the argument in three lines)

1. Validators only consume signed rows → the thin publisher emits v4 scores
   AS rows under the frozen schema/key/task_type — proven byte-compatible.
2. Miners keep the Lane A surface through a deprecation window; the 7-day
   scoring window makes every ramp gradual and reversible.
3. The only validator-touching change (burn step) is deliberately deferred
   to one bundled, incentivized release.

## Known live issues v4 fixes (and must not accidentally "fix" early)

- Flat 1.0 scoring → v4 differentiates via PAR-2/receipt order. Ramp the
  differentiation gradually (publisher-side row values) so no live miner's
  income cliffs inside a 7-day window.
- tier-3 sha256_preimage has no solvability proof (the tier-3 honesty gap)
  → superseded by Lane I disagreement-proven hardness; leave the existing
  challenge alone until then.
