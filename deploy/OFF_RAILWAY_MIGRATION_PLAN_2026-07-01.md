# Off-Railway Migration Plan — Strangler Pattern

Date: 2026-07-01
Author: infra planning pass (read-only analysis; no code changed)
Status: draft plan for operator execution

## TL;DR — the one first move

**Stand up the V2 lean-ingress mechanism path (`scaffold/publisher/v2_lean_ingress.py`
— FastAPI + local SQLite WAL, no Postgres) on the owned Polaris validator host,
co-located with the weight-setting validator, behind a new Cloudflare origin.**
Take zero V1 traffic off Railway on day one. This is the
strangler seed: a new, attestable, low-latency ACK surface that grows while
Railway shrinks. Everything else in this plan hangs off that move.

Why this first: the live pain is `cathedral-submit` throwing
`psycopg2.pool.PoolError: connection pool exhausted` under load, and the measured
Postgres is at its `max_connections` cap (`FATAL: sorry, too many clients
already`, see `deploy/POSTGRES_STORAGE_TRIAGE_2026-06-29.md`). Railway/Postgres is
structurally the wrong low-latency ACK path. The lean-ingress module was *built
for exactly this* — its own docstring says "keeps the ACK path off
Railway/Postgres while preserving idempotency and auditability." It is the
cheapest, lowest-risk thing to move and it proves the pattern.

---

## Thesis anchor

Cathedral is a **Verified Artifact Engine**: `issued → verified → scored →
trained`, rewarding proof, not claims. The "Secure Compute" lane requires boxes
whose execution can be **attested** (TDX quote, Intel collateral). **Railway
cannot be attested** — you do not control the host, cannot pull a raw quote, and
cannot bind `report_data`. Therefore the engine's trust-bearing stages
(scored→weights composition, and eventually attested verification) *must* run on
owned hardware. This migration is not just cost/latency hygiene; it is the
precondition for the Secure-Compute story to be true.

---

## Ground truth (what exists today)

**Railway project `cathedral-subnet` services:**

| Service | Public surface | Role |
|---|---|---|
| `cathedral-publisher` | `api.cathedral.computer` | thin publisher (v4) |
| `cathedral-read` | `read.cathedral.computer` (READ_ORIGIN) | board/challenge reads |
| `cathedral-submit` | `cathedral-submit-production.up.railway.app` (SUBMIT_ORIGIN) | submit ingress — the pool-exhaustion victim |
| `cathedral-testlane` | (testnet lane) | test lane |
| `cathedral-v2-beta` | `v2-beta.cathedral.computer` (SHADOW_V1_MIRROR_ORIGIN) | isolated V2 shadow/beta |
| `Postgres` | internal | V1 legacy DB — **114 GB** |
| `Postgres-GlUA` | internal | isolated V2 DB (GlUA) |

Confirmed live origins from `deploy/edge-router/wrangler.toml`:
`READ_ORIGIN=https://read.cathedral.computer`,
`SUBMIT_ORIGIN=https://cathedral-submit-production.up.railway.app`,
`SHADOW_V1_MIRROR_ORIGIN=https://v2-beta.cathedral.computer`.

**Already off Railway:**
- Thin **validator** → owned Polaris validator host (weight-setter).
- **Edge** → Cloudflare Worker `cathedral-edge-router` (provider-agnostic; routes
  `api.cathedral.computer/*` + `submit.cathedral.computer/*`). Moving origins is a
  Worker var / DNS change, **not** a Railway operation. This is the lever.

**Attestation assets:**
- Polaris attestor: `POST :8077/attest` (live), `:8078` test; runs on
  `polarisserver` reachable over the operator's private network (Stitch); GCE spot box
  `attest-spot`. Verifier `~/attestor/{guest_quote.sh,polaris_verify.py}` (Route B,
  raw TDX quote binding `report_data`).
- Publisher-side verify: `scaffold/publisher/attest.py`, env
  `CATHEDRAL_ATTEST_ENABLED` (default OFF).

**Mechanism router:**
- Contract is authoritative (`deploy/MECHANISM_ROUTER_CONTRACT.md`), target module
  `scaffold/publisher/mechanism_router.py` — **not yet built** (INFERRED from file
  absence in `scaffold/publisher/`). The `scored → weights` composer with the one
  allocation knob. Default OFF ⇒ byte-identical to V1 vector.
- The **lean ingress** (`v2_lean_ingress.py`) already exists and is the ACK
  half of the new mechanism (SQLite WAL, single-worker enforced, idempotent
  receipts, disk/backpressure guards).

> Flag — INFERRED vs KNOWN: The task frames the validator and attestor host as
> "the owned box." KNOWN: the validator is on the owned Polaris host; KNOWN:
> attestor currently answers on `polarisserver` (Stitch) and the
> GCE `attest-spot`. Whether the validator box and a TDX-capable attest box are the
> **same** physical machine is NOT confirmed here. The validator host (Hetzner-style
> Polaris native Linux) is likely a bare VPS, not TDX-capable. **Resolve this before
> Phase 4** — the Secure-Compute attestation needs a TDX host, which may be
> `attest-spot`/a separate confidential box, not the weight box. Co-locating the
> *router* with the *validator* (Phase 1) does not need TDX; co-locating *attested
> verification* (Phase 4) does.

---

## Design principles for the strangle

1. **New surface first, migration never big-bang.** Grow the owned-box footprint
   one origin at a time; each cutover is a Cloudflare var/DNS flip with a
   sub-minute rollback.
2. **Kill the cross-provider hop.** The weight-setter is on the owned validator host. Every
   piece of the `scored → weights` path that lives on Railway is a cross-provider
   network hop on the emissions-critical path. Co-locate router + composer + lean
   ingress with the validator so the hot path never leaves the box.
3. **SQLite over Postgres for the new path.** The new mechanism path is
   append-mostly receipts + a small spec/score table. It does not need a 114 GB
   Postgres. `v2_lean_ingress` already uses SQLite WAL; the router's
   `MechanismStore` is "a table" — SQLite file on local disk. No pool to exhaust.
4. **Postgres is the last thing to move, and mostly it should shrink, not move.**
   (See Phase 3.)
5. **Default-OFF everywhere.** Router default-OFF = V1 vector byte-identical.
   Attestation default-OFF. Every phase must be a no-op until an explicit env flip.

---

## Phased plan (dependency order)

### Phase 0 — Prep on the owned box (no traffic)

- Confirm the owned validator host provisioning: Python 3.11, `.venv`, systemd unit
  capability, local disk headroom for SQLite (lean ingress caps:
  `DEFAULT_MAX_STORAGE_BYTES=1e9`, `DEFAULT_MIN_FREE_DISK_BYTES=1e8`).
- Deploy the app image/checkout to the box (systemd, not Railway). Reuse the
  validator's deploy mechanism from
  `code/cathedral-validator-migration` (KNOWN: that runbook moved the validator here).
- Put the box behind Cloudflare (grey-cloud a health check first; do **not** route
  production yet). Provision a new origin hostname, e.g.
  `ingress.cathedral.computer` (NEW — reserve it), pointing at the box.
- Verify the box can reach the Polaris attestor (`:8077`) and, if the same repo,
  that `mechanism_router.py` will be built to the contract before enabling.

Exit: box serves `/health/live` on the new origin; no public routes touch it.

### Phase 1 — Move the NEW mechanism path (lean ingress + router) to the box

This is the first move. Nothing V1 is touched.

1. Run `v2_lean_ingress` on the owned validator host (single worker — the module *refuses*
   multi-worker for SQLite WAL). SQLite receipt DB on local disk.
2. Build `scaffold/publisher/mechanism_router.py` to
   `deploy/MECHANISM_ROUTER_CONTRACT.md`, `MechanismStore` backed by a local
   SQLite table, and run its `compose()` **in the validator process** (same box).
   Default: no mechanism enabled ⇒ weight output byte-identical to V1.
3. Wire the V2 bitset submit test traffic (`/v2/agents/submit-bitset`, currently
   pointed at `v2-beta.cathedral.computer` on Railway) to the box's new origin via
   the Worker's `SHADOW_V1_MIRROR_ORIGIN` / a new V2 origin var. Keep sample low
   (mirror is already `0.1%`, default-off).
4. Soak: run the isolated public replay-spam test path (already built,
   `deploy/V2_LEAN_INGRESS_SPAM_TEST_RUNBOOK`) against the box, not Railway.

Rollback: point the V2 origin var back at `v2-beta.cathedral.computer`. V1 never
moved, so V1 miners are unaffected regardless.

Exit: V2 ingress ACKs served from owned box; router composes on-box (still 0%
weight); Railway `cathedral-v2-beta` is now shadowed by the box.

### Phase 2 — Repoint Cloudflare origins, one service at a time

The Worker (`worker.mjs`, vars in `wrangler.toml`) is the cutover switch. Order by
**lowest blast radius first**. Each step = change one origin var + `wrangler
deploy`, watch, keep the old Railway service warm for rollback.

Recommended order:
1. **Reads** (`READ_ORIGIN` → box). Cacheable, idempotent, non-emissions.
   Reads already have a board-failover worker (`edge-router/board-failover/`) for
   isolation — use it as the rollback path. Risk: the box must serve the read
   endpoints listed in `ROLE_SPLIT_RUNBOOK` (active-challenges, leaderboard,
   weights/next, JWKS) with the `CATHEDRAL_PG_STATEMENT_TIMEOUT_MS=4000` guard —
   **but** these reads currently need V1 Postgres. Do NOT move reads until the box
   can reach the V1 data (see Phase 3); until then reads **stay on Railway**.
2. **Submit** (`SUBMIT_ORIGIN` → box). This is the PoolError victim and the biggest
   win. Move only once the box's submit path is durable (lean ingress receipt +
   async flush to the scoring ledger). Because submit ACK on the box is SQLite,
   pool exhaustion cannot recur on the ACK. Risk: verification/scoring still needs
   the ledger (Postgres) during the strangle — the flusher writes async, so a
   Postgres blip degrades to backlog, not a 5xx to the miner.
3. **Publisher `api.cathedral.computer`** last — it is the compatibility surface and
   the most-integrated; move it only after read+submit are proven on the box.

Per-step rollback: revert the single Worker var, `wrangler deploy`, done in
seconds. DNS (orange-cloud) stays on Cloudflare throughout, so no propagation wait.

Exit: `read` and `submit` origins served from the owned validator host; publisher optionally
still on Railway as compat.

### Phase 3 — The hard part: the two Postgres (114 GB V1 + GlUA V2)

Three options for the V1 `Postgres` (114 GB):

- **A. Keep V1 Postgres on Railway during the strangle (recommended interim).**
  The box's read/submit services connect to Railway Postgres over the public proxy
  during the transition. Pros: no data migration risk, no downtime. Cons: keeps a
  cross-provider hop for reads and a Railway dependency; **must first relieve the
  connection cap** (lower per-service `CATHEDRAL_PG_POOL_MAX`, add PgBouncer, or
  raise `max_connections` — the triage flagged the DB at its cap as a direct cause
  of read-plane timeouts). This is the pragmatic default so migration isn't blocked
  on a 114 GB move.

- **B. Migrate to self-hosted Postgres on owned hardware.** Stand up Postgres on
  the box (or a sibling), `pg_dump`/logical-replicate, cut over. Pros: no Railway,
  no cross-provider hop, own the connection ceiling. Cons: **114 GB is a real
  migration** — hours of copy, a replication catch-up window, and the box needs the
  disk + a real backup story you now own. High operational weight.

- **C. Shrink then retire (recommended end-state, do it regardless of A/B).**
  The triage shows the 114 GB is ~all live data, and `eval_runs` (53 GB) +
  `per_miner_witnesses` (20 GB) = ~64% is already covered by **default-off
  retention** (`scaffold/publisher/retention.py`). Enable retention (needs explicit
  `DB WRITE APPROVED`), close the gaps (`agent_submissions`, `submit_signatures`,
  `per_miner_attempts.solution_body`), then `pg_repack`/`VACUUM FULL` to physically
  shrink. A shrunk DB (~40–50 GB or less) makes option B *cheap*, or makes A
  affordable long-term.

**Recommendation:** **A now, C in parallel, B only after C** (and only if you want
Railway fully gone). Concretely: relieve the connection cap this week so
read/submit can safely dual-home; run retention + repack to get the DB small; then
a small DB is a weekend `pg_dump | psql` to the owned box for the final retire.

**GlUA (V2) DB:** isolated and (INFERRED) small — V2 is beta. Migrate GlUA to the
box's local Postgres/SQLite early and cheaply; it has no legacy weight. Doing GlUA
first de-risks the V1 move by rehearsing the pattern on a small DB.

Risks to name explicitly:
- `VACUUM FULL` takes an exclusive lock + needs free headroom ≈ table size; use
  `pg_repack` if available. Do inside a maintenance window.
- Any DELETE/UPDATE/VACUUM is gated on explicit `DB WRITE APPROVED` per the triage
  safety boundary. This plan does not authorize writes.
- A logical-replication cutover (B) must fence the single-writer publisher
  sequencer (`DISTRIBUTION_ARCHITECTURE_PLAN` invariant #1) so no stale writer
  publishes after the switch.

### Phase 4 — Attestation: light up the Secure-Compute story on owned hardware

Now that trust-bearing stages run on a box you control:

1. Enable `CATHEDRAL_ATTEST_ENABLED` on the box (default-OFF today). The
   publisher-side verifier (`attest.py`) can now gate the attested multiplier on a
   real TDX quote instead of the stub — because the box can call the Polaris
   attestor (`POST :8077/attest`) and, if the box itself is TDX-capable, produce
   its own `guest_quote.sh` quote.
2. Bind the **router/verifier execution** into `report_data`: the value of running
   on owned hardware is that "which solver produced this / on what host" becomes
   provable. Railway could never sign this.
3. **Resolve the host question from the top flag:** confirm whether the owned validator host
   is TDX-capable. If not, attested *verification* runs on the confidential box
   (`attest-spot`/a TDX H100/H200 per the Horde pivot) while the *weight
   composition* stays on the validator host — both owned, neither Railway. The attestor
   delegate pattern (`LANE2_SECURE_COMPUTE_PLAN` → `attestor_delegate` to Polaris
   attestor) already supports this split.

Exit: the Secure-Compute lane can make attested claims because the engine runs
where you can pull a quote — the thing Railway structurally blocks.

### Phase 5 — Retire Railway

Preconditions: reads + submit + publisher origins all served from owned box(es);
V1 DB either self-hosted (B) or shrunk-and-copied; GlUA migrated; 1–2 weeks of
clean soak with no Railway origin in the hot path.

Then, one service at a time: remove the Railway service from the Cloudflare origin
map (already done in Phase 2), stop the Railway service, wait a cooldown, delete.
Delete Postgres/Postgres-GlUA **last**, only after a verified off-Railway backup
exists. Keep the final `pg_dump` artifact off Railway (mirror the Firestore-archive
pattern: private repo / local tarball).

---

## What stays on Railway until fully strangled

- **V1 `Postgres` (114 GB)** — until Phase 3 option B completes (or you accept A as
  long-term). This is the tail dependency.
- **`cathedral-read` / `cathedral-publisher`** — until the box can serve their reads
  against the (migrated or dual-homed) V1 data.
- **`cathedral-testlane`** — low priority; can linger or move with GlUA.

Everything else (edge, validator, new V2 ingress, mechanism router) is either
already off Railway or moves in Phases 1–2.

---

## Rollback summary (per phase)

| Phase | Rollback |
|---|---|
| 1 (new path) | Point V2 origin var back to `v2-beta.cathedral.computer`; V1 untouched. |
| 2 (origin flip) | Revert one Worker var, `wrangler deploy` (seconds); Railway service kept warm. |
| 3A (dual-home) | Point box services back at Railway Postgres proxy. |
| 3B (DB migrate) | Keep Railway Postgres as hot standby until cutover verified; fail back by DSN. |
| 3C (retention) | Gated on `DB WRITE APPROVED`; deletes are irreversible — dry-run first (`CATHEDRAL_RETENTION_DRY_RUN=1`). |
| 4 (attest) | `CATHEDRAL_ATTEST_ENABLED=false`; multiplier reverts to base rate, no submit path affected. |
| 5 (retire) | Do not delete until backup verified; stop-then-wait before delete. |

---

## Operator checklist (crisp, in order)

- [ ] **P0** Provision the owned validator host: py3.11 venv, systemd, disk headroom, attestor reachability. Reserve `ingress.cathedral.computer`, grey-cloud health only.
- [ ] **P0** Relieve Postgres connection cap NOW (lower `CATHEDRAL_PG_POOL_MAX` / PgBouncer / raise `max_connections`) — unblocks everything.
- [ ] **P1** Deploy `v2_lean_ingress` on the box (single worker, SQLite WAL); verify durable receipts + idempotency.
- [ ] **P1** Build `mechanism_router.py` to the contract; run `compose()` in-validator on-box, default 0% (V1 byte-identical). Add tests, keep suites green.
- [ ] **P1** Route V2 test traffic to the box origin via Worker var; run the spam-test soak against the box.
- [ ] **P2** Flip `SUBMIT_ORIGIN` → box only after async flush→ledger is durable; watch PoolError disappear on ACK.
- [ ] **P3** Migrate **GlUA/V2 DB** to the box first (rehearsal).
- [ ] **P3** Enable retention (dry-run → `DB WRITE APPROVED` → run → `pg_repack`) to shrink V1 to <~50 GB.
- [ ] **P2** Flip `READ_ORIGIN` → box once box can reach V1 data (dual-home A or migrated B); board-failover worker is the rollback.
- [ ] **P3** `pg_dump | psql` shrunk V1 to owned Postgres; cut DSN over; keep Railway PG as standby.
- [ ] **P2** Flip `api.cathedral.computer` publisher origin → box (last, compat surface).
- [ ] **P4** Confirm TDX host (resolve the validator host vs `attest-spot`); enable `CATHEDRAL_ATTEST_ENABLED`; bind router/verifier into `report_data`.
- [ ] **P5** Verify off-Railway backups; stop → cooldown → delete Railway services; delete `Postgres`/`Postgres-GlUA` last.

---

## Open questions to close before executing

1. Is the owned validator host TDX-capable, or does attested verification need a separate
   confidential box? (Gates Phase 4 shape.)
2. Disk budget on the owned box for a self-hosted 114 GB (or shrunk) Postgres +
   backups? (Gates Phase 3 A vs B.)
3. Is `mechanism_router.py` built yet on any branch? (Contract exists;
   file absent in `scaffold/publisher/` as of this pass.)
4. Confirmed size of the GlUA V2 DB (assumed small).
