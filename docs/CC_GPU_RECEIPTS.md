# Confidential GPU receipt verification

Status: software contract implemented. Live supported-hardware proof is NOT PROVEN.
Validators must not reward this class until the complete live GCP A3 High,
Intel TDX, NVIDIA H100 attestation, confidential job, deletion, receipt, and
validator-ingestion path has been observed.

## Selected launch profile

The first exact profile is `gcp-a3-high-h100-tdx-v1`:

- provider: `gcp`
- machine type: `a3-highgpu-1g`
- zone: `us-central1-a`
- CPU TEE: `intel_tdx`
- GPU: one `nvidia_h100_80gb`
- provisioning model: `spot` only; the selected launch backend creates one
  standalone VM and does not implement the separate flex-start MIG workflow

This choice follows Google's production
[Confidential Space workload](https://docs.cloud.google.com/confidential-computing/confidential-space/docs/deploy-workloads),
[attestation-token claims](https://docs.cloud.google.com/confidential-computing/confidential-space/docs/reference/token-claims),
and [Confidential VM with GPU](https://docs.cloud.google.com/confidential-computing/confidential-vm/docs/create-a-confidential-vm-instance-with-gpu)
documentation. The selected verifier consumes Google's composite Intel TDX and
NVIDIA claim set, plus the same guest's locally observed H100 ReadyState.
Documentation is necessary but is not launch evidence.

## Flat receipt wire contract

`cathedral_cc_gpu_job_receipt_v1` is canonical JSON. Unknown keys, duplicate
keys, floats, noncanonical encoding, invalid identifiers, unpinned policies,
inactive registries, inactive signing keys, and invalid Ed25519 signatures fail
closed. The receipt binds all of the following:

- worker, job, attempt, and miner hotkey;
- exact hardware profile and profile-registry authority;
- image, policy, input, model, result, and artifact-manifest digests;
- a domain-separated job-context digest;
- admission and completion bundle, Intel TDX, NVIDIA GPU, and GPU identity-set
  evidence digests;
- a distinct fresh nonce for admission and completion;
- the channel binding used for protected input and model delivery;
- the secret-release grant and provider-deletion evidence; and
- a completed outcome with confirmed deletion.

The content-derived receipt ID is
`cc-gpu-receipt-sha256:<64 lowercase hex>`. Its Ed25519 signature covers the
canonical document including that receipt ID and excluding only `signature`.

## Independent validator checks

`cathedral_thin.cc_gpu_receipts.verify_cc_gpu_receipt` requires the receipt
bytes, every referenced evidence blob, a locally pinned trust policy, and an
independent composite evidence verifier. The verifier receives the phase
bundle, raw CPU and GPU evidence, GPU identity-set evidence, secret-release
grant, and deletion evidence. It verifies both admission and completion. For
each phase, it must report:

- an allowed verifier digest;
- a digest of the verified Intel TDX claim set;
- a digest of the verified NVIDIA H100 claim set; and
- the nonce digest extracted independently from both CPU and GPU evidence.

It must also independently bind the evidence to the exact job context, miner
hotkey, channel, and GPU identity set. It must affirm that CPU and GPU evidence
describes the same guest, GPU confidential-computing mode and ReadyState are
valid, measurement policy and runtime isolation pass, the secret-release grant
has valid semantics and signature, and the deletion record has valid semantics
and a Cathedral control-plane signature over a fresh provider-API absence
observation. That signature authenticates Cathedral's observation; it is not a
Google-signed deletion attestation. Hash-matching grant or deletion bytes are
insufficient without those independent checks.

Both extracted nonce digests must equal the phase nonce committed by the
receipt. Admission and completion must use the same GPU identity set, different
nonces, and six distinct bundle, CPU, and GPU evidence objects. Receipt age,
registry validity, and signing-key validity are evaluated at `issued_at`.

Batch verification applies global replay protection across all miners. Receipt
IDs, worker IDs, job IDs, attempt IDs, admission and completion evidence,
admission and completion nonces, secret-release grants, and deletion evidence
must be unique. A report producer cannot make duplicated work unique by changing
the credited hotkey. Claims carry a conservative expiry beyond the receipt's
maximum age, future skew, and a safety margin. Expired claims are pruned before
the active-ledger capacity check, while a durable wall-clock watermark makes a
clock rollback fail closed.

Before any positive vector is submitted, the validator atomically persists the
admitted receipt, worker, job, attempt, and evidence digests alongside its
pending vector. The append-only bounded ledger is checked across score classes,
rounds, retries, and process restarts. A pending submission retry reuses the
already bound decision rather than loading the report again. Schema-4 validator
state is migrated to schema 6 with an empty CC GPU ledger because schema 4 could
not admit this receipt class. Schema-5 list claims migrate with a fresh full
retention window before they can expire. Existing challenge state, checkpoints,
EMA values, and pending-vector metadata are preserved.

## Validator score integration

`cc_gpu_score_report_body` derives `verified_cc_gpu_jobs` from validator-verified
receipt objects. It emits one `cathedral_cc_gpu_job_receipt_v1` evidence
reference per unique job, bound to the canonical receipt digest and subject
hotkey.

When a score class requires this evidence kind, `ValidatorRunner` calls its
configured `cc_gpu_receipt_loader`. The loader must retrieve the referenced raw
receipt and evidence bytes, run the independent verification above, and return
the verified objects keyed by receipt ID. Without that loader, or when an ID,
digest, subject hotkey, evidence set, metric count, or global uniqueness check
does not match, assignment fails closed and no new vector is submitted.

No default policy enables this score class. Enabling it is an operator decision
after live proof is PASS. Local fixtures and simulated evidence demonstrate
software behavior only and cannot change the launch status from NOT PROVEN.

## Evidence export transport

Polaris exposes raw evidence only through the authenticated, owner-scoped
`GET /v1/receipts/{receipt_uuid}/evidence` route. The response schema is
`cathedral_cc_gpu_evidence_export_v1` with exact top-level fields:

- `receipt_id` and the unchanged flat signed receipt object;
- `artifacts`, keyed by canonical SHA-256 digest;
- canonical padded base64 bytes, decoded byte length, and sorted semantic kinds
  for each artifact;
- `authentication: {"owner_scoped": true}`; and
- an integrity declaration for Ed25519 receipt signatures and SHA-256 artifact
  digests.

`CcGpuReceiptLoader` supports two explicit transports. An HTTPS evidence URI
must use a validator-pinned origin and a bearer token supplied from an operator
environment variable. Redirects, credentials in URLs, query strings, fragments,
non-JSON responses, oversized bodies, and unpinned origins fail closed. For an
offline acceptance run, the operator may instead place an export in the pinned
local directory as `<receipt-id-hex>.evidence.json` or pass its path directly to
the acceptance command.

The loader does not trust the export's labels. It canonicalizes the embedded
receipt, verifies its content digest and Ed25519 signature, requires the exact
digest-to-kind mapping, canonical-decodes every bounded artifact, recomputes
every SHA-256 digest, and invokes the pinned independent verifier before
returning a `VerifiedCcGpuReceipt`. The external verifier must be a static,
native, 64-bit ELF with no interpreter or dynamic segment. The validator hashes
an already-open descriptor, executes that exact descriptor through
`/proc/self/fd`, supplies a minimal environment, invokes no shell, and kills the
verifier process group on timeout. Scripts and dynamic runtime closures are
rejected.

One report or offline acceptance batch may reference at most 128 receipts and
256 MiB of exports. A configured 1..600 second monotonic batch deadline is shared
by all authenticated fetches and both verifier phases, so per-request timeouts
cannot multiply into an unbounded validator tick.

The validator enables this loader only when started with
`--cc-gpu-loader-config <canonical-json>`. A score policy that requires
`cathedral_cc_gpu_job_receipt_v1` but omits that configuration stops at startup.
The default 100% local SAT policy does not load or enable CC GPU rewards.

An operator can verify a saved Polaris export without broadcasting weights:

```bash
cathedral-thin-cc-gpu-accept \
  --loader-config /etc/cathedral/cc-gpu-loader.json \
  --export ./polaris-cc-gpu-evidence.json
```

The command returns `PASS` only for software verification and always reports
the launch status as `NOT PROVEN`. Repeating the same export in one acceptance
bundle, changing any artifact bytes, changing semantic kinds, or mismatching a
score evidence ID or digest is rejected.
