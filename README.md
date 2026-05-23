<p align="center">
  <img src="docs/assets/cathedral-mark.svg" alt="Cathedral mark" width="112">
</p>

<h1 align="center">Cathedral</h1>

<p align="center">Competitive SAT solving as a Bittensor incentive market.</p>

<p align="center">
  <a href="https://cathedral.computer">Site</a> |
  <a href="https://api.cathedral.computer">Publisher API</a>
</p>

## ⛪ [What Is Cathedral](#what-is-cathedral)

Cathedral turns SAT solving into a live mining market. Miners run private solvers. Cathedral gives them private DIMACS formulas, verifies satisfying assignments, signs score rows, and validators set weights on chain.

SAT is a private DIMACS race. One formula is active. Eligible miners race the same formula. The first submitted valid solution wins the SAT lane score.

## 🧭 [Why Cathedral](#why-cathedral)

SAT asks whether a boolean formula can be satisfied. It is a core search problem behind verification, planning, scheduling, compiler optimization, hardware reasoning, and automated theorem proving.

Better SAT solvers lower the cost of proving, finding, and optimizing real systems. Cathedral creates a Bittensor incentive loop for that work.

- **Built for Bittensor.** SAT scoring is deterministic and instance-private. Signed score rows are cryptographically verifiable. Validators check signatures, not opinions. The mechanism is designed to be hard to game and easy to audit, which is what Bittensor incentive design rewards.

- **Strong today, stronger tomorrow.** A SAT-solving market is useful on day one: miners earn for solving instances faster than the field. As agent capability improves, miners move from calling solvers like Kissat or Z3 to composing, configuring, and eventually evolving them. [SolSearch](https://arxiv.org/abs/2502.14328) showed LLM-driven SAT solver code generation improving Z3 PAR-2 by 11 percent on its reported benchmark.

- **Real demand.** Hard SAT instances drive workloads in chip verification, cryptanalysis, scheduling, and theorem proving. Today these teams pay specialist consultants or license EDA tooling. Cathedral is a third path: verified hard-instance solving via an open mining market.

## ⚙️ [How It Works](#how-it-works)

### Incentive Mechanism

1. Miner is scored under a registered Bittensor hotkey.
2. Cathedral gives the miner a private SAT challenge.
3. Miner returns a DIMACS satisfying assignment.
4. Cathedral checks the assignment against the private formula and records publisher-observed receipt time.
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
| Registered-hotkey scoped | Signed rows are mapped to current metagraph UIDs. Unmapped hotkeys are dropped. |
| Publisher-authentic | Eval rows are Ed25519-signed by Cathedral and verified by validators. |
| Remote-policy-authentic | When enabled, validators require a pinned key and verify the vector signature, key id, network, netuid, expiry, and burn snapshot. |
| Hash-only public feed | Miners receive token-gated CNF URLs. Public schema-5 rows expose hashes, not raw formulas or answers. |
| Publisher-checkable | Cathedral parses DIMACS and checks clauses before signing a score row. |
| Receipt-ordered | Winning SAT receipt is selected by publisher-observed receipt time after Hermes stdout returns. |
| Burn-configured | Current mainnet config sets `burn_uid = 204` and `forced_burn_percentage = 95.0`. If no positive non-burn scores exist, weight falls back to the burn UID. |

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

- registered hotkey
- Linux SSH host
- Hermes on `PATH`
- private solver or wrapper

Return exactly:

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
