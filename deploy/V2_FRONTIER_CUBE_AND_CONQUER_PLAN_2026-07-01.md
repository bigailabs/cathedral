# Cathedral SN39 → a real scientific-frontier move: decentralized, incentivized, proof-carrying cube-and-conquer SAT

**Author:** Claude, planner. Grounded in the actual repo (`scaffold/dimacs.py`,
`scaffold/publisher/per_miner.py`, the V2 bitset + artifact/proof lanes) and live platform state.
No customer required — the goal is a novel systems/e-science contribution demonstrated on a real
mathematical target.

## The honest starting point (what the generator actually is)

`scaffold/dimacs.py::gen_planted_3sat` produces instances that are **guaranteed SAT by a hidden
planted assignment the publisher already knows**. Miners find *any* satisfying assignment;
`verify_witness` cheaply confirms it. So the subnet today is a **proof-of-work race over synthetic
instances with known answers** — zero external/mathematical content. The reusable jewel is the
*shape*: a trusted generator mints many independent challenges; untrusted miners solve them; a
verifier checks each answer cheaply; signed receipts + a merkle audit bundle make it all
independently checkable.

## The thesis (what's genuinely novel)

Hard combinatorial results are settled today by **cube-and-conquer SAT**: a trusted "cube" step
splits one giant problem into millions of independent subproblems; a fleet solves them in parallel;
each subproblem emits a **machine-checkable proof**, and the proofs compose into a result no human
can check by hand (Boolean Pythagorean triples — Heule 2016, 200 TB proof; Schur number 5 —
2017; Keller dim-7 — 2020). This has only ever run on **trusted HPC clusters**.

**The novel move: do frontier-style cube-and-conquer on a decentralized, token-incentivized,
adversarial fleet, where correctness rests entirely on per-cube proof certificates rather than on
trusting the workers.** The contribution is the *system* — first proof-carrying, incentive-aligned,
decentralized SAT attack on a frontier combinatorial problem — demonstrated by re-deriving a known
result and then extending toward an open case. You do NOT need to beat Heule's records; you need to
be the first to do it this way, with an artifact anyone can verify without trusting the network.

## Why the architecture fits (the trust boundary is already yours)

Cube-and-conquer's correct trust split is **"trust the split, verify the solve"**:
- **Trusted, centralized (you):** encode the problem to CNF, generate the cube set, and *prove the
  cubes tile the entire search space* (cover-completeness). This is the scientifically load-bearing
  part and must not be delegated to miners.
- **Untrusted, decentralized (miners):** solve each cube and return a **proof** (a satisfying
  witness if a cube is SAT = a counterexample found; a refutation proof if UNSAT).
- **Verifier:** independently checks each returned proof. A miner that lies about a cube fails the
  check and scores 0 — which is exactly why proofs, not trust, carry the result.

This is your publisher(generator) / miner(solver) / verifier(checker) split verbatim. The cube set
replaces `gen_planted_3sat`; the proof checker replaces/augments `verify_witness`.

## The two lanes map onto the two problem halves

- **SAT half (a cube contains a counterexample):** the answer is a satisfying assignment — **cheap
  to verify** with the existing `verify_witness`. Carried by the **tiny bitset lane** you already
  built. Finding a counterexample *settles the whole problem* (existence disproved).
- **UNSAT half (a cube has no solution):** the answer is a **refutation proof** (DRAT/LRAT), which
  is **large and non-trivial to check**. Carried by the **artifact/proof lane** (Hippius CID +
  streaming caps) whose design you already reviewed (LRAT currently `received_unverified` until a
  checker exists). Proving *every* cube UNSAT settles the problem the other way (existence proved).

So both submit paths you've built are needed, for the two halves of one real problem. The missing
capability is the **proof checker** for the UNSAT half.

## Plan — phased, each phase independently demonstrable

### Phase A — convert the pipeline from synthetic to REAL (SAT witnesses only) — de-risk
**Goal:** prove the network solves *real, non-planted* instances end-to-end with checkable
witnesses, on the plumbing that already works.
1. Add a challenge-source adapter: replace `per_miner.generate_instance`'s call into
   `gen_planted_3sat` with a source that serves instances from a **real corpus** (SAT Competition /
   SATLIB), keyed deterministically by (epoch, tier, seq) so the per-miner assignment logic and
   HMAC token binding are unchanged. Keep `verify_witness` as-is (it already checks any assignment
   against any CNF — it does not care that the CNF is real).
2. Route these through the existing v2-beta verify pipeline; collect signed receipts + audit bundle.
3. **Demo artifact:** "N real SAT-Competition instances solved by the decentralized network; here is
   each signed witness — verify them yourself." Modest novelty, but it makes everything downstream
   real and proves the throughput story on real data (pairs with the reliability build plan's W1/W3).

### Phase B — cube-and-conquer, reproducing a KNOWN result (the frontier method, safe target)
**Goal:** stand up the full method and validate it against a published answer.
1. **Pick a target with a math collaborator.** Start with a **mid-size, already-settled** result so
   you have ground truth: a van der Waerden or Schur-type bound that was SAT-settled, or a
   Pythagorean-triple-scale reproduction. (Choosing/encoding the target is the one genuinely external
   dependency — see Risks.)
2. **Cube generator (trusted):** a new module that takes the target CNF + a splitting heuristic
   (standard tool: `march_cu` emits a cube file) and mints cubes as challenges via the existing
   per-miner/token machinery. Each cube = CNF + assumption literals; challenge_id = hash(cube).
3. **Cover-completeness auditor (trusted, load-bearing):** prove the cube set partitions the whole
   search space (the union of cube assumptions is a tautology / the DNF of cubes covers all
   assignments). Without this the scientific claim is void. This runs centrally and is itself
   checkable.
4. **Proof-checking verifier (new capability):** integrate an LRAT checker — prefer a
   **formally-verified** one (`cake_lpr`) or `drat-trim`/`lrat-check` — into the verify path so an
   UNSAT cube's returned proof is *checked*, not trusted. Miners return DRAT; convert to LRAT and
   check. Score only on a *checked* proof (fixes the artifact-lane "received_unverified" gap).
5. **Aggregator:** collect one checked proof per cube, confirm the cover is complete, and emit a
   single **composed, machine-checkable proof bundle** + merkle root + signed receipts.
6. **Demo artifact:** "The decentralized network re-derived <known result>; here is the composed
   proof and a one-command independent check." This validates the entire system against ground truth.

### Phase C — point it at an OPEN (or record-extending) case + publish
**Goal:** the actual frontier contribution.
1. With the validated system, target a **genuinely open** case (an open van der Waerden number, a
   Schur/Rado bound, or extend a coloring record) chosen with the collaborator.
2. Run it on the real fleet at scale (this is where the reliability work — W1 verifier throughput,
   W2 flusher, W3 load-proof — is a hard prerequisite; UNSAT-proof checking needs *real* verifier
   compute, unlike cheap witnesses).
3. Publish: the result, the composed proof, the "verify-it-yourself" artifact, and a short paper
   framing the **systems contribution** (proof-carrying decentralized incentive-aligned
   cube-and-conquer). Brand the proof/receipt layer as Polaris Attest.

## New components to build (net of what exists)

| Component | New? | Notes |
|---|---|---|
| Real-corpus / cube challenge source | new | adapter behind `generate_instance`; keep token+per-miner logic |
| Cube generator (`march_cu` wrapper) | new | trusted, centralized |
| Cover-completeness auditor | new | the correctness crux; centralized + checkable |
| LRAT/DRAT proof checker in verify path | new | prefer `cake_lpr` (formally verified); this is the missing "checker" the artifact review flagged |
| Proof aggregator + composed bundle | new | extends existing `audit_bundle` merkle machinery |
| Public "verify it yourself" artifact | new | the demo surface; ties to Attest |
| Tiny-bitset lane (SAT witness) | reuse | already built |
| Artifact/proof lane (Hippius, streaming caps) | reuse | already designed/reviewed |
| Lean ingress + flush/verify at scale | reuse | the W1/W2/W3 reliability plan is the backbone |
| Signed receipts + merkle audit bundle | reuse | `v2_pipeline.audit_bundle` |

## Honest risks / where this is hard

- **Cover-completeness is the whole ballgame.** If the cube set doesn't provably tile the space, a
  correct-looking pile of UNSAT proofs proves nothing. This must be generated and audited centrally,
  and the audit must itself be checkable. Get a SAT/combinatorics collaborator to review it.
- **UNSAT proof checking breaks the cheap-verify asymmetry.** Checking a DRAT proof can cost as much
  as solving. Mitigate with LRAT + a verified linear-time checker, redundancy/spot-checks, and by
  budgeting *real* verifier compute (not the tiny-witness assumption). This is a genuine cost, not a
  footnote.
- **Proof sizes are large** (MB–GB per hard cube) — this is the artifact/Hippius lane, not the tiny
  lane; watch storage/streaming caps and retention.
- **Target selection + encoding needs a domain collaborator.** Picking the problem, writing the CNF
  encoding, and choosing the split are expert tasks. This is the one dependency you can't fake — but
  encodings for the classic targets are public, and reproduction targets have ground truth.
- **Adversarial miners** may return bogus UNSAT claims — defended by mandatory per-cube proof
  checking (the reason the whole design is proof-carrying), but only if the checker is actually wired
  and no cube is scored on an unchecked proof.
- **Novelty framing must stay honest:** lead with the *systems* contribution (first decentralized,
  incentivized, proof-carrying cube-and-conquer), demonstrated by reproduction first; treat any open
  result as upside, not the claim you open with.

## Smallest thing that already looks like the future

Before any of the math: run **Phase A** on a few dozen real SAT-Competition instances through the
existing pipeline and publish the signed, independently-checkable witnesses. It's days of work on
plumbing that already runs, it converts the subnet from synthetic to real, and it's the honest first
step of the exact same system that later carries the frontier proof.

## Dependencies on the reliability plan

Phase C at scale requires the earlier build plan (`V2_RAMP_BUILD_PLAN_FOR_AGENT_2026-07-01.md`):
W1 (single, observable verifier), W2 (flusher), W3 (verify ≥ accept load-proof). The proof-checking
verifier is a *new verify mode* added alongside W1's witness verifier.
