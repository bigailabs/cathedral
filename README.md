<p align="center">
  <img src="docs/assets/cathedral-mark.svg" alt="Cathedral mark" width="112">
</p>

<h1 align="center">Cathedral</h1>

<h3 align="center">Cathedral pays for fundamental algorithmic change in the substrate underneath modern computing.</h3>

<p align="center">A Bittensor subnet with publisher-scored work and on-chain weight finalization.</p>

<p align="center">
  <a href="https://cathedral.computer">Site</a> |
  <a href="https://api.cathedral.computer">Publisher API</a> |
  <a href="https://github.com/cathedralai/cathedral/releases">Releases</a>
</p>

## How Cathedral works

- Miners run their own agents and infrastructure. The live agent path is BYO Box: miners submit an agent bundle, and Cathedral evaluates it through SSH/Hermes.
- Cathedral still keeps the legacy `/v1/claim` path alive for existing Polaris-evidence submissions.
- Cathedral signs every evaluation row with its Ed25519 key.
- Validators pull signed rows, verify Cathedral signatures, map hotkeys to local metagraph uids, and call `set_weights`. Validators do not re-run the eval.
- `synthetic_boolean_v1` (SAT) is the first Task Family lane on top of this scored-and-signed pipeline. It is one lane, not a new protocol.

## Current path and SAT lane

The live production path is the agent pipeline. Miners submit bundles through `POST /v1/agents/submit`; the publisher evaluates them and signs rows that validators pull. The legacy `/v1/claim` worker still exists for older Polaris-evidence submissions.

Mainnet SAT is disabled by default. `config/mainnet.toml` keeps `task_family_weights = { synthetic_boolean_v1 = 0.0 }` and `forced_burn_percentage = 95.0`. Moving SAT weight above `0.0` requires an intentional operator release.

Public main includes the SAT lane, CNF URL transport, signed schema-5 rows, first-submitted receipt ordering, and the global zero-score kill switch. Public feeds stay hash-only for SAT rows.

Static site copy and demo views must not be treated as live SAT metrics.

## Getting started

### For miners

[docs/miner/QUICKSTART.md](docs/miner/QUICKSTART.md)

You need:

- A registered Bittensor hotkey.
- A reachable Linux host that Cathedral can SSH into.
- Hermes installed for the SSH user Cathedral will run.
- Your own agent, solver, or wrapper available inside that environment.

Miners keep solver source, wrappers, logs, and infrastructure private. Cathedral verifies only the final answer returned by the run.

### For validators

[docs/validator/RUNBOOK.md](docs/validator/RUNBOOK.md)

The validator pulls signed eval rows, verifies Ed25519 signatures, stores rows, maps hotkeys to uids, computes weights, and calls `set_weights`. The remote signed-weight path is opt-in and shipped behind a pinned public key.

### For operators

[docs/lanes/synthetic-boolean-launch-rails.md](docs/lanes/synthetic-boolean-launch-rails.md)

One active formula at a time. Eligible miners race the same formula. The launch rule is first submitted among valid receipts: Cathedral records receipt time before trace collection, verification marks receipts valid or invalid, and durable selection waits behind earlier unresolved receipts before selecting the earliest valid answer.

## Install

```bash
git clone https://github.com/cathedralai/cathedral
cd cathedral
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

This installs `cathedral`, `cathedral-validator`, and `cathedral-miner`.

## Tests

```bash
PYTHONPATH=src pytest tests/lanes/test_contract.py -k synthetic_boolean_v1 -q
PYTHONPATH=src pytest tests/lanes/test_synthetic_boolean_runtime.py tests/test_remote_weight_loop.py tests/test_publisher_weight_policy.py -q
```

## Run

```bash
export CATHEDRAL_BEARER=$(openssl rand -hex 32)
export CATHEDRAL_PUBLIC_KEY_HEX=<cathedral-eval-signing-public-key>

cathedral-validator migrate --config config/mainnet.toml
cathedral-validator serve --config config/mainnet.toml
```

## Trust model in one paragraph

Cathedral is the verifier-of-record for private SAT challenges. Validators verify Cathedral's signature and policy fields on each row, not the SAT answer itself. The private corpus and active formulas stay publisher-private. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#trust-model) for the full statement and the conditions under which validator-independent verification could be added later.

## Public and private boundaries

Public surfaces may expose only hash-backed SAT result rows: task id hash, answer hash, verifier details hash, score fields, schema version, Cathedral signature.

Public surfaces must not expose raw CNF text, submitted DIMACS answers, hidden metadata, private corpus material, trace bundle URLs, manifest URLs, or private score material.

The public repository must not contain real `.cnf`, `.dimacs`, or `.sol` files.

## Documentation map

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): component map, async loops, database, trust model.
- [docs/VALIDATOR.md](docs/VALIDATOR.md): validator mechanism notes.
- [docs/validator/RUNBOOK.md](docs/validator/RUNBOOK.md): day-2 validator operator runbook.
- [docs/miner/QUICKSTART.md](docs/miner/QUICKSTART.md): primary miner guide.
- [docs/miner/MIGRATION_TO_SAT.md](docs/miner/MIGRATION_TO_SAT.md): staged SAT miner migration plan.
- [docs/lanes/synthetic-boolean-launch-rails.md](docs/lanes/synthetic-boolean-launch-rails.md): SAT launch sequencing and public/private boundaries.
- [RELEASES.md](RELEASES.md): shipped release history.
