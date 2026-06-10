# Cathedral v4 — release status

**One line:** the thin publisher is built, byte-compatible with the live signed
feed, and verified deployable — **two buttons from taking over
`api.cathedral.computer`**. What remains before miners are *paid more for harder
work* lives in the live SN39 emission path, not in this scaffold.

## Verified (all gates green)

| Gate | Result | Proves |
|---|---|---|
| `rc_verify.py` | 36/36 (38/38 where real solvers installed) | scoring invariants across all lanes; liar rejected; self-reported `wall_ms` ignored; attested ≥ floor; tampered elapsed breaks the quote |
| `wire_compat.py` | 8/8 | our canonical-JSON + Ed25519 signing reproduces **live production rows byte-for-byte** (50 live rows verify under the prod pubkey `10890a66…`), incl. tamper checks |
| `publisher_verify.py` | 32/32 | end-to-end: sim miner signs (real sr25519) → fetches CNF → solves → submits → validator-style pull verifies every signature |
| Soak (`soak.py`) | PASS | 4,000/4,000 live rows verify under the production key; 7-day per-hotkey divergence 0.0000 |
| Arena (on Stitch, real solvers) | 38/38 | kissat 4.0.4 dethroned the champion through the full certified loop; every cheat scored 0 with reasons |

## Go-live — two buttons (RUNBOOK §0–8)

Purely additive; monolith stays warm for instant rollback. Sequence:
**stage → seed (`seed_live.py`, rows re-served verbatim, never re-signed) →
soak (~2 weeks green) → 🔑 production signing key → 🔀 same-URL domain swap.**

1. 🔑 Set `CATHEDRAL_EVAL_SIGNING_KEY` = the production key on the new Railway service.
2. 🔀 Move `api.cathedral.computer` to the thin-publisher service (rollback = move it back; abort criteria in RUNBOOK §8).

**Not in this swap (deferred by design):** burn step-down (ships as the first
record-fall jackpot release) and Lane S/I activation as on-chain value.

## Gates before miners are paid *for difficulty* (in priority order)

1. **Payout / Path-A — the real blocker, lives in live SN39, not here.** Verified
   work is scored correctly but emission still flows via the score-blind local
   path, so difficulty/score-multiplier only lands for the Path-B-relayed
   portion. Fix = bake the multiplier into the signed score + verify on-chain it
   moves weight. (issue #251 in `cathedralai/cathedral`.)
2. **Difficulty ladder** — the calibrated solve-time tiers (5–10 min → 30 min →
   more) with work-proportional `score_multiplier`. Needs a solver-robust,
   monotonic hard-instance family (reduced-round preimage or cube-of-real-instance,
   not threshold random-3SAT — falsified by the P0 spike). Plan + P0 results in
   the handoffs.
3. **Real attestor for Lane B/I** — binding + tamper-evidence are real; wire the
   in-TEE measurement (live Polaris `/v1/attest`) so elapsed is hardware-measured.
4. **Variance run** (Stitch, 5 solvers × 500 instances × 300s) → sets launch
   batch size, dethrone margin, quorum-k.

## Open questions (research-resolved items in `V4-DESIGN.md` → Open questions)

- Multiplier calibration `m` (how large to draw attested supply without making
  unattested participation worthless).
- Quorum size `k` for attested title matches (depends on the variance run).
