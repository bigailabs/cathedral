# Primer — Sonnet research session: Cathedral v4 open questions

You are the research fan-out for Cathedral v4 (Bittensor SN39). The design is
locked (`V4-DESIGN.md`, evidence in `V4-DESIGN.html`); your job is to close the
named open questions with primary sources and small runnable experiments —
honest numbers, flat/negative results are real results.

## Read first
1. `V4-DESIGN.md` — especially "Lane F" and "Open questions".
2. `V4-DESIGN.html` — the primary-source evidence base (what's already been
   read and refuted; do not re-derive it, build on it).
3. `~/code/sat-research-center/` — the corpus, briefs, and falsification docs.

## Question 1 — Lane F gap: WAN transfer for trustless cube-and-conquer
Cube dispatch + selective clause sharing over the open internet, between
anonymous miners. The blocker per MallobSat: clause sharing assumes
datacenter-speed links.
- Start from Fred's decentralized-training session analysis (his attempts at
  DT — gradient compression, async/hierarchical topologies, bandwidth
  scheduling). Locate the handoff/notes (check `~/Documents/INBOX/handoffs/`,
  ask Fred if not found) and map each DT transfer technique onto C&C traffic
  classes: (a) cube dispatch (tiny, latency-tolerant), (b) per-cube results +
  certificates (medium, bursty), (c) clause sharing (the hard one — is
  *selective, high-value-only* clause export over WAN worth anything, or is
  zero-sharing C&C the honest design?).
- Deliverable: a bandwidth × latency envelope table per traffic class, with
  the literature/DT-technique that does or does not fit each, and a verdict:
  what Lane F can ship WITHOUT clause sharing (pure independent cubes) vs what
  sharing would add.

## Question 2 — Lane F gap: proof custody (check-then-discard)
Per-cube UNSAT proofs reach terabytes if stored. Candidate answer: attested
on-the-fly LRAT checking (2024 literature: runs at solving speed) inside a TDX
runner that signs verdicts; persist only signed verdicts + cube-cover proof.
- Verify the on-the-fly LRAT claims from primary sources (FRAT 2022, "Fast and
  Verified LRAT Checking" 2024, "Trusted Scalable SAT Solving with On-The-Fly
  LRAT" 2024).
- The credibility question: for a *publishable* mathematical result (e.g., a
  new van der Waerden bound), is "signed attested verdicts + cover proof"
  accepted practice, or does the math community require replayable full
  proofs? Look at how Pythagorean Triples / Schur 5 verification was actually
  accepted (independent re-checking culture).
- Deliverable: a proof-custody protocol sketch + what must persist, with the
  trust assumptions stated exactly.

## Question 3 — community-benchmark variance (feeds the build directly)
Design update (V4-DESIGN.md §eval): we run NO eval infrastructure. The
community benchmarks solvers — unattested cohort races + coverage at base
rate, attested (Polaris `/v1/attest`) work at a multiplier, title matches by
k-of-n attested quorum.
- Experiment (kissat vs cadical vs one tweaked config): (a) how many
  qualifier observations — coverage points + cohort receipt-order races on
  heterogeneous simulated hardware (vary CPU throttle) — for a stable
  nomination signal between two close solvers? (b) what quorum size k makes
  a title-match median decisive given solver runtime variance?
- Deliverable: observations → rank-flip-probability curve; recommended
  nomination threshold, k, and the dethrone margin they support.

## Question 4 — Lane I pricing
Breaker instances pay while the champion's weakness stays open, decaying as
solvers adapt. Propose the decay function: per-round decay vs
solved-by-champion cutoff vs share-of-separation. Check Lane I gaming: can a
miner submit an instance + a private solver that only they can close, and
farm it? (Disagreement proof requires the closing solver to be a *published*
Lane S solver — does that fully close the loop? Edge cases.)

## Question 5 — attested-multiplier calibration
Attestation is opt-in for a multiplier ×m (it buys solver attribution +
trusted timing; TDX via Polaris). How large must m be to draw enough
TDX-capable supply for quorums and audits, without making unattested
participation worthless? Survey what TDX-capable hardware actually costs a
miner (cloud TDX instance pricing vs commodity boxes), model the equilibrium,
and check the failure modes (m too low → no quorum supply; m too high →
base-tier onboarding dies). Deliverable: recommended launch m + the data to
revisit it.

## Ground rules
- Primary sources only for claims; the briefs in `sat-research-center/ideas/`
  contain known overstatements (RLAF "2-10x", planted-hardness) — treat as
  leads, not facts.
- Experiments on Stitch, tight scope, `RESULTS.md` per experiment with honest
  numbers.
- Concise reports, bottom line first. Commit as Fred only.
