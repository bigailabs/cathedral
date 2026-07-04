# Cathedral Fast Path — Miner Guide

**What it is:** a faster submit lane for SN39 miners, now serving **real, checkable problems** mixed into your challenges — and **10% of subnet incentive routes to miners who solve on the fast path.**

Cathedral rewards *proof of work done*, not claims. The fast path is where that starts.

---

## What's new

- **Real puzzles.** ~10% of your per-miner challenges are now genuine combinatorial problems — **graph k-coloring** and **Latin-square completion** encoded as SAT — not synthetic filler. Cathedral verifies your solution's witness exactly. The other ~90% are the usual planted 3-SAT.
- **Tiny signed submits.** The fast path takes a small signed *bitset* of your assignment instead of a full solution body — cheap to send, cheap to verify.
- **10% reward.** Verified fast-path solves feed a `cathedral_sat_fast` score that is blended into the signed weight vector at **10%** (registered miners only). Solve on the fast path → earn a share of that 10%.

## Requirements

- A hotkey **registered on SN39** (the 10% is registration-gated — unregistered hotkeys are not paid).
- A SAT solver (e.g. `cadical`, `kissat`) — real puzzles are guaranteed satisfiable and small; any competent solver handles them instantly.

## What to do

Point your miner at the V2 fast path on `v2-beta.cathedral.computer`:

**1. Fetch your challenges** (signed read, your hotkey):
```
GET https://v2-beta.cathedral.computer/v2/synthetic-boolean/per-miner/challenges?limit=64
```
Each item gives you `challenge_id`, `tier`, `seq`, `n_vars`, `cnf_sha256`, `submit_token`, and `kind` (`coloring` / `latin` / planted).

**2. Fetch the CNF** and solve it:
```
GET https://v2-beta.cathedral.computer/v2/synthetic-boolean/per-miner/cnf?challenge_id=...&tier=...&seq=...
```

**3. Submit the tiny signed bitset** of your satisfying assignment:
```
POST https://v2-beta.cathedral.computer/v2/agents/submit-bitset
```

**4. Check your receipt** and watch your score:
```
GET https://v2-beta.cathedral.computer/v2/agents/submit-bitset/receipts/{receipt_id}
GET https://v2-beta.cathedral.computer/v2/validator/weights/next   # the fast-path scoreboard
```

### Reference miner

A working end-to-end example lives in the repo: **`scripts/v2_bitset_miner_e2e.py`** (fetch → solve → sign → submit → receipt). Point it at the fast path:
```
python3 scripts/v2_bitset_miner_e2e.py \
  --challenge-base https://v2-beta.cathedral.computer \
  --submit-base   https://v2-beta.cathedral.computer \
  --limit 8
```

## How the reward works

1. You solve fast-path challenges; Cathedral verifies each witness.
2. Verified solves become your `raw_score` on the fast-path scoreboard.
3. Cathedral blends that scoreboard into the **real** signed weight vector at 10%, **registered miners only**, normalized and capped.
4. Validators consume that one Cathedral-signed vector — so your fast-path work shows up in your on-chain weight.

## Watch it live

- Solve/verify rate + backlog: `https://v2-beta.cathedral.computer/v2/verify/metrics`
- Who's scoring: `https://v2-beta.cathedral.computer/v2/validator/weights/next`

---

*This is an early lane and will grow. Solve honestly — a wrong-but-fast answer scores 0 (the witness check is real). Questions in the subnet channel.*
