# Lane 3 Adaptation Notes

Status: design pass. What to learn from a competitor distillation stack (SparkDistill / SparkProof, SN74, and its sibling eval subnet SN97 "Distil") and how it maps onto OUR Lane 3, rebased on Cathedral's own compute and attestation instead of their rented Targon Blackwell.

Scope: this is design + cheap doc edits only. No scaffold/verifier code was changed, no training run, no git history touched. Every claim below cites file:line in the current `feature/distillation-e2e` tree or says "not found."

The one-line thesis: they had to build trustless distillation from scratch on rented compute they do not control and a teacher API (Claude/GPT) that creates ToS liability. We already have (a) a self-verifying corpus so there is nothing to prove about a teacher, (b) a full content-bound hash chain from verified trace to served model, and (c) a live, production-deployed TDX quote verifier that binds arbitrary digests into `report_data`. The rebase is not "copy their proof system." It is "point our existing attestation primitive at the corpus hash, and dogfood Lane 2 as Lane 3's compute."

---

## (a) What we already have that they had to build

Confirmed against current code. Memory was correct on all five load-bearing claims.

| Property | Where | Status |
|---|---|---|
| Content-bound `member_hash` (mutating any member field breaks it) | `scaffold/distillation_corpus.py:231`, recomputed `:367-376`, enforced `:397-411` | Confirmed |
| `pairs_hash` binds the FULL manifest (rows + split + corpus_hash + format + member set) so a train/test relabel breaks the hash | `scaffold/distillation_pairs.py:135-164`; authenticity re-check against trusted corpus `:197-207` | Confirmed |
| Dedup + split by `source_trace_hash` (true example identity, not export salt) | `scaffold/distillation_corpus.py:273-285` (split), `:310-318` (dedup) | Confirmed |
| `RedactionPolicy` / `export_trace` private-by-default, public disclosure-gated, strong-salt-required | `scaffold/distillation.py:24-86`; strong-salt check `:218-220` | Confirmed |
| Earning FAILS CLOSED via `TIER_B_ATTESTATION_VERIFIER = None` default | `scaffold/distillation_serve.py:96`, gate `:107-109`, exercised `distillation_e2e_verify.py:336` | Confirmed |

Two things worth stating plainly because they change the whole comparison:

1. **Our corpus is self-verifying, so their central problem does not exist for us.** Their rows are teacher completions; they must prove the teacher API actually served the model and that the published verified set matches what was attested. Our rows are replayed exploit witnesses: an accepted member already carries deterministic replay evidence against pinned target logic (`CATHEDRAL_V0_LANES.md:60-64`, `DISTILLATION_E2E_SPEC.md:16-24`). The "did the teacher really produce this" question has no analogue. Do not import their teacher-attestation machinery. It solves a problem we do not have.

2. **We already have the provenance chain they describe as their trust story.** verified trace -> export -> corpus member -> pair -> model -> serving, every hop recomputed not assumed (`DISTILLATION_E2E_SPEC.md:238-271`, gate check `provenance_chain_intact` at `distillation_e2e_verify.py:334-337`). Their raw->verified subset proof is one hop of this chain that we happen to enforce differently (see gap G2).

The strategic asymmetry to keep in the pitch: **no teacher-ToS exposure.** They shipped a disclaimer because they distill from Claude/GPT. We distill from our own verifier output. That is a cleaner legal and trust position and should be said out loud, not buried.

---

## (b) Compute rebase: Cathedral instead of Targon (ranked)

The central question. They rent attested Blackwell CC compute from Targon (SN4). We should not. We already run the exact attestation primitive their proof depends on, in production, today.

**The primitive we already own.** `scaffold/publisher/attest.py:266-271` computes:

```
report_data[0:32] = sha256(nonce_hex || miner_pubkey_b64)      # WHO + anti-replay
report_data[32:64] = sha256(solver_digest || sha256(receipt))  # WHAT ran || produced
```

This is generic. `solver_digest` is "the thing that ran," `receipt` is "what it produced." Lane 2's Route B recipe is identical (`LANE2_SECURE_COMPUTE_PLAN.md:26-48`). The same verifier is digest-pinned and live in production (standard-TCB posture). **We do not need Targon's attestation story because we have our own, running, with a first positive mainnet weight already achieved 2026-07-23.**

The corpus-binding answer to "can we bind a corpus hash into OUR TDX report_data": yes, with zero new crypto. Set `solver_digest = pairs_hash` (or `corpus_hash`) and `receipt = artifact_sha256 + eval_hash`. The lower half stays WHO/anti-replay. Then `report_data[32:64]` commits the TEE run to the exact dataset that trained the model and the exact eval that scored it. That is SparkDistill mechanism #1, and it drops onto our existing binding with a field rename, not a new protocol.

### Option 1 (RANKED #1): Bind Lane 3 into the existing Route B verifier on the production TDX node. Dogfood the live TDX box.
- **Exists today:** the TDX quote path, the pure-python `report_data` recompute (`attest.py:266`), the `CommandIntelVerifier` production adapter (`attest.py:~95-175`), the digest-pinned verifier binary, standard-TCB policy, and a working epoch/canary loop.
- **Missing:** a thin caller that runs the fine-tune inside the TDX guest and emits `receipt = {artifact_sha256, corpus_hash, pairs_hash, eval_hash}` as the in-TEE stdout, plus a Lane 3 `TIER_B_ATTESTATION_VERIFIER` implementation that calls `verify_attestation`-style logic and checks `report_data[32:64] == sha256(pairs_hash || sha256(receipt))`. This is the fail-closed seam at `distillation_serve.py:96`, already shaped for exactly this.
- **Attestation story:** strongest we have. Same verifier, same collateral, same box that already earns mainnet weight. Route B proves "this attested box ran this training bound to this corpus and produced this artifact + eval." Note the honest limit (Route B not Route A): the base VM measurement is fixed, the training image is bound in `report_data`, not made the MRTD (`LANE2_SECURE_COMPUTE_PLAN.md:41-48`). That is fine for a data-provenance claim; it is not measured-launch of the trainer.
- **Effort:** low. Field rename + one verifier function + the in-guest runner wrapper. No GPU CC needed if the model is small enough to train on the TDX CPU box or a CPU-only LoRA; if it needs a GPU, see Option 2.
- **Dogfooding upside:** high and specific. Lane 3 becomes the first workload that consumes Lane 2's attestation output for something other than SAT solve credit. It proves the primitive generalizes beyond the arena.

### Option 2 (RANKED #2): Lane 3 becomes Lane 2's first paying customer. Train on an attested 8x-GPU node admitted through the Lane 2 gates.
- **Exists today:** the entire Lane 2 five-gate intake (`LANE2_SECURE_COMPUTE_PLAN.md:118-260`), signed offers, deterministic preflight for 8x h200/b200/b300/pro_6000, the `verify-evidence` endpoint that creates `cryptographically_verified`, and the health/usage receipt surface.
- **Missing:** GPU confidential-compute evidence verification is the acknowledged hard, not-exact part (`LANE2_SECURE_COMPUTE_PLAN.md:348-358`, Phase 4). No accepted real machine has completed the full evidence->listing->health->revenue loop yet (`SOLVER_ATTESTATION_STATUS.md:76-87`). Also missing: the glue that turns a Lane 2 `usage_receipt` into Lane 3's serving `usage_receipt` (both already exist as concepts; `distillation_serve.py:374-388` reads exactly the receipt shape Lane 2 emits).
- **Attestation story:** strong for the GPU CC case they actually target, but gated on the one thing Lane 2 has not finished (real GPU evidence). Do not claim it before a real 8-GPU node passes the "Production Hardware Ask Gate" (`LANE2_SECURE_COMPUTE_PLAN.md:400-431`).
- **Effort:** medium-high, but almost all of it is Lane 2 work that has to happen anyway. Lane 3 adds only the receipt bridge.
- **Dogfooding upside:** highest strategically. Lane 3 paying Lane 2 for attested GPU hours is the literal "internal customer" story and validates the revenue engine with a real workload. This is the move that makes the platform narrative true.

### Option 3 (RANKED #3): CPU-only / Kaggle TPU dry-to-real bridge, no TEE binding yet.
- **Exists today:** `distillation_train.py:129` explicitly targets Kaggle TPU v5e-8 / modest GPU for the real fine-tune; a working local-to-Kaggle runner already exists.
- **Missing:** any attestation. A Kaggle run is not attested, so it can produce a `ModelArtifact` with a real `artifact_sha256` but can NEVER reach `earning` (fail-closed at `distillation_serve.py:107`). It tops out at `ready`/`healthy`.
- **Attestation story:** none. This is a capability bridge, not a trust bridge.
- **Effort:** lowest to first real weights; highest to first real revenue (because revenue needs Option 1 or 2 on top).
- **Dogfooding upside:** none for the platform story. Useful only to de-risk the training code itself before spending attested compute on it.

**Recommendation.** Sequence, not either/or. Do Option 3 first as a throwaway to prove the fine-tune produces a model that beats baseline (cheap, no attestation, no earning claim). Then Option 1 to get a real attested artifact bound to the corpus on a box we already trust, which unlocks `earning` through the existing seam. Option 2 is the destination once Lane 2's GPU CC evidence path is real; it is the strongest dogfood but should not block Lane 3, because Option 1 already gives a defensible earning path on hardware we control.

Fred-relevant caveat: this is runtime coupling, not code coupling. Lane 3 does not need to import Lane 2 modules. It needs the receipt shape and the verifier function, both of which are already stable seams. Do not build a shared "compute abstraction" for two callers.

---

## (c) Prioritized Tier B gap list (their 7 mechanisms mapped)

For each of their mechanisms: covered / N-A / real gap, with file:line. Genuine gaps get a minimal design that fits the existing fail-closed seam and hash contract. No premature abstraction.

| # | Their mechanism | Our status | Evidence |
|---|---|---|---|
| 1 | Attestation nonce == data hash; TDX `report_data` bound to it | **Real gap, cheap** | Primitive exists (`attest.py:266`), just not wired to corpus. See G1. |
| 2 | Raw->verified subset proof (published set is subset of attested raw) | **Mostly N-A / partial** | Our corpus is not raw-vs-verified; members come straight from `export_trace`. Provenance-subset exists (`distillation_pairs.py:194`) but it is "pairs within declared members," not "verified subset of attested raw." See G2. |
| 3 | Prove-on-TEE / verify-on-any-CPU | **Covered offline, gap online** | Offline hash/chain verify is the whole `distillation_e2e_verify.py` gate (CPU, no GPU, sockets off `:20-58`). Online DCAP/NRAS check is Lane 2's `CommandIntelVerifier` (`attest.py:~95`). Gap is only wiring the online half to Lane 3. Rolls into G1. |
| 4 | Explicit "what verification does NOT prove" honesty | **Partial** | Spec has a scope correction (`DISTILLATION_E2E_SPEC.md:279-291`) but no single honest-limits table. See section (e); cheap doc gap, filled below. |
| 5 | Semantic/structural dedup + cross-registry novelty | **Real gap** | Dedup is exact `source_trace_hash`/`export_hash` only (`distillation_corpus.py:314-319`). No AST / prompt-hash / semantic fingerprint / novelty threshold. See G3. |
| 6 | Pin-grace: training gate accepts any canonical dataset hash from merge-base..HEAD | **Real gap, low priority** | No merge-base/pin-grace logic anywhere (grep: not found in scaffold). Our `corpus_hash` is single-valued; a mid-train corpus update invalidates lineage. See G4. |
| 7 | Composite eval, anti-CoT-collapse, procedural items | **Real gap** | Eval is single-axis accuracy + positive_recall + baseline (`distillation_serve.py:259-266`). No worst-axis floor, no reasoning-density probe, no procedural generation. See section (d). |

### Top 3 gaps to close, in priority order

**G1 (priority 1): Bind the corpus hash into the Tier B attestation.**
This is mechanism #1 + #3 and it is the load-bearing one because it is what makes `earning` mean something. Minimal design, fits the existing seam:
- In-TEE runner emits `receipt = canonical_json({artifact_sha256, corpus_hash, pairs_hash, eval_hash})` as stdout.
- The attested run's `report_data[32:64] = sha256(pairs_hash || sha256(receipt))`, reusing `attest.py:266` unchanged (pass `solver_digest=pairs_hash`).
- Implement `TIER_B_ATTESTATION_VERIFIER` (currently `None` at `distillation_serve.py:96`) as: recompute the two halves, delegate genuineness to the same `CommandIntelVerifier` Lane 2 uses, then assert `predict.bound_artifact_sha256 == receipt.artifact_sha256` AND `receipt.corpus_hash == artifact.corpus_hash`. Return True only if all pass.
- Fail-closed is already the default, so a partial wire cannot accidentally earn. The e2e gate already proves a fabricated attestation string cannot earn while `TIER_B_ATTESTATION_VERIFIER is None` (`distillation_e2e_verify.py:333-348`), and that the mechanism works when wired (`:350-367`). G1 is "make the wired verifier real," and the test scaffold for it already exists.
- Effort: one function + one runner wrapper. No schema change, no new hash.

**G2 (priority 2): Decide the raw->verified consistency posture and write it down.**
This is the one where their mechanism is mostly N-A for us and asserting otherwise would be inventing a problem. Our members are not a cherry-picked subset of a larger attested raw set; each member is an independently replay-verified trace. The real, smaller risk is: **an operator assembling the corpus could silently drop verified members** (include a biased subset), which the hash chain does not currently prevent because the chain binds whatever member set you assembled, not "all eligible members."
- Minimal design: at assembly, record `candidate_count` (how many training-safe exports were offered) alongside `n_members`, and put both under the attestation. Then a verifier can see "you attested 900 members out of 1000 eligible" and an operator has to justify the drop. This is a counted-completeness claim, not a subset proof, because we have no adversary swapping rows after attestation the way they do.
- Do NOT build their raw/verified two-file attestation. It answers a threat model (miner swaps rows post-attest) that does not apply when every row is self-verifying and assembly is operator-run.
- Effort: add two counters to the manifest + surface them in the Tier B receipt. Low.

**G3 (priority 3): Semantic / near-duplicate dedup.**
Exact-hash dedup (`distillation_corpus.py:314-319`) will let near-identical witnesses (same exploit, trivially different assignment) inflate the corpus and let a miner farm one bug into many rows. This matters more as the corpus grows and if Lane 3 ever pays per accepted trace.
- Minimal design that fits the existing contract: add an optional `near_dup_key` computed from the already-present redacted structural fields (`task.invariant_id`, `task.invariant_hash`, `decode_kind`, `verdict.decoded_witness_hash`, `severity_hint`) at `training_safe_view` time. In `assemble_corpus`, treat a collision on `near_dup_key` as a duplicate drop (extend the existing `seen_source` pattern at `:314-318` with a `seen_near_dup` set). No embeddings, no model, no new dependency; it is structural dedup over fields we already extract.
- Semantic-fingerprint / AST dedup like theirs is overkill here: their rows are code (kernels), ours are structured audit verdicts, so structural keys already carry most of the signal. Do the cheap structural dedup first; only reach for embeddings if structural dedup demonstrably misses a real farming pattern.
- Effort: one key function + one set in the assembly loop. Low-medium.

**G4 (deferred): Pin-grace.**
Real but low value now. It matters when many miners train against a moving corpus and you do not want a mid-train corpus bump to waste GPU. We are pre-first-real-training-run. Note it, do not build it. When it matters, the minimal form is: the training gate accepts any `corpus_hash` in a small operator-pinned allowlist (the last N canonical hashes), rather than a single exact hash. That fits `distillation_train.py:_require_lineage` without a merge-base git dependency. Deferring is the correct call, not a miss.

---

## (d) Eval gate sketch (borrowing SN97, for a security-reasoning student)

Their eval lesson: single-metric (pure KL) scoring is gameable. A student wins KL by emitting "wait, let me reconsider" filler and never answering (CoT collapse). Their fix: composite `final = 0.75*worst-3-axis-mean + 0.25*weighted`, reasoning-density / thinking-collapse probes, procedurally-generated eval items, and re-running the base model every round to drop broken axes.

Our student is different: it classifies audit witnesses (reproduces / does-not-reproduce + reason), not math or kernels. So the axes and the "procedural item" analogue must be security-shaped. Keep it minimal and fail-closed. The current eval (`distillation_serve.py:124-215`) already has accuracy + positive_recall + baseline; this extends it, it does not replace the hash-binding.

**Axes (worst-axis floor, not single number).** Score each, take the composite the way they do:
- **Witness validity accuracy** (the existing accuracy axis) - does it call reproduces/does-not-reproduce correctly.
- **Rejection-reason correctness** - on negatives, does it name the right category (`cnf_unsatisfied` / `decode_failed` / `replay_not_reproduced` / `stale_package`), not just "rejected." This is the security analogue of "actually reasoning vs emitting filler." A model that always says "rejected (unspecified)" collapses this axis the way a filler-CoT student collapses answer axes.
- **Negative-control robustness** - accuracy specifically on the `rejected_claim_negative_control` members, so a model that only learns to rubber-stamp positives is caught (this is the imbalanced-set trap the baseline check already targets at `:164-166`, promoted to its own axis).
- **Severity calibration** (if `severity_hint` present) - does it order high vs low severity witnesses sensibly. Optional; drop the axis if the field is sparse (their "drop eval-setup-broken axes" idea).

Composite: `final = 0.75 * mean(worst 2 axes) + 0.25 * mean(all axes)`. Direct port of their 0.75/0.25 worst-floor, sized to our axis count. A model that is great at positives and useless at reason-naming cannot buy back the floor with the weighted term.

**Anti-collapse probe (security analogue of thinking-collapse).** Their student games KL with filler. Ours would game classification by outputting the majority label. We already reject the pure-majority classifier via `must_beat_baseline` (`distillation_serve.py:264`). Add the reason-density check: the fraction of negative predictions that carry a *specific* (non-`unspecified`) reason must exceed a floor, else the reason axis scores 0. That is our "did it actually reason or just emit the safe token."

**Procedural / IPT analogue (block-seeded, anti-memorization).** Their procedural items (name-rotation, irrelevant-clause injection) stop a student memorizing static benches. Our security-shaped equivalents, all derivable from existing corpus fields with no new model:
- **Invariant-name rotation:** re-render a test member's `invariant_id` / task context with rotated identifiers (the redaction already hashes ids; rotate the salt) so a memorizer keyed on the literal id fails while a real classifier holds.
- **Irrelevant-clause injection:** pad the witness context with extra decoded-but-inert fields; a robust student ignores them, a pattern-matcher flips.
- **Held-out-invariant split:** carve the test split so entire `invariant_id` families are unseen in train (the split is already seeded by `source_trace_hash` at `:273`; add an invariant-family holdout mode). This is the strongest anti-memorization move and the most security-relevant, because it tests whether the student generalizes the *class* of exploit, not the instance.

**Run the base model every round.** Cheap and worth keeping: score the untuned base on the same items each eval; if the base already passes an axis, that axis is eval-setup-broken (too easy) and gets dropped from the floor. The existing baseline-accuracy machinery (`:164-166`) is halfway there; generalize "majority baseline" to "base-model baseline per axis."

Keep it fail-closed: all of this sits inside `evaluate(...)` before the serving state machine. If an axis cannot be computed (missing field, empty split), it scores 0 and drags the floor, rather than being skipped. A model that cannot be evaluated does not pass, same discipline as the rest of the lane.

Do NOT port their KL / reasoning-density-over-tokens machinery literally. Our target is a short structured verdict, not a long chain of thought, so token-level CoT metrics do not apply. The collapse analogue for us is label/reason degeneracy, and the axis floor + reason-density floor catch it.

---

## (e) What our verification does NOT prove (honesty table)

Their explicit-limits section is a feature, not a hedge. Ours, stated plainly. This fills gap #4.

| Claim | What the proof actually shows | What it does NOT prove |
|---|---|---|
| Corpus integrity | Every member is content-bound (`member_hash`) and the member set + splits are hash-bound (`corpus_hash`); post-assembly mutation is detected (`distillation_corpus.py:397-411`) | That the assembler included *all* eligible members. It binds the set you assembled, not completeness (see G2). |
| Provenance chain | served model -> pairs -> corpus -> export_hashes recomputes and resolves each `source_trace_hash` against the retained index (`distillation_e2e_verify.py:334-337`) | That the underlying verified trace was itself correct. It inherits Lane 1's replay verdict; it does not re-audit the exploit. |
| Training-safety | No member retains raw repo/commit/witness/agent-trace; unsafe exports are rejected (`distillation_corpus.py:123-232`) | That a *determined* operator who sets private-audience raw-include flags upstream produced a safe export. The gate rejects those, but the redaction quality of what remains is a policy call, not a proof. |
| Earning state | Reaching `earning` requires a trusted re-run eval driven by an `AttestedPredictor` bound to the artifact plus fresh signed receipts (`distillation_serve.py:306-337`) | That the model is *good*, only that it passed the configured thresholds on the held-out split. Threshold choice is an operator claim. |
| Tier B attestation (once G1 is wired) | An attested TDX box ran a training bound to this `pairs_hash` and produced this `artifact_sha256` + `eval_hash` (Route B, `LANE2_SECURE_COMPUTE_PLAN.md:26-48`) | That the training *image* is the measured guest. Route B binds the image in `report_data`; it is NOT Route A measured-launch. The base VM measurement is fixed (`:41-48`). |
| Revenue receipt | A signed, fresh usage/revenue receipt exists (`distillation_serve.py:374-388`) | That revenue is real money in a bank. Off-chain receipt is operator/provider-attested, same posture as Lane 2 (`LANE2_SECURE_COMPUTE_PLAN.md:253-259`). |
| No teacher liability | Rows are self-verifying replayed witnesses; there is no teacher API call to attest (`CATHEDRAL_V0_LANES.md:60-64`) | Nothing to disclaim here. This is the one place we are strictly *stronger* than them, not weaker. |

---

## Cheap doc edits made in this pass

None to working scaffold/verifier code (out of bounds by instruction). This notes file is the deliverable; the honesty table (section e) and the eval axes (section d) are the material a future Tier B PR should lift into `DISTILLATION_E2E_SPEC.md` when G1/eval land, not before. Flagged rather than applied, because editing the spec ahead of the code would make the spec claim things the code does not yet do, which is the exact overclaim posture the lane avoids.
