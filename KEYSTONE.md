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
