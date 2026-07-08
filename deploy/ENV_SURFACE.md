# Cathedral publisher: core env surface

Goal: one coherent launch profile instead of per-feature toggle sprawl. A
deployment sets the **profile**, the **role**, the **stores/secrets**, and the
**mechanism calibration** below — and nothing else. Everything outside this
table is either internal (tests/surgical rollout), deprecated, or a candidate
for deletion in the post-relaunch env cleanup.

## 1. Profile (what mechanism runs)

| Env | Values | Default | Meaning |
|---|---|---|---|
| `CATHEDRAL_LAUNCH_PROFILE` | unset, `v2-converged` | unset | `v2-converged` = the single unified miner protocol: V2 surface + bitset submit + lazy issuance + PM payout bridge. Unset = legacy per-flag behavior. |

Fail-closed at boot (RuntimeError, never a warning):
- Unknown profile value.
- Profile + any implied flag explicitly forced off (contradiction), including
  `CATHEDRAL_V2_PERMINER_ENABLED` (the profile implies the per-miner surface).
- Profile without `CATHEDRAL_V2_SUBMIT_TOKEN_SECRET`.
- Profile without a stable per-miner seed (`CATHEDRAL_V2_PERMINER_SEED_SECRET`
  or `CATHEDRAL_PERMINER_SEED_SECRET`).
- Profile (or the bridge flag) + a split V2 DB (`CATHEDRAL_V2_DATABASE_URL` /
  `CATHEDRAL_V2_DB_PATH`) — bridged payout rows must land in the scoring store.
- Profile without a shared Postgres store (`DATABASE_URL`) — the SQLite
  fallback would silently stop the two deployment processes sharing state
  (pytest is exempt so tests can build local stores).

## 2. Role (what this process does)

| Env | Values | Default | Meaning |
|---|---|---|---|
| `CATHEDRAL_SERVICE_ROLE` | `all`, `read`, `submit`, `worker` | `all` | Which route/background surfaces this process serves. Public origin: `all` with the verify worker disabled; private process: `all` with the worker on. |
| `CATHEDRAL_V2_VERIFY_WORKER_ENABLED` | bool | off | Singleton async verifier + payout bridge executor. Exactly one process per deployment. |

## 3. Stores and secrets (fail if missing where required)

| Env | Meaning |
|---|---|
| `DATABASE_URL` | Postgres DSN; single shared store for scoring + V2 under the converged profile. |
| `CATHEDRAL_EVAL_SIGNING_KEY` | Ed25519 weight/eval signing key (hex). Never rotate casually: validators pin it. If unset, a THROWAWAY dev key is generated and validators will reject everything. |
| `CATHEDRAL_V2_SUBMIT_TOKEN_SECRET` | HMAC secret binding submit tokens to (hotkey, challenge, epoch, tier, seq, nvars, cnf_sha). Required by the profile. |
| `CATHEDRAL_PERMINER_SEED_SECRET` | Deterministic instance derivation seed. Required for per-miner issuance. |
| `CATHEDRAL_CNF_TOKEN_SECRET` | Legacy V1 CNF token HMAC (V1 miner routes are edge-gated; keep until V1 removal). |

## 4. Mechanism calibration (meaningful, documented, rarely changed)

| Env | Default | Meaning |
|---|---|---|
| `CATHEDRAL_PERMINER_EPOCH_BUCKET_HOURS` | 1 | Epoch width. Epoch gate accepts {current, current-1} everywhere (mint, admit, verify, payout). |
| `CATHEDRAL_PERMINER_ALLOTMENT_T1/T2` | 10000 | Virtual per-tier allotment (lazy; miners page as deep as they can solve). |
| `CATHEDRAL_PERMINER_NVARS_T*` / `NCLAUSES_T*` | 400/1704 | Instance shape per tier (α≈4.26 band). |
| `CATHEDRAL_PERMINER_WEIGHT_T*` | 1.0 / 2.0 | Tier difficulty weight. The payout bridge records the EXACT verifier weight. |
| `CATHEDRAL_V2_REAL_FRACTION` | 0 | Fraction of real (unplanted) instances. |
| `CATHEDRAL_WEIGHTS_COLDKEY_COLLAPSE` | off | Sybil hardening; assignment identity = coldkey when mapped. V2 is identity-aligned with V1; pre-bake resolves identities. |
| `CATHEDRAL_PERMINER_MAX_PAGE_LIMIT` | 50 | Page size cap (edge Worker additionally clamps public traffic). |
| `CATHEDRAL_PM_READ_HARD_CAP` | 128 | Concurrent PM-read gate (set low on small origins). |

## 5. Internal / test-only (do not set in deployments; use the profile)

`CATHEDRAL_V2_ENABLED`, `CATHEDRAL_V2_SUBMIT_BITSET_ENABLED`,
`CATHEDRAL_V2_LAZY_ISSUANCE`, `CATHEDRAL_V2_PM_PAYOUT_BRIDGE`,
`CATHEDRAL_V2_PERMINER_ENABLED` — implied by the profile; explicit values only
for tests and surgical rollout, contradictions fail closed.

Reference sandbox layout (two processes, one shared env file):

```sh
# shared .env.sh
export DATABASE_URL='postgresql://...'        # required by the profile
export CATHEDRAL_LAUNCH_PROFILE=v2-converged
export CATHEDRAL_EVAL_SIGNING_KEY='<pinned hex>'
export CATHEDRAL_V2_SUBMIT_TOKEN_SECRET='<stable>'
export CATHEDRAL_PERMINER_SEED_SECRET='<stable>'
export CATHEDRAL_CNF_TOKEN_SECRET='<stable, until V1 removal>'
export CATHEDRAL_WEIGHTS_COLDKEY_COLLAPSE=1
# never set CATHEDRAL_PERMINER_ENABLED (V1 surface stays off; enables env pin)

# public origin (:8080): CATHEDRAL_SERVICE_ROLE=all, CATHEDRAL_V2_VERIFY_WORKER_ENABLED=0
# private process (:8000): CATHEDRAL_SERVICE_ROLE=all, CATHEDRAL_V2_VERIFY_WORKER_ENABLED=1
```

Exactly one process per deployment runs the verify worker (payout bridge
executor); add a deploy smoke check for this.

## 6. Deprecation queue (post-relaunch cleanup, tracked for deletion)

- V1 miner-route flags (`CATHEDRAL_PERMINER_ENABLED` V1 surface, V1 submit
  paths) once V1 removal ships — routes are already edge-gated permanently.
- `CATHEDRAL_V2_SHADOW_V1_*` (shadow mirror of V1 submits).
- `CATHEDRAL_V2_PERMINER_ENV_PIN` and the whole V2->legacy env bridge once V1
  per-miner env is gone (kills the pm env lock serialization).
- Duplicated `CATHEDRAL_V2_PERMINER_*` twins of calibration envs.
- `CATHEDRAL_V2_DATABASE_URL`/`CATHEDRAL_V2_DB_PATH` split-store option
  (converged profile forbids it; delete after V1 removal).
