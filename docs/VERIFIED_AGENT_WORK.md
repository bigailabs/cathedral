# Cathedral Verified Agent Work

Status: implemented local protocol and weight-composition proof; no SN39 chain
write and no production TDX quote in this evidence set.

## Product in one sentence

Miners turn examples of an agent decision into a compact, inspectable policy;
validators replay it on a committed hidden suite, score normal and rare cases
separately, and can attach Cathedral execution provenance before deciding how
much of their own weight budget the work deserves.

This is a deliberately narrow first market for "verified agent work." It is
useful for tool routing, fixed-action agents, policy and compliance decisions,
and other structured choices. It is not intended to judge open-ended chat.

## Research inspiration, not an implementation claim

Grzegorz Góra's RIONA research combines instance-based and rule-based learning,
and his later work shows how the resulting local classifier can be represented
with human-interpretable rules. RIONIDA extends that research toward imbalanced
data. Cathedral does **not** implement or claim equivalence to RIONA or
RIONIDA. The engineering lesson used here is narrower:

- bind a decision to inspectable examples and rules;
- evaluate rare classes explicitly instead of hiding them in aggregate
  accuracy;
- expose the basis for a score so each validator can reproduce it.

Primary sources:

- [RIONA explainability paper](https://annals-csis.org/Volume_35/drp/4139.html)
- [University of Warsaw dissertation](https://www.mimuw.edu.pl/media/uploads/doctorates/thesis-grzegorz-gora.pdf)
- [RIONIDA paper](https://doi.org/10.1016/j.ins.2025.122015)

## Thin architecture

1. A validator creates an individualized task for one miner. The signed task
   binds the network, netuid, block window, nonce, public examples, metric
   policy, and a salted commitment to hidden cases. The hidden bytes also bind
   the task nonce, and the commitment covers the full task identity.
2. The reference miner greedily generalizes pure rules from the public
   examples. Another miner implementation may use any method, but the result
   must be the same bounded decision-list format.
3. The miner signs the artifact. Its identifier commits to the exact task,
   ordered rules, cited support examples, default decision, miner, and time.
4. The validator verifies the task and artifact signatures, opens its committed
   hidden suite, and deterministically replays the policy locally.
5. The validator signs an evaluation containing independent measurements:
   balanced accuracy, rare-class recall, cited-example faithfulness,
   quality-gated compactness, artifact signature status, and attested execution
   status.
6. The evaluation becomes a normal Cathedral score-class report. The source
   supplies measurements and evidence, never final weights. Each validator
   independently chooses class allocations, metrics, caps, trusted keys,
   source registrations, coldkey collapse, and the final on-chain vector.

The owner runs no API, database, queue, benchmark farm, or inference service.
Task and evidence files can be exchanged directly or mirrored as immutable
objects. Validators perform verification and scoring locally. Cost grows with
the work a validator elects to verify, not with an owner-hosted control plane.

## What the provenance proves

| Claim | Evidence | Remaining assumption |
| --- | --- | --- |
| The validator issued this exact task | sr25519 task signature and task id | The validator selected a fair suite |
| This registered miner signed the artifact | sr25519 artifact signature | The miner may have copied the content |
| Hidden cases were not changed after issuance | Salted suite commitment in the task | The suite may have leaked before reveal |
| The published metrics are reproducible | Hidden reveal, artifact, deterministic replay, signed evaluation | Independent validators must fetch the same artifacts |
| Cited examples support a rule | Validator checks every cited example against the rule and label | Support examples do not prove causal reasoning |
| Rare behavior is preserved | Separate macro recall over validator-declared rare labels | The declared rare cases must represent production risk |
| The artifact was the committed execution output | Verified inference receipt output commitment | An unattested receipt proves signing and binding, not measured hardware |
| Measured execution matched an allowed TDX policy | Independently verified quote and pinned verifier, when present | Attestation does not prove semantic correctness |

## Validator-owned classes

The example policy assigns four independent budgets:

- `policy_fidelity`: 40%, using balanced hidden-suite accuracy;
- `rare_case_retention`: 30%, using recall on declared rare labels after both
  signed quality floors pass;
- `evidence_faithfulness`: 20%, using mechanically checked support examples
  after both signed quality floors pass;
- `policy_compactness`: 10%, awarded only after fidelity and rare-case floors.

These are examples, not subnet-owner mandates. A validator may use different
allocations, reject a source, require attested execution, or create classes for
different agent-work families. The final class composition still goes through
the existing registration, evidence, checkpoint, coldkey-collapse, UID
alignment, and `set_weights` path.

A testnet subnet owner can contribute without receiving authority over a
validator. The owner signs a registration that names its delegate hotkey,
classes, report locations, and report keys. The receiving validator verifies
current source-subnet ownership and target registration, then locally chooses
whether and how much budget to allocate. If the source later graduates to
production, validators can change the class allocation without changing the
artifact protocol or ceding their signing keys.

## Threat model

| Strategy | Current defense | Residual risk |
| --- | --- | --- |
| Cross-miner replay | Per-miner task id, nonce, hotkey, block window, and both signatures | A copier can copy rules and sign a fresh artifact for its own task |
| Copying or collusion | Individual hidden suites, short windows, immutable provenance, coldkey collapse | Digital knowledge cannot be proven independently invented; leaked suites remain dangerous |
| Hidden-suite manipulation | Pre-committed salted suite and public reveal for replay | A validator can still choose a biased suite, so other validators should use independent suites |
| Majority-class gaming | Balanced accuracy and a separate rare-recall class | Poorly chosen labels or too few rare cases can still mislead |
| Rare-default or tiny-policy gaming | Rare recall, evidence faithfulness, and compactness all become zero below either signed quality floor | A policy can overfit a leaked suite |
| Fake explanations | Every cited example must match the rule and decision | Faithful examples do not guarantee a complete explanation |
| Sybil splitting | Existing coldkey collapse before final weights | Independent coldkeys are not proven independent people |
| Source reward capture | Sources report facts; validators own metrics, caps, classes, and allocations | Validators can coordinate or copy each other's policy choices |
| Score/report tampering | Canonical JSON, domain-separated ids and signatures, time and block bounds, checkpoints | Key compromise still requires rotation and validator policy updates |
| Weight manipulation | Fail-closed report verification, current metagraph remap, UID uniqueness, normalization, persisted decision digest | Bittensor consensus and validator key custody remain external assumptions |
| TDX marketing overreach | Attestation and semantic score are separate fields and classes | A permitted measurement can still run a bad model or bad policy |

There is no "uncheatable" claim. The strongest remaining problem is copied
content: the protocol can show who signed what and how it performed, but it
cannot prove the signer originated a digital rule set.

## Run the complete local proof

From the repository root:

```bash
python -m cathedral_thin.policy_cli demo
```

The command exercises task signing, reference mining, artifact signing,
hidden-suite replay, rare-case scoring, score-report signing, four validator
classes, weight composition, and construction of a UID-aligned on-chain vector.
It prints `chain_write_submitted: false` and `owner_hosted_services: 0`.
The checked-in [proof artifact](evidence/verified-policy-demo-2026-07-19.json)
is deterministic and can be regenerated byte for byte with the same command.
The [independent Fable review](VERIFIED_POLICY_FABLE_REVIEW.md) records the
initial requested changes, remediation, adversarial follow-up, and `ACCEPT`
verdict.

Run the focused tests:

```bash
PYTHONPATH=. python -m pytest -q tests/thin/test_verified_policy.py
```

Run the whole thin-subnet suite before release:

```bash
PYTHONPATH=. python -m pytest -q tests/thin
ruff check cathedral_thin tests/thin
```

## Operate the file protocol

Create a task and keep the hidden suite private until evaluation:

```bash
cathedral-verified-policy issue \
  --network finney --netuid 39 --current-block BLOCK \
  --source-epoch EPOCH --miner-hotkey MINER_SS58 \
  --wallet-name VALIDATOR_WALLET --wallet-hotkey VALIDATOR_HOTKEY \
  --spec config/verified-policy-task.example.json \
  --task-output task.json --hidden-output hidden-suite.json
```

On the miner:

```bash
cathedral-verified-policy mine \
  --network finney --netuid 39 --current-block BLOCK \
  --wallet-name MINER_WALLET --wallet-hotkey MINER_HOTKEY \
  --task task.json --output artifact.json
```

Back on the validator:

```bash
cathedral-verified-policy evaluate \
  --network finney --netuid 39 --current-block BLOCK \
  --wallet-name VALIDATOR_WALLET --wallet-hotkey VALIDATOR_HOTKEY \
  --task task.json --artifact artifact.json \
  --hidden-suite hidden-suite.json --output evaluation.json
```

`config/thin-score-policy.verified-policy.example.json` shows how the four
measurements become validator-owned classes. Replace the placeholder report
key and locations. For a source subnet owner, use the existing
`cathedral-thin-contributor` registration flow instead of hard-coding trust.

## Cathedral Confidential binding

The policy artifact is ordinary canonical bytes. A generic Cathedral
Confidential workload can emit those exact bytes; `cathedral-verifyml` commits
them as the receipt output. The validator first verifies the receipt and any
TDX evidence under its pinned policy, then passes the verified receipt into
`evaluate_policy`. The evaluator rejects a receipt whose network, netuid,
miner, or output commitment does not match the artifact.

The execution class remains zero unless attestation was independently verified.
Signed but unattested receipts are retained as provenance and are never
silently promoted to hardware-backed execution.

## Production gates

Before assigning SN39 emissions to this work:

1. Replace the toy task pack with a useful, versioned agent-policy dataset and
   independent hidden-suite generation.
2. Run enough miners to measure class balance, leakage, latency, and score
   stability across repeated epochs.
3. Exercise a real TDX quote and pinned verifier with the exact artifact output.
4. Register a permitted SN39 validator and perform dry-run UID mapping at the
   intended block.
5. Register at least one real source subnet owner and verify its report through
   the full owner-delegation path.
6. Obtain independent review of the incentive design and adversarial cases.
7. Only then enable a small, reversible class allocation and monitor the first
   weight epochs. Do not infer readiness from a zero-weight mainnet validator.
