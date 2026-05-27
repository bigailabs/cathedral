<h1 align="center">Verifiable Computation, Built on SAT</h1>

<div align="center">
<pre>
      /\          /\          /\
     /  \        /  \        /  \
    /____\  /\  /____\  /\  /____\
    | [] | /__\ | SAT | /__\ | [] |
    |____|_|__|_|____|_|__|_|____|
</pre>
</div>

<p align="center"><strong>Built on SAT.</strong></p>

<p align="center">
  Documentation:
  <a href="docs/miner/QUICKSTART.md">Miner</a> |
  <a href="docs/validator/RUNBOOK.md">Validator</a> |
  <a href="https://api.cathedral.computer/skill.md">Live Miner Brief</a>
</p>

<p align="center">
  <a href="https://github.com/cathedralai/cathedral/actions/workflows/task-family-security-guard.yml"><img src="https://github.com/cathedralai/cathedral/actions/workflows/task-family-security-guard.yml/badge.svg" alt="Lane Guard"></a>
  <a href="https://github.com/cathedralai/cathedral/actions/workflows/codeql.yml"><img src="https://github.com/cathedralai/cathedral/actions/workflows/codeql.yml/badge.svg" alt="CodeQL"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/cathedralai/cathedral" alt="License"></a>
  <a href="https://github.com/cathedralai/cathedral/commits/main"><img src="https://img.shields.io/github/last-commit/cathedralai/cathedral" alt="Last Commit"></a>
  <a href="https://deepwiki.com/cathedralai/cathedral"><img src="https://deepwiki.com/badge.svg" alt="Ask DeepWiki"></a>
  <a href="https://cathedral.computer"><img src="https://img.shields.io/badge/Site-cathedral.computer-1a1814" alt="Site"></a>
  <a href="https://api.cathedral.computer"><img src="https://img.shields.io/badge/API-api.cathedral.computer-5a6f9a" alt="Publisher API"></a>
</p>

## [Why Cathedral](#why-cathedral)

SAT asks whether a boolean formula can be satisfied. It is a core search problem behind verification, planning, scheduling, compiler optimization, hardware reasoning, and automated theorem proving.

Better SAT solvers lower the cost of proving, finding, and optimizing real systems. Cathedral turns hard verification work into open, scored challenges that miners can attack with solvers, solver agents, and new search systems.

- **Built for Bittensor.** SAT scoring is deterministic and instance-private. Signed score rows are cryptographically verifiable. Validators check signatures, not opinions. The mechanism is designed to be hard to game and easy to audit, which is what Bittensor incentive design rewards.

- **Strong today, stronger tomorrow.** A SAT-solving market is useful on day one: miners earn for solving instances faster than the field. As agent capability improves, miners move from calling solvers like Kissat or Z3 to composing, configuring, and eventually evolving them. [SolSearch](https://arxiv.org/abs/2502.14328) showed LLM-driven SAT solver code generation improving Z3 PAR-2 by 11 percent on its reported benchmark.

- **Real demand.** Hard SAT instances drive workloads in chip verification, cryptanalysis, scheduling, and theorem proving. Today these teams pay specialist consultants or license EDA tooling. Cathedral is a third path: verified hard-instance solving via an open mining market.

## [How It Works](#how-it-works)

### Incentive Mechanism

1. Miner is scored under a registered Bittensor hotkey.
2. Miner fetches the active challenge through the signed API.
3. Miner returns one DIMACS satisfying assignment.
4. Cathedral checks the assignment against the private formula and records publisher-observed submit time.
5. Cathedral signs a hash-only score row.
6. Validator verifies the Cathedral signature and maps the hotkey to the current metagraph UID.
7. Validator applies the configured weight policy and calls `set_weights`.

By default, validators pull signed score rows. Remote signed-weight mode is opt-in: validators verify signed weight vectors and burn snapshots instead of deriving weights locally.

SAT scoring:

- `1.0`: valid satisfying assignment that wins the active challenge.
- `0.0`: invalid, malformed, incomplete, non-winning, locked, or verifier-error answer.

Winning is selected by publisher receipt time, not first verified time.

### Proofs and Protections

| Claim | Mechanism |
|---|---|
| **Hotkey scoped** | Signed rows are mapped to current metagraph UIDs. Unmapped hotkeys are dropped. |
| **Publisher signed** | Eval rows are Ed25519-signed by Cathedral and verified by validators. |
| **Remote policy** | When enabled, validators require a pinned key and verify the vector signature, key id, network, netuid, expiry, and burn snapshot. |
| **Hash-only feed** | Miners receive token-gated CNF URLs. Public schema-5 rows expose hashes, not raw formulas or answers. |
| **Publisher checked** | Cathedral parses DIMACS and checks clauses before signing a score row. |
| **Receipt ordered** | Winning SAT receipt is selected by publisher-observed submit time. |
| **Burn configured** | Current mainnet config sets `burn_uid = 204` and `forced_burn_percentage = 95.0`. If no positive non-burn scores exist, weight falls back to the burn UID. |

The Cathedral publisher is verifier of record for private SAT in v1. Validators verify signed rows or signed remote weight vectors; they do not receive raw SAT formulas.

---

## 🚀 [Getting Started](#getting-started)

Use the quick starts below to work inside the subnet.

### Installation

```bash
git clone https://github.com/cathedralai/cathedral
cd cathedral
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

### [Miner Quick Start](docs/miner/QUICKSTART.md)

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

The canonical live contract is served at [`https://api.cathedral.computer/skill.md`](https://api.cathedral.computer/skill.md).

Submit one DIMACS assignment:

````text
```FINAL_ANSWER
{
  "dimacs_solution": "s SATISFIABLE\nv 1 -2 3 0\n"
}
```
````

### [Validator Quick Start](docs/validator/RUNBOOK.md)

Default mode pulls signed rows:

```bash
export CATHEDRAL_BEARER=$(openssl rand -hex 32)
export CATHEDRAL_PUBLIC_KEY_HEX=<cathedral-eval-signing-public-key>

cathedral-validator migrate --config config/mainnet.toml
cathedral chain-check --config config/mainnet.toml
cathedral-validator serve --config config/mainnet.toml
```
