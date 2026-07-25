# Cathedral Thin Subnet Runbook

This is the direct Bittensor path. The subnet owner does not operate an API,
database, object store, queue, scorer, or solver farm.

## 1. Prove it locally

Requirements: Python 3.11+ and a Linux host for production Axon operation.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[test]"
cathedral-thin-e2e --pretty
python -m pytest -q tests/thin
```

## Cathedral Confidential score classes

Cathedral Confidential can now export a frozen epoch directly as a
`cathedral_score_class_report_v1`. Positive `verified_work_units` are accepted
only when the ledger contains the exact atomically stored
`cathedral_assurance_receipt_v2`; zero rows remain in the complete snapshot to
revoke prior work. The report gives the validator the receipt ID, digest and
optional HTTPS location, while the validator retains the class policy, wallet,
decision record and final `set_weights` call.

The producer freezes the first exported bytes per epoch/target class and
automatically chains the next source epoch to the last durable report ID. This
keeps upload retries and mirror repair from looking like source equivocation.
An out-of-grammar work-unit value zeros only its own entry with an explicit
reason rather than invalidating the whole class.

From the Cathedral Confidential checkout, run the local cross-repository proof:

```console
PYTHONPATH="$PWD:/path/to/cathedralsubnet-production-ready" \
  /path/to/python scripts/thin_subnet_e2e.py \
  --validator-repo /path/to/cathedralsubnet-production-ready \
  --pretty
```

The proof supplies external report bytes through the validator's normal strict
parser and Ed25519 verification path. It also records receipt provenance in the
weight decision and proves an altered metric is rejected. See
`cathedralconfidential/docs/THIN_SUBNET_INTEGRATION.md` for the producer command
and remaining assumptions.

Acceptance is `"ok": true`, all thin-subnet tests passing, zero owner-hosted
services, positive honest weights, rejected copy/replay/identity attacks, and a
pending vector that survives restart and confirms on retry. The E2E also signs
a Cathedral Confidential-shaped report, assigns its class from verified work
units, composes fixed 60/40 budgets, writes the decision record, and binds that
record into the retried vector.

The focused inference-receipt flow is documented in
[`VERIFYML.md`](VERIFYML.md). `cathedral-verifyml run-local` exercises the
model/receipt plumbing without pretending that a local run is attested;
production `verified_work_units` require a genuine quote and a
validator-pinned verifier.

## 2. Create wallets and register

Use the current official `btcli` release. Test locally or on Bittensor testnet
before mainnet. Subnet creation and registration are chain writes and may burn
or lock TAO; inspect the current cost before confirming them.

```bash
btcli wallet new-coldkey --wallet-name owner
btcli wallet new-hotkey --wallet-name owner --hotkey default
btcli wallet new-coldkey --wallet-name validator
btcli wallet new-hotkey --wallet-name validator --hotkey default
btcli wallet new-coldkey --wallet-name miner
btcli wallet new-hotkey --wallet-name miner --hotkey default

btcli subnet create --network test
btcli subnet register --network test --netuid <NETUID> --wallet-name validator --hotkey default
btcli subnet register --network test --netuid <NETUID> --wallet-name miner --hotkey default
```

Confirm registration with the read-only preflight before starting services:

```bash
cathedral-thin-preflight --network test --netuid <NETUID> --role validator \
  --wallet-name validator --wallet-hotkey default
cathedral-thin-preflight --network test --netuid <NETUID> --role miner \
  --wallet-name miner --wallet-hotkey default
```

New subnets are inactive until the owner starts them. Configure and validate
the network first, then use the current official start command:

```bash
btcli subnet start --network test --netuid <NETUID>
```

Do not copy placeholder mainnet commands into an funded wallet without checking
the current official Bittensor documentation and interactive transaction.

## 3. Run a miner

Install a SAT solver such as Kissat on the miner host. The command must emit
standard DIMACS `s SATISFIABLE` and `v ... 0` lines. The runtime never sends the
validator's hidden generator seed or planted assignment to the miner.
Keep the host clock synchronized with NTP or chrony. A miner more than 30
seconds behind a validator rejects its challenges as future-dated; a clock far
ahead can treat valid challenges as expired.

```bash
export BT_NETWORK=test
export BT_NETUID=<NETUID>
export BT_WALLET_NAME=miner
export BT_WALLET_HOTKEY=default
export BT_AXON_PORT=8091
export BT_EXTERNAL_IP=<PUBLIC_IPV4>
export CATHEDRAL_SOLVER_COMMAND='kissat {cnf}'
export CATHEDRAL_VALIDATOR_RPM=4

cathedral-thin-miner
```

After the Axon is published, require serving status in the read-only check:

```bash
cathedral-thin-preflight --network test --netuid <NETUID> --role miner \
  --wallet-name miner --wallet-hotkey default --require-serving
```

Open only the configured Axon port. The miner rejects callers that do not have
a current validator permit, wrong-target challenges, expired envelopes,
oversized CNFs, digest mismatches, and decompression bombs. The default solver
concurrency is one so validators cannot accidentally oversubscribe the host.
Each permitted validator is rate-limited, may submit only one distinct CNF
digest per declared round, and receives a cached response when it retries the
same formula with a refreshed envelope. These are miner protection limits, not
scoring inputs.

The example systemd unit is
[`deploy/thin/cathedral-thin-miner.service`](../deploy/thin/cathedral-thin-miner.service).
Copy
[`deploy/thin/miner.env.example`](../deploy/thin/miner.env.example) to
`/etc/cathedral-thin/miner.env`, replace every placeholder, and restrict the
file to the service account. `BT_WALLET_PATH` must point to the existing wallet
directory because the hardened unit gives the service a private writable home.

## 4. Run a validator safely

The default is dry-run. It queries miners and prints the vector but will not
write weights or advance the persistent scoring round. A long-running dry-run
queries a given chain round only once per process so it does not waste miner
capacity.

```bash
export BT_NETWORK=test
export BT_NETUID=<NETUID>
export BT_WALLET_NAME=validator
export BT_WALLET_HOTKEY=default
export CATHEDRAL_THIN_STATE=/var/lib/cathedral-thin/validator.json

cathedral-thin-validator --once
```

Check that:

- the validator hotkey is registered and permitted;
- every expected miner Axon is reachable;
- verified count and rejection reasons are plausible;
- UID weights are finite, non-negative, and sum to one;
- challenge dimensions fit the Axon body limit and the round completes well
  inside the configured timeout.

Then explicitly enable chain writes:

```bash
cathedral-thin-validator --broadcast
```

### Optional score classes

The default is 100% local SAT. To let Cathedral Confidential or another
component contribute a class, copy the canonical example and replace its
network, netuid, locations, allocation, and test-only public key:

```bash
cp config/thin-score-policy.example.json /etc/cathedral-thin/score-policy.json
export CATHEDRAL_THIN_SCORE_POLICY=/etc/cathedral-thin/score-policy.json
export CATHEDRAL_THIN_DECISION_DIR=/var/lib/cathedral-thin/decisions
cathedral-thin-validator --once
```

The policy file itself must be canonical JSON. Class allocations must sum
exactly to one. Inspect the dry-run decision record and confirm:

- each source report ID, epoch, key, policy digest, and verifier digest is the
  expected one;
- the selected metric and transform express this validator's decision;
- every positive external entry has the expected receipt/evidence IDs;
- every positive entry satisfies the validator's required reason codes and
  evidence kinds;
- each class retained its configured budget after coldkey collapse;
- the final UID/hotkey mapping matches the current metagraph.

If a configured class is invalid, stale, empty, or cannot satisfy current chain
limits, the command fails closed and retains the prior on-chain vector. Do not
work around this by temporarily deleting a class unless the validator operator
is intentionally changing policy and accepts the new config fingerprint.

External producers create a 32-byte Ed25519 seed under their own secret
management, publish only the derived public key, freeze a canonical unsigned
report from their epoch transaction, and sign it with
`cathedral-thin-score-report`. The report contains facts, reason codes, and
evidence references; it never contains a wallet key or chain transaction.
Cathedral Confidential must export `verified_work_units` and exact assurance
receipt IDs/digests rather than wrap its older normalized HMAC score stream.
The complete schema, key rotation, source compromise, and residual-trust rules
are in [`THIN_SCORE_CLASSES.md`](THIN_SCORE_CLASSES.md).

### Admit a testnet subnet owner

Use owner registration when another subnet owner should contribute one or more
classes without receiving our validator wallet. The source owner first creates
a dedicated delegate hotkey under the source-owner coldkey and registers that
hotkey on the target subnet using the standard Bittensor registration flow:

```bash
btcli wallet new-hotkey --wallet-name source-owner --hotkey cathedral-contributor
btcli subnet register --network test --netuid <TARGET_NETUID> \
  --wallet-name source-owner --hotkey cathedral-contributor
```

After finalization, the owner generates a report-signing key, publishes its
public key, and creates the signed delegation artifact:

```bash
cathedral-thin-score-report public-key \
  --key-file /run/secrets/source-score.seed

cathedral-thin-contributor \
  --network test \
  --source-netuid <SOURCE_NETUID> \
  --target-netuid <TARGET_NETUID> \
  --wallet-name source-owner \
  --wallet-hotkey cathedral-contributor \
  --source-id source_subnet_<SOURCE_NETUID> \
  --class-id confidential_compute \
  --report-key score-key-1=<PUBLIC_KEY_BASE64> \
  --report-location https://source.example/score-class-latest.json \
  --valid-seconds 86400 \
  --valid-blocks 7200 \
  --output owner-registration.json
```

The contributor command only reads chain state and writes a signed file. It
does not register the hotkey, submit weights, or contact a Cathedral owner
service. Publish the artifact at the URL locally admitted by each validator.
Each validator copies
[`config/thin-score-policy.registered-owner.example.json`](../config/thin-score-policy.registered-owner.example.json),
sets the source and target netuids, source ID, registration URL, allocation,
exact report URL, assignment, and evidence requirements, and accepts the config
change in dry-run before enabling broadcast. The signed registration must name
that exact locally approved report URL; contributor-selected URLs are rejected.

The registration output does not grant a final vector. Every validator checks
the current source owner and target delegate registration, then assigns the
class from the signed report using its own policy. A validator may reject the
source, use a different allocation, or admit different classes. This is the
decentralization boundary: common provenance, independent judgment, and
independent on-chain signatures.

The example systemd unit is
[`deploy/thin/cathedral-thin-validator.service`](../deploy/thin/cathedral-thin-validator.service).
Copy
[`deploy/thin/validator.env.example`](../deploy/thin/validator.env.example) to
`/etc/cathedral-thin/validator.env`, replace every placeholder, and use mode
`0600`. On Linux, run `systemd-analyze verify` on both units before enabling
them. Keep the validator in dry-run outside systemd until its output is sane;
the example validator unit intentionally includes `--broadcast` for the final
operator-approved service.

## 5. Commit-reveal and subnet parameters

Inspect current hyperparameters before launch:

```bash
btcli subnet hyperparameters --network test --netuid <NETUID>
```

The tagged SN39 Intel TDX CPU release requires
`commit_reveal_weights_enabled = false`. Its public proof names one finalized
`set_mechanism_weights` extrinsic and proves the applied vector at that block;
a finalized commitment is not equivalent evidence. The validator reads this
hyperparameter at the canonical finalized head and fails closed when it is
enabled. Submission calls the explicit `set_mechanism_weights` extrinsic path,
not the SDK entry point that can auto-route to commit-reveal after preflight.

Thin and FULL-authority services must also share one absolute owner-only
`runtime_root` (mode `0700`, `/var/lib/cathedral-validator` in the launch
config). That directory contains the cross-mode single-flight lock and common
pending-attempt journal. It is deliberately independent of `HOME` and the
lane-specific state files: an ambiguous call in either mode blocks both modes
until an operator reconciles the named extrinsic.

Commit-reveal may be enabled only after a later release persists and publicly
verifies the complete commitment, reveal round, applied-vector block, and
their bindings. At that point, keep neuron immunity longer than the full reveal
interval so a new miner is not pruned before its weights can be revealed.

Set owner hyperparameters only with the current official `btcli sudo set`
workflow. These are chain writes; review them interactively rather than using
an unreviewed copied command.

## 6. Capacity

The default challenge is 384 variables and 1,635 clauses. One round is one
bounded RPC and one linear witness verification per registered UID. With 256
UIDs, the default concurrency of 32 produces at most 32 simultaneous outbound
validator requests. There is no owner data plane in the scaling path.

Before increasing dimensions or subnet size:

1. Measure compressed request bytes and round wall time.
2. Keep miner concurrency explicit and bounded.
3. Keep validator round time below the scoring interval.
4. Increase validator concurrency only after checking file descriptors,
   bandwidth, and Dendrite timeouts.
5. Treat a solver timeout as score zero, never as proof of cheating.

## 7. Recovery and rollback

The validator state file contains the private challenge master secret, EMA
scores, last completed round, external source high-water checkpoints, and
pending/confirmed weight and decision digests. Back it up with mode `0600`;
never publish it or copy it to miners. Decision records contain public scoring
provenance rather than secrets, but default to `0600` and should be retained for
the claimed audit window.

- **Process restart:** use the same state file. Pending state is preserved;
  definitive failures retry unchanged and ambiguous outcomes remain held.
- **Explicit chain rejection:** the identical pending vector retries with
  bounded backoff.
- **Ambiguous chain response/exception:** do not delete state. The validator
  holds the vector because a commit-reveal extrinsic is not assumed to be
  idempotent. Inspect the chain/commit state, then pass `--retry-ambiguous` only
  when an explicit retry is the intended recovery action; remove the flag
  afterward.
- **Corrupt state:** the validator fails closed. Restore the last good backup.
  Do not silently create a new file while the old validator is live.
- **Intentional scoring/config change:** stop the service, back up state, deploy
  the reviewed release, resolve any pending vector, and pass
  `--accept-config-change` once. Acceptance resets EMA, last round, and the
  confirmed digest while preserving the private master secret. Remove the flag
  after the new fingerprint is stored.
- **UID reassigned during retry:** the validator cancels the pending vector
  before broadcast. It never transfers the stale weight to the replacement
  hotkey; the previous on-chain vector remains until a later scoring round.
- **Owner/delegate/registration changes during retry:** the validator cancels
  the pending vector before broadcast. Registration IDs are digest-bound and
  current ownership plus the delegate coldkey pair are rechecked on every
  attempt. A transient artifact or chain-read failure holds without submitting.
- **Release rollback:** stop the service, restore both the prior code and the
  matching state backup, run dry-run `--once`, then restore `--broadcast`.
- **No positive scores:** no new vector is submitted; the prior on-chain vector
  remains active.
- **External rollback/equivocation:** do not delete the checkpoint. Inspect the
  signed artifacts and source key; a lower epoch, same-epoch different report,
  or broken contiguous link is intentionally rejected.
- **External class outage:** restore a valid report inside its time/block
  window. The validator will not redistribute the missing class budget.
- **Owner registration rejected:** verify the current source owner coldkey,
  delegate hotkey/coldkey pair on the target metagraph, registration time/block
  window, sequence, prior registration ID, and exact validator-approved HTTPS
  report locations. Do not delete either high-water checkpoint to force
  acceptance.
- **Source ownership transfer or delegate deregistration:** expect the class to
  hold. The new owner must register its own delegate and publish a newly signed
  artifact; the old owner cannot extend the chain after transfer.
- **Score-policy change:** resolve a pending vector first, review key/allocation
  changes, pass `--accept-config-change` once, and preserve external high-water
  checkpoints across the change.

## 8. Monitoring

Alert on:

- zero verified miners for two rounds;
- a sudden reason shift to `transport_status`, `miner_error`, or
  `axon_identity_mismatch`;
- repeated `miner_error` responses containing `challenge issue time is in the
  future` or `challenge expired`, which usually indicate host clock skew;
- repeated `validator permit refresh held previous snapshot` miner logs, which
  indicate that the permit snapshot is becoming stale during an RPC outage;
- a pending vector whose retry count keeps increasing;
- round time approaching the configured timeout or interval;
- state-file backup failure;
- Bittensor SDK major-version drift;
- commit-reveal or weight-rate-limit changes.
- stale external class reports, mirror disagreement, source epoch rollback, or
  decision-record write failure;
- owner-registration expiry, mirror equivocation, owner transfer, or delegate
  deregistration;
- loss of referenced receipts or policy registries before the audit-retention
  window ends.

No central application health endpoint exists by design. Each operator monitors
its own miner or validator process and the public Subtensor state.
