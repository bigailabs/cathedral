<h2 align="center">Decentralized&nbsp;formal&nbsp;verification.&nbsp;Built&nbsp;on&nbsp;SAT.</h2>

<p align="center">
  <b>Cathedral v4</b> — the thin publisher. ~5k lines replacing the 46k-line monolith,
  byte-identical on the signed wire surface validators verify.
</p>

<p align="center">
  Documentation:
  <a href="V4-DESIGN.md">Design</a> |
  <a href="VALIDATOR.md">Run a Validator</a> |
  <a href="COMPAT.md">Wire Compatibility</a> |
  <a href="ATTESTATION.md">Attestation</a> |
  <a href="deploy/RUNBOOK.md">Deploy Runbook</a> |
  <a href="RELEASE_STATUS.md">Release Status</a>
</p>

<p align="center">
  <a href="https://cathedral.computer"><img src="https://img.shields.io/badge/Site-cathedral.computer-1a1814" alt="Site"></a>
  <a href="https://api.cathedral.computer"><img src="https://img.shields.io/badge/API-api.cathedral.computer-5a6f9a" alt="Publisher API"></a>
  <a href="https://api.cathedral.computer/skill.md"><img src="https://img.shields.io/badge/Live-Miner_Brief-2d5a3d" alt="Live Miner Brief"></a>
</p>

## Why v4

Cathedral pays weight for exactly one thing: **work it can independently
verify.** Miners race certificate-checked SAT challenges; every accepted
answer is re-verified against the formula before a score row is signed.
v4 rebuilds the publisher thin:

- **Byte-identical wire surface.** Validators consume Ed25519-signed eval
  rows. v4 emits the same v5/v6 row schema, canonical JSON, cursor semantics,
  and signing key as production — proven against live rows (`wire_compat.py`,
  8/8 on sampled production data). Validators require no update.
- **Open window, real ranks.** A challenge accepts one solve per distinct
  hotkey while active; each solve carries its true first-seen rank. Re-solves
  are rejected. One scored solve per (challenge, hotkey), ever — enforced by
  schema.
- **Scoring is one signed number per miner.** The orchestrator composes every
  miner's final weight (recency window, multi-challenge blend, burn) and signs
  it; validators verify the signature and apply it — no local averaging, no
  rolling window, no row database. Recency, burn rate, and future scoring
  changes ship orchestrator-side with **no validator release**. See
  [VALIDATOR.md](VALIDATOR.md).

## How It Works

1. The publisher mints calibrated CNF challenges and serves a public board.
2. A miner fetches the tokenized CNF through the signed `active-cnf` flow.
3. The miner solves locally and submits one DIMACS satisfying assignment.
4. The publisher verifies every clause against the private formula.
5. An accepted solve is claimed atomically with its first-seen rank.
6. The publisher signs a v6 score row (plus a v5-compat mirror) and serves
   both on the leaderboard feed — the public, re-checkable audit trail.
7. The orchestrator composes those solves into one final weight per miner and
   signs the vector served at `/v1/validator/weights/next`.
8. Validators fetch the signed vector, verify it against the pinned key, apply
   the signed burn, and set weights ([VALIDATOR.md](VALIDATOR.md)).

## Proofs and Protections

| Claim | Mechanism |
|---|---|
| **Answers verified, not trusted** | Every DIMACS assignment is checked clause-by-clause before scoring; 9 adversarial fixtures guard the parser |
| **Rows publisher-signed** | Ed25519 over the canonical v5/v6 subset; validators pin the public key |
| **One solve per miner per challenge** | `UNIQUE(family_id, challenge_id, miner_hotkey)` — the claim is the dedup |
| **No duplicate challenges** | Challenge id and CNF content derive from the same `(seed, tier, sequence)` triple — identical content cannot appear under two ids |
| **Replay rejected** | Submission signatures are single-use; clock-skew bounded |
| **CNF access gated** | HMAC-tokenized, constant-time fetch; hash-only public rows |
| **Self-reported timing ignored** | Speed claims require server measurement or hardware attestation (see [ATTESTATION.md](ATTESTATION.md)) |

## Run It

```bash
python -m scaffold.validator_thin --help   # the v4 validator (see VALIDATOR.md)
python -m scaffold.dashboard     # one-page board -> http://127.0.0.1:8099
python -m scaffold.publisher.app # the thin publisher (orchestrator)
```

Release gates — all must pass before the wire surface ships:

```bash
python rc_verify.py          # scoring invariants across all lanes
python wire_compat.py        # byte-compat vs live production rows
python publisher_verify.py   # end-to-end miner sign -> fetch -> solve -> submit -> validator pull
```

## Deploy

The thin publisher takes over the live API with zero validator updates via a
same-URL swap; the previous backend stays warm for instant rollback. Full
sequence — stage, seed from the live feed, soak, swap, abort criteria — in
[`deploy/RUNBOOK.md`](deploy/RUNBOOK.md).

## Layout

- `scaffold/wire.py` — the frozen signed-row surface (v5/v6 key sets, canonical JSON)
- `scaffold/publisher/` — app, store, scoring policy, seed/refill/soak
- `scaffold/lanes/` — lane implementations (SAT challenge, solver arena, encoding)
- `scaffold/contract.py` — the lane contract (pure generate / verify / score)
- `deploy/` — Dockerfile, railway.toml, runbook
- `fixtures/live-20260609/` — golden vectors sampled from the live network
