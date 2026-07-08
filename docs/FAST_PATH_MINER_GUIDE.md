# Cathedral Fast Path — Miner Guide

**What it is:** a faster, push-driven miner lane for SN39 — **real, checkable problems**, tiny signed submits, and **10% of subnet incentive** routed to the miners who solve them.

Cathedral rewards *proof of work done*, not claims. The fast path is where that starts.

---

## What's new

- **Real puzzles.** ~10% of your challenges are now genuine combinatorial problems — **graph k-coloring** and **Latin-square completion** encoded as SAT — verified exactly. The other ~90% are the usual planted 3-SAT.
- **Push, don't poll.** The challenge board is now published as a single, consistent, edge-cached snapshot. Read it once when it changes instead of hammering GET in a loop (details below).
- **Tiny signed submits.** Submit a compact signed *bitset* of your assignment instead of a full DIMACS body.
- **Solver provenance.** Your submit now carries a signed `solver_id` + `solver_hash` (and optional `image_url`) — see [Solver provenance](#solver-provenance).
- **10% reward.** Verified fast-path solves feed a `cathedral_sat_fast` score blended into the signed weight vector at **10%** (registered hotkeys only). Solve on the fast path → earn a share of that 10%.

## 1. Stop polling — read the broadcast

The board is published as one small, **consistent** snapshot on Cloudflare's edge. Don't loop-poll the challenge endpoints — track the snapshot instead:

- **Pointer (read this):** `https://api.cathedral.computer/sat/latest.json` — one small file: a `sequence` id, and a link to the current `board.json`. It's edge-cached and identical on every request, so it's cheap and safe to read often.
- **Push nudge:** subscribe to the stream `https://api.cathedral.computer/sat/events` (Server-Sent Events). You get an event the moment a new snapshot is ready; then re-read `latest.json`. No busy-polling.
- **Detecting change:** the `sequence` changes only when the board changes. Cache it; act only when it differs.

> Use `api.cathedral.computer` or `cathedral.computer` for the broadcast. (`read.` and `v2-beta.` bypass the edge and won't serve the consistent snapshot.)

## 2. Fast-path flow

Your per-miner challenges are private to your hotkey, so you fetch your own set from the V2 endpoints on `v2-beta.cathedral.computer`:

1. **Fetch your challenges** (signed read, your hotkey):
   `GET https://v2-beta.cathedral.computer/v2/synthetic-boolean/per-miner/challenges?limit=10`
   → each item: `challenge_id`, `tier`, `seq`, `n_vars`, `kind` (`coloring`/`latin`/planted), `cnf_sha256`, and `token_source`.
2. **Fetch the CNF and solve it:**
   `GET https://v2-beta.cathedral.computer/v2/synthetic-boolean/per-miner/cnf?challenge_id=...&tier=...&seq=...`
   → read the `X-Cathedral-Submit-Token` response header. Under the converged V2 profile, challenge pages are lazy and do **not** include per-item `submit_token` values.
3. **Submit the signed bitset** with that header token (and solver provenance — see below):
   `POST https://v2-beta.cathedral.computer/v2/agents/submit-bitset`
4. **Check your receipt:**
   `GET https://v2-beta.cathedral.computer/v2/agents/submit-bitset/receipts/{receipt_id}`

## 3. Solver provenance

Your bitset submit now includes a signed declaration of *how* you solved it:

- `solver_id` — your solver name/version, e.g. `kissat-4.0.1` (chars `A–Z a–z 0–9 _ . : + / -`, ≤64).
- `solver_hash` — a hash identifying that solver build, hex or `sha256:...` (≤80).
- `image_url` — *optional*: a link to your solver image (`https://`, `oci://`, `docker://`, `ipfs://`, `hippius://`). **Stored, not used yet.**

These fields are part of the **signed** submit — they're bound to your hotkey and can't be forged. They do **not** affect scoring today; they're how Cathedral will verify *how* miners solve as the subnet moves toward attested execution. Include them now so you're ready.

*(Currently optional; will become required — set it up now.)*

## Requirements

- A hotkey **registered on SN39** (the 10% is registration-gated — unregistered hotkeys are not paid).
- A SAT solver (e.g. `cadical`, `kissat`). The real puzzles are small and guaranteed satisfiable — any competent solver handles them instantly.

## Reference miner

`scripts/v2_bitset_miner_e2e.py` does the whole loop (fetch → solve → sign → submit with solver metadata → receipt):
```
python3 scripts/v2_bitset_miner_e2e.py \
  --challenge-base https://v2-beta.cathedral.computer \
  --submit-base   https://v2-beta.cathedral.computer \
  --solver kissat-4.0.1 --limit 8
```

## How the reward works

1. You solve fast-path challenges; Cathedral verifies each witness.
2. Verified solves become your `raw_score` on the fast-path scoreboard.
3. Cathedral blends that scoreboard into the **real** signed weight vector at 10%, **registered miners only**, normalized and capped.
4. Validators consume that one Cathedral-signed vector — so your fast-path work shows up in your on-chain weight. Nothing changes on the validator side.

## Watch it live

- Solve/verify rate + backlog: `https://v2-beta.cathedral.computer/v2/verify/metrics`
- Fast-path scoreboard: `https://v2-beta.cathedral.computer/v2/validator/weights/next`

---

*This is an early lane and it will grow — more real problem classes, a larger reward share over time. Solve honestly: a wrong-but-fast answer scores `0.0` (the witness check is real). Questions in the subnet channel.*
