# Cathedral — the local game

A playable, testable, fully-offline instance of the Cathedral mechanism. It
wires the **real** `scaffold/` primitives (it does not reimplement them) into one
round loop with clear rules, fair scoring, anti-cheat, and an obvious path to
winning. No chain, no cloud, no paid resources.

Read [`GAME_SPEC.md`](GAME_SPEC.md) for the full design. The one rule:

```
reward = linear_metric_to_maximize × boolean_gate
reward_i = liveness_gate_i × Σ_c [ verified(c) × attest_gate(c) × tier_weight(t_c) × speed(c) ]
```

## Run it

```bash
pip install -e .            # the scaffold package (offline; or use the repo .venv)
python -m game             # one round
python -m game 3          # three rounds (fresh epoch each round, credits accumulate)
cathedral-game 3           # same after editable install
```

You get a scoreboard: per-miner metric / reward / weight, a per-solve ledger
showing every factor of the rule, a Sybil-resistance panel, and an
independently-verifiable Ed25519-signed weight vector.

## Test it

```bash
python -m pytest game/tests -q
```

- `test_game.py` — full-round assertions: cheaters and copiers score 0, dead
  miners are gated out, the unprovisioned miner earns the tier-1 floor but not
  the attested-compute premium, faster honest solvers earn more, the Sybil
  collapse caps an operator's coldkey, and the signed vector verifies.
- `test_anticopy.py` — fast primitive-level proofs (no sandbox) that a stolen
  answer cannot satisfy a different miner's CNF and a foreign challenge-id is
  rejected.

## What is real vs modeled (honest map)

- **Real:** per-miner HMAC-seeded challenge generation + anti-copy, deterministic
  SAT witness verification, network-isolated **host-timed** sandbox execution of
  the miner's solver, the speed curve, the attestation report-data binding recipe,
  Ed25519 signing/verification of the emitted vector, coldkey Sybil collapse.
- **Modeled / stubbed (offline, honestly marked):** the TDX quote is the
  `PolarisClient(live=False)` stub — it exercises the *real binding recipe* but
  does **not** verify Intel's chain (production routes `verify_attestation` to a
  real DCAP verifier). Liveness heartbeats are in-process, not a network probe.
  The chain set_weights is not called — the signed vector is the emission a
  validator would relay.

## Files

| file | role |
|---|---|
| `config.py` | generator knobs (small fast shapes), liveness window, tier refs |
| `miner.py` | `MinerEnv` archetypes — identity, solver behavior, compute slot, heartbeat |
| `runner.py` | the miner's solver, executed by the publisher inside the sandbox |
| `publisher.py` | the referee: mint, poll liveness, run+verify+attest, grade |
| `reward.py` | the Const rule, Sybil collapse, signed-vector emission |
| `engine.py` | runs N rounds, wires it together |
| `scoreboard.py` | human-readable render |
