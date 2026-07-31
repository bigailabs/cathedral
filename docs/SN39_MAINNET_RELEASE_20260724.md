# SN39 Intel TDX CPU mainnet release

The only authorized public claim, and only after the tagged launch submission
gate below is **PASS**, is:

> **SN39 mainnet: validated Intel TDX CPU compute.**

This release deliberately makes no GPU, general confidential-compute, or
whole-epoch FULL-provenance claim.

## Current launch boundary

| Gate | Status | Evidence |
|---|---|---|
| Prior thin mainnet proof | **PASS** | Pre-release validator `98b862bfe40c4918e1e1ace09a55de11270af9cf` submitted UID 163 / burn UID 204 at the logical 90/10 target in extrinsic `0x4ef1307460f6bcdf3acc17dc7a1070f0918cf1080d74fb9409897353fe6cb371`; an independent chain read returned wire weights `[65535, 7282]`. This proves the mechanism lineage, not the final tagged release. |
| Tagged launch submission | **NOT_PROVEN** until the release gate runs | The root-signed `release.json` must name the final tag commit, exact signed vector, historical metagraph snapshot, inclusion block/extrinsic, historical on-chain weights, and frozen evidence checkpoint. The public assertion rejects a missing or mutable substitute. |
| Intel TDX positive-work replay | **PASS in the pre-release validator; tagged finalizer NOT_PROVEN** | The pre-release validator replayed the admitted worker. The final tagged seal is not allowed to inherit that claim from its mutable journal: the root finalizer must independently read the digest-named controlled envelope, public receipt/work/result blobs, and pinned verifier bytes, execute the canonical replay again, and bind every exact input digest into the signed replay result. That final replay has not run. |
| Concurrent provenance mode | **PASS** | Thin remains submission authority while the independent provenance audit runs in a bounded, single-flight background worker. |
| Whole-epoch FULL provenance | **NOT_PROVEN** | Non-verified candidates have explicit zero rows but do not publish candidate-specific replayable negative evidence. |
| Independent external reproduction | **NOT_PROVEN** until an outside operator runs the release | The exact public inputs and command are below. A controlled package is additionally required to replay raw TDX evidence. |
| Burn-only revocation fail-safe | **PASS at the observed chain boundary; rechecked before every write** | Finalized Finney block `8697317` reported `min_allowed_weights=1`, `max_weight_limit=1.0`, and commit-reveal disabled. The release refuses every SN39 write if any of those facts change, so a revoked final miner can be replaced by one 100% burn destination instead of leaving stale earning weights. |
| UID identity race guard | **NOT_PROVEN until the live gate below passes** | UIDs are never treated as permanent identities. A target may be rotated to a fresh, dedicated launch hotkey in a separately approved preparatory chain write, but the launch does not require one: only a target's own coldkey can replace its hotkey, and both SN39 target coldkeys are operator-controlled. That assumption is recorded in the proof itself as `"stability_basis": "operator_controlled_coldkeys"`. At finalized preflight block `B`, each target's lock is published as `swap_lock`: `never_rotated` when `LastHotkeySwapOnNetuid` is `0`, `active` when its value `S` satisfies `B + 4 - 1 <= S + HotkeySwapOnSubnetInterval`, and `expired` otherwise. Only an `active` lock carries a rotation proof, and a claimed `active` lock is proven in full: the validator, finalizer, and public reproducer require one unique successful `SubtensorModule.swap_hotkey_v2` call at the recorded finalized block, signed by the expected owner coldkey with the exact old hotkey, new hotkey, netuid `39`, and reviewed `keep_stake` value; the matching `HotkeySwappedOnSubnet` event and post-call root/successor lineage must also verify. The coldkey-wide `LastHotkeySwapOnNetuid` value alone is not target proof. Regardless of lock state, every target must have no pending `ColdkeySwapAnnouncements`, and the live storage value `ColdkeySwapAnnouncementDelay` must be at least `4`. These hotkey-swap and coldkey-swap checks are additional to the existing registration-replacement safety proof: the validator still resolves the signed worker hotkey, validator signer, subnet-owner burn hotkey, exact next epoch, live weight cooldown, registration capacity, `immunity_period`, `MinNonImmuneUids`, owner coldkey, `OwnedHotkeys`, and `ImmuneOwnerUidsLimit`, and reproduces the runtime's bounded owner-immortal set. The inclusion proof must recheck every hotkey binding, owner, swap guard, epoch, policy fact, call argument, successful execution, and finality. A returned canonical receipt is fsynced before archive reads. `NOT_PROVEN` leaves the attempt fenced for restart-only reproof and is published as `NOT_PROVEN`, not `FAIL`; `FAIL` requires operator investigation. Neither condition can trigger a second weight write. |

After the tagged launch gate passes, the public status and sanitized event
stream are published at:

- `https://api.cathedral.computer/v1/evidence/release.json`
- `https://api.cathedral.computer/v1/evidence/logs/status.json`
- `https://api.cathedral.computer/v1/evidence/logs/validator-events.jsonl`
- `https://api.cathedral.computer/v1/evidence/index.json`

## Current operational blockers

The software candidate is not the final launch release, and this document does
not authorize remediation, infrastructure spend, a chain write, or a public
claim. The following observations remain blocking:

| Boundary | Status | Current observation |
|---|---|---|
| Evidence epoch producer | **FAIL** | The live epoch producer is wedged on `signed report policy_digest does not match supplied registry`; it is not producing a qualifying fresh epoch. |
| Intel TDX worker | **FAIL** | The required worker is `TERMINATED`. Starting or replacing it is a paid production mutation and requires explicit authority. |
| Producer revision | **FAIL** | Three values disagreed. The host exporter stamped `b77c7cfacab34de75b1102360f6e3fc1edf5b796` into every signed manifest, this repository pinned `fa39af97e738fdbed5c454f976b61246590b5794`, and the producer actually runs `655c264421a1f5f2e625a372a40f595aa1e114ab` from `/opt/cathedral-sn39/venvs/9540de44...`. The installed venv is the only value that is evidence rather than assertion, so every pin in this repository now reads `9540de44...`. The boundary stays FAIL until the host exporter is redeployed to derive its stamp from the installed venv basename instead of hardcoding it, because until then signed evidence still misstates what produced it and FULL provenance cannot match. |
| Producer, enrollment, and controlled-package install contract | **IMPLEMENTED; live release proof required** | The producer atomically selects `/var/lib/cathedral-validator-controlled-sn39/current`; its real epoch directory is `root:cathedral-validator-evidence` mode `2750` with root-owned mode-`0640` regular files, and the validator service receives only that supplementary read group. The immutable release still fails closed unless the live host proves this exact contract. |
| Public launch evidence | **NOT_PROVEN** | The currently published evidence is stale and the current vector is empty/all-burn. It cannot authorize the launch weight. |
| Root-signed release | **NOT_PROVEN** | The final release and its detached signature are absent. A mutable candidate or unsigned `release.json` is not a sealed release. |

The safe order is therefore fixed: if a preparatory rotation is being performed,
review its bundle, select the exact replacement hotkeys, perform each separately
approved rotation, and update every rewarded and burn identity pin; repair the
producer, enrollment, and controlled-package installation contracts; pass final
review, CI, tag, and immutable installation; obtain explicit authority to start
the paid TDX worker; generate fresh evidence; pass the final read-only gate; and
only then request separate approval for one weight write.

## Blocking final eligibility

The release is **not ready for the final on-chain weight test** until all of
the following are proven from canonical finalized Finney state:

1. Every target whose owner coldkey holds a live rotation lock has that
   rotation proven: its unique decoded `swap_hotkey_v2` call, successful
   finalized receipt, matching `HotkeySwappedOnSubnet` event, and
   root/successor lineage all verify independently. A target that has never
   rotated, or whose cooldown has expired, carries no lock and no receipt.
   Rotation is not a launch prerequisite; a rotation that is performed must
   still prove out in full.
2. The rewarded target and the owner/burn target are evaluated independently.
   Each resolves its own owner coldkey, lock state, and proof; neither inherits
   a lock, a receipt, or a lineage from the other.
3. All Intel TDX evidence, workload/result receipts, the candidate snapshot,
   signed weight vector, and public provenance artifacts postdate every proven
   rotation. Evidence bound to an old hotkey cannot authorize the launch write.
   Where at least one target carries a proven rotation, the evidence candidate
   block must be strictly greater than the latest rotation block and every
   signed artifact timestamp must be later than the latest finalized rotation
   timestamp; where no target does, there is no rotation floor to clear. In
   both cases the candidate block's finalized hash must bind the TDX challenge.
4. At the exact finalized preflight block `B`, each target's lock state is
   recorded from its `LastHotkeySwapOnNetuid` block `S`, where a lock is
   `active` when:

   ```text
   B + 4 - 1 <= S + HotkeySwapOnSubnetInterval
   ```

   The `4` is the signed extrinsic's mortal-era period. An `active` lock proves
   that the target hotkey cannot be swapped again before the complete inclusion
   window ends. `S = 0` is recorded as `never_rotated` and a lock that no longer
   covers the window as `expired`; neither blocks the write, because only the
   target's own operator-controlled coldkey can replace its hotkey.
5. Each target owner coldkey has no pending `ColdkeySwapAnnouncements`, and
   the live runtime `ColdkeySwapAnnouncementDelay` is at least `4`. This closes
   the coldkey-transfer path for the same inclusion window.
6. Both targets independently pass the existing registration-replacement
   safety proof for all four blocks: unused-slot coverage, runtime-derived
   owner immortality, or registration immunity plus the conservative
   non-immune pruning buffer.
7. The final validator constant, both mainnet validator configurations, and
   the scorer runtime/environment pin the hotkey each target currently runs
   under, and no superseded identity. The producer registry and enrollment
   state name those same identities.
8. The controlled-package installation has an explicitly reviewed owner,
   group, directory-mode, and file-mode contract, and the exact package for the
   fresh evidence epoch is installed under that contract.
9. Final CI, tag resolution, immutable installation, controlled replay, and
   the complete read-only Finney preflight are all **PASS**. The root-signed
   public reproduction follows the one-shot finalizer; pre-write review proves
   its inputs and readiness rather than fabricating a sealed launch. Passing
   these gates still does not authorize a weight write.

A rotation is a preparatory chain write, not part of the one-shot weight
submission budget, and the launch gate does not require one. This guide does
not authorize any rotation: each requires explicit operator approval, exact
signer/target review, and a finalized receipt. The
locked Bittensor 10.5.0 environment has no reviewed convenience wrapper for
this call, so the approved operator procedure must compose
`SubtensorModule.swap_hotkey_v2` from runtime metadata, sign with the owning
coldkey, submit with finalization enabled, and retain the decoded call and
events. A legacy swap wrapper or an inferred state change is not acceptable
evidence. If any rotation, fresh-evidence, swap-safety, or
registration-replacement fact is `FAIL` or `NOT_PROVEN`, do not sign or
broadcast the launch weight call and do not make the public launch claim.

These checks reduce identity races; they do not remove governance trust. The
release still trusts the canonical Finney runtime and Bittensor
governance/root not to replace pallet semantics, mutate the relevant runtime
constants, or make an exceptional state change during the four-block mortal
era. The root-signed Cathedral release attests what Cathedral reproduced; it
does not supersede on-chain governance or prove that governance is unable to
intervene.

### Deterministic rotation handoff

Use `scripts/sn39_hotkey_rotation_operator.py` for one target at a time. Its
default is inspect-only: it connects to finalized Finney state, verifies the
named coldkey owns the old hotkey, verifies its exact expected UID and
owner/non-owner classification, checks that the named local new-hotkey wallet
matches a fresh on-chain identity, and composes the runtime-metadata
`SubtensorModule.swap_hotkey_v2` call. The `rewarded` label does not itself
establish reward eligibility; the fresh evidence gate below does that. Default
mode does **not** unlock, sign, or submit.

Do not run this section from a mutable checkout. The rotation operator and its
locked runtime must first be copied into a separately reviewed,
content-addressed, root-owned **preparatory rotation bundle**. Record and review
the exact source commit, file digests, dependency locks, interpreter, authority
host, and authority UID used by that bundle. This is not the final validator
tag or final immutable release: those cannot be created until the rotations
have established the exact hotkeys that the validator, both configurations,
scorer, producer registry, and enrollment state must pin. Before the first
write, confirm the rewarded and owner/burn targets have distinct owner
coldkeys; the runtime's receipt locator is coldkey-wide, so two target
rotations under one coldkey cannot both satisfy the later exact-receipt gate.

Build that bundle from the exact reviewed commit. The manifest builder reads
the requested Git blobs rather than trusting mutable working-tree bytes,
validates the installed environment against both hash locks, and binds every
bundle and environment byte and mode. It must run as root on the same host as
the named non-root rotation authority. Existing content-addressed directories
are never overwritten.

```bash
set -euo pipefail
umask 077
rotation_source='<absolute pristine checkout of the reviewed commit>'
rotation_sha="$(/usr/bin/git -C "$rotation_source" rev-parse HEAD)"
test "$(/usr/bin/git -C "$rotation_source" status \
  --porcelain=v1 --untracked-files=all --ignored=matching)" = ''
authority_host="$(uname -n)"
authority_uid="$(id -u)"
authority_user="$(id -un)"
authority_group="$(id -gn)"
authority_state="/var/lib/cathedral-sn39-rotation/uid-$authority_uid"
test "$authority_uid" -gt 0

rotation_stage="$(mktemp -d)"
trap 'chmod -R u+w "$rotation_stage" 2>/dev/null || true; rm -rf "$rotation_stage"' EXIT
bundle_stage="$rotation_stage/bundle"
venv_stage="$rotation_stage/venv"
mkdir -p "$bundle_stage"
/usr/bin/git -C "$rotation_source" archive \
  --format=tar \
  --output="$rotation_stage/bundle.tar" \
  "$rotation_sha" -- \
  scripts/sn39_hotkey_rotation_operator.py \
  scripts/build_sn39_rotation_manifest.py \
  scripts/build_sn39_release_manifest.py \
  deploy/sn39/cathedral-sn39-rotation-launcher.py \
  requirements/sn39-reproduction.lock \
  requirements/sn39-build.lock
/usr/bin/tar -xf "$rotation_stage/bundle.tar" -C "$bundle_stage"

/usr/bin/python3 -m venv "$venv_stage"
"$venv_stage/bin/python" -m pip install \
  --require-hashes -r "$bundle_stage/requirements/sn39-build.lock"
"$venv_stage/bin/python" -m pip install \
  --no-build-isolation \
  --require-hashes -r "$bundle_stage/requirements/sn39-reproduction.lock"

rotation_bundle="/opt/cathedral-sn39/rotation-bundles/$rotation_sha"
rotation_venv="/opt/cathedral-sn39/rotation-venvs/$rotation_sha"
sudo install -d -o root -g root -m 0755 \
  /opt/cathedral-sn39/rotation-bundles \
  /opt/cathedral-sn39/rotation-venvs \
  /usr/local/libexec \
  /etc/cathedral \
  /var/lib/cathedral-sn39-rotation
if ! sudo test -e "$authority_state"; then
  sudo install -d -o "$authority_user" -g "$authority_group" -m 0700 \
    "$authority_state"
fi
sudo test ! -e "$rotation_bundle"
sudo test ! -e "$rotation_venv"
sudo cp -a "$bundle_stage" "$rotation_bundle"
sudo cp -a "$venv_stage" "$rotation_venv"
sudo chown -R root:root "$rotation_bundle" "$rotation_venv"
sudo chmod -R u+rwX,go+rX,go-w "$rotation_bundle" "$rotation_venv"
sudo install -o root -g root -m 0755 \
  "$rotation_bundle/deploy/sn39/cathedral-sn39-rotation-launcher.py" \
  /usr/local/libexec/cathedral-sn39-rotation

manifest_tmp="$rotation_stage/sn39-rotation-manifest.json"
sudo /usr/bin/python3 -I -B \
  "$rotation_bundle/scripts/build_sn39_rotation_manifest.py" \
  --source "$rotation_source" \
  --source-sha "$rotation_sha" \
  --bundle "$rotation_bundle" \
  --venv "$rotation_venv" \
  --authority-host "$authority_host" \
  --authority-uid "$authority_uid" \
  > "$manifest_tmp"
sudo install -o root -g root -m 0444 \
  "$manifest_tmp" /etc/cathedral/sn39-rotation-manifest.json

sudo -u "$authority_user" /usr/bin/python3 -I -E -s \
  /usr/local/libexec/cathedral-sn39-rotation --help >/dev/null
sha256sum "$manifest_tmp"
```

Review the manifest bytes and displayed digest before either inspection. The
launcher refuses a different source file set, dependency environment,
bootstrap interpreter, host, UID, authority home, durable attempt directory,
launcher, bundle path, or venv path. It passes those exact digests and paths
to the operator as a canonical execution context. That context is part of the
confirmation digest, so an approval produced under one bundle cannot be used
under a replacement bundle. The launcher also starts the operator with an
allowlisted environment, so ambient Python or wallet-path variables cannot
substitute runtime code. Rebuilding a bundle or replacing the fixed root
manifest is a new operator action; it is not implied by approval of a
rotation.

Create one owner-only directory and save the inspection result:

```bash
set -euo pipefail
umask 077
/usr/bin/python3 -I -B -c \
  'import os; print("authority_host=" + os.uname().nodename); print("authority_uid=" + str(os.geteuid()))'
authority_host='<reviewed exact authority_host>'
authority_uid='<reviewed exact authority_uid>'
rotation_dir="/var/lib/cathedral-sn39-rotation/uid-$authority_uid"

/usr/bin/python3 -I -E -s \
  /usr/local/libexec/cathedral-sn39-rotation \
  --wallet-name validator \
  --new-wallet-name launch \
  --new-wallet-hotkey rewarded \
  --authority-host "$authority_host" \
  --authority-uid "$authority_uid" \
  --max-transaction-fee-rao \
    '<separately reviewed positive pre-sign fee-estimate ceiling>' \
  --expected-coldkey '<expected owner coldkey SS58>' \
  --old-hotkey '<current rewarded hotkey SS58>' \
  --new-hotkey '<fresh dedicated rewarded hotkey SS58>' \
  --expected-uid '<reviewed current UID>' \
  --role rewarded \
  --netuid 39 \
  --keep-stake \
  > "$rotation_dir/rewarded.inspect.json"
```

The example displays `keep_stake=true` syntax only; it does not approve that
choice. Replace it with `--do-not-keep-stake` when that is the separately
reviewed intent.

Review every field in `approval` and `observation`, including the complete
`execution_bundle`, `call_hex`,
`signer_coldkey`, both hotkeys, role, `netuid=39`, `keep_stake`, current UID,
owner, pinned genesis hash, reviewed finalized block/hash, nonce, approval
inclusion deadline, runtime spec/transaction versions, `KeySwapOnSubnetCost`,
coldkey free balance, `reviewed_transaction_fee_estimate_ceiling_rao`,
`reviewed_maximum_estimated_spend_rao`,
`on_chain_spend_cap_enforced=false`, and the explicit
`cost_authorization_model`. The confirmation digest binds those fields and the
exact composed call. The reviewed maximum is an estimated pre-sign boundary,
not an enforceable spend cap:
`swap_hotkey_v2` has no maximum-cost argument, and governance can change its
dispatch cost or fee behavior between the last check and inclusion. Separate
approval of the confirmation digest accepts that disclosed residual drift
risk; it must never be described as approval of an enforced maximum. The
inclusion deadline is fixed at two mortal eras after inspection, and the
operator refuses to sign unless the complete 64-block transaction era still
fits inside it. The broadcast invocation proves control of the new hotkey by
signing that domain-separated approval before it unlocks the coldkey, then
repeats all live ownership, freshness, registration, role, runtime, balance,
cost, nonce, canonical call, and chain checks after both unlocks. It estimates
the fee with the exact approved nonce, zero tip, and the same 64-block mortal
era/reference later passed to signing. It then repeats the current-head
runtime, call, ownership, nonce, balance, and key-swap-cost checks before
signing. It refuses before creating the broadcast intent when the estimate
exceeds the reviewed estimate ceiling. A detectable changed call, runtime,
economic boundary, nonce, or chain snapshot fails closed.

Only after separate explicit approval, copy the displayed
`confirmation_digest`, `attempt_scope.id`, and the four reviewed snapshot
fields from `approval` into a new command manually:

```bash
attempt_id='<copied attempt_scope.id>'
reviewed_finalized_block='<copied approval.reviewed_finalized_block>'
reviewed_finalized_hash='<copied approval.reviewed_finalized_hash>'
reviewed_coldkey_nonce='<copied approval.reviewed_coldkey_nonce>'
approval_valid_until_block='<copied approval.approval_valid_until_block>'
/usr/bin/python3 -I -E -s \
  /usr/local/libexec/cathedral-sn39-rotation \
  --wallet-name validator \
  --new-wallet-name launch \
  --new-wallet-hotkey rewarded \
  --authority-host "$authority_host" \
  --authority-uid "$authority_uid" \
  --max-transaction-fee-rao \
    '<same reviewed positive pre-sign fee-estimate ceiling>' \
  --expected-coldkey '<same expected owner coldkey SS58>' \
  --old-hotkey '<same current rewarded hotkey SS58>' \
  --new-hotkey '<same fresh dedicated rewarded hotkey SS58>' \
  --expected-uid '<same reviewed current UID>' \
  --role rewarded \
  --netuid 39 \
  --keep-stake \
  --broadcast \
  --confirmation-digest 'sha256:<copied reviewed digest>' \
  --reviewed-finalized-block "$reviewed_finalized_block" \
  --reviewed-finalized-hash "$reviewed_finalized_hash" \
  --reviewed-coldkey-nonce "$reviewed_coldkey_nonce" \
  --approval-valid-until-block "$approval_valid_until_block" \
  --state-file "$rotation_dir/rotation-$attempt_id.attempt.json" \
  --receipt-out "$rotation_dir/rotation-$attempt_id.receipt.json"
```

The state and receipt paths must be absolute, absent, and inside the one
manifest-bound, owner-only durable state directory shown above. Their exact
basenames are derived from the network, netuid, coldkey, and **old** hotkey and
are enforced by the tool. A different directory, new hotkey, role, or
`keep_stake` choice therefore cannot bypass an unresolved attempt for that old
hotkey. The reviewed bundle, authority host, exact OS UID, account home, and
durable directory are all bound into the approval digest. Do not copy the
coldkey or attempt files to another host or UID, and do not retry elsewhere
while an attempt is pending. If the original authority host is unavailable,
reconcile the recorded signed hash from canonical chain history before any
new approval.
Before the RPC submission, the tool fsyncs the signed hash, nonce, era
reference, exact call, and approval digest. It waits for finalization and emits
a canonical, secret-free receipt containing exactly the call,
extrinsic/block identity, block timestamp, index, coldkey, old/new hotkeys,
netuid, `keep_stake`, and matching `HotkeySwappedOnSubnet` event used by the
validator, finalizer, and public reproducer. It fsyncs that receipt before any
post-state reads, then requires the same UID, owner, role, swap block,
`HotkeyRoot`/`HotkeySuccessor` lineage, and no pending coldkey swap before
returning `PASS`. When the finalized SDK receipt exposes
`total_fee_amount`, the private attempt state also verifies it against the
canonical historical receipt and records `actual_transaction_fee_rao` plus
whether it remained within the reviewed estimate ceiling. If the SDK exposes
no actual fee, the state says `NOT_EXPOSED_BY_SDK`; it never fabricates one or
turns the estimate into an actual charge.

Exit `3` means **NOT_PROVEN**: the signed hash may have broadcast but the exact
finalized result was unavailable. Do not delete the attempt file and do not
rerun the broadcast. After the complete recorded 64-block mortal window is
finalized, run the same immutable bundle in reconciliation mode with the same
public target arguments:

```bash
attempt_id='<same copied attempt_scope.id>'
/usr/bin/python3 -I -E -s \
  /usr/local/libexec/cathedral-sn39-rotation \
  --wallet-name validator \
  --new-wallet-name launch \
  --new-wallet-hotkey rewarded \
  --authority-host "$authority_host" \
  --authority-uid "$authority_uid" \
  --max-transaction-fee-rao \
    '<same reviewed positive pre-sign fee-estimate ceiling>' \
  --expected-coldkey '<same expected owner coldkey SS58>' \
  --old-hotkey '<same current rewarded hotkey SS58>' \
  --new-hotkey '<same fresh dedicated rewarded hotkey SS58>' \
  --expected-uid '<same reviewed current UID>' \
  --role rewarded \
  --netuid 39 \
  --keep-stake \
  --reconcile \
  --state-file "$rotation_dir/rotation-$attempt_id.attempt.json" \
  --receipt-out "$rotation_dir/rotation-$attempt_id.receipt.json"
```

Reconciliation reads the original private signed-intent state, verifies its
new-hotkey possession signature and exact bundle-bound approval, searches only
the recorded mortal window for that exact hash, independently checks the
canonical call, historical success, event, timestamp, and post-rotation
lineage, and then completes the original files. It never unlocks, signs, or
submits. If the entire finalized window has no matching hash, or any exact
field contradicts history, the attempt remains fenced for explicit operator
direction. Any existing pending attempt blocks a second broadcast invocation.

After the rewarded transaction, canonical receipt, and post-rotation proof are
all conclusively `PASS`, repeat the complete
inspect, human review, separate approval, and broadcast flow for the
owner/burn target with distinct files, `--role owner-burn`, its exact owner
coldkey, and an independently reviewed `keep_stake` choice. Never reuse a
confirmation digest between roles or targets.

### Materialize fresh launch evidence

This is an external producer prerequisite, not functionality completed by the
rotation operator. The rotation tool does not generate, sign, or publish
Cathedral evidence.
After every performed rotation's receipt artifacts are `PASS`, first update
every rewarded and
owner/burn identity pin in the validator, both mainnet configurations, scorer,
producer registry, and enrollment state. Repair the producer deployment to the
required revision, fix its policy-digest/registry mismatch, and explicitly
review the controlled-package installation ownership and mode contract. Only
after final CI, tag, and immutable installation pass may an operator obtain
separate explicit authority to start the paid TDX worker. The authorized
Cathedral evidence producer must then create a new source epoch through its
existing publisher interfaces. Do not hand-edit or synthesize an index,
manifest, report, vector, receipt, replay result, or public blob in this
repository.

For the final launch service, materialize only the two existing inputs:

1. The producer publishes its new signed index, content-addressed manifest,
   score report, receipts, work/result blobs, and signed weight vector through
   the configured Cathedral endpoints in
   `config/validator-mainnet-sn39-launch.toml`.
2. The authorized controlled-disclosure package for that same manifest is
   installed byte-for-byte in an immutable epoch directory below
   `/var/lib/cathedral-validator-controlled-sn39`, selected atomically by its
   `current` symlink. The root is mode `2750`; epoch directories are
   `root:cathedral-validator-evidence` mode `2750`; and regular package files
   are root-owned, group `cathedral-validator-evidence`, mode `0640`, and never
   symlinks. The validator unit receives that group only as a supplementary
   read capability. The pinned verifier remains
   `/opt/cathedral-sn39/bin/cathedral-tdx-verifier`.

Before the final weight test, record the canonical receipt JSON file for every
rotation that was performed and confirm with the evidence producer that its new
epoch names the hotkeys each target currently runs under. The one-shot launch
service then enforces the actual gate before it signs: the candidate block and
report validity start are strictly after every proven rotation block; the
candidate block hash is canonical and binds the TDX challenge; manifest,
report, vector, and signed-index timestamps are strictly after every proven
rotation block timestamp; every rewarded row replays from the
controlled package; and the recomputed 90/10 vector exactly matches the signed
vector. Any missing public blob, controlled byte, freshness binding, target
identity, or replay result stops before signing.

There is deliberately no local command that fabricates a PASS for this step.
Until the producer runs the required revision without the policy-digest
mismatch, the paid TDX worker is explicitly authorized and running, and the
producer has published the new epoch and delivered its matching controlled
package, the launch evidence boundary remains **NOT_PROVEN** and
`cathedral-validator-sn39-launch.service` must not be started.

## Immutable release

This table describes the required final release, not the current candidate.
Do not create the tag, install the validator, or sign the release while either
configuration, validator constant, scorer, producer registry, or enrollment
state still names a superseded hotkey. The final release can be frozen only
after every performed rotation receipt verifies and all identity pins and
installation contracts have been reviewed together.

| Component | Revision or digest |
|---|---|
| SN39 validator | tag `sn39-mainnet-tdx-20260724`; the exact commit is bound by the root-signed public release |
| Cathedral Confidential producer | `655c264421a1f5f2e625a372a40f595aa1e114ab` |
| Registry key bundle | `sha256:5fb8f00cd2541606927373f596c2ba77d4ce485df0539f4afd5091858af48512` |
| Score-report key bundle | `sha256:30e438fff5b0508402b233eb5eec590a834882801a552edbbf7e62e45cf98c70` |
| Evidence-index key bundle | `sha256:1e35b9ce36b3da3362a88feb93dfa90f1fe03ab7c42e902b13ac3789324f7611` |
| Release-attestation key bundle | `sha256:1a60a22de160853d460b22853a426d0534fab4df0fe9f89e5859d60bb4ed3d12` |
| Reproduction dependency lock | `sha256:765f90428c0fbdb8fc58f03e82edd6dd9ea1cc50bd685dc2bdbbecef30aa1624` |
| Build-backend dependency lock | `sha256:b212eed198712c8f54ad6250dc64575485bef5c3c311d71ee3c24a2c80396912` |
| Verifier binary blob | `sha256:35bb55f89f411d5dcf5f72be90488e999ee68c41dfc0429a0dcb8cc2b448b6bb` |
| Verifier implementation | `sha256:8292b085e4dbe228f8ffd2ec7046a1c0f1324ff5e7a29d1574ce16963f9b098f` |

The four public key files are committed under `config/provenance/` as the
exact bytes whose digests appear above. They contain public Ed25519 keys only.

## Reproduce the public decision path

This command reads the root-signed release, the exact historical Finney blocks,
and the frozen Cathedral evidence checkpoint. It never consults the mutable
current weight feed and does not write to the chain:

```bash
set -euo pipefail
git clone https://github.com/cathedralai/cathedral.git
cd cathedral
git fetch --tags origin
# Release gate: this must resolve before the launch is announced.
release_commit="$(
  git rev-parse --verify 'refs/tags/sn39-mainnet-tdx-20260724^{commit}'
)"
git checkout --detach "$release_commit"
test "$(git rev-parse HEAD)" = "$release_commit"
# The final assertion also requires HEAD to equal the root-signed public
# release manifest's exact reproducer_revision.
git merge-base --is-ancestor \
  98b862bfe40c4918e1e1ace09a55de11270af9cf "$release_commit"
repro_tmp="$(mktemp -d /tmp/cathedral-sn39-repro.XXXXXX)"
trap 'rm -rf "$repro_tmp"' EXIT
python3 -m venv "$repro_tmp/venv"
"$repro_tmp/venv/bin/python" -m pip install \
  --require-hashes -r requirements/sn39-build.lock
"$repro_tmp/venv/bin/python" -m pip install \
  --no-build-isolation \
  --require-hashes -r requirements/sn39-reproduction.lock

env -i HOME="$HOME" PATH="$PATH" PYTHONDONTWRITEBYTECODE=1 \
  "$repro_tmp/venv/bin/python" -B scripts/run_sn39_public_reproduction.py
```

There is deliberately no installed console entry point for this operation.
Run the reviewed script from the pristine tagged checkout so its code,
configuration, and Git revision are verified as one release.

The environment is deliberately outside the checkout and bytecode is disabled:
the reproducer rejects modified, untracked, **or ignored** files before using
the repository revision. The direct runner binds imports to its own checkout
and then verifies that checkout against the signed release. The provenance
dependency still carries a legacy console-script name, so an older reused
environment can otherwise resolve `cathedral-validator` to the wrong package.
A fresh environment plus `python -m scaffold.cli` is deterministic.

## Install the final reviewed release

The production services do not run a mutable checkout or editable package.
After any rotation, update and review all identity pins, repair
the producer and enrollment contracts, resolve the controlled-package
ownership and mode contract, and require final CI to pass. Only then create the
tag and install its exact commit in a root-owned checkout. Build its versioned
environment from the two committed hash locks. The first lock installs the
producer's build backend; the second disables build isolation, so Python cannot
download an unpinned build tool while installing the byte-pinned producer
archive. Install every reviewed config and unit before generating the manifest:

The bootstrap is the host's resolved, versioned `/usr/bin/python3.12` regular
file—not the `/usr/bin/python3` symlink. Builder, manifest, launcher, and
systemd units all pin that same path and its digest, so an interpreter change
fails closed instead of making symlink mode bits part of the trust decision.

```bash
set -euo pipefail
release_sha="<reviewed-tag-commit>"
release="/opt/cathedral-sn39/releases/$release_sha"
venv="/opt/cathedral-sn39/venvs/$release_sha"

/usr/bin/python3.12 -m venv "$venv"
"$venv/bin/python" -m pip install \
  --require-hashes -r "$release/requirements/sn39-build.lock"
"$venv/bin/python" -m pip install \
  --no-build-isolation \
  --require-hashes -r "$release/requirements/sn39-reproduction.lock"

install -D -o root -g root -m 0755 \
  "$release/deploy/sn39/cathedral-sn39-release-launcher.py" \
  /usr/local/libexec/cathedral-sn39-release
install -D -o root -g root -m 0644 \
  "$release/deploy/sn39/cathedral-validator-sn39.service" \
  /etc/systemd/system/cathedral-validator-sn39.service
install -D -o root -g root -m 0644 \
  "$release/deploy/sn39/cathedral-validator-sn39-launch.service" \
  /etc/systemd/system/cathedral-validator-sn39-launch.service
install -D -o root -g root -m 0644 \
  "$release/deploy/sn39/cathedral-validator-sn39-reconcile.service" \
  /etc/systemd/system/cathedral-validator-sn39-reconcile.service
install -D -o root -g root -m 0644 \
  "$release/deploy/sn39/cathedral-sn39-public-status.service" \
  /etc/systemd/system/cathedral-sn39-public-status.service
install -D -o root -g root -m 0644 \
  "$release/deploy/sn39/cathedral-sn39-public-status.timer" \
  /etc/systemd/system/cathedral-sn39-public-status.timer
install -D -o root -g root -m 0644 \
  "$release/deploy/sn39/cathedral-sn39-validator.sysusers" \
  /etc/sysusers.d/cathedral-sn39-validator.conf
install -D -o root -g root -m 0644 \
  "$release/deploy/sn39/cathedral-sn39-validator.tmpfiles" \
  /etc/tmpfiles.d/cathedral-sn39-validator.conf
install -d -o root -g root -m 0755 /etc/cathedral-validator
install -D -o root -g root -m 0644 \
  "$release/config/validator-mainnet-sn39.toml" \
  /etc/cathedral-validator/validator-mainnet-sn39.toml
install -D -o root -g root -m 0644 \
  "$release/config/validator-mainnet-sn39-launch.toml" \
  /etc/cathedral-validator/validator-mainnet-sn39-launch.toml
systemd-sysusers /etc/sysusers.d/cathedral-sn39-validator.conf
systemd-tmpfiles --create /etc/tmpfiles.d/cathedral-sn39-validator.conf

# Provision only the already-registered validator HOTKEY into the service
# account. Run this on a secure interactive console. The source key may prompt
# for its password; neither its mnemonic nor private bytes are printed.
source_wallet_root='<reviewed source Bittensor wallets directory>'
source_wallet_name='<reviewed source wallet name>'
source_hotkey_name='<reviewed source hotkey name>'
expected_validator_hotkey='<reviewed registered validator hotkey SS58>'
umask 077
"$venv/bin/python" -I -B - \
  "$source_wallet_root" "$source_wallet_name" "$source_hotkey_name" \
  "$expected_validator_hotkey" <<'PY'
import sys
from bittensor_wallet import Wallet

root, name, hotkey_name, expected = sys.argv[1:]
source = Wallet(path=root, name=name, hotkey=hotkey_name)
key = source.get_hotkey()  # interactive decrypt when the source is encrypted
if key.ss58_address != expected:
    raise SystemExit("source hotkey does not match the reviewed validator")
target = Wallet(
    path="/var/lib/cathedral-validator/.bittensor/wallets",
    name="validator",
    hotkey="default",
)
if target.hotkey_file.exists_on_device():
    raise SystemExit("destination validator hotkey already exists")
target.set_hotkey(key, encrypt=False, overwrite=False)
PY
chown -R cathedral-validator:cathedral-validator \
  /var/lib/cathedral-validator/.bittensor
find /var/lib/cathedral-validator/.bittensor -type d -exec chmod 0700 {} +
find /var/lib/cathedral-validator/.bittensor -type f -exec chmod 0600 {} +

# Prove the service account can load the key noninteractively and locally sign
# a fixed challenge. This performs no network request and no chain write.
timeout 10s sudo -u cathedral-validator env -i \
  HOME=/var/lib/cathedral-validator \
  "$venv/bin/python" -I -E -s -c \
  'from bittensor_wallet import Wallet; w=Wallet(name="validator",hotkey="default"); assert not w.hotkey_file.is_encrypted(); k=w.hotkey; m=b"cathedral-sn39-local-signing-probe-v1"; s=k.sign(m); assert k.verify(m,s); print(k.ss58_address)'

# The printed public address must equal $expected_validator_hotkey. Never copy
# the validator coldkey, source mnemonic, password, or private bytes to this
# host, release manifest, logs, shell history, or approval artifact.
systemctl mask --now cathedral-thin-validator.service
legacy_state="$(systemctl is-active cathedral-thin-validator.service || true)"
case "$legacy_state" in
  inactive|failed) ;;
  *) echo "legacy validator did not stop: $legacy_state" >&2; exit 1 ;;
esac
systemctl daemon-reload

manifest_tmp="$(mktemp /etc/cathedral-validator/sn39-release-manifest.json.XXXXXX)"
/usr/bin/python3.12 -I -E -s \
  "$release/scripts/build_sn39_release_manifest.py" \
  --release "$release" \
  --release-sha "$release_sha" \
  --venv "$venv" \
  > "$manifest_tmp"
chown root:root "$manifest_tmp"
chmod 0644 "$manifest_tmp"
mv -f "$manifest_tmp" /etc/cathedral-validator/sn39-release-manifest.json
```

Manifest schema v3 binds the pristine release, lock-created environment,
reviewed configs and units, verifier binary, and the resolved root-managed
`/usr/bin/python3` bootstrap interpreter. The systemd units start that absolute
interpreter in isolated, environment-ignoring mode. The launcher then passes a
fixed allowlisted child environment, so ambient variables cannot substitute
settings, Python imports, or the shared submission journal.

The environment commitment accepts a directory symlink only when its resolved
target stays inside the same immutable environment. This covers the standard
Linux `venv` layout (`lib64 -> lib`) while continuing to reject directory
symlinks that escape to mutable or uncommitted trees.

The builder and launcher also require every committed tree directory to be
root-controlled and readable/searchable by the unprivileged service account,
and every regular file to be root-controlled, single-linked, non-writable by
group or world, and service-readable. This rejects a root-only staging
directory before it can produce a manifest that the shipped unit cannot use.

The non-secret validator configs and release manifest live under the dedicated
root-owned, world-traversable `/etc/cathedral-validator` directory. They must
not be installed under `/etc/cathedral`: production keeps that directory
non-traversable by the validator account because it also contains producer
signing keys and other service secrets.

Git verification runs with `safe.directory` set to the exact manifest-selected
release path. This lets the unprivileged validator verify a root-owned checkout
without trusting any other repository or a wildcard safe-directory rule.

The signing hotkey is intentionally outside the public release manifest:
hashing a secret key into public artifacts would create a durable verifier for
guessing or exfiltration attempts. Its presence is instead gated by exact
owner/mode checks, the noninteractive local challenge above, and the live
preflight requirement that its public address is the reviewed registered
validator with a permit. The validator **coldkey is never installed**.

## Execute and seal the one-shot launch

Do not start the continuous writer first. The launch order is deliberately
one-way:

1. Steps 1 and 2 apply only if a target is being rotated; the launch gate does
   not require a rotation. Review and hash-lock the preparatory rotation bundle
   and its runtime while keeping every validator service stopped. Select the
   exact fresh replacement hotkeys, confirm their distinct owner coldkeys, and
   run the operator in inspect-only mode. This preparatory bundle is not a
   final validator tag and does not authorize any rotation.
2. Obtain separate explicit operator approval for each preparatory chain write
   that rotates a target to a fresh launch hotkey. Review each signer, coldkey,
   old hotkey, new hotkey, netuid, and `keep_stake` choice before broadcast,
   and retain every finalized receipt. For each receipt, retain the unique
   decoded `SubtensorModule.swap_hotkey_v2` call and matching
   `HotkeySwappedOnSubnet` event. Confirm the post-call
   `HotkeyRoot`/`HotkeySuccessor` lineage for netuid `39`. A rotation is
   not implicitly authorized by approval of the later weight test.
3. Only after every performed rotation finalizes, update the rewarded and
   owner/burn identity pins in the validator, both mainnet configurations,
   scorer,
   producer registry, and enrollment state. Repair the producer to the required
   revision and resolve its policy-digest/registry mismatch. Resolve and review
   the controlled-package owner, group, directory mode, and file mode instead
   of inferring them. Then pass final review and CI, create the final tag, and
   install the immutable release above. Freeze subnet-owner changes to tempo,
   owner identity, registration limits, commit-reveal, and weight policy for
   the complete remaining one-shot window.
4. Obtain explicit authority for the paid production mutation before starting
   the TDX worker. Only after it is running may the repaired producer create an
   entirely fresh launch evidence generation: raw TDX evidence,
   workload/result receipts, candidate snapshot, signed vector, evidence
   checkpoint, and provenance artifacts.
   Reject any artifact whose identity, creation boundary, or digest traces to
   a superseded hotkey. Require the evidence candidate block to be strictly
   greater than every proven rotation block and its finalized hash to be the
   TDX challenge anchor; require the manifest, score report, vector, and signed
   index times to be later than every proven rotation timestamp. At the exact
   finalized preflight block `B`, record each target's lock state from its
   `LastHotkeySwapOnNetuid` value `S`, prove every target whose lock is
   `active` under `B + 4 - 1 <= S + HotkeySwapOnSubnetInterval`, and prove for
   every target that its owner coldkey has no pending
   `ColdkeySwapAnnouncements` and that the live storage value
   `ColdkeySwapAnnouncementDelay >= 4`.
5. With the launch service still stopped, run the complete read-only release,
   evidence, controlled-replay, and finalized-Finney eligibility gate. It must
   prove the final tag and installation, every claimed rotation lock, every
   identity pin, the fresh evidence boundary, exact 90/10 vector,
   live weight policy, cooldown, epoch, and replacement and swap safety. Any
   `FAIL` or `NOT_PROVEN` stops here. A green read-only gate does not authorize
   a chain write.
6. Only after step 5 is entirely **PASS**, request separate explicit approval
   for one exact `set_mechanism_weights` attempt. The approval must name the
   final release, validator signer, two target hotkeys, vector digest, attempt
   boundary, and current finalized preflight. Do not reuse a rotation
   approval and do not start the service without this new authorization.
7. Start `cathedral-validator-sn39-launch.service` only under that approval. It
   can reserve at most one
   launch attempt and writes only after rewarded-set raw TDX replay, exact
   vector agreement, finalized hotkey-to-UID mapping, `min_allowed_weights=1`,
   `max_weight_limit=1.0`, and commit-reveal disabled all pass.
   The current UIDs are derived from hotkeys; historical UID numbers are not
   launch configuration. The preflight also requires that the validator has
   strictly exceeded `WeightsSetRateLimit`, that the SDK epoch countdown equals
   the exact next-epoch block, and that every target is replacement-safe for
   the complete four-block mortal era. A full subnet therefore requires the
   rewarded hotkey's registration immunity to span the era plus a conservative
   `MinNonImmuneUids` pruning buffer after excluding every runtime-derived
   owner-immortal UID. The owner/burn target must independently pass the same
   replacement-safe-set test. These checks do not replace the swap-safety
   checks in step 2; both sets must pass for the full four-block mortal era.
   The exact inclusion block must still map the submitted UIDs to the same
   rewarded and owner/burn hotkeys, preserve their checked owner coldkeys and
   swap guards, and retain the same epoch and direct-write policy. If the
   finalized head advances between this preflight and signing, the launch
   refuses before producing or journaling a transaction.
8. If the service does not return a named finalized extrinsic, stop. A pending
   journal is ambiguous and must never be retried automatically.
   SN39 composes and signs the normalized pallet call against the exact
   finalized preflight block. The signed hash, nonce, era reference, version,
   UID vector, and wire weights are fsynced before submission. If the process
   dies after broadcast but before a receipt returns, restart recovery searches
   only that four-block mortal window for the pre-journaled hash, then verifies
   its signer, decoded call, successful historical execution, and inclusion
   contract before finalizing the original journal.
   When a canonical receipt returned before an archive/RPC read became
   unavailable, the service fsyncs that exact receipt and exits `NOT_PROVEN`.
   A launch or continuous-thin restart may only re-read the same signed
   historical extrinsic and finalize the same journal; it cannot call
   `set_mechanism_weights` again.
   A positive mismatch is **FAIL** and requires operator investigation. If no
   unique exact successful call is present, the attempt remains fenced for
   later proof or manual reconciliation. The service has a 20-minute bound for
   preflight, synchronous rewarded-set replay, one primary submission, receipt
   persistence, and proof.
9. With the launch service stopped, run the root-only finalizer against the
   single `journal-<chain-and-hotkey-digest>.json`:

```bash
sudo /usr/bin/python3 -I -E -s \
  /usr/local/libexec/cathedral-sn39-release finalize \
  /var/lib/cathedral-validator/journal-<64-hex-digest>.json
```

The root-owned launcher rechecks the installed manifest, complete immutable
release tree, versioned environment tree, bootstrap interpreter, service
configs, and executable bytes immediately before it enters the finalizer. The
finalizer rejects direct entry or a changed manifest. It pins the supplied
archive to Finney genesis
`0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03`,
then re-reads the historical mapping and inclusion blocks, verifies the exact
extrinsic and applied wire weights,
re-verifies every claimed target-rotation receipt and lineage, enforces the
post-rotation evidence boundary, recomputes the frozen public evidence
checkpoint, and then independently executes the canonical positive-TDX replay
from the actual digest-named controlled envelope and immutable verifier bytes.
The root-signed replay result names the exact envelope, evidence, challenge,
receipt, workload, result, verifier, manifest, and report digests that passed;
the finalizer never synthesizes replay success from journal booleans. Missing,
aliased, substituted, or unreadable controlled bytes leave the release
`NOT_PROVEN` and stop before signing. It checks the root-only private key
against the committed public key and only then publishes `release.json` and
its detached signature exactly once.

Every journal, install manifest, signing seed, controlled envelope, verifier,
public blob, and public release artifact must be a single-link regular file;
hardlink aliases fail closed. Each public-evidence directory has one
persistent, root-controlled, single-link mode-0600 advisory lock. The
finalizer holds its exclusive kernel lock across recovery, staging, linking,
directory `fsync`, cleanup, and final reread, so overlapping finalizer
processes cannot unlink or substitute each other's staging inode. Process
death releases the kernel lock without unlinking its stable lock inode. Public
publication then uses deterministic content-addressed staging, file and
directory `fsync`, and an idempotent recovery step, so a process death before
the durable link or between linking and staging cleanup can resume without
accepting partial bytes or replacing a different seal. An idempotent rerun may
confirm identical bytes, but the finalizer rejects an attempt to replace an
existing seal. The launch journal must be owned by the validator service
account in its mode-0700 runtime directory; public release files and bounded
evidence blobs remain root-owned and non-writable by group or world. It never
publishes controlled envelope bytes or prints private key material.

10. Run the public reproduction from a pristine tagged checkout. An independent
   operator must run the same command for the external-reproduction gate.
11. Start `cathedral-validator-sn39-reconcile.service`. It independently
   re-verifies the public signature, archive record, frozen evidence, and local
   one-shot journal before setting the durable continuous-operation seal.
12. Reconciliation still does **not** authorize recurring chain writes. As a
   separate operator decision, create a short-lived, attempt-bounded
   root-signed authorization for the exact reconciled launch, release, Finney
   genesis, validator hotkey, runtime paths, mechanism, call, and submission
   lane. Use a freshly reviewed finalized block:

```bash
# Thin submission remains the default. This example allows at most 48 accepted
# validator account nonces over at most one day / 7,200 blocks. The command
# creates approval files only; it performs no chain call. The root launcher
# first verifies the complete immutable release and venv.
sudo /usr/bin/python3 -I -E -s \
  /usr/local/libexec/cathedral-sn39-release authorize-recurring \
  --journal /var/lib/cathedral-validator/journal-<64-hex-digest>.json \
  --expected-validator-hotkey "$expected_validator_hotkey" \
  --reviewed-finalized-block <CURRENT_FINALIZED_FINNEY_BLOCK> \
  --reviewed-validator-nonce <CURRENT_VALIDATOR_ACCOUNT_NONCE> \
  --max-attempts 48 \
  --valid-for-blocks 7200 \
  --valid-for-seconds 86400 \
  --i-authorize-recurring-mainnet-writes
```

   The root launcher runs with `-I -E -s`, discards the caller's Python paths
   and environment, verifies the manifest, pristine release tree, interpreter,
   venv, configs, and every reviewed byte, and then enters only the immutable
   authorization module with an argument- and manifest-bound launcher context;
   direct module entry is rejected. The operator tool independently reproduces
   the signed public launch release rather than trusting the service-owned
   journal alone,
   requires the explicit acknowledgement above, derives the immutable scope,
   signs the byte-canonical authorization with the separately protected
   release-attestation key, and writes:

   - `/etc/cathedral-validator/sn39-recurring-write-authorization.json`
   - `/etc/cathedral-validator/sn39-recurring-write-authorization.json.sig`

   Both files and their parent must remain root-owned and non-writable by group
   or world. The validator re-verifies their detached Ed25519 signature and
   exact bytes and scope before every reservation and again at the lowest
   chain-call boundary. It rechecks lane, block, time, and the live validator
   account nonce before unlocking the wallet. The signed nonce interval is
   exactly `max_attempts` wide, so restoring an older service journal cannot
   create additional accepted chain writes; unrelated accepted transactions
   conservatively consume the same allowance. The durable local journal also
   consumes its attempt budget when an exact signed transaction is fsynced, not
   merely when a tick starts. Expiry, exhaustion, deletion, replacement,
   tampering, nonce-range exhaustion, a signer/release/genesis mismatch, or a
   missing authorization all fail closed before another write.

   Do not pass `--allow-full-authority-writes` for the normal thin-plus-FULL-
   shadow profile. That switch is a separate explicit approval for the
   independently recomputed authority lane; it never enables an automatic
   thin/FULL fallback. Renewal requires another reviewed command with
   `--replace-existing`; stop the continuous service before replacement and
   restart it afterward so it observes exactly the reviewed pair. Revocation
   likewise means stopping the service first and removing both files before
   any restart; never rely on replacing or deleting one file under a running
   daemon. The previous launch seal alone can never authorize renewal.
13. Only after reconciliation and the separate recurring authorization pass,
   enable
   `cathedral-validator-sn39.service` and
   `cathedral-sn39-public-status.timer`.

The public status card is operational telemetry, not launch authorization. It
reports authority `PASS` only for a fresh observed exact 90/10 submission whose
UIDs were resolved from the signed rewarded and owner/burn hotkeys. A
100% burn fail-safe is safe for emissions but is intentionally
`NOT_PROVEN` as the advertised validated-supply boundary.

The release is not publishable until the tag-resolution gate above succeeds
against the exact reviewed merge commit. The final assertion is mandatory. It
rejects a missing or invalid root signature, a different reproducer revision,
source revision, key or verifier pin, historical candidate set, launch vector,
UID mapping, inclusion extrinsic, on-chain weights, or frozen evidence result.

Expected public-only result:

- the root-signed launch vector, source revisions, pins, and historical
  candidate set verify;
- every claimed target rotation, its successful event and lineage, and the
  post-rotation evidence boundary verify independently;
- the launch vector maps to one admitted Intel TDX worker plus the logical
  10% burn target, with the effective protocol-quantized shares published;
- the exact inclusion extrinsic and historical on-chain weights verify;
- no chain write occurs and no validator wallet is needed;
- frozen public receipt/report/index recomputation runs;
- raw-evidence FULL assurance remains `NOT_PROVEN` without the controlled
  package.

The root-signed release and content-addressed evidence form the stable public
audit record. A failed signature, source pin, network, subnet, candidate-set,
digest, or historical-chain check fails closed.

## Check current validator health

This is a separate, time-dependent operational check. It proves what the
current feed would do now; it is not substituted for the immutable launch
reproduction above:

```bash
cp config/validator-mainnet-sn39.toml validator.local.toml
# Set wallet_name and validator_hotkey to an existing registered validator.
install -d -m 700 "$HOME/.cathedral"
repro_dir="$(mktemp -d "$HOME/.cathedral/sn39-current.XXXXXX")"
python -m scaffold.cli serve \
  --config validator.local.toml \
  --state-file "$repro_dir/validator.json" \
  --runtime-root "$repro_dir/runtime" \
  --jsonl "$repro_dir/validator-events.jsonl" \
  --dry-run --once
```

The current check must fail closed if the live feed, freshness, signature,
policy, finalized mapping, commit-reveal state, state persistence, or
concurrent shadow audit is unhealthy. Legitimate future policy changes may
produce a different current vector without changing the historical launch
proof.

## Independently replay raw Intel TDX evidence

Raw TDX quotes and machine identity are controlled-disclosure data, not public
logs. An authorized validator receives:

1. the controlled package for the selected source epoch;
2. the verifier binary matching both verifier digests above;
3. a secure out-of-band confirmation of the release pins.

It then adds:

```toml
controlled_dir = "/path/to/controlled/epoch"
verifier_binary = "/path/to/cathedral-tdx-verifier"
```

to `[provenance]` and repeats the dry-run. Every controlled envelope is first
content-addressed against the public manifest, then the quote, nonce,
finalized-block anchor, worker identity, channel binding, work input/result,
receipt, registry policy, and verifier implementation are checked.

The default continuous profile keeps thin submission authority and concurrent
shadow recomputation. Do not switch `mode = "authority"` merely because one
positive worker replays. FULL authority refuses to submit unless the complete
historically anchored candidate epoch reaches FULL assurance. After the
root-signed launch has been independently reconciled, a current FULL audit may
make a one-way thin-to-FULL authority handoff: both modes share the same
cross-process lock and durable attempt journal, and the handoff is committed
only with the first FULL signed intent. There is no automatic FULL-to-thin
fallback; changing authority again requires explicit operator reconciliation.

## Privacy and operator controls

Public artifacts contain the signed candidate hotkey set needed to prove that
eligible registered identities were not silently omitted. They do not contain
machine endpoints, customer inputs, secrets, operator credentials, or raw TDX
quotes. The public log publisher uses an allowlist and identifier redaction;
raw evidence remains in the controlled package.

The reward mechanism is `validated_supply_v1`: its logical target routes 90%
to validated Intel TDX CPU supply and 10% to the burn destination.
Bittensor's u16 wire encoding is `[65535, 7282]`, which yields effective shares
of about 89.999588% and 10.000412%. Both values are published so the proof does
not claim mathematical precision the protocol cannot encode. Registration,
uptime, or self-reported volume never earns weight by itself.

This release also requires SN39 commit-reveal to remain disabled. The named
launch extrinsic must directly apply `set_mechanism_weights`, and the validator
proves its block canonical at or below the current finalized head before it
consumes the durable pending-attempt fence.
