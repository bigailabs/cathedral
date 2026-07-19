# Validator-Owned Score Classes

Status: implemented, exercised locally, and independently reviewed by Fable;
Cathedral Confidential producer rollout and a testnet broadcast remain gates.

## The boundary

An external subsystem can contribute a **score class**. It cannot call
`set_weights`, hold a validator wallet, submit an opaque replacement vector, or
choose another validator's allocation. Every validator independently pins
source keys, selects class budgets and assignment rules, maps current hotkeys to
UIDs, writes a provenance record, and signs its own Subtensor transaction.

```text
Cathedral Confidential ---- signed facts + receipt digests ---+
other subnet component ---- signed facts + evidence ----------+--> validator
local SAT challenge -------- locally verified observations ---+       |
                                                                       +-- pin keys
                                                                       +-- reject stale/replay/equivocation
                                                                       +-- assign each class locally
                                                                       +-- coldkey-collapse each class
                                                                       +-- compose fixed budgets
                                                                       +-- record full decision
                                                                       +-- set_weights with validator wallet
```

The default policy remains 100% validator-local SAT. A validator may configure
any mix, including 100% external classes, without running an owner scoring API
or querying miners itself.

## Source-subnet owner registration

A validator can admit the owner of another subnet without centrally issuing an
API credential. In `owner_registration` mode, the validator locally chooses the
source netuid, source ID, permitted class, allocation, metric, cap, transform,
required reasons/evidence, registration mirrors, exact report mirrors, and
freshness limits. The source owner then:

1. registers a delegate hotkey under its owner coldkey on the target subnet;
2. signs `cathedral_owner_score_registration_v1` with the source subnet owner
   coldkey;
3. delegates bounded class IDs and one to eight Ed25519 report keys, while
   binding the exact credential-free HTTPS report locations already approved
   in each validator's policy; and
4. publishes the small canonical registration artifact from any static HTTPS
   origin or mirror.

Every target validator independently checks the current source subnet owner,
the owner/delegate pair in the current target metagraph, the owner signature,
network and both netuids, class/source binding, time and block windows, and the
monotonic registration chain. The registration's report locations must exactly
match that validator's local policy, so an owner cannot turn validator fetches
into arbitrary HTTPS requests. Only then does the validator use the delegated
report key for that round. The delegate never receives a validator wallet and
never calls `set_weights`; it supplies signed facts to each validator's
deterministic assignment rule.

The owner creates the artifact after its delegate registration is finalized:

```bash
cathedral-thin-contributor \
  --network test \
  --source-netuid <SOURCE_NETUID> \
  --target-netuid <TARGET_NETUID> \
  --wallet-name source-owner \
  --wallet-hotkey cathedral-contributor \
  --source-id source_subnet_<SOURCE_NETUID> \
  --class-id confidential_compute \
  --report-key score-key-1=<ED25519_PUBLIC_KEY_BASE64> \
  --report-location https://source.example/score-class-latest.json \
  --valid-seconds 86400 \
  --valid-blocks 7200 \
  --output owner-registration.json
```

This command performs read-only chain preflight and writes a signed artifact;
it submits neither a registration extrinsic nor weights. The delegate hotkey
must first be registered through the standard Bittensor flow. Validators opt
in with
[`config/thin-score-policy.registered-owner.example.json`](../config/thin-score-policy.registered-owner.example.json).

For rotation, increment `--sequence`, pass the prior registration ID via
`--previous-registration-id`, and use `--replace-latest` only for an explicit
mutable latest path. Rollback, a different artifact at the accepted sequence,
or a broken contiguous link holds the update. If Subtensor reports an ownership
transfer, the old signature immediately fails; the new owner may begin a fresh
sequence. Deregistering or moving the delegate to another coldkey also holds
the update. Validators can revoke the source independently by removing the
class or registration stanza from their local policy.

## Signed report contract

`cathedral_score_class_report_v1` is canonical JSON signed with a locally
pinned Ed25519 key. It binds:

- network, netuid, class ID, source ID, source epoch, and completeness;
- generated/expiry times and an exclusive Subtensor block window;
- the exact source policy and verifier digests;
- a previous-report ID for contiguous hash-chain checks;
- one sorted entry per observed miner hotkey;
- canonical decimal metrics, an optional asserted score, sorted reason codes,
  and bounded evidence references;
- signing key ID, content-derived report ID, and domain-separated signature.

Each evidence reference records a typed ID, digest, and optional retrieval URI.
For Cathedral Confidential, use `cathedral_assurance_receipt_v2` references and
the exact policy-registry and verifier digests that authorized those receipts.
Do not include raw quotes, credentials, customer data, or stable hardware
identifiers.

Reports are bounded to 1 MiB, 4,096 miner entries, 32 metrics and 32 evidence
references per entry. Duplicate JSON keys, floats, noncanonical JSON, unknown
fields, duplicate hotkeys, invalid decimals, bad signatures, untrusted keys,
wrong network/netuid/class/source, stale or future reports, expired validity,
and over-wide block windows fail closed.

## Validator-local assignment

The local canonical policy supports two explicit modes:

- `metric`: select a named source-signed metric, apply an optional cap and a
  `linear` or `binary` transform, then normalize. This is the preferred mode;
  Cathedral Confidential should expose `verified_work_units`, not a final
  weight.
- `asserted_score`: use a source-asserted scalar with the same cap/transform.
  This is a compatibility trust mode. Provenance is still retained, but the
  validator is trusting the source's scoring judgment rather than assigning
  from a fact.

Each assignment can additionally require specific reason codes and evidence
kinds for every positive entry. A Cathedral Confidential validator can, for
example, require both `receipt_verified` and `work_verified` plus a
`cathedral_assurance_receipt_v2` reference. A source cannot earn credit with a
different explanation merely because it supplied a positive number.

Each class is coldkey-collapsed and normalized independently before its local
allocation is applied:

```text
class_weight(hotkey) = coldkey_collapse(assign(report_entry))
final(hotkey) = sum(allocation[class] * class_weight[class][hotkey])
```

Allocations are canonical decimal strings and must sum exactly to `1`. A
configured class that is absent, invalid, stale, empty, or unable to satisfy
chain constraints causes the validator to retain its previous on-chain vector.
Its budget is never silently donated to a surviving class.

## Replay and source compromise

Validators persist a high-water checkpoint per external class. A lower epoch
is rollback, a different report at the accepted epoch is equivocation, and the
next contiguous epoch must name the accepted report ID. Multiple configured
locations are untrusted mirrors: the validator verifies every available
artifact, selects the highest valid epoch, and rejects same-epoch disagreement.
Freshness and short block windows limit an old but correctly signed report.

Key rotation is validator-controlled. Pin old and replacement public keys
during a bounded overlap, move producers to the replacement key, then remove
the old key in a reviewed policy change. A source cannot introduce its own new
trust root. If a score key is compromised, validators remove it, stop accepting
its class, and hold prior weights until a trusted report is available.

For an owner-registered class, the on-chain owner is the trust root and may
rotate delegated report keys through its signed registration chain. The
validator still controls whether that source subnet is admitted and exactly
what its class can affect. Directly pinned classes retain the stricter local-key
rotation model above.

## Decision provenance

For every evaluated round the validator writes an immutable
`cathedral_weight_decision_v1` record under its local decision directory. It
contains:

- validator, network, netuid, round, block, and local policy digest;
- every class allocation, source epoch/report ID, assignment rule, raw score,
  normalized class weight, metric, reason code, and evidence reference;
- the exact metagraph UID/hotkey/coldkey snapshot;
- the final processed on-chain UID vector;
- a domain-separated SHA-256 decision digest.

The pending vector commits to that decision digest. A retry therefore cannot
silently substitute different provenance for the same UID/weight operation.
For owner-registered classes it additionally binds the exact registration IDs;
the validator rechecks current ownership, delegate registration, artifact
validity, and those IDs before every submission attempt. Transfer,
deregistration, or rotation cancels the stale pending vector. The record
contains no validator master secret or wallet key and is mode `0600` by
default.

## Cathedral Confidential integration

The current Cathedral Confidential runtime already has durable assurance
receipts, signed policy registries, verified work units, and an immutable epoch
ledger. Its older complete-score publication is not accepted as a strong class
report: it is normalized before publication and its HMAC authenticates the
ingest transport, not independent validators.

The producer integration should emit one unsigned class body from the frozen
epoch transaction, with:

- `class_id=confidential_compute` and
  `source_id=cathedralconfidential`;
- `metrics.verified_work_units` derived from verified receipt rows;
- reason codes for verified, failed, stale, revoked, or ineligible work;
- the receipt ID and exact stored receipt-body digest for every credited row;
- the signed policy-registry and verifier digests;
- explicit zero entries so stale credit is revoked.

Then sign the immutable body:

```bash
cathedral-thin-score-report sign \
  --key-file /run/secrets/confidential-score.seed \
  --body /var/lib/cathedral/score-class-body.json \
  --output /var/lib/cathedral/score-class-latest.json \
  --replace-latest
```

Without `--replace-latest`, the utility refuses to overwrite a different
artifact and is appropriate for epoch-addressed archives. The explicit flag
atomically updates a mutable latest pointer; validators still reject rollback
or equivocation using their persistent source checkpoint.

Producers may call `cathedral_thin.score_classes.sign_report` directly instead
of spawning the CLI. Validators consume the signed artifact from a local
sidecar path or one of up to eight HTTPS mirrors. No central Cathedral subnet
service is required.

## Scalability and residual trust

This layer adds one small signed artifact fetch per configured external class
per scoring round, not one request per miner. Artifacts can be static files,
served by the subsystem that already generates them, cached, or mirrored by
untrusted parties because validators verify the bytes. With no local SAT class,
the validator makes zero miner RPCs. There is no owner database, queue, object
store, scorer fleet, or proxy in the weight-setting path.

Full provenance is not the same as zero trust. In `metric` mode the validator
chooses the assignment but still trusts the pinned source to report the metric
that corresponds to the referenced evidence. Receipt IDs and digests make that
claim auditable; validators that require independent Cathedral receipt
verification must run the published receipt verifier against the referenced
registry before pinning or accepting that source. The thin core deliberately
does not duplicate Cathedral's TDX/DCAP and receipt-verification stack.

Base-layer limits remain: a validator-stake majority can collude, separate
coldkeys cannot be linked, a compromised accepted source key can lie within its
class until validators remove it, and Subtensor constraints can make a chosen
class policy unrepresentable. These are explicit governance and monitoring
risks, not reasons to hand weight authority to a central service.
