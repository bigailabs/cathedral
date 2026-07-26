# Full-provenance validation for SN39

> [!IMPORTANT]
> **Release-candidate status.** The public evidence surface is deployed, but
> endpoint availability alone is not `FULL` assurance. A public validator
> still needs the supported immutable release, independent key and digest
> pins, historical chain access, and—where raw replay is required—the
> controlled-disclosure package. Commands below are a verification contract,
> not permission to write weights.

Two concurrent modes ship in `cathedral-validator serve`:

| Mode | Submits | Trust basis |
|---|---|---|
| `shadow` (default) | Cathedral's signed vector (thin gates) | audits the published evidence every tick in a single-flight background worker; never delays or changes the thin submission |
| `authority` | the validator's OWN recomputation | requires **FULL assurance**: raw-evidence replay through the pinned verifier |

**Mainnet launch mode is `shadow`.** Thin validation remains the submission
authority while the independent provenance audit runs concurrently. Authority
mode refuses an epoch unless every historically anchored candidate is
independently replayable as `verified`; the launch artifact model does not
publish candidate-specific raw negative evidence, so any `rejected` or
`retired` row truthfully downgrades that epoch to `receipts_only`. Do not
describe such an epoch as FULL, and do not expect authority mode to submit it.

**Assurance levels.** `receipts_only` means Cathedral's signed registry →
receipt → report chain is internally consistent — PARTIAL provenance, always
reported `NOT_PROVEN`, never a submission basis. `full` additionally proves,
per positive miner: the controlled envelope's bytes hash to the public
manifest's `envelope_digest`; the reconstructed raw CPU-TDX evidence
reproduces the ledger `evidence_digest`; the verifier binary matches BOTH the
manifest's content blob digest and the operator-pinned implementation digest
recomputed from the declared command/artifacts and those exact bytes (static
x86-64 ELF enforced); and the canonical strict verification path re-verifies
the quote against the original nonce/worker/channel binding and the receipt's
measurement under the signed registry policy at receipt time.

**Chain-anchored freshness and candidates.** The TDX challenge nonce is not
issuer-random: it is derived as `sha256("cathedral-tdx-challenge-v2\0" ||
canonical{block, block_hash, network, netuid, source_epoch, miner_hotkey})`
— the normalized finalized HEIGHT is bound alongside the hash — from the
SN39 block durably anchored on the producing epoch, so the audit recomputes
the expected nonce itself and cross-epoch evidence reuse fails
cryptographically (no replay cache is a security dependency). A
receipts-only shadow audit is reported NOT_PROVEN: it never emits a
provenance PASS and never persists the durable reservation state.

Candidate membership is proven against HISTORY, never the present. The
signed score report binds the exact `cathedral_candidate_snapshot_v1` it
was built from (digest, block, hash, full sorted hotkey set), the manifest
`candidate_set` must equal that binding, and a FULL audit additionally
queries the validator's OWN chain connection for
`Subtensor.metagraph(netuid, block=candidate_set.block)` and
`get_block_hash(block)`, requiring EXACT set equality — not a subset — with
the manifest candidates and exact hash equality with the anchor. An omitted
historically registered hotkey or a fabricated extra candidate FAILS; an
unavailable or malformed historical lookup is NOT_PROVEN and can never back
authority. The per-tick current-metagraph snapshot supplies only the UID
map and the current block for the validity window; today's membership
proves nothing about the anchored epoch and is deliberately not an input
to candidate verification. Every historically registered hotkey must be
accounted for with an explicit report row (verified with evidence, or
zero/rejected) — omission is a manifest defect, not a scoring choice.

Operators capture snapshots with the one supported command:

```bash
cathedral-candidate-snapshot --network finney --netuid 39 \
  --block <finalized block> --output candidate-snapshot.json
```

## Operator pins (never self-authorized by the manifest)

Configure ALL of these independently — from the release notes and the
byte-pinned key bundle in `config/provenance/`, not from anything the evidence
surface serves. The current exact pins are recorded in
[`SN39_MAINNET_RELEASE_20260724.md`](SN39_MAINNET_RELEASE_20260724.md):

```toml
[provenance]
mode = "shadow"                     # or "authority"
registry_keys = "/etc/cathedral/pins/registry-keys.json"
registry_keys_digest = "sha256:<from the release notes>"
report_keys = "/etc/cathedral/pins/report-keys.json"
report_keys_digest = "sha256:<...>"
index_keys = "/etc/cathedral/pins/index-keys.json"
index_keys_digest = "sha256:<...>"
verifier_digest = "sha256:<pinned implementation digest>"
source_revision = "<pinned cathedralconfidential commit>"
mechanism = "validated_supply_v1"   # fixed 10% burn is part of this version
burn_hotkey = "<configured burn destination ss58>"
# FULL assurance additionally requires:
controlled_dir = "<controlled-disclosure package directory>"
verifier_binary = "<local pinned verifier binary>"
```

Key files map `key_id -> base64 32-byte Ed25519 public key`. The dependency
on `cathedralconfidential` is pinned to an immutable commit in
`pyproject.toml` (`[provenance]` extra); upgrade only through the reviewed
release process.

## Controlled disclosure

Raw TDX quotes are never public. Cathedral operators produce a package with
`cathedral provenance export-controlled` (0700 directory, 0600 files named by
envelope digest + `controlled-manifest.json`). An authorized validator points
`controlled_dir` at it; every byte is verified against the public manifest
digests before use, so no trust in the transport is required. Without the
package, shadow mode still audits the public receipts chain and logs
`NOT_PROVEN`; authority mode refuses to submit.

## Reproducing a decision from scratch

```bash
# From the exact reviewed Cathedral tag/commit:
python -m pip install -e '.[provenance]'

# Capture the anchored candidate set through YOUR OWN historical chain access.
cathedral-candidate-snapshot \
  --network finney \
  --netuid 39 \
  --block <anchored block> \
  --output independent-snapshot.json

cathedral provenance verify \
  --evidence-url https://api.cathedral.computer/v1/evidence \
  --network finney --netuid 39 \
  --registry-keys pins/registry-keys.json --registry-keys-digest sha256:... \
  --report-keys pins/report-keys.json --report-keys-digest sha256:... \
  --index-keys pins/index-keys.json --index-keys-digest sha256:... \
  --verifier-digest sha256:... --source-revision <commit> \
  --controlled-dir ./controlled \
  --independent-candidate-snapshot independent-snapshot.json \
  --production --current-block <current finalized block> \
  --state-file ./provenance-state.json \
  --publisher-url https://api.cathedral.computer \
  --weight-policy-public-key-hex <pinned hex> \
  --jsonl audit.jsonl --audit-out audit.json
```

Exit 0 = FULL assurance PASS and the recomputation matches the signed
vector. Watch the streams: `tail -f audit.jsonl | jq .` (stable JSONL) or
run on a TTY for the colored human view.
