# Cathedral SN39 validator

**Turn verified compute evidence into a transparent, fail-closed Bittensor
weight decision.**

This repository contains Cathedral's SN39 validator and the mechanisms it can
audit. It is the validator side of the Cathedral system:

- [Cathedral Computer](https://cathedral.computer/) is the customer-facing
  account, billing, API, job, and receipt product.
- [`cathedralconfidential`](https://github.com/cathedralai/cathedralconfidential)
  produces Intel TDX evidence, verified-work receipts, and signed score reports.
- This repository lets an independent validator verify those inputs, map public
  hotkeys to the current metagraph, and decide whether to set weights.

Cathedral is in **live testing**. The public signed feed and evidence surfaces
are available, and the validator software implements both thin and independent
provenance modes. The public release/tag and final launch acceptance are
separate gates. **Do not run a chain-writing command from an arbitrary `main`
checkout.**

## Choose your path

| Role | Start here |
|---|---|
| Cathedral Computer customer | [Product and API documentation](https://cathedral.computer/docs/) |
| Validator operator | [Validator guide](VALIDATOR.md) |
| Independent auditor | [Full-provenance verification](docs/PROVENANCE.md) |
| Intel TDX compute provider | [Cathedral Confidential mining guide](https://github.com/cathedralai/cathedralconfidential/blob/main/MINING.md) |
| Mechanism or subnet developer | [Score-class contract](docs/THIN_SCORE_CLASSES.md) |
| Contributor | [Open issues](https://github.com/cathedralai/cathedral/issues) |

## What the validator does

Every tick, the validator:

1. fetches Cathedral's signed weight vector;
2. verifies the Ed25519 signature, key id, network, netuid, policy, expiry, and
   rollback fence;
3. checks the signed burn contract and resolves every hotkey against a fresh
   metagraph;
4. runs the configured provenance audit concurrently;
5. fails closed when a gate belonging to the active submission authority
   fails; and
6. only then constructs the UID-aligned weight decision.

The validator wallet remains the sole authority for any `set_weights`
transaction. A score source can publish evidence and measurements; it cannot
sign with the validator's wallet or bypass validator-local policy.

## Two modes run together

| Mode | Default | What it trusts | What it submits |
|---|---:|---|---|
| **Thin + shadow audit** | Yes | A pinned Cathedral signature for the fast path, while a background audit checks public provenance | The gated signed vector |
| **Full-provenance authority** | No | The validator's own replay through pinned keys, source, verifier, historical candidate set, and controlled raw evidence | The validator's independently recomputed vector |

Shadow mode is intentionally non-blocking: a slow independent audit does not
delay the thin tick, and a shadow `FAIL` or `NOT_PROVEN` is observational—it
does not veto a thin submission whose own signature, scope, policy, freshness,
rollback, burn, and mapping gates pass. Authority mode is stricter and refuses
to submit unless the epoch reaches the documented `FULL` assurance level.
Signed receipts alone are useful provenance but are not `FULL`.

See [the provenance contract](docs/PROVENANCE.md) for the exact trust boundary,
candidate-set rules, controlled-disclosure package, and independent replay
command.

## Current capability boundary

| Capability | Status |
|---|---|
| Signed SN39 weight-vector verification | Deployed feed; validator implementation available |
| Default thin mode with concurrent provenance audit | Implemented in the launch candidate |
| Independent full-provenance recomputation | Implemented; requires all operator pins and controlled evidence |
| Current deployed vector vs independent verifier | `FAIL`: deployed v1/GPU-allocation shape has not converged with the v2/fixed-burn/body-binding verifier |
| Intel TDX verified-supply input | Current confidential-compute path |
| Confidential GPU subnet admission | Not currently admitted for positive weight |
| Registration or uptime rewards | Never sufficient; verified work is required |
| Self-service mainnet validator launch | Pending a tagged release and launch notice |

The current vector is deliberately allowed to contain no positive miners. In
that case the mechanism routes eligible mass to the configured burn
destination rather than preserving stale credit.

## Read-only quick start

Use a clean checkout and Python 3.11 or newer:

```bash
git clone https://github.com/cathedralai/cathedral.git
cd cathedral

python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[provenance]'

cp config/validator.toml my-validator.toml
```

Before trusting the example configuration:

1. select the exact tagged release announced for validators;
2. compare its release digest with your checkout or installed artifact;
3. verify the `cathedral-weight-policy` key through the live
   [JWKS](https://api.cathedral.computer/.well-known/cathedral-jwks.json);
4. pin the provenance keys, key-file digests, verifier digest, source revision,
   and burn hotkey from that same release; and
5. add your own wallet names without copying any secret into the repository.

Run one no-chain verification tick. It still fetches the signed vector and
shadow evidence over HTTPS, but opens no chain connection and cannot broadcast:

```bash
cathedral-validator serve \
  --config my-validator.toml \
  --offline \
  --once
```

Then run a metagraph-backed preview that still cannot write weights:

```bash
cathedral-validator serve \
  --config my-validator.toml \
  --dry-run \
  --once
```

Stop there until the tagged release, public launch notice, and your own
preflight are all green. The launch candidate is non-writing by default:
only an explicit `--broadcast` permits a chain-write attempt, and SN39's
signed release and transition gates must still authorize it.

## Observe and audit

TTY logs are concise and human-readable. A validator can also publish a stable
JSONL stream for dashboards and independent monitoring. Create the parent
directory first and keep it private:

```bash
install -d -m 700 "$HOME/.cathedral"
export CATHEDRAL_VALIDATOR_JSONL="$HOME/.cathedral/validator-events.jsonl"
tail -f "$CATHEDRAL_VALIDATOR_JSONL" | jq .
```

Events name the mode and stage and report `PASS`, `FAIL`, `NOT_PROVEN`, or
`INFO`. They redact credential-shaped values and are not a substitute for
retaining the signed artifacts they reference.

## Documentation map

### Launch path

- [SN39 Intel TDX CPU mainnet release boundary](docs/SN39_MAINNET_RELEASE_20260724.md)
- [Validator operator guide](VALIDATOR.md)
- [Full-provenance verification](docs/PROVENANCE.md)
- [Score-class and contributor contract](docs/THIN_SCORE_CLASSES.md)
- [Thin-subnet design and threat model](docs/THIN_SUBNET_DESIGN.md)
- [Thin-subnet evidence record](docs/THIN_SUBNET_EVIDENCE.md)
- [Thin-subnet runbook](docs/THIN_SUBNET_RUNBOOK.md)
- [Confidential CPU publisher canary](docs/CONFIDENTIAL_CPU_PUBLISHER_CANARY.md)

### Experimental and reference mechanisms

The repository also preserves SAT, agent-policy, VerifyML, Violet, arena, and
V2 fast-path work. These are research or integration surfaces unless a current
tagged release explicitly promotes them. They are not evidence that an endpoint
is deployed or that a reward class is active.

- [Verified Agent Work](docs/VERIFIED_AGENT_WORK.md)
- [VerifyML](docs/VERIFYML.md)
- [Violet external scores](docs/VIOLET_EXTERNAL_SCORES.md)
- [Fast-path miner guide](docs/FAST_PATH_MINER_GUIDE.md)
- [Local arena](game/arena/ARENA.md)

## Security

- Never put a wallet seed, private key, bearer token, cloud credential, or
  controlled raw quote in an issue, log, config committed to Git, or public
  evidence bundle.
- Do not infer current eligibility from a past receipt, historical chain row,
  or local test.
- Verify live keys and release digests through two independent channels before
  enabling a wallet.
- Treat `PASS`, `FAIL`, and `NOT_PROVEN` as distinct outcomes. Missing evidence
  is not success.

## Licensing

This repository does not currently publish a license file. Do not assume
redistribution rights; contact the maintainers before using it outside the
permissions granted by applicable law.
