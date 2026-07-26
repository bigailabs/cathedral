# Run a Cathedral SN39 validator

Cathedral's validator turns a signed, evidence-backed compute report into a
UID-aligned Bittensor weight decision. It supports two concurrent paths:

- a **thin path** that verifies Cathedral's signed vector; and
- an **independent provenance audit** that re-checks the public artifacts and,
  when configured with controlled evidence, can recompute the vector itself.

> [!CAUTION]
> `cathedral-validator serve` is **non-writing by default** in the launch
> candidate. Only an explicit `--broadcast` permits a chain-write attempt, and
> Finney SN39 still requires the signed release and transition gates. Until
> Cathedral publishes the supported tag, immutable pin bundle, and launch
> notice, use only `--offline --once` and `--dry-run --once`.

## Status

| Component | Current state |
|---|---|
| Signed vector, JWKS, and public evidence index | Deployed |
| Validator thin-path checks | Implemented |
| Concurrent shadow provenance audit | Implemented; default mode |
| Full-provenance authority mode | Implemented; requires `FULL` evidence and every independent pin |
| Current deployed vector vs independent verifier | `FAIL`: public v1/GPU-allocation contract does not match the v2/fixed-burn/body-binding verifier |
| General validator launch | Pending tagged release and final acceptance |

The current public contract mismatch is a launch blocker. Shadow mode reports
it but does not veto an otherwise valid thin vector, which is why operators
must remain in non-writing preview modes until the supported release
converges.

## What happens on each tick

```text
signed vector ── verify signature, scope, policy, expiry, rollback
      │
      ├── thin path ── map public hotkeys to fresh metagraph UIDs
      │
      └── provenance audit ── verify artifacts and recompute when possible
                                      │
                                      v
                            PASS / FAIL / NOT_PROVEN
      │
      v
local weight decision ── dry-run preview or authorized set_weights
```

The validator checks:

- Ed25519 signature and pinned `key_id`;
- `network = finney` and `netuid = 39`;
- the required reward-policy version;
- expiry and a durable monotonic rollback fence;
- burn contract and complete finite, non-negative weights;
- current hotkey-to-UID mapping immediately before a write; and
- the configured provenance mode's acceptance requirements.

A failed gate belonging to the active submission authority fails closed.
Default shadow provenance is observational: its `FAIL` or `NOT_PROVEN` result
does not block a thin submission whose own signed-vector gates pass.
Registration, uptime, attestation, and self-reported work never create positive
weight by themselves.

## Provenance modes

| Mode | Submission authority | Behavior |
|---|---|---|
| `shadow` | Thin path | Default. Independently audits evidence in the background without delaying the thin tick. |
| `authority` | Full-provenance recomputation | Refuses unless the epoch reaches `FULL` assurance and the recomputed vector passes every gate. |
| `off` | Thin path | Disables the independent audit. This is an explicit reduction in assurance. |

`receipts_only` is reported as `NOT_PROVEN`; it is not accepted as `FULL`.
Authority mode requires controlled raw evidence, a pinned static verifier,
independently pinned keys and digests, an immutable source revision, and an
independently queried historical candidate set. Read
[the full provenance contract](docs/PROVENANCE.md) before selecting it.

## Prerequisites

For the default thin + shadow mode:

- Linux or macOS with Python 3.11 or newer;
- a stable network connection;
- for metagraph-backed checks, a Bittensor-compatible Finney RPC endpoint; and
- for any future write, a registered SN39 validator hotkey with the required
  chain permissions.

Thin mode does not need TDX or GPU hardware. Full-provenance authority
additionally needs a Linux x86-64 host for the pinned static TDX verifier and
access to the controlled-disclosure package for the epoch being replayed.

Never place wallet seeds or private keys in TOML, environment files committed
to Git, logs, issues, or provenance bundles.

## Install a reviewed build

Do not install a moving branch for chain use. When a supported release is
published, verify the tag, commit, package digest, and release notes before
installation.

For source review and non-writing testing only:

```bash
git clone https://github.com/cathedralai/cathedral.git
cd cathedral

python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[provenance]'

cp config/validator.toml my-validator.toml
```

The project distribution is currently built from this repository; there is no
published `cathedralsubnet` PyPI package. A copied sample config is suitable
for review and previews only. Public launch operators must use the immutable
release configuration and installation named by the launch notice.

## Configure

Edit your copy, not the repository sample:

```toml
[network]
name = "finney"
netuid = 39
wallet_name = "<your-wallet-name>"
validator_hotkey = "<your-validator-hotkey-name>"

[publisher]
url = "https://api.cathedral.computer"

[weight_policy]
key_id = "cathedral-weight-policy"
public_key_hex = "<independently verified key from the supported release>"
require_policy = "validated_supply_v1"

[provenance]
mode = "shadow"
mechanism = "validated_supply_v1"
```

Create a private user-owned runtime directory, then set the state and log paths
through environment variables:

```bash
install -d -m 700 "$HOME/.cathedral"
export CATHEDRAL_VALIDATOR_STATE="$HOME/.cathedral/validator-state.json"
export CATHEDRAL_VALIDATOR_JSONL="$HOME/.cathedral/validator-events.jsonl"
```

Do not accept the key merely because it appears in the same checkout or
payload you are verifying. Compare the supported release's pin with the live
[JWKS](https://api.cathedral.computer/.well-known/cathedral-jwks.json) through
an independent channel.

For authority mode, add every pin documented in
[`docs/PROVENANCE.md`](docs/PROVENANCE.md). A missing pin is not inferred from
the manifest or public evidence server.

### Optional RPC endpoint

```bash
export CATHEDRAL_CHAIN_ENDPOINT="wss://your-finney-node.example:443"
```

The endpoint must serve the same Finney chain and historical state required by
the evidence anchor. Changing the RPC connection does not change the signed
`network` label.

## Non-writing acceptance

Run these in order.

### 1. Synthetic-map tick

This fetches the signed vector and shadow evidence over HTTPS, uses a synthetic
UID map, opens no chain connection, and cannot broadcast:

```bash
cathedral-validator serve \
  --config my-validator.toml \
  --runtime-root "$HOME/.cathedral" \
  --offline \
  --once
```

Confirm:

- signature and policy checks pass;
- the vector is fresh and scoped to Finney SN39;
- burn and weights are finite and normalized;
- the provenance result is clearly `PASS`, `FAIL`, or `NOT_PROVEN`; and
- no wallet or chain client is initialized, and no broadcast is attempted.

### 2. Metagraph-backed dry run

This resolves hotkeys and computes the exact UID vector without writing:

```bash
cathedral-validator serve \
  --config my-validator.toml \
  --runtime-root "$HOME/.cathedral" \
  --dry-run \
  --once
```

Confirm the current metagraph, burn destination, candidate mapping, normalized
weight sum, rollback state, and explicit “no chain writes” banner.

If there are no eligible miners, a burn-only outcome is expected fail-closed
behavior—not a reason to preserve an old positive vector.

## Observe the validator

TTY output is designed for a human operator. JSONL is the stable integration
surface:

```bash
tail -f "$CATHEDRAL_VALIDATOR_JSONL" | jq .
```

Useful lifecycle stages include `FEED`, `SIGNATURE`, `FRESHNESS`, `ROLLBACK`,
`PROVENANCE`, `VECTOR`, `PREFLIGHT`, `MAP`, `WEIGHTS`, and `CHAIN`. Records
include the active mode and a `PASS`, `FAIL`, `NOT_PROVEN`, or `INFO` status.
Credential-shaped values are redacted, but logs should still be protected as
operational data.

The lane state file stores rollback fences, provenance reservations, and
durable pending/finalized attempt records. The release-managed runtime root
also holds the cross-mode lock and ambiguity journal used to prevent a retry
after an uncertain submission. Keep both on durable owner-only storage; never
delete, roll back, or replace them to clear a refused attempt. Follow the
release runbook for recovery.

## Chain-writing launch gate

Do not add `--broadcast` until all of the following are true:

- [ ] Cathedral has published a supported immutable tag and launch notice.
- [ ] You verified the source/package digest and all signing-key pins.
- [ ] Synthetic-map and metagraph-backed dry runs passed on your machine.
- [ ] The current vector, evidence index, and provenance outcome match your
      intended assurance level; the known public contract mismatch is resolved.
- [ ] Your validator hotkey, permit, wallet isolation, RPC, and rollback-state
      backup are confirmed.
- [ ] You have explicit operator authorization for a mainnet transaction.
- [ ] You understand that only `--broadcast` permits a chain attempt, and the
      SN39 release and authorization state can still refuse it.

Only an authorized operator should then start the continuous service:

```bash
# MAINNET WRITE: calls set_weights when all gates pass.
cathedral-validator serve \
  --config <immutable-release-config> \
  --broadcast
```

Stop the service on any unexpected key, policy, candidate, provenance, burn,
rollback, or chain result. Do not “fix” a failed gate by disabling it during a
live launch.

## Updating or rotating keys

Treat a release or signing-key change as a new trust decision:

1. stop the chain-writing service;
2. fetch the new immutable release and pin bundle;
3. verify the change through an independent channel;
4. install into a new environment;
5. preserve and migrate the rollback state deliberately;
6. repeat synthetic-map and metagraph-backed dry runs; and
7. resume only after explicit operator authorization.

Never accept a replacement key from a weight payload signed only by the old or
new key itself.

## Further reading

- [SN39 Intel TDX CPU mainnet release boundary](docs/SN39_MAINNET_RELEASE_20260724.md)
- [Full-provenance verification](docs/PROVENANCE.md)
- [Score-class contract](docs/THIN_SCORE_CLASSES.md)
- [Threat model](docs/THIN_SUBNET_DESIGN.md)
- [Evidence record](docs/THIN_SUBNET_EVIDENCE.md)
- [Operator runbook for the experimental owner-independent path](docs/THIN_SUBNET_RUNBOOK.md)
