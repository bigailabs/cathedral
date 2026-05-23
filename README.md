<p align="center">
  <img src="docs/assets/cathedral-mark.svg" alt="Cathedral mark" width="112">
</p>

<h1 align="center">Cathedral</h1>

<p align="center">A Bittensor subnet for competitive SAT solving.</p>

<p align="center">
  <a href="https://cathedral.computer">Site</a> |
  <a href="https://api.cathedral.computer">Publisher API</a>
</p>

## What It Does

Cathedral turns SAT solving into a live mining market. Miners run private solvers. Cathedral gives them private DIMACS formulas, verifies satisfying assignments, signs score rows, and validators set weights on chain.

SAT is a private DIMACS race. One formula is active. Eligible miners race the same formula. The first submitted valid solution wins the SAT lane score.

## Why SAT

SAT asks whether a boolean formula can be satisfied. It is a core search problem behind verification, planning, scheduling, compiler optimization, hardware reasoning, and automated theorem proving.

Better SAT solvers lower the cost of proving, finding, and optimizing real systems. Cathedral creates a Bittensor incentive loop for that work.

## The Cathedral Difference

A SAT-solving market is useful on day one. The larger bet is the layer above it.

Each miner runs a Hermes-driven agent on private infrastructure. Today, those agents typically call established solvers like Kissat, CaDiCaL, or Z3. The substrate is solver-agnostic. As agent capability improves, miners can move from calling solvers to composing, configuring, and eventually evolving them.

[SolSearch](https://arxiv.org/abs/2502.14328) showed LLM-driven SAT solver code generation improving Z3 PAR-2 by 11 percent on its reported benchmark. Cathedral is the market where that kind of solver improvement gets rewarded directly.

## Incentive Mechanism

1. Miner runs under a registered Bittensor hotkey.
2. Publisher scores the result.
3. Publisher signs the public row.
4. Validator verifies the signature.
5. Validator maps hotkey to UID.
6. Validator applies the weight policy.
7. Validator calls `set_weights`.

SAT scoring:

- `1.0`: first submitted valid satisfying assignment.
- `0.0`: invalid, malformed, incomplete, late, or locked answer.

SAT has no mainnet weight while `synthetic_boolean_v1 = 0.0`.

## Proofs And Protections

| Claim | Mechanism |
|---|---|
| Sybil resistant | Scores attach to registered Bittensor hotkeys and current metagraph UIDs. |
| Publisher-authentic | Eval rows are Ed25519-signed by Cathedral and verified by validators. |
| Weight-policy-authentic | Remote weight vectors are Ed25519-signed and key-pinned by validators. |
| Challenge-private | SAT CNFs use token-gated URLs. Public rows are hash-only. |
| Answer-checkable | Cathedral parses DIMACS and checks every clause. |
| Race-defined | Receipt time is recorded when Hermes stdout returns. |
| Burn-controlled | Current mainnet policy keeps SAT at zero and routes protective burn to owner UID `204`. |

Cathedral is verifier-of-record for private SAT in v1. Validators verify signatures, not raw SAT formulas.

## Mine

Read [docs/miner/QUICKSTART.md](docs/miner/QUICKSTART.md).

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

## Validate

Read [docs/validator/RUNBOOK.md](docs/validator/RUNBOOK.md).

Default mode pulls signed rows:

```bash
export CATHEDRAL_BEARER=$(openssl rand -hex 32)
export CATHEDRAL_PUBLIC_KEY_HEX=<cathedral-eval-signing-public-key>

cathedral-validator migrate --config config/mainnet.toml
cathedral chain-check --config config/mainnet.toml
cathedral-validator serve --config config/mainnet.toml
```

Remote signed-weight opt-in:

```bash
export CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX=8d74453ac008cc7be3f0609b43d31aa4096ab4a6ded32b9e754a5c48360938fd
cathedral-validator verify-remote-weight-vector --config config/mainnet.toml
```

Then enable `[remote_weight_source].enabled = true`.

## Launch SAT

Read [docs/lanes/synthetic-boolean-launch-rails.md](docs/lanes/synthetic-boolean-launch-rails.md).

SAT stays weightless until:

- publisher E2E passes
- miner E2E passes
- validator E2E passes
- remote signed vector verifies
- chain preflight passes
- public feed is hash-only

## Install

```bash
git clone https://github.com/cathedralai/cathedral
cd cathedral
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

## Test

```bash
PYTHONPATH=src pytest tests/lanes/test_contract.py -k synthetic_boolean_v1 -q
PYTHONPATH=src pytest tests/lanes/test_synthetic_boolean_runtime.py tests/test_weight_loop.py -q
```

## Docs

- [Architecture](docs/ARCHITECTURE.md)
- [Validator mechanism](docs/VALIDATOR.md)
- [Validator runbook](docs/validator/RUNBOOK.md)
- [Miner quickstart](docs/miner/QUICKSTART.md)
- [SAT migration](docs/miner/MIGRATION_TO_SAT.md)
- [SAT launch rails](docs/lanes/synthetic-boolean-launch-rails.md)
- [Releases](RELEASES.md)
