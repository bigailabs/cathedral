# Cathedral publisher: core env surface

Goal: one coherent launch profile instead of per-feature toggle sprawl. A
deployment sets the **profile**, the **role**, the **stores/secrets**, the
small **runtime guardrail** set, and the **mechanism calibration** below — and
nothing else.

Executable audit:

```sh
python3 deploy/check_env_surface.py --env-file /path/to/cathedral.env
```

Everything outside this document is either internal (tests/surgical rollout),
deprecated, or a candidate for deletion in the post-relaunch env cleanup.

## 1. Profile (what mechanism runs)

| Env | Values | Default | Meaning |
|---|---|---|---|
| `CATHEDRAL_LAUNCH_PROFILE` | unset, `v2-converged` | unset | `v2-converged` = the single unified miner protocol: V2 surface + bitset submit + lazy issuance + PM payout bridge + startup env pinning. Unset = legacy per-flag behavior. |

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
| `CATHEDRAL_SERVICE_ROLE` | `all`, `read`, `submit`, `worker` | `all` | Which route/background surfaces this process serves. Public origin: `all` with the verify worker disabled; private process: `all` with the worker on. Set it explicitly anyway. |
| `CATHEDRAL_V2_VERIFY_WORKER_ENABLED` | bool | off | Singleton async verifier + payout bridge executor. Exactly one process per deployment. Set `0`/`1` explicitly per process. |

## 3. Stores and secrets (fail if missing where required)

| Env | Meaning |
|---|---|
| `DATABASE_URL` | Postgres DSN; single shared store for scoring + V2 under the converged profile. |
| `CATHEDRAL_EVAL_SIGNING_KEY` | Ed25519 weight/eval signing key (hex). Never rotate casually: validators pin it. If unset, a THROWAWAY dev key is generated and validators will reject everything. |
| `CATHEDRAL_V2_SUBMIT_TOKEN_SECRET` | HMAC secret binding submit tokens to (hotkey, challenge, epoch, tier, seq, nvars, cnf_sha). Required by the profile. |
| `CATHEDRAL_PERMINER_SEED_SECRET` | Deterministic instance derivation seed. Required for per-miner issuance. |
| `CATHEDRAL_CNF_TOKEN_SECRET` | Legacy V1 CNF token HMAC (V1 miner routes are edge-gated; keep until V1 removal). |

## 4. Runtime guardrails (small set, high leverage)

| Env | Launch value | Meaning |
|---|---|---|
| `CATHEDRAL_PG_STATEMENT_TIMEOUT_MS` | `4000` | Mandatory on read-serving roles. Prevents slow board/leaderboard scans from pinning Postgres connections and starving weights/miners. |
| `CATHEDRAL_PM_READ_HARD_CAP` | start `4` on small origins | Bounds concurrent per-miner read work. Raise only with live evidence. |
| `CATHEDRAL_V2_READ_THREADS` | start `4` | Dedicated V2 read executor. Keep close to the read cap until the origin has headroom. |
| `CATHEDRAL_V2_SUBMIT_BITSET_THREADS` | start `4` | Dedicated bitset submit executor so submit verification cannot starve reads/health. |
| `CATHEDRAL_V2_VERIFY_BATCH_SIZE` | `8` | Async verifier batch size. |
| `CATHEDRAL_V2_VERIFY_INTERVAL_SECS` | `1` | Async verifier loop interval. |
| `CATHEDRAL_V2_VERIFY_LOCK_SECS` | `120` | Verifier claim lock TTL. |
| `CATHEDRAL_V2_VERIFY_PARALLEL_CLAIMS` | optional | Multi-claim verifier mode. Leave unset unless batch drain metrics require it. |
| `CATHEDRAL_V2_BITSET_VERIFY_THREADS` | optional | Per-batch bitset verification parallelism in the verifier process. |
| `CATHEDRAL_RATELIMIT_RPM` + `CATHEDRAL_PER_HOTKEY_*` | optional | Public origin flood/fairness guardrails. Set intentionally; do not cargo-cult. |
| `CATHEDRAL_SUBMIT_*` queue/cap knobs | optional | Legacy submit-path backpressure. Keep only while legacy submit surfaces remain served. |
| snapshot/cache TTLs | optional | `CATHEDRAL_BOARD_TTL_SECS`, `CATHEDRAL_RECENT_CACHE_TTL_SECS`, `CATHEDRAL_MATERIALIZED_SNAPSHOT_*`, and `CATHEDRAL_DASHBOARD_SNAPSHOT_*` protect read-heavy non-miner routes. |
| `CATHEDRAL_PG_POOL_MAX` / `CATHEDRAL_THREADPOOL_TOKENS` | optional | Only set when sizing a known host. Do not cargo-cult old Railway values. |

The relaunch preflight intentionally enforces the current small-origin launch
posture over SSH: positive read admission, `CATHEDRAL_PM_READ_HARD_CAP <= 8`,
`CATHEDRAL_V2_READ_THREADS <= 4`, `CATHEDRAL_PG_STATEMENT_TIMEOUT_MS <= 4000`,
and the temporary `CATHEDRAL_WEIGHTS_WINDOW_HOURS >= 48` bridge while the gate
is held closed. Raise those ceilings only with fresh latency and coverage
evidence, not by copying old Railway defaults.

The same preflight runs `retention_tick(..., dry=True)` against the live store.
That check proves the bounded hot-state pruning path is executable and would
retain at least the active scoring window; it does not enable destructive
retention. Turning retention on is a separate DB-write decision.

It also verifies vector continuity on the private publisher: the persisted
`signed_weight_vectors.latest` row must match `weight_policy_state`, both the
persisted and served vectors must verify under the configured Ed25519 key, and
their policy versions must stay in the epoch-ms range validators expect.

## 5. Mechanism calibration (meaningful, documented, rarely changed)

| Env | Default | Meaning |
|---|---|---|
| `CATHEDRAL_PERMINER_EPOCH_BUCKET_HOURS` | 1 | Epoch width. Epoch gate accepts {current, current-1} everywhere (mint, admit, verify, payout). |
| `CATHEDRAL_PERMINER_ALLOTMENT_T1/T2` | 10000 | Virtual per-tier allotment (lazy; miners page as deep as they can solve). |
| `CATHEDRAL_PERMINER_NVARS_T*` / `NCLAUSES_T*` | 400/1704 | Instance shape per tier (α≈4.26 band). |
| `CATHEDRAL_PERMINER_WEIGHT_T*` | 1.0 / 2.0 | Tier difficulty weight. The payout bridge records the EXACT verifier weight. |
| `CATHEDRAL_V2_REAL_FRACTION` | 0 | Fraction of real (unplanted) instances. |
| `CATHEDRAL_WEIGHTS_COLDKEY_COLLAPSE` | off | Sybil hardening; assignment identity = coldkey when mapped. V2 is identity-aligned with V1; pre-bake resolves identities. |
| `CATHEDRAL_PERMINER_MAX_PAGE_LIMIT` | 50 | Origin page size cap. The edge Worker additionally clamps public traffic; the launch example pins `10` until load is proven. |
| `CATHEDRAL_PERMINER_SCORING_MODE` | bonus | `pm_primary` makes verified assigned solves the paying lane. Set explicitly for the converged relaunch. |
| `CATHEDRAL_WEIGHTS_MODE` / `CATHEDRAL_WEIGHTS_TIER2_MULT` | proportional / 3.0 | Weight-vector composition and tier multiplier. Treat as payout policy, not runtime tuning. |
| `CATHEDRAL_WEIGHTS_WINDOW_HOURS` | 24 | Trailing verified-solve window for the signed vector. Only widen deliberately as a temporary fairness bridge when relaunch timing would otherwise age broad PM coverage out before miners can refill it. |
| `CATHEDRAL_WEIGHT_POLICY_FORCED_BURN_PERCENTAGE_V2` | 0 | Explicit burn-policy override. The legacy `CATHEDRAL_WEIGHT_POLICY_FORCED_BURN_PERCENTAGE` key is ignored and should not be set. |

## 6. Internal / test-only (do not set in deployments; use the profile)

`CATHEDRAL_V2_ENABLED`, `CATHEDRAL_V2_SUBMIT_BITSET_ENABLED`,
`CATHEDRAL_V2_LAZY_ISSUANCE`, `CATHEDRAL_V2_PM_PAYOUT_BRIDGE`,
`CATHEDRAL_V2_PERMINER_ENABLED` — implied by the profile; explicit values only
for tests and surgical rollout, contradictions fail closed.

Reference relaunch layout (shared env plus per-process overlay):

```sh
# shared .env.sh
export DATABASE_URL='postgresql://...'        # required by the profile
export CATHEDRAL_LAUNCH_PROFILE=v2-converged
export CATHEDRAL_EVAL_SIGNING_KEY='<pinned hex>'
export CATHEDRAL_V2_SUBMIT_TOKEN_SECRET='<stable>'
export CATHEDRAL_PERMINER_SEED_SECRET='<stable>'
export CATHEDRAL_CNF_TOKEN_SECRET='<stable, until V1 removal>'
export CATHEDRAL_WEIGHTS_COLDKEY_COLLAPSE=1
export CATHEDRAL_PG_STATEMENT_TIMEOUT_MS=4000
export CATHEDRAL_PM_READ_HARD_CAP=4
export CATHEDRAL_V2_READ_THREADS=4
export CATHEDRAL_V2_SUBMIT_BITSET_THREADS=4
export CATHEDRAL_V2_VERIFY_BATCH_SIZE=8
export CATHEDRAL_V2_VERIFY_INTERVAL_SECS=1
export CATHEDRAL_V2_VERIFY_LOCK_SECS=120
# Optional temporary launch bridge if relaunch slips close to the PM coverage cliff:
# export CATHEDRAL_WEIGHTS_WINDOW_HOURS=48
# never set CATHEDRAL_PERMINER_ENABLED (V1 surface stays off)

# public origin (:8080): CATHEDRAL_SERVICE_ROLE=all, CATHEDRAL_V2_VERIFY_WORKER_ENABLED=0
# private process (:8000): CATHEDRAL_SERVICE_ROLE=all, CATHEDRAL_V2_VERIFY_WORKER_ENABLED=1
```

Exactly one process per deployment runs the verify worker (payout bridge
executor); add a deploy smoke check for this.

## 7. Deprecation queue (post-relaunch cleanup, tracked for deletion)

- V1 miner-route flags (`CATHEDRAL_PERMINER_ENABLED` V1 surface, V1 submit
  paths) once V1 removal ships — routes are already edge-gated permanently.
- Old V1 async rollout flags (`CATHEDRAL_SUBMIT_ASYNC_ENABLED`,
  `CATHEDRAL_PM_SUBMIT_ASYNC_ENABLED`, `CATHEDRAL_ASYNC_VERIFY_ENABLED`) for
  this relaunch path; V2 bitset submit and `CATHEDRAL_V2_VERIFY_WORKER_ENABLED`
  are the path that matters.
- `CATHEDRAL_V2_SHADOW_V1_*` (shadow mirror of V1 submits).
- `CATHEDRAL_V2_PERMINER_ENV_PIN` and the whole V2->legacy env bridge once V1
  per-miner env is gone (`v2-converged` already pins automatically).
- Duplicated `CATHEDRAL_V2_PERMINER_*` twins of calibration envs.
- `CATHEDRAL_V2_DATABASE_URL`/`CATHEDRAL_V2_DB_PATH` split-store option
  (converged profile forbids it; delete after V1 removal).
