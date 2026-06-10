# Attestation in the Cathedral v4 solver flow

**Status:** spec (open — publish it, open-source the verifier) · **Date:** 2026-06-10
**Companion code:** `scaffold/polaris.py` (the `/v1/attest` seam), `scaffold/grading.py`
(the attest policy), `scaffold/lanes/solver_docker.py` (attested-runner primitive),
`scaffold/wire.py` (`SIGNED_KEYS_V6` → Path-B multiplier). Shipped Route B reference:
`~/attestor/{guest_quote.sh, polaris_verify.py}`.

> Attestation is not a feature bolted onto the subnet. It is the **trust primitive
> under every claim a certificate cannot prove** — and the subnet is the demand
> engine that makes miners pay for it. Miners are the customers: they produce an
> attestation wherever they can run a TEE, or from Polaris in one `/v1/attest` call.

---

## 1. The load-bearing principle: attest only the unfalsifiable

A certificate is cheap, total truth for **correctness**. A SAT witness re-checks
against the CNF; an UNSAT DRAT/LRAT proof checks in `drat-trim` — microseconds,
zero trust. **Never require attestation for correctness.** It is free, and taxing
it with a TEE requirement is a cost miners will resent.

`grading.py` already encodes exactly this:

```python
def attestation_required(outcome: Outcome) -> bool:
    """Cost-minimizing rule: attest TIMEOUTS only."""
    return outcome == Outcome.TIMEOUT
```

Attestation earns its cost only on the three things a certificate **cannot** prove:

| Claim a cert can't prove | Why attestation is the only proof |
|---|---|
| **(1) Which solver produced the result** | The *laundering problem*: a cert proves the answer, not the producer. A copy of the champion can submit the champion's certs. |
| **(2) Wall-clock speed** | Every "I'm faster" champion claim. Self-reported `wall_ms` is worthless; speed must be server-measured (Lane A) or hardware-attested (Lane S/B). |
| **(3) Timeout / hardness** | "The champion failed here" (Lane I). A timeout is the one outcome a miner cannot self-certify. |

**Consequence — gate the title/reward, not the submission.** Anyone may submit and
be certificate-checked at the base rate. Claiming the **champion title** or earning
the **speed / hard-tier multiplier** requires an attested run. This keeps the
on-ramp open and makes attestation a value purchase, not a toll.

---

## 2. The attestation spec (open; verifier is `polaris_verify.py`)

A valid attestation is a **raw TDX quote** whose 64-byte `report_data` binds, in two
SHA-256 halves, everything a reward claim rests on. This is the *shipped* Route B
layout (`guest_quote.sh`, verified against Intel's chain 2026-06-04):

```
report_data[0:32]  = sha256( nonce_hex || miner_pubkey_b64 )        # WHO + anti-replay
report_data[32:64] = sha256( solver_digest || sha256(receipt) )     # WHAT ran || WHAT it produced
```

- **`solver_digest`** = the OCI **content** digest of the solver image, captured
  **inside the attested box** (`docker inspect RepoDigests` → the `@sha256:…`),
  never caller-claimed. (`guest_quote.sh` step 2.) This closes the laundering
  problem: the digest in the quote is the digest that actually ran.
- **`receipt`** = the canonical stdout the attested solver emits. To bind speed,
  challenge identity, and answer, the runner **must print a one-line JSON receipt**
  as its stdout (the shipped binding hashes stdout, so anything in it is bound):

  ```json
  {"challenge_id":"sat-t1-…","instance_sha256":"<cnf hash>","solver_digest":"sha256:…",
   "wall_ms":<int>,"outcome":"SAT|UNSAT|TIMEOUT","answer_hash":"<witness/cert hash>",
   "miner_pubkey":"<b64>"}
  ```

  `wall_ms` measured **in-TEE** is the speed claim, tamper-evident because altering
  it changes `sha256(receipt)` → `report_data[32:64]` no longer matches the quote.
  (`polaris.py` already threads `measured_elapsed_ms` into stdout for this reason.)
- **`nonce`** = a **Polaris/publisher-issued, per-(miner,challenge) value**, bound in
  `[0:32]`. This stops replay and, crucially, **binds the attestation to *this*
  challenge** — a quote from a different challenge has a different nonce and is
  rejected. (Mint it from the same HMAC machinery as the active-CNF fetch token.)

**The verifier (open-source, runs on the publisher):**
1. Recompute both `report_data` halves from the returned fields; check against the
   quote bytes (`q[568:632]`). Pure-Python, trust nothing — `polaris_verify.verify`.
2. Confirm the quote is genuine **offline against Intel collateral** (PCK chain +
   TCB info + QE identity + CRLs), **not** by calling Intel each time —
   `polaris_verify.verify_intel` / `dcap-qvl`. The collateral bundle ships in the
   response (partner ask A2, already implemented in the Go verifier).
3. Check `nonce` is the one we issued (unused), `challenge_id`/`instance_sha256`
   match the served challenge, `solver_digest` matches the registered solver, and
   `outcome`/`answer_hash` match the certificate already checked at the base rate.
4. **Pin the hardware class** so temp-0 reruns reproduce: the quote's PCK `FMSPC`
   attests the platform family; record `machine_type` + `cpu_model` in the payload
   (partner ask A7 — c3-standard-4 / Sapphire Rapids today).

What is **not** measured: MRTD. It measures the fixed base VM (invariant across
workloads — proven), so it is not the image identity; image pinning rides on
`report_data[32:64]`, not MRTD. Don't overclaim "the image is the MRTD."

---

## 3. Provider neutrality — and the Azure asymmetry (decision needed)

The spec must be honestly "bring your own TEE." Where it works:

| Source | report_data binding | Status |
|---|---|---|
| **GCP c3 / bare-metal** | writes `report_data` directly → spec works as written | ✅ supported |
| **Polaris `/v1/attest`** | does all of §2 in one call (Route B shipped: image mode, sealed egress, exit codes, file mounts) | ✅ the convenience we sell |
| **Azure** | OpenHCL paravisor **owns `report_data`** → direct binding is impossible; Azure only offers **MAA tokens** (a different verification path) | ⚠️ decision |

**Decision to make now (don't ship "get it anywhere" while Azure is infinite effort):**
- **Recommended:** spec is **"GCP / bare-metal / Polaris"**; Azure is **out of v1**,
  documented as a known limitation with MAA as a *possible future path*. Honest,
  shippable now.
- **Alternative:** the verifier *also* accepts the MAA path (claims-based, no direct
  `report_data`). Keeps Azure in, materially more verifier work and a second trust
  model to audit. Only take this if Azure-resident miner supply is a real demand.

Polaris's pitch is unchanged either way: the open spec keeps "bring your own" true;
**the one `/v1/attest` call is the convenience** — that's the product.

---

## 4. Reward shape

- **Tiny non-zero baseline** for solve-only (certificate-checked, unattested) — keeps
  liveness and the on-ramp, avoids looking extractive.
- **Big reward requires attested *AND* hard-tier/speed.** Never attestation alone, or
  miners farm the multiplier on trivial solves. The multiplier is a product of
  (difficulty tier) × (attested speed/identity), not either by itself.
- **Margin must clearly beat attest cost** (~$0.0003/run on a Polaris box, or the
  miner's own GCP-spot cost) by enough that opting in is obviously worth it. Calibrate
  `m` against that floor (open question Q5 in `V4-DESIGN.md`).

This maps onto the existing money table (`V4-DESIGN.md` → *Money*): attested work earns
**×m over unattested on the same work**; attested quorums additionally earn title-match
and fraud-proof audit fees.

---

## 5. How it reaches chain (don't over-build)

The multiplier rides the **existing Path-B signed weight vector** — no new rails:

- `scaffold/wire.py` `SIGNED_KEYS_V6` already signs `challenge_value` (= the per-
  challenge `score_multiplier`), `solve_rank`, `solved`, `operator`.
- The publisher computes the attested multiplier into `score_multiplier` /
  `challenge_value`, signs the row, serves it at `/v1/validator/weights/next`.
- **Deployed validators already relay this** (Path B). **No validator release is
  needed for the attestation flow** — it is pure publisher-side scoring policy.
- The *only* thing that ever needs a validator release is the **burn step-down**,
  bundled into the first record-fall jackpot (`V4-DESIGN.md` → *Migration*).

Caveat #251 (Path-A): validators scoring purely locally are blind to
`score_multiplier`. Resolution is the same as for the difficulty ladder — bake the
multiplier into the signed score and verify on-chain it moves weight.

---

## 6. Two constraints not to trip over

1. **Verification load is a prerequisite, not a cleanup.** Every attested run = one
   Intel-collateral verify on the publisher. At subnet volume this lands on the
   **single-SQLite-connection bottleneck that has wedged production three times**
   (board freezes; see publisher ops history). A separate read path / Postgres for
   the verify+score load is a **prerequisite** for turning this flow on, not a later
   optimization. (Also why the `/data` volume hit 96% — same single-writer design.)
2. **Honesty gate.** This flow is **release-to-identity / proof-of-execution** — fine
   and true. Do **not** claim "Polaris-blind," and do **not** build the KBS /
   secret-release lane on top of it **unless custody is HSM-enforced** (locked
   decision). Attesting *that a run happened* is not the same as *releasing a secret
   only to an attested run*; the latter needs hardware key custody we don't yet have.

---

## 7. Implementation map (what changes where)

| Change | Where | Note |
|---|---|---|
| Extend the attest gate beyond TIMEOUT to **speed/identity title claims** | `grading.py` (`attestation_required` + score path) | keep SAT/UNSAT base-rate free; require attestation only to *claim the title or the multiplier* |
| Issue per-(miner,challenge) attest nonce | publisher (`scaffold/publisher/auth.py`, alongside the CNF fetch token) | binds the quote to this challenge; one-time |
| Verify endpoint: recompute report_data + offline Intel collateral + nonce/digest/challenge checks | publisher (new route) over `polaris_verify.py` | **behind the read-path fix (§6.1)** |
| Canonical in-TEE receipt to stdout | solver-runner contract / `lanes/solver_docker.py` | the JSON in §2; this is what binds speed + challenge id |
| Champion/title gate requires attested run | `lanes/solver_arena.py` | dethrone-with-attestation; unattested = base rate only |
| Multiplier → signed row | `score_multiplier` → `challenge_value` in v6 (`wire.py` already signs it) | no validator release |

**Open (carried from `V4-DESIGN.md`):** Q5 multiplier magnitude `m`; Q6 quorum `k`
for attested title matches (both depend on the Stitch variance run).

---

### One line
Correctness is free and self-proving; **attestation is the price of a claim no
certificate can make** — producer, speed, and hardness — and the subnet is what
makes miners line up to pay Polaris for it.
