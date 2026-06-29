<h2 align="center">Decentralized&nbsp;formal&nbsp;verification.&nbsp;Built&nbsp;on&nbsp;SAT.</h2>

<p align="center">
  Documentation:
  <a href="VALIDATOR.md">Validator</a> |
  <a href="CATHEDRAL_V0_LANES.md">v0 Lanes</a> |
  <a href="LAUNCH_V0_RUNBOOK.md">v0 Launch Runbook</a> |
  <a href="game/arena/ARENA.md">Arena</a> |
  <a href="https://api.cathedral.computer/v1/synthetic-boolean/active-challenges">Active Challenges</a> |
  <a href="https://github.com/cathedralai/cathedral/releases">Releases</a>
</p>

<p align="center">
  <a href="https://github.com/cathedralai/cathedral/commits/main"><img src="https://img.shields.io/github/last-commit/cathedralai/cathedral" alt="Last Commit"></a>
  <a href="https://deepwiki.com/cathedralai/cathedral"><img src="https://deepwiki.com/badge.svg" alt="Ask DeepWiki"></a>
  <a href="https://cathedral.computer"><img src="https://img.shields.io/badge/Site-cathedral.computer-1a1814" alt="Site"></a>
  <a href="https://api.cathedral.computer"><img src="https://img.shields.io/badge/API-api.cathedral.computer-5a6f9a" alt="Publisher API"></a>
</p>

## [Why Cathedral](#why-cathedral)

Cathedral is decentralized formal verification: compute whose answers can
be checked deterministically and rewarded through an open miner market.
The substrate is Boolean satisfiability, the canonical NP-complete
problem. Any question that can be reduced to SAT can be raced by miners
and verified by Cathedral before validators use the signed result for
weights.

SAT asks whether a boolean formula can be satisfied. It is a core search
problem behind verification, planning, scheduling, compiler optimization,
hardware reasoning, and automated theorem proving. Better SAT solvers
lower the cost of proving, finding, and optimizing real systems. Cathedral
turns hard verification work into open, scored challenges that miners can
attack with solvers, solver agents, and new search systems.

- **Built for Bittensor.** SAT scoring is deterministic and instance-private. Signed score rows are cryptographically verifiable. Validators check signatures, not opinions. The mechanism is designed to be hard to game and easy to audit, which is what Bittensor incentive design rewards.

- **Designed for solver evolution.** A SAT-solving market is useful on day one: miners are scored for verified solves, with rewards determined by the live signed validator weight policy. As agent capability improves, miners move from calling solvers like Kissat or Z3 to composing, configuring, and eventually evolving them. Recent work such as [SATLUTION](https://arxiv.org/abs/2509.07367) points toward LLM-driven solver evolution under correctness checks; Cathedral makes that kind of work economic.

- **Real demand.** Hard SAT and SMT instances drive workloads at industrial scale: Amazon has described AWS automated-reasoning systems generating [a billion SMT queries a day](https://www.amazon.science/blog/a-billion-smt-queries-a-day). Chip companies depend on formal verification platforms from Cadence, Synopsys, and Siemens. Today these teams either pay enterprise EDA licenses or run reasoning workloads inside closed cloud services. Cathedral is a third path: verifiable hard-instance solving through an open mining market.

## [How It Works](#how-it-works)

### Incentive Mechanism

1. Each miner is scored under their registered Bittensor hotkey.
2. Miner fetches the active challenge through the signed API.
3. Miner returns one DIMACS satisfying assignment.
4. Cathedral checks the assignment against the private formula and records publisher-observed submit time.
5. Cathedral signs a hash-only score row.
6. Validator verifies the Cathedral signature and maps the hotkey to the current metagraph UID.
7. Validator applies the signed weight policy and calls `set_weights`.

Validators apply one Cathedral-signed weight vector: a final score per miner plus the signed burn snapshot, verified against a pinned key. Scoring (recency window, multi-challenge composition, burn rate) is composed on the publisher and signed, so validators relay the signed number rather than recomputing it locally. The per-solve feed (`/v1/leaderboard/recent`) stays public as an independently re-checkable audit trail.

External mechanisms can feed Cathedral without changing validators. For example, Violet posts normalized audio miner scores to `POST /v1/external-scores/violet`; Cathedral stores/blends those publisher-side and still emits the single signed `/v1/validator/weights/next` vector. See [`docs/VIOLET_EXTERNAL_SCORES.md`](docs/VIOLET_EXTERNAL_SCORES.md).

SAT scoring:

- A valid satisfying assignment records a verified solve for that hotkey and challenge.
- Invalid, malformed, incomplete, non-winning, locked, or verifier-error answers score `0.0`.
- When proportional mode is enabled and deployed, signed miner weights are composed from distinct verified solves in the recency window, weighted by challenge tier.

Winning is selected by publisher receipt time, not first verified time. After
this publisher release is deployed, it exposes the current generator policy,
tier mix, and scoring weights in
`/v1/synthetic-boolean/active-challenges`.

### Proofs and Protections

| Claim | Mechanism |
|---|---|
| **Hotkey scoped** | Signed rows are mapped to current metagraph UIDs. Unmapped hotkeys are dropped. |
| **Publisher signed** | Eval rows are Ed25519-signed by Cathedral and verified by validators. |
| **Signed weight policy** | Validators pin the publisher key and verify the weight vector's signature, key id, network, netuid, expiry, and burn snapshot before applying it. |
| **Hash-only feed** | Miners receive token-gated CNF URLs. Public score rows expose hashes, not raw formulas or answers. |
| **Publisher checked** | Cathedral parses DIMACS and checks clauses before signing a score row. |
| **Receipt ordered** | Winning SAT receipt is selected by publisher-observed submit time. |
| **Burn configured** | Current mainnet config sets `burn_uid = 204` and `forced_burn_percentage = 0.0`; scored miner work receives the full non-fallback vector. If no positive non-burn scores exist, weight falls back to the burn UID. |

The Cathedral publisher is verifier of record for private SAT in v1. Validators verify the signed weight vector; they do not receive raw SAT formulas.

---

## [Getting Started](#getting-started)

Use the quick starts below to work inside the subnet.

### Installation

```bash
git clone https://github.com/cathedralai/cathedral
cd cathedral
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Miner Quick Start

> **Heads up:** the `cathedral-runtime` Docker image (`ghcr.io/cathedralai/cathedral-runtime:v1.0.7`) is **deprecated**. Current miners do not need it. Run Hermes directly on a Linux SSH host.

You need:

- registered Bittensor hotkey
- private SAT solver, solver agent, or wrapper
- SSH host with Hermes for post-win audit
- access to the signed challenge API

Live miner flow:

1. Read the public challenge metadata.
2. Fetch the tokenized CNF through signed `active-cnf`.
3. Verify the CNF SHA-256.
4. Solve locally.
5. Submit `challenge_id` and `dimacs_solution` to `/v1/agents/submit`.

The canonical live board is served at
[`https://api.cathedral.computer/v1/synthetic-boolean/active-challenges`](https://api.cathedral.computer/v1/synthetic-boolean/active-challenges).

Submit a DIMACS assignment:

````text
```FINAL_ANSWER
{
  "dimacs_solution": "s SATISFIABLE\nv 1 -2 3 0\n"
}
```
````

Per-miner private challenges use the same submit endpoint, but miners fetch
their own assigned set first:

- `GET /v1/synthetic-boolean/per-miner/challenges`
- `GET /v1/synthetic-boolean/per-miner/cnf?challenge_id=pm-...`
- `GET /v1/synthetic-boolean/per-miner/status`
- `POST /v1/agents/submit`

Use the same Cathedral hotkey signing headers as `active-cnf`. The status
endpoint shows accepted PM solves, rejected PM attempts, rejection reasons,
eligible/unique/verified solves, tier mix, current epoch totals, and 24h totals.

Submit full DIMACS solver output for both public and per-miner challenges:

```text
s SATISFIABLE
v 1 -2 3 0
```

Do not submit raw literals only (`1 -2 3 0`). That fails with
`solution_missing_status`.

Retry behavior:

- `429 submit_busy_retry`: Cathedral submit slots are full. Retry the same
  signed payload after `Retry-After` with jittered backoff. This is not a bad
  solve.
- `429 rate_limited`: the same hotkey/challenge submitted too quickly. Wait
  longer before retrying.
- `400 solution_missing_status`: the payload is malformed; include
  `s SATISFIABLE` and one or more `v ... 0` lines.

Operator visibility:

- `GET /v1/admin/synthetic-boolean/submit-metrics` shows submit pressure,
  429s, and rejection reason counters.
- Set `CATHEDRAL_PUBLISHER_ADMIN_TOKEN` and pass
  `Authorization: Bearer <token>` to read it.
- `CATHEDRAL_SUBMIT_LOG_EVENTS=1` enables per-submit rejection logs. Leave it
  off for normal high-throughput operation; counters still record by default.

### Local Verification Arena

The repo also includes **Subnet Breaker**, a local playable arena for the
agent/audit path. It does not touch chain state, Railway, paid compute, or the
live validator. It exists to show the Cathedral loop end to end:

```text
probe -> encode -> solve -> replay -> attest -> seal
```

Run it locally:

```bash
python -m game.arena.serve 8790
# open http://127.0.0.1:8790/game
```

Useful checks:

```bash
python -m game.arena.playthrough
python -m game.arena.audit_scanner_smoke
python -m game.arena --shot
python -m game.arena.verify game/arena/out
python -m game.arena.bundle game/arena/out/proof_bundle.json
```

Full guide: [`game/arena/ARENA.md`](game/arena/ARENA.md).

The production-style audit scanner bridge lives at `/v1/audit-scanner/*`.
It is default-off, signed, replay-scored, and not connected to payment weights
until explicitly promoted. `python -m game.arena.audit_scanner_smoke` proves
the full bridge contract locally.

### [Validator Quick Start](VALIDATOR.md)

The v4 validator is one loop: fetch one signed score per miner from the publisher, verify the signature, set weights. Scoring lives on the publisher, so there is no re-release for scoring changes.

```bash
pip install -e .                              # installs the `cathedral-validator` command
cp config/validator.toml my-validator.toml    # add your wallet + pin the publisher key

# Preview the weights it would set, without touching the chain:
cathedral-validator serve --config my-validator.toml --dry-run --once

# Go live:
cathedral-validator serve --config my-validator.toml
```

Pin the publisher's signing key in your config and verify it against the live [JWKS](https://api.cathedral.computer/.well-known/cathedral-jwks.json) before going live. Full guide: [VALIDATOR.md](VALIDATOR.md).
