# Cathedral: The Game (local)

A playable, testable, end-to-end local instance of the Cathedral mechanism.
It wires the **real** scaffold primitives — it does not reimplement them — into
one round loop with clear rules, fair scoring, anti-cheat, and an obvious path
to winning. Runs fully offline (no chain, no cloud, no paid resources).

> Design priority: a working mechanism, not a story. Every rule below is
> enforced by code in `game/` that calls a real primitive in `scaffold/`.

---

## 1. The one-sentence game

> Stay alive, solve **your own** assigned SAT challenges fast, and **prove** your
> compute — and you earn weight proportional to verified, tier-weighted,
> speed-scaled work. Cheat, copy, go dark, or fake your compute, and you earn
> zero on that work, deterministically.

---

## 2. The measurement rule (Const's structure)

Every reward in the game has exactly one shape:

```
reward = linear_metric_to_maximize × boolean_gate
```

Concretely, for miner *i* in a round:

```
reward_i = liveness_gate_i × Σ_c  [ verified(c) × attest_gate(c) × tier_weight(t_c) × speed(c) ]
                              └────────────────── over challenges c assigned to i ──────────────┘
```

- **Linear metric to maximize** — the sum: tier-weighted, speed-scaled count of
  *verified* solves. It is monotone: one more correct, fast, hard solve strictly
  increases it. Nothing else moves it. There is no volume race (each assigned
  instance can be credited at most once), so the metric is bounded by the
  assigned allotment — you maximize it by solving *more of your own set, faster,
  on harder tiers*.

- **Boolean gates** — the multipliers that are 0 or 1:
  - `liveness_gate_i` ∈ {0,1}: miner-level. A miner must have a **fresh
    heartbeat** within the liveness window or the whole sum is zeroed.
  - `verified(c)` ∈ {0,1}: per-solve. The submitted assignment must satisfy
    **this miner's own** CNF (deterministic witness check). Wrong answers and
    copied answers ⇒ 0.
  - `attest_gate(c)` ∈ {0,1}: per-solve. If the tier requires attested compute,
    the miner's attested run must verify (right image, real quote binding) ⇒
    else 0. Tiers that don't require it are always 1.

`speed(c)` and `tier_weight(t_c)` are the **continuous, linear** part — they
scale a credited solve but can never *manufacture* credit, because they are
multiplied by the booleans.

Final per-miner **weight** = `reward_i / max_j reward_j` (normalize to the
leader), then Ed25519-signed into a weight vector — the same emission shape a
validator would relay. Sybil identities are collapsed by coldkey before
normalizing.

---

## 3. The primitives it uses (all real, all local)

| Game mechanic | Real primitive | File |
|---|---|---|
| Agent submission | `Submission` answer dict, parsed never trusted as free text | `scaffold/contract.py` |
| **SSH-connected miner environment** | each miner is a worker dir + `solve.py` the publisher executes in a **network-isolated, host-timed sandbox** | `scaffold/lanes/sandbox.py:run_solver` |
| Per-miner challenges (anti-copy) | HMAC(hotkey)-seeded unique CNFs; copying fails because the CNF differs | `scaffold/publisher/per_miner.py` |
| SAT deterministic verification | independent witness check (a fast-but-wrong solve scores 0) | `scaffold/dimacs.py:verify_witness` |
| Polling / liveness | heartbeat freshness vs a window (the TEE-GPU `last_heartbeat_iso` pattern) | `game/miner.py` (models `tee_gpu` receipts) |
| Attestation / trusted compute | TDX quote binding (nonce∥pubkey, image∥result), verified under the real recipe | `scaffold/polaris.py`, `scaffold/verify.py:verify_attestation` |
| Compute provisioning as a gated resource | tier-2 reward requires a **provisioned (attestable) compute slot**; no slot ⇒ premium credit gated to 0 | `game/publisher.py` |
| Speed scoring | scale-free `ref/(ref+wall)` curve on **host-measured** wall time | `scaffold/grading.py:speed_bonus` |
| Clear score emission per miner | normalized, Ed25519-signed per-miner weight vector | `game/reward.py` |
| Sybil resistance | coldkey-collapse dedups/splits reward across an operator's hotkeys | `game/reward.py` (mirrors `weights.py`) |

---

## 4. The round loop (one tick)

1. **Provision & poll.** Publisher reads each miner's heartbeat. Stale ⇒ miner
   is marked not-live (`liveness_gate = 0`) and skipped for credit.
2. **Mint.** Publisher assigns each live miner a fresh unique set of CNFs for
   the epoch: tier-1 (easy participation floor, `biased`) and tier-2 (hard
   differentiator, `ajm`). Instances are HMAC(hotkey)-seeded — nobody else's
   answer fits.
3. **Solve (in the sandbox).** Publisher executes each miner's `solve.py`
   against its own CNF inside `run_solver` — network-isolated, rlimit-bounded,
   **wall time measured by the host**, timeout observed by the host. The miner
   never sees the planted witness.
4. **Attest (gated compute).** For tier-2 (premium) solves, the miner must
   produce an attested run binding its solver image + result. A miner holding a
   provisioned slot (a pullable/attestable image + e2e pubkey) verifies; one
   without gets `attest_gate = 0` on tier-2 — it can still earn tier-1.
5. **Verify & grade.** Deterministic witness check ⇒ `verified`. Speed bonus
   from host wall time. Record the solve in the ledger.
6. **Emit.** Compose `reward = metric × gate` per miner, collapse sybils,
   normalize to the leader, sign the vector, render the scoreboard.

---

## 5. Miner archetypes (so every gate is visible)

| Archetype | Behavior | Which gate fires | Expected result |
|---|---|---|---|
| `honest_fast` | solves own set fast, live, attested compute | none | **wins** (top weight) |
| `honest_slow` | solves correctly but slowly | speed term (continuous) | positive, lower |
| `cheater_wrong` | submits a non-satisfying assignment | `verified = 0` | **0** |
| `copier` | submits a victim's assignment under its own id | `verified = 0` (anti-copy: different CNF) | **0** |
| `dead` | stops heartbeating | `liveness_gate = 0` | **0** |
| `unprovisioned` | solves tier-2 correctly but has no attestable slot | `attest_gate = 0` on tier-2 | tier-1 only |
| `sybil_a` / `sybil_b` | one operator, two hotkeys, one coldkey | coldkey-collapse | reward split, no Sybil gain |

---

## 6. Why this is a good game

- **Clear rules.** Solve your own challenges, stay live, prove your compute.
- **Fair scoring.** Strictly more verified/fast/hard work ⇒ strictly more
  reward; the metric is linear and monotone.
- **Anti-cheat by construction.** Three independent deterministic gates
  (witness verify, per-miner anti-copy, attestation) plus liveness and Sybil
  collapse. None depends on opinion or consensus voting.
- **Obvious path to winning.** There is exactly one: be live, run attestable
  compute, and solve more of your own hard instances faster. No shortcut, no
  farm, no copy.
</content>
</invoke>
