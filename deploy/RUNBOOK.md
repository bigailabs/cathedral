# GO-LIVE RUNBOOK — Cathedral v4 thin publisher

Take over `https://api.cathedral.computer` with the thin publisher, **zero
validator updates**, minimal miner disruption. This is the step-by-step for
Fred. The two buttons only Fred presses are flagged 🔑 (production signing key)
and 🔀 (Railway same-URL swap).

> Invariant the whole plan rests on: validators only consume signed rows. The
> thin publisher emits byte-identical v5/v6 rows under the same Ed25519 key
> (proven by `wire_compat.py` 8/8 and `publisher_verify.py`). As long as the
> feed stays signed by the production key under the frozen schema, validators
> never know the backend changed.

---

## 0. Pre-flight (do once, before touching anything live)

On a machine with the cathedral venv:

```bash
cd ~/code/cathedral-scaffold
~/code/cathedral/.venv/bin/python rc_verify.py            # expect: RC GATE PASS
~/code/cathedral/.venv/bin/python wire_compat.py          # expect: WIRE COMPAT PASS 8/8
~/code/cathedral/.venv/bin/python publisher_verify.py     # expect: PUBLISHER VERIFY PASS
```

All three must be green. If any fails, STOP — the wire surface is not safe to ship.

Build the image locally to confirm it builds:

```bash
docker build -f deploy/Dockerfile -t cathedral-thin:golive .
docker run --rm -e PORT=8000 -p 8000:8000 cathedral-thin:golive &
curl -s localhost:8000/health    # {"status":"ok",...,"sr25519_backend":"bittensor"}
```

`sr25519_backend` MUST read `bittensor` (not `stub-fail-closed`). If it says
stub, the image is missing `bittensor-wallet` and every miner submit will 401 —
do not deploy.

---

## 1. Stage the thin publisher (NEW Railway service, NOT the live one)

Create a **second** Railway service from this repo (`deploy/railway.toml`,
`deploy/Dockerfile`). Give it its own URL (e.g. `thin-staging.up.railway.app`).
Attach a **persistent volume** mounted at `/app/data` (the SQLite store).

Set env (see `deploy/.env.example`). For staging:

- `CATHEDRAL_EVAL_SIGNING_KEY` — **leave UNSET** (staging uses a throwaway dev
  key; that's fine for shadow soak — the soak harness verifies seeded rows under
  the production key and newly-minted rows under the staging key separately).
- `CATHEDRAL_DB_PATH=/app/data/publisher.db`
- `CATHEDRAL_REFILL_ENABLED=true`  ·  `CATHEDRAL_REFILL_TARGET_T1=25` ·
  `CATHEDRAL_REFILL_TARGET_T2=25`

Deploy. Confirm `GET /health` is 200 and `sr25519_backend=bittensor`.

---

## 2. Seed the staging store from the LIVE public surface (G1)

From any box that can reach both the live API and the staging volume (or run it
inside the staging container via `railway run`):

```bash
# dry-run first — prints what would be seeded, writes nothing
python -m scaffold.publisher.seed_live --db /app/data/publisher.db --dry-run

# real seed: last 7 days of signed rows (verbatim, NOT re-signed) + board mirror
python -m scaffold.publisher.seed_live --db /app/data/publisher.db --days 7
```

Notes:
- The seeder pulls `/v1/leaderboard/recent` with the tuple cursor and stores
  rows **verbatim including their existing `cathedral_signature`** — re-serving
  rows signed by the production key is valid; we never re-sign.
- It is **idempotent + resumable**: re-run any time, it resumes from the durable
  watermark. The live feed is slow (~50s per 500-row page), so a full 7-day seed
  can take a while — leave it running; it picks up where it stopped.
- It mirrors the active board as **metadata-only** challenges
  (`cnf_source=external`); CNF bodies stay on live / are replaced by G2 mints.

Verify the seeded watermark equals the newest live `(ran_at,id)` so the cursor
is continuous (the seeder prints it; this prevents validator double-pull/miss at
swap time).

---

## 3. Soak / shadow checks (G4) — gate the swap

Run the soak harness against the staging publisher. Pass the staging dev pubkey
so the keyset accepts both production-signed (seeded) and staging-signed (minted)
rows:

```bash
THIN=https://thin-staging.up.railway.app
THIN_PUB=$(curl -s $THIN/.well-known/cathedral-jwks.json \
  | python -c "import sys,json;print(next(k['public_key_hex'] for k in json.load(sys.stdin)['keys'] if k['kid']=='cathedral-eval-signing'))")
CATHEDRAL_SOAK_EXTRA_PUBKEYS="staging_test=$THIN_PUB" \
  python -m scaffold.publisher.soak --thin $THIN --days 7
```

The report must read **`SOAK REPORT: PASS`**, with:
- `unverified (thin) = 0` — every served row verifies under a configured key.
- `max 7d divergence` under the **0.10 fail** threshold (warn at 0.01). Live
  scoring is flat 1.0, so a faithful re-served feed diverges ~0.0000.
- `miner smoke = ok` — token fetch + solve + submit ranked on a minted challenge.

Let it soak (V4-DESIGN target: ~2 weeks green) before the swap. Re-run the soak
daily; watch the divergence band stays OK/WARN, never FAIL.

---

## 4. 🔑 Promote the production signing key (staging → prod config)

When soak is green and you're ready, set on the **target** service:

- `CATHEDRAL_EVAL_SIGNING_KEY` = the 32-byte hex of the **live production key**
  (pubkey `10890a66…f25e26`). This is the only true secret. Store it in Railway
  service variables, never in git.

Restart; confirm `GET /.well-known/cathedral-jwks.json` now serves
`public_key_hex=10890a66…f25e26` (identical to live), and that newly-minted rows
verify under the production key (re-run the soak WITHOUT the extra staging key —
`unverified` must stay 0).

Re-seed once more right before swap so the watermark is current:
`python -m scaffold.publisher.seed_live --db /app/data/publisher.db --days 7`.

---

## 5. 🔀 Same-URL swap (Railway)

Keep the monolith service **warm** (do not delete/stop it) for instant rollback.

In Railway, move the custom domain `api.cathedral.computer` from the monolith
service to the thin-publisher service (Settings → Domains). Railway holds traffic
on the thin service until its `/health` is 200 (`healthcheckPath=/health`), so the
cutover only completes once it is actually serving.

Immediately after the domain flips:

```bash
curl -s https://api.cathedral.computer/health
curl -s https://api.cathedral.computer/.well-known/cathedral-jwks.json | grep 10890a66
python -m scaffold.publisher.soak --thin https://api.cathedral.computer --days 7
```

---

## 6. Soak checks post-swap (first hour, then daily)

- `/health` 200, `sr25519_backend=bittensor`, `signing_key=loaded`.
- Validators still advancing their cursor (no error spikes; rows keep flowing).
- Board holds 25 tier1 + 25 tier2 active (refill loop running).
- Miners getting `ranked` submits; distinct-solver retirement turning challenges
  over (~1h age / 64-solver cap).
- Soak `max 7d divergence` stays under 0.10.

---

## 7. ROLLBACK (instant — keep the monolith warm)

If anything below the abort line trips: **move the domain back to the monolith
service** in Railway. That's it — the monolith never stopped, its DB is intact,
and validators resume against it transparently (same URL, same key, same rows).
No data migration needed (the thin publisher only re-served the live feed; it
authored no rows the monolith doesn't also have until after swap, and those are
just more signed rows under the same key).

After rollback, capture the thin publisher's logs + DB for diagnosis before
redeploying.

---

## 8. ABORT CRITERIA (flip back immediately if ANY occurs)

- `/health` not 200, or `sr25519_backend=stub-fail-closed`, or
  `signing_key` not `loaded`.
- JWKS does not serve `10890a66…f25e26` (validators would reject every row).
- Soak `unverified > 0` (a served row fails signature) — the wire surface broke.
- Soak `max 7d divergence ≥ 0.10` against live — scores would move miners' income.
- Validator error rate climbs or cursor stalls (double-pull / missed rows).
- Mass miner `401`/`409` on submit (auth or lock regression).
- Any unhandled 5xx on `/v1/leaderboard/recent` (the frozen feed must never 5xx).

---

## What is NOT in this swap (deferred by design)

- **Burn step-down.** The 85% burn is hardcoded validator-side (local-weights
  mode). It does NOT change at swap — it ships later as the one bundled
  "jackpot release" (V4-DESIGN.md migration §). This swap is feed-backend-only.
- **Lane S/I activation as on-chain value.** Endpoints exist (additive) but
  ramping their row values is a deliberate, gradual publisher-side decision
  inside the 7-day window — not part of go-live.
