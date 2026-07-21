# Cathedral VerifyML

Status: receipt protocol, independent verification, bundle aggregation, and
score-class handoff are implemented and covered by local tests. A real
open-model remote run is recorded in the evidence ledger. A genuine TDX quote
and an admitted mainnet validator remain live-evidence gates.

## The focused utility

**Cathedral is the verification layer for AI inference.**

A user or validator can answer a narrow question: did the exact model and
runtime I selected produce the output I received? The answer is a portable,
content-addressed receipt that any validator can check without asking
Cathedral's subnet owner for permission.

SAT remains a bounded hidden integrity canary. It is not the product claim and
does not receive useful-work credit merely for existing.

## What a receipt proves

`cathedral_ml_inference_receipt_v1` binds:

- Bittensor network, netuid, source epoch, validator hotkey, miner hotkey,
  block window, and a fresh validator-issued 32-byte nonce;
- exact model-weight, tokenizer, runtime-image, and runner digests;
- privacy-preserving commitments to the input, generation parameters, and
  output;
- input/output token counts, observed latency, and source-reported work units;
- an optional content-addressed TDX quote and validator-pinned attestation
  policy; and
- a validator-hotkey signature over the exact pre-inference request, plus a
  content-derived receipt ID and miner-hotkey sr25519 execution signature.

The TDX `report_data` is reconstructed from two independent 32-byte bindings:

```text
identity half  = SHA256(network, netuid, request, nonce, validator, miner, time, blocks)
execution half = SHA256(request, model, runtime, input, parameters, output, work facts)
```

A miner signature proves authorship, not execution. An unattested receipt is
reported as signed provenance and cannot produce `verified_work_units`. Only a
validator-local verifier whose executable, model, image, runner, and
attestation-policy digests are separately allowlisted can set
`attestation_verified=true`. Production policy also pins the exact validator
hotkey that authorized the request.

## Decentralized scoring flow

```text
customer request
      |
      v
miner-hosted model ---> output + miner-signed receipt + TDX evidence
      |                                      |
      +------------------ static HTTPS/IPFS -+
                                             |
              +------------------------------+---------------------------+
              |                              |                           |
         validator A                    validator B                 validator C
         pins models                    pins models                 pins models
         verifies quotes                verifies quotes             verifies quotes
         chooses classes                chooses classes             chooses classes
         assigns metrics                assigns metrics             assigns metrics
         signs weights                  signs weights               signs weights
```

Receipts are sorted into `cathedral_ml_inference_bundle_v1`, a maximum
16 MiB/4,096-receipt content-addressed epoch artifact. Structural bundle
failure, rollback, same-epoch equivocation, broken epoch chains, stale time
windows, and invalid block windows fail closed. Receipt-level failures are
isolated and reported, so one disallowed model, missing quote, or malformed
receipt cannot erase another miner's valid work. Each validator therefore gets
a deterministic admitted subset under its own allowlists. Bundles can be
cached or mirrored by untrusted static hosts because validators verify their
bytes.

An O(1) validator checkpoint stores only network, netuid, accepted epoch,
bundle ID, and generation time. Every validator request authorization names one
source epoch, and a bundle rejects receipts authorized for any other epoch. A
receipt must also complete after the prior generation boundary and no later
than its containing bundle. The epoch binding prevents a miner from re-running
one authorized request for credit in later epochs; the temporal partition
prevents the same signed execution from crossing an epoch boundary. Neither
requires an ever-growing request database.

After verification, the tool emits a standard Cathedral score-class body with
facts such as `verified_work_units`, `verified_requests`, and token counts. It
does not emit weights. The existing score-class path lets every validator
independently select:

- whether to admit the source or source-subnet owner;
- the class allocation;
- the metric, cap, linear/binary transform, reason requirements, and evidence
  requirements;
- coldkey-level Sybil collapse and normalization; and
- whether to sign and submit the final vector.

The bundle ID, receipt reasons, policy digest, verifier digest, per-miner facts,
metagraph mapping, and final vector are retained in validator decision
provenance.

## Smallest production topology

Cathedral operates no inference fleet, request router, receipt database, score
API, queue, or model store in the weight path.

- Miners host models and publish small signed receipts/evidence.
- Customers, existing provider systems, or other subnet owners publish epoch
  bundles from infrastructure they already operate.
- Static HTTPS, IPFS, mirrors, or a CDN distribute immutable bytes.
- Validators run the verifier and score assignment locally.
- Subtensor remains the only weight-coordination layer.

Per epoch, a validator downloads one bounded bundle plus the attestation
evidence it chooses to verify. Model inference does not run on the validator.
The owner does not scale with miners, requests, validators, or model size.

## Operator flow

Install the thin package, then inspect the CLI:

```bash
python -m pip install -e ".[test]"
cathedral-verifyml --help
```

### 1. Plumbing test with a real local model runner

The runner command receives the input on stdin. `{input_path}` and
`{model_path}` placeholders are also available. This mode intentionally issues
an unattested receipt and prints `creditable_as_verified_work=false`.

```bash
cathedral-verifyml run-local \
  --network finney --netuid 39 \
  --source-epoch <EPOCH> \
  --wallet-name miner --wallet-hotkey default \
  --validator-hotkey <VALIDATOR_HOTKEY> \
  --valid-from-block <FIRST> --valid-until-block <EXCLUSIVE_LAST> \
  --model-id HuggingFaceTB/SmolLM2-135M-Instruct \
  --weights-file /models/smollm2.gguf \
  --image-digest sha256:<PINNED_CONTAINER_DIGEST> \
  --runner-file /usr/local/bin/llama-cli \
  --input-file prompt.txt --parameters-file parameters.json \
  --output-file output.txt --receipt receipt.json \
  -- /usr/local/bin/llama-cli -m '{model_path}' -f '{input_path}' -n 64

cathedral-verifyml verify \
  --network finney --netuid 39 --current-block <BLOCK> \
  --receipt receipt.json --allow-unattested \
  --input-reveal prompt.txt --parameters-reveal parameters.json \
  --output-reveal output.txt
```

### 2. Production TDX receipt

The validator first authorizes the exact miner, prompt, parameters, model,
runtime, nonce, and block window. The command prints the nonce, issue time, and
validator hotkey that the miner must use unchanged:

```bash
cathedral-verifyml authorize \
  --network finney --netuid 39 \
  --source-epoch <EPOCH> \
  --wallet-name validator --wallet-hotkey default \
  --miner-hotkey <MINER_HOTKEY> \
  --valid-from-block <FIRST> --valid-until-block <EXCLUSIVE_LAST> \
  --model-id <MODEL_ID> --weights-digest sha256:<WEIGHTS> \
  --image-digest sha256:<IMAGE> --runner-digest sha256:<RUNNER> \
  --input-file prompt.bin --parameters-file parameters.json \
  --output request-authorization.json
```

The attested runner then generates a genuine quote whose report data is the
64-byte value specified by the receipt contract. The miner issues the signed
artifact using the values printed above:

```bash
cathedral-verifyml issue \
  --network finney --netuid 39 \
  --source-epoch <SAME_EPOCH> \
  --wallet-name miner --wallet-hotkey default \
  --validator-hotkey <VALIDATOR_HOTKEY> \
  --nonce-base64 <AUTHORIZED_NONCE> --issued-at <AUTHORIZED_ISSUE_TIME> \
  --valid-from-block <FIRST> --valid-until-block <EXCLUSIVE_LAST> \
  --model-id <MODEL_ID> --weights-digest sha256:<WEIGHTS> \
  --image-digest sha256:<IMAGE> --runner-digest sha256:<RUNNER> \
  --input-file prompt.bin --parameters-file parameters.json \
  --output-file output.bin --input-tokens 24 --output-tokens 64 \
  --latency-ms 842.331 --work-units 1 \
  --attestation-evidence quote.bin \
  --attestation-evidence-uri https://miner.example/receipts/<ID>/quote.bin \
  --attestation-policy-digest sha256:<POLICY> \
  --request-authorization request-authorization.json \
  --receipt receipt.json
```

Verification fails unless the quote bytes, expected report data, attestation
policy, model/runtime allowlists, and locally hashed verifier executable all
match:

```bash
cathedral-verifyml verify \
  --network finney --netuid 39 --current-block <BLOCK> \
  --expected-validator-hotkey <VALIDATOR_HOTKEY> \
  --receipt receipt.json --attestation-evidence quote.bin \
  --allow-model-digest sha256:<WEIGHTS> \
  --allow-image-digest sha256:<IMAGE> \
  --allow-runner-digest sha256:<RUNNER> \
  --allow-attestation-policy-digest sha256:<POLICY> \
  --allow-verifier-digest sha256:<VERIFIER_EXECUTABLE> \
  --attestation-verifier-digest sha256:<VERIFIER_EXECUTABLE> \
  --attestation-verifier-command \
    '/usr/local/bin/verify-tdx {evidence_path} {report_data_hex} {policy_digest} {result_path}'
```

The verifier is invoked without a shell. Its digest is checked before use and
its result must bind the exact report data and policy digest.

### 3. Bundle and derive score facts

```bash
cathedral-verifyml bundle \
  --network finney --netuid 39 --source-epoch 1 \
  --valid-from-block <FIRST> --valid-until-block <EXCLUSIVE_LAST> \
  --valid-until 2026-07-19T13:00:00.000000Z \
  --receipt receipt-a.json --receipt receipt-b.json \
  --output inference-bundle.json

cathedral-verifyml score-body \
  --network finney --netuid 39 --current-block <BLOCK> \
  --bundle inference-bundle.json \
  --checkpoint state/verifyml-bundle-checkpoint.json \
  --evidence sha256:<QUOTE_A>=quote-a.bin \
  --evidence sha256:<QUOTE_B>=quote-b.bin \
  --source-id independent_receipt_verifier \
  --signing-key-id verifier-key-1 \
  --score-policy-digest sha256:<RECEIPT_ADMISSION_POLICY> \
  --score-verifier-digest sha256:<VERIFIER_EXECUTABLE> \
  --expected-validator-hotkey <VALIDATOR_HOTKEY> \
  --allow-model-digest sha256:<WEIGHTS> \
  --allow-image-digest sha256:<IMAGE> \
  --allow-runner-digest sha256:<RUNNER> \
  --allow-attestation-policy-digest sha256:<POLICY> \
  --allow-verifier-digest sha256:<VERIFIER_EXECUTABLE> \
  --attestation-verifier-digest sha256:<VERIFIER_EXECUTABLE> \
  --attestation-verifier-command \
    '/usr/local/bin/verify-tdx {evidence_path} {report_data_hex} {policy_digest} {result_path}' \
  --output score-body.json

cathedral-thin-score-report sign \
  --key-file /run/secrets/validator-receipt-score.seed \
  --body score-body.json --output verified-inference-report.json
```

`verify-bundle` and `score-body` require the same validator-local checkpoint
path. The first accepted bundle creates it atomically; every later bundle must
use the next epoch and the previous bundle ID. Back up this tiny file with the
rest of the validator state. A per-checkpoint advisory file lock serializes
concurrent local verification/scoring commands through read, verification,
output, and checkpoint persistence. The CLI reports admitted and rejected receipt
counts and reasons. It never converts a rejected receipt into a zero for an
otherwise valid miner, and it refuses to score a bundle with no admitted
receipt.

Point a validator-local score policy at the signed report. The example
[`thin-score-policy.verifyml.example.json`](../config/thin-score-policy.verifyml.example.json)
uses an illustrative 90% verified-inference / 10% integrity-canary split. It is
not a production allocation recommendation; each validator must choose and
publish its own class policy.

## Threat model

| Threat | Defense | Residual risk |
|---|---|---|
| Miner claims a different model | Model weights, tokenizer, runner, image, request, and output are bound into TDX report data and the signed receipt | Trust depends on the locally pinned quote verifier and measurement policy |
| Replay | Validator-issued nonce, signed request ID and source epoch, validator/miner identity, short time/block windows, bundle chain, and an O(1) completion-time checkpoint | Loss of validator checkpoints requires explicit recovery from the last accepted bundle |
| Self-generated trivial demand | Production credit requires the validator hotkey to sign the exact pre-inference request, and policy pins that authorizing hotkey | A validator can intentionally authorize low-value work; quality and demand policy remain its responsibility |
| Copy another output/receipt | The validator-signed request is miner-targeted; the miner signature and TDX identity binding name the same miner | A miner may outsource a live request while preserving its own attested endpoint |
| Inflate tokens or work units | Counts and work facts are inside the execution binding; validator caps and hidden evaluation classes remain local | A malicious allowed runner image can mismeasure unless its code/digest is reviewed |
| Substitute mutable model tags | Only SHA-256 weight/runtime digests are admitted for credit | The process that computes the model digest must cover every effective shard and adapter |
| Receipt aggregator censors miners | Validators may pin different mirrors/aggregators and reproduce the bundle from public receipts | Availability and discoverability are not solved by signatures alone |
| Bundle replay/equivocation | Epoch, previous bundle ID, time/block windows, persisted checkpoints, and cross-epoch completion boundaries | Different validators can temporarily see different latest epochs; bundle mirror comparison is an operator/source concern |
| Invalid receipt griefing | Validators isolate receipt-level failures, expose deterministic rejection reasons, and score the admitted subset | A structurally invalid or entirely inadmissible bundle still fails as a unit |
| Score source assigns favored weights | Source emits metrics only; validators choose metric, cap, transform, class budget, coldkey collapse, and final vector | Validators may voluntarily choose weak policies or asserted-score mode |
| Sybil miners | Each class is collapsed by current coldkey before normalization | Separately funded coldkeys remain unlinkable |
| Validator manipulation/collusion | Independent verification, class assignment, decision records, and on-chain signatures | A validator-stake majority can still capture Yuma consensus |
| Customer privacy leak | Receipt contains commitments, not raw input/output; reveals are optional and local | Length, token counts, model identity, timing, and access patterns remain metadata |
| Compromised verifier | Validator pins and re-hashes the verifier executable and pins its attestation-policy digest | Write access to the validator host leaves a narrow hash-to-exec race; widespread adoption of one flawed verifier creates correlated failure |
| TEE compromise | Short-lived evidence, measurement allowlists, verifier rotation, and separate quality/integrity classes limit scope | Hardware/vendor attestation is an explicit trust assumption, not mathematical proof |

## Non-claims and remaining gates

- A receipt does not prove output quality; hidden evaluator classes must measure
  quality separately.
- TDX proves an allowed measured execution, not that the model is useful,
  unbiased, licensed, or safe.
- Signed but unattested receipts are not production useful-work evidence.
- An independently verified real TDX quote has not yet been recorded for this
  schema.
- The current SN39 validator hotkey is not registered/permitted, so it cannot
  provide live mainnet scoring or set weights today.
- No weight broadcast, registration, TAO spend, or merge is implied by the
  receipt tooling.
