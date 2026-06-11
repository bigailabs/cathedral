# Keystone — the load-proof publisher architecture

*Decided 2026-06-10/11. The infrastructure rebuild that makes the king-factory
runnable. Supersedes "just swap SQLite for Postgres" — that was half of it.*

## The problem (proven twice in production)
The live publisher is ONE service with ONE SQLite connection behind ONE lock,
doing all of: serving challenges (read-heavy — thousands of miners poll the
board + fetch CNFs), accepting submissions (write-heavy — ~5k solves/hr),
minting, signing, and the validator feed. A burst of reads starves writes (and
vice-versa) → everything queues → **wedge**. It froze the board on 2026-06-03
and again 2026-06-10. The 30s response cache (#267) and IP limiter (#266) were
band-aids on exactly this. Also: the publisher sits at **20 GB RAM (63% of the
32 GB ceiling) and climbing** — cost + OOM risk.

## The insight
- **Postgres fixes the *contention*:** MVCC = readers never block writers,
  many concurrent connections. The wedge class dies. NECESSARY.
- **But the read load shouldn't hit a database at all.** "Publishing challenges"
  is a *broadcast* problem: the board is ~51 challenges changing ~hourly; CNF
  bodies are **immutable** (a challenge_id maps to a fixed file forever). Serving
  that by re-querying a DB thousands of times is backwards. Broadcast it and the
  publisher's read load goes to **~zero**. SUFFICIENT.

Postgres kills the wedge; the broadcast tier kills the load. Both, complementary.

## The architecture — three tiers
| Tier | Job | Scales | Touches DB |
|---|---|---|---|
| **Serve / broadcast** — CDN + cache | board + CNFs to miners | horizontally, ~infinite, cheap | no |
| **Write** — thin publisher + Postgres | submissions, minting, signing | bounded by solve rate (~5k/hr, trivial) | yes (Postgres, no lock) |
| **Feed** — read replica / cache | validator pulls (~11 validators) | tiny | read-only |

- **CNF bodies → object storage + CDN** (R2 / S3 / Cloudflare). Immutable →
  cache-forever; **signed URLs** preserve today's access-gating. The CDN absorbs
  the flood; the DB never sees a fetch.
- **Board (active set) → one small JSON** at the edge / short-TTL cache (this is
  where Redis earns a clear job, if used). Updated only on mint/retire.
- **Submissions** are the only true write path → publisher + Postgres.
- **Validator feed** off a read path, not the write primary.

This is load-proof by design: the part that gets hammered (reads) scales at the
edge with zero DB; the part that needs state (writes) is bounded and small. The
flood that took us down would hit a CDN and evaporate.

## Deployment — a clean, isolated project (net-new = free to split)
We're standing up the thin publisher + a new Postgres anyway, so do it right:
- **New Railway project `cathedral-subnet`** — isolated billing + blast radius,
  finally separated from the Polaris platform (which keeps `keen-passion`).
- **Thin publisher** deployed there from the `v4` branch (the in-app seeder
  re-pulls the feed automatically; signing key is one env var → ~no rework lost
  from the keen-passion staging).
- **Dedicated Postgres** in that project (co-located → internal network).
- **`api.cathedral.computer` points at the new project's publisher** at cutover
  (a domain can target a service in any project). Monolith stays warm in
  keen-passion for instant rollback.
- **`sat-generator` STAYS in keen-passion** (Fred's call — different problem
  space; it MINTS challenges, the publisher SERVES them; they talk over the
  generator's public URL already).
- **Redis:** open question — confirm whether the publisher needs it; with the
  broadcast tier, Redis's natural job is the cached board (not the write path).

## What it fixes (all at once)
- The wedge (Postgres, no lock).
- The load (broadcast tier — reads never touch the DB).
- The 20 GB RAM / OOM risk + Railway cost (thin publisher is ~0.5 GB; DB load
  moves to Postgres, not the app's heap).
- Isolation from Polaris (own project, clean billing).
- Gives the **arena (king-factory) a stable place to run** — the whole point.

## Build order
1. New `cathedral-subnet` project + dedicated Postgres.
2. Port the thin publisher `Store` (SQLite → Postgres pool; ~190 clean lines,
   one query/write boundary). Re-seed (no data migration).
3. Broadcast tier: CNFs → object store + signed URLs; board → cached JSON.
4. Soak (divergence 0 under the production key), then point the domain.
5. THEN activate Lane S (arena) on the stable base.

Scaffold-only and net-new throughout — live prod keeps serving from keen-passion
until the deliberate domain swap. Zero risk to production during the build.

---

## Build state (2026-06-11)

Steps 1-4 of the build order are **done and verified**; step 4 (soak under the
PROD key) and step 5 (arena) remain, both Fred-gated. Live prod was never
touched — everything below is net-new in the isolated `cathedral-subnet` project.

### Provisioned (Railway — workspace Polaris Cloud AI, 63bd40db-…)
| Resource | ID | Notes |
|---|---|---|
| Project `cathedral-subnet` | `0f7f451f-dca5-4457-9c2d-53aa5e711d87` | isolated billing + blast radius |
| Environment `production` | `2021bae8-7bb0-4bc4-aa71-09b400eb3d12` | |
| Service `Postgres` | `072b84b3-b70b-4226-985d-9a6125a62068` | image `postgres-ssl:18`, US West, 48.8 GB vol |
| Service `cathedral-publisher` | `b2f3e147-3231-4766-89b3-330b39ba504a` | GitHub `cathedralai/cathedral` branch `v4`, Dockerfile `deploy/Dockerfile` |
| Bucket `cathedral-cnf` | `c28759ad-6dfd-4169-a91c-1dc03269c571` | region `sjc`, for the CNF bucket backend (creds not yet wired) |
| Public URL | `https://cathedral-publisher-production-f2ae.up.railway.app` | Railway-generated; NOT api.cathedral.computer |

- **Internal DATABASE_URL pattern:** `postgresql://postgres:<pw>@postgres.railway.internal:5432/railway`
  (wired into the publisher as `DATABASE_URL=${{Postgres.DATABASE_URL}}`).
- **Public proxy (for off-box verify only):** `postgresql://postgres:<pw>@acela.proxy.rlwy.net:13794/railway`.
- **No volume on the publisher** — Postgres makes the app stateless (also kills
  the volume-deploy-deadlock class). The Dockerfile's `/app/data` path is unused
  in PG mode (Store ignores CATHEDRAL_DB_PATH when DATABASE_URL is a PG DSN).
- **Publisher env:** `CATHEDRAL_SEED_ON_BOOT=true`, `CATHEDRAL_REFILL_ENABLED=true`
  (targets 25/25 per tier via refill defaults), `CATHEDRAL_SEED_BASE_URL=
  https://api.cathedral.computer`, `RAILWAY_DOCKERFILE_PATH=deploy/Dockerfile`.
  Signing key is UNSET → auto-generated DEV key (kid `cathedral-eval-signing`,
  pub `d8e9a3e0…`). NOT the prod key.

### Built (scaffold — committed on master + mirrored to public `v4`)
- **Dual-backend Store** (`scaffold/publisher/store.py`): backend chosen by the
  connection string — a `postgres[ql]://` DSN (passed in or via `DATABASE_URL`)
  selects a psycopg2 `ThreadedConnectionPool` with **no global write lock** (MVCC
  kills the single-lock wedge); anything else is SQLite (single conn + RLock +
  BEGIN IMMEDIATE, unchanged). A narrow dialect translator (`_translate_sql`,
  `_PgConn`) rewrites the inline SQLite SQL the codebase emits (`?`→`%s`,
  `INSERT OR IGNORE`→`ON CONFLICT DO NOTHING`, `INSERT OR REPLACE`→`ON CONFLICT … DO UPDATE`,
  `datetime('now')`→`now()`) so `app.py`/`scoring.py`/`refill.py`/`seed_live.py`
  run **unchanged**. Parallel `_MIGRATIONS_PG` (portable DDL). `psycopg2-binary`
  added to `deploy/requirements.txt`.
- **Broadcast tier — board** (`scaffold/publisher/board_cache.py` + `app.py`
  `/v1/synthetic-boolean/active-challenges`): served from an in-process cached
  snapshot rebuilt only on mint/retire (process-global `invalidate_all()` called
  from `seed_challenge`, the submit lock path, and `refill.retire_ready`) plus a
  10 s TTL safety net. ETag + `Cache-Control: public, max-age=15`; conditional GET
  returns 304. Reads no longer hit the DB per request.
- **Broadcast tier — CNF** (`scaffold/publisher/cnf_store.py` + `app.py`
  `/v1/challenges/{id}/cnf`): backend-switchable `CNFStore` — `db` (inline body +
  `Cache-Control: public, max-age=2592000, immutable` + sha256 ETag) today;
  `bucket` (S3-compatible: put-on-mint, serve via presigned 302) when
  `CATHEDRAL_CNF_BACKEND=bucket` and the four `CATHEDRAL_CNF_S3_*` vars are set
  (fail-closed to `db` if incomplete). The HMAC fetch-token gate is preserved in
  both backends. `boto3` added (lazy import — inert under `db`).

### Gate + integration results (all green)
Run in the prod container as a pure python sandbox (read-only; isolated `/tmp`
venv for the PG/HTTP checks — the prod service was never modified):
- `rc_verify.py` → **PASS 35/35**
- `wire_compat.py` → **PASS 8/8**
- `publisher_verify.py` → **PASS 67/67** (note: now 67 checks, the brief's "41"
  was stale — the scaffold grew arena/coverage checks)
- `postgres_verify.py` (NEW, vs the real Railway Postgres) → **PASS 19/19**
  (migrations idempotent, insert_row OR-IGNORE, seed_state upsert, recent_rows
  tuple cursor, claim_solve distinct claim, OR REPLACE + board ON CONFLICT
  upsert, and a 4-thread×20-write MVCC concurrency proof: 80 rows, 0 errors, 0.4 s)
- broadcast-tier check (board ETag/304/no-rebuild + CNF immutable headers) → PASS
- `live_smoke.py` (NEW, vs the DEPLOYED publisher over HTTP) → see deploy verify.

### Deploy verification (live)
- `/health` → 200, `sr25519_backend=bittensor` (prod backend, not the stub).
- `/v1/synthetic-boolean/active-challenges` → 200, **count 50** local minted
  challenges (refill 25/25 per tier), `Cache-Control: public, max-age=15` + ETag;
  conditional GET → **304**.
- `/v1/leaderboard/recent` → serving seeded signed rows (verbatim from the live
  feed; the in-app seeder is backfilling per the `[seed]` logs).
- `/.well-known/cathedral-jwks.json` → the publisher's own dev key.
- Miner smoke (sign → token-fetch CNF → DPLL solve → submit → feed pull): the
  write path commits to Postgres and the fresh row verifies under the JWKS key.

### Remaining — FRED-ONLY (do not automate)
1. **Soak** the staging publisher against the **production signing key**
   (`CATHEDRAL_EVAL_SIGNING_KEY` upsert on `cathedral-publisher`, fresh deploy)
   and confirm divergence 0 vs live before any swap. Key promotion is deliberate.
2. **Domain swap**: point `api.cathedral.computer` at `cathedral-publisher` in
   `cathedral-subnet` (a domain can target a service in any project). Keep the
   keen-passion monolith warm for instant rollback. No customDomain mutation has
   been or should be done by automation.
3. (Optional) **Wire the CNF bucket**: `railway bucket credentials --bucket
   cathedral-cnf` → set `CATHEDRAL_CNF_BACKEND=bucket` + `CATHEDRAL_CNF_S3_ENDPOINT
   /_BUCKET/_ACCESS_KEY/_SECRET_KEY`, fresh deploy. Inert until then.
4. **Lane S (arena)** activation on the stable base.

### Exact verification commands
```
# Gates + PG + smoke (prod container as sandbox; isolated /tmp venv):
cd /c/Users/fred/code/cathedral-scaffold && tar czf /tmp/sc.tgz --exclude=.git --exclude=.pgvenv .
scp -o StrictHostKeyChecking=accept-new -o ControlMaster=no -o ControlPath=none \
  -i /c/Users/fred/.ssh/remote_access /tmp/sc.tgz railway-cathedral-publisher:/tmp/sc.tgz
ssh … railway-cathedral-publisher "cd /tmp/sc && tar xzf /tmp/sc.tgz -C /tmp/sc && \
  python3 rc_verify.py 2>&1|tail -2 && python3 wire_compat.py 2>&1|tail -2 && \
  python3 publisher_verify.py 2>&1|tail -2"
# postgres_verify (needs DATABASE_URL=<public proxy DSN> in /tmp/sc/pg_dsn.env):
ssh … "cd /tmp/sc && set -a && . ./pg_dsn.env && set +a && .pgvenv/bin/python postgres_verify.py"
# live smoke vs the deployed publisher:
ssh … "cd /tmp/sc && BASE_URL=https://cathedral-publisher-production-f2ae.up.railway.app \
  .pgvenv/bin/python live_smoke.py"
# live health/board:
curl -s https://cathedral-publisher-production-f2ae.up.railway.app/health
curl -s -D - https://cathedral-publisher-production-f2ae.up.railway.app/v1/synthetic-boolean/active-challenges | head
```
