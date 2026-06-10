# STRATEGY — decision of record (2026-06-10)

**Status:** locked. This document exists to stop strategy churn. Seven pivots were
generated and falsified across May–June 2026; each began by asking the asset what
it wanted to be. The findings below are evidence-backed (live DB measurements,
adversarially-reviewed handoffs, primary literature). Change this document only
with *new external evidence* — not a new idea.

---

## The anti-pivot rule (read this first)

> **Never let the asset push for a use. Let demand pull the asset.**

Asset-first questions ("we own a subnet/emission/fleet — what could it do?") are
pivot machines: an asset can do infinitely many plausible things and no buyer is
present to falsify any of them. Every dead end died the same death — capability
built before demand was verified (bug-hunting, the solve marketplace, the
difficulty curve, the attestation demand-pump, the compute-subsidy fleet).

Before any build: **name the external party who pays for or uses it, and prove
the demand isn't manufactured by our own emission.**

---

## The three tracks

### 1. Attestor = the product (revenue track)
The only demand signal that survives adversarial testing across the entire
corpus: **"prove a computation ran as claimed."** A real partner (the OC team)
is integrating against `/v1/attest` today and has filed concrete asks (A1–A7).

- Roadmap is **partner-driven**: async job API (A6 — the real gap), offline
  collateral verification, the spot attestor pool, **receipt persistence +
  public `/verify/{id}` permalinks** (the receipt IS the product; today it
  vanishes on reload).
- Attestation in the subnet: per `ATTESTATION.md` — attest only the
  unfalsifiable (producer / speed / timeout), gate the title not the
  submission, never pay for attestation alone.
- **Rejected:** "163k solves/day → 163k attest calls/day." That is wash-trading
  (we emit the TAO that pays for the calls). A funnel fantasy, not demand.

### 2. Subnet Lane A = the community (keep healthy, stop optimizing)
What the live data proved (2026-06-10, publisher DB ground truth):

| Measured | Value |
|---|---|
| Hotkeys ever / activated / won | 737 / 523 (71%) / 299 |
| Daily active solvers | 54 → 366 in 11 days |
| Daily solves | 168 → 21,948 (130×) |
| Concentration | top-10 = 13.7% (genuine long tail, median 51 solves) |
| Copying (identical-answer signature) | 1.1% — they compute honestly |
| Rejections | 0.4%, dominated by DIMACS format friction |
| Race behavior | p50 time-to-first-solve = **40 min** on seconds-easy CNFs — **nobody races** |

Consequences (settled empirically, not by argument):
- **Speed/rank/winner-take-all mechanics are fiction** — users poll lazily;
  rank is cron-timing luck. Do not build racing.
- **Difficulty grading on synthetic SAT is dead** — P0 spike: planted flattens
  (~16 s @ 12.8k vars), threshold random has 6–104× variance, kissat closes
  everything generatable, SLS demolishes satisfiable-near-threshold. Binary
  1.0 correctness is the honest score for synthetic supply. **Retired.**
- The supply flood was self-inflicted: 92% of 37,853 minted challenges expired
  untouched. Board depth and rotation health matter; the 8k/day mint target
  does not. **Retired.**
- What Lane A gets: **work-proportional scoring via row values** (solve more →
  earn more; recency-encoded so coasting decays — see SCORING below), and a
  **reference miner kit** to fix the format friction (71% → 90% activation).

### 3. Lane F cubes = what the burn buys (credibility track, gated)
The fleet's revealed profile — latency-insensitive, honest, lazy, growing — is
exactly the worker profile **cube-and-conquer** needs and that every other
mechanic needs them not to be. Cubes are graded on completion + LRAT
certificate, so the unsolvable difficulty-curve problem disappears. This is
**not pivot #8**: Lane F was already designed (`V4-DESIGN.md`), its research
gaps resolved (zero-sharing dispatch, check-then-hold proof custody); the user
data *selected* it among existing options. The bar it must clear is the current
zero (emission burned on disposable random-3SAT) — and a certificate-verified
assault on a real open problem clears it: headline + live demo of verifiable
distributed compute + working Lane F.

**Target menu (rich — the menu is NOT the bottleneck):** Kochen–Specker minimum
(bounds ~24–31, already C&C-parallelized via AlphaMapleSAT, Bright/Ganesh's
active agenda; 22→24 was SAT+CAS with a 40 TiB certificate) · Schur S(6) (S(5)
fell 2018, ~2 PB proof; S(6) untouched) · Ramsey R(5,5) (≤46 since Sept 2024) ·
van der Waerden / Keller-style instances (Keller and Boolean Pythagorean
Triples both fell to this method).

**The real constraint (why this track is gated):** frontiers move by **method,
not compute** — R(5,5) shrank on modest hardware via LP+case-checking; KS via a
35,000× method gain. The few problems that genuinely need fleet-scale require a
Heule-grade expert to encode and split them. We don't have one.

**The gate:** the **Bright/Ganesh email** — offer the fleet as free, always-on,
certificate-verified compute for their KS pipeline. They bring the cuber and
credibility; we bring cores with no allocation requests. Cheapest possible
falsification of the only real gap. **No cube infrastructure gets built before
a credible partner says yes.** (Bonus interlock: a cube that times out is
exactly the attestation-requiring claim in `ATTESTATION.md` tier-3 — Lane F
generates the honest, small version of attestation demand.)

---

## SCORING (Lane A, the one change worth making)

Live validators run **Path-A local weights**: they pull our Ed25519-signed rows
and aggregate per-hotkey over a 7-day window **in validator code**. Therefore:

- **Effective immediately, no validator involvement:** make row values carry
  the policy. `weighted_score` = volume-with-recency (e.g. coverage of
  available challenges in a trailing window, capped at 1.0). Validators
  average what we sign → solve more earns more; slowing down decays your new
  rows; the natural cap (you can only solve each board challenge once)
  prevents a volume arms race.
- **The validator-update model (corrected 2026-06-10):** we CAN ship validator
  code changes whenever warranted — the constraint is *adoption lag*, not
  impossibility. A release takes effect per-validator as each operator pulls
  it; we can request updates when there's a high-value reason (major release,
  jackpot), but must never depend on the timing. So: the 7-day window CAN be
  killed (→ instant/spot payment) via a validator release; until adoption
  completes, a stopped miner keeps a frozen tail of ≤7 days on each
  not-yet-updated validator. Bundle window-kill into the planned major
  release; design every publisher-side policy to be correct under MIXED
  validator versions.
- Policy ships **flag-gated** in the thin publisher (`flat` default for the
  byte-faithful cutover; flip to `coverage` after the swap). Cutover first with
  divergence 0, then ramp policy — per `deploy/RUNBOOK.md` abort criteria.

### Remote weight vector & burn rate (verified against deployed validator code)

- **v4 does NOT reintroduce remote weight setting.** The thin publisher serves
  the row feed only — there is no `/v1/validator/weights/next` endpoint in the
  scaffold. Validators aggregate locally, exactly as today.
- **Remote burn rate is supported by the existing validator schema** if/when we
  want it: the signed vector's `BurnSnapshot` is Pydantic-validated with
  `forced_burn_percentage: float, ge=0.0, le=100.0` and `burn_uid: int|None,
  ge=0`, with `extra="forbid"` (`policy/signing.py:63-70`). The remote loop
  applies the vector's burn directly (`remote_weight_loop.py:353-360`).
  Flags a vector CAN throw (all verified): out-of-range / non-numeric burn,
  any unexpected field (extra=forbid), wrong `key_id` (pinned), wrong
  network/netuid, expired `expires_at`, or a non-increasing `policy_version`
  (rollback protection). Send a float in [0,100], bump policy_version
  monotonically, and none fire.
- **Reach caveat:** remote mode is opt-in and default OFF on deployed
  validators — they apply the HARDCODED 85% locally. Remote burn control only
  reaches validators that update to (or opt into) remote mode → same adoption-
  lag model as above. This is why burn changes ride the bundled release.

## Sequenced plan

| # | Move | Track | Status |
|---|---|---|---|
| 1 | v4 cutover (RUNBOOK §0–8, byte-faithful, two buttons) | Lane A | ready, soak gates it |
| 2 | Coverage scoring flag + real solve_rank in thin publisher | Lane A | build now |
| 3 | Reference miner kit (fix `malformed_answer`) | Lane A | next |
| 4 | Bright/Ganesh email | Lane F gate | send before building anything |
| 5 | Publisher throughput keystone (file-backed CNF + read path) | all | prerequisite for cubes AND attestation verify |
| 6 | Partner attestor roadmap (A6 async, receipts `/verify/{id}`, pool) | product | the real hours |

## Explicitly retired (do not regenerate)

- Calibrated hard-CNF difficulty tiers (falsified: P0 spike)
- Speed/rank racing mechanics (falsified: 40-min race data)
- 8k/day challenge supply (falsified: 92% expired untouched)
- Attestation demand-pump at solve volume (wash-trading)
- Emission-subsidized commodity compute fleet (no buyer at any subsidy;
  untrusted CPU only does self-verifying work)
- Bug-hunting / per-instance solve marketplace (falsified 2026-05/06)
- Renaming the subnet's purpose in a deflated moment (see anti-pivot rule)
