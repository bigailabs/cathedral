# Cathedral

Cathedral turns verified work into a fail-closed weight decision for Bittensor
SN39.

This repository is the current authoritative source for the SN39 validator and
the publisher that signs its weight feed. It is also the system map: the
mechanism repositories below produce evidence, and this repository decides what
that evidence is worth.

## Start here

| Goal | Go to |
|---|---|
| Provide Intel TDX CPU compute | [cathedral-compute](https://github.com/cathedralai/cathedral-compute) |
| Compete in the Distill (CyberGym) track | [cathedral-distill](https://github.com/cathedralai/cathedral-distill) |
| Run or audit a validator | [Validator guide](VALIDATOR.md) in this repository |
| Use confidential compute as a customer | [Cathedral Computer](https://cathedral.computer/docs/) |
| Read the extraction experiment | [cathedral-validator](https://github.com/cathedralai/cathedral-validator), derived, not authoritative |
| Test one command surface for all roles | [cathedral-cli](https://github.com/cathedralai/cathedral-cli), early beta |

Two routing notes that are easy to get wrong:

- **`cathedral-validator` is a derived extraction, not the deployment target.**
  It proves the validator's boundary is separable. The running SN39 validator is
  built from this repository, the reproduction lock and release manifests pin
  paths here, and no cutover has happened. Use `VALIDATOR.md` in this repository
  to operate a validator.
- **`cathedral-cli` is early beta.** It is one interface for testing and issue
  reporting. It has not replaced any repository's operator guide, and chain
  writes and rewards stay off by default in it. Use it to test, then
  [report problems](https://github.com/cathedralai/cathedral-cli/issues).

## How the loop fits together

1. Compute and Distill define what admissible work and evidence are.
2. Miners perform work and submit evidence for a specific mechanism.
3. The publisher verifies evidence and signs a weight vector.
4. The validator independently verifies signature, scope, policy, freshness,
   rollback state, and the burn contract, resolves every hotkey against a fresh
   metagraph, and either produces a UID-aligned decision or refuses the input.

The validator wallet is the only authority for a `set_weights` transaction. A
mechanism, feed, receipt, or CLI can publish evidence; none of them can sign
with the validator's wallet or bypass validator-local policy.

Positive weight requires work admitted by the active validator policy. In the
current signed-vector path, the publisher derives and signs the proposed
allocation, while the validator verifies the feed contract and decides whether
to authorize it. The shadow provenance audit is observational and does not veto
that path. Full-provenance authority mode independently recomputes from the
controlled evidence package.

Registration, uptime, hardware ownership, a valid attestation, or self-reported
volume are never sufficient on their own. A vector with zero positive miners is
a valid fail-closed outcome; in that case eligible mass routes to the configured
burn destination rather than preserving stale credit.

## What is available now

| Capability | Status |
|---|---|
| Signed SN39 weight-vector feed | Deployed |
| Validator: signed-feed verification, thin mode with concurrent shadow audit | Implemented here |
| Validator: independent full-provenance recomputation | Implemented; requires all operator pins and the controlled evidence package |
| Intel TDX CPU verified-supply input | Current confidential-compute path |
| Distill (CyberGym) scored-to-weights bridge | Merged, default off; the emission-weight registration is an owner step |
| Confidential GPU subnet admission | Not admitted for positive weight |
| Self-service mainnet validator launch | Pending a tagged release and launch notice |

Mechanism source, a passing local test, a receipt, or a historical chain row
does not prove a lane is active, admitted, or earning. Check the relevant
repository and the current release before operating it.

The deployed feed and the independent verifier have not been shown to agree on
one contract shape. The dated comparison recorded `FAIL` on the v1 shape
(2026-07-25, recorded in
[cathedral-compute's evidence record](https://github.com/cathedralai/cathedral-compute/blob/main/BUILD_STATUS.md)),
and the live payload still mixes v1 and v2 `contract_version` metadata blocks as
of 2026-08-01. Treat convergence as unproven.

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

Run one no-chain verification tick with a synthetic UID map. It fetches the
signed vector and shadow evidence over HTTPS, opens no chain connection, and
cannot broadcast:

```bash
cathedral-validator serve \
  --config my-validator.toml \
  --runtime-root "$HOME/.cathedral" \
  --offline \
  --once
```

Then run a metagraph-backed preview that still cannot write weights:

```bash
cathedral-validator serve \
  --config my-validator.toml \
  --runtime-root "$HOME/.cathedral" \
  --dry-run \
  --once
```

Stop there until the tagged release, the public launch notice, and your own
preflight are all green. `serve` is non-writing by default: only an explicit
`--broadcast` permits a chain-write attempt, and the signed release and
transition gates must still authorize it. Do not run a chain-writing command
from a mutable `main` checkout.

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

### Operating and auditing

- [Validator operator guide](VALIDATOR.md)
- [Full-provenance verification](docs/PROVENANCE.md)
- [SN39 Intel TDX CPU mainnet release boundary](docs/SN39_MAINNET_RELEASE_20260724.md)
- [Score-class and contributor contract](docs/THIN_SCORE_CLASSES.md)
- [Thin-subnet design and threat model](docs/THIN_SUBNET_DESIGN.md)
- [Thin-subnet evidence record](docs/THIN_SUBNET_EVIDENCE.md)
- [Thin-subnet runbook](docs/THIN_SUBNET_RUNBOOK.md)
- [Confidential CPU publisher canary](docs/CONFIDENTIAL_CPU_PUBLISHER_CANARY.md)

### Experimental and reference mechanisms

The repository also preserves SAT, agent-policy, VerifyML, Violet, arena, and V2
fast-path work. These are research or integration surfaces unless a current
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
- Do not infer current eligibility from a past receipt, historical chain row, or
  local test.
- Verify live keys and release digests through two independent channels before
  enabling a wallet.
- Treat `PASS`, `FAIL`, and `NOT_PROVEN` as distinct outcomes. Missing evidence
  is not success.

## Licensing

This repository does not currently publish a license file. Do not assume
redistribution rights; contact the maintainers before using it outside the
permissions granted by applicable law.
