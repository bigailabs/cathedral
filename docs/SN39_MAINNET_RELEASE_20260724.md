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
| Intel TDX positive-work replay | **PASS** | The controlled evidence for the admitted worker replays through the pinned verifier implementation. |
| Concurrent provenance mode | **PASS** | Thin remains submission authority while the independent provenance audit runs in a bounded, single-flight background worker. |
| Whole-epoch FULL provenance | **NOT_PROVEN** | Non-verified candidates have explicit zero rows but do not publish candidate-specific replayable negative evidence. |
| Independent external reproduction | **NOT_PROVEN** until an outside operator runs the release | The exact public inputs and command are below. A controlled package is additionally required to replay raw TDX evidence. |
| Burn-only revocation fail-safe | **PASS at the observed chain boundary; rechecked before every write** | Finalized Finney block `8697317` reported `min_allowed_weights=1`, `max_weight_limit=1.0`, and commit-reveal disabled. The release refuses every SN39 write if any of those facts change, so a revoked final miner can be replaced by one 100% burn destination instead of leaving stale earning weights. |

After the tagged launch gate passes, the public status and sanitized event
stream are published at:

- `https://api.cathedral.computer/v1/evidence/release.json`
- `https://api.cathedral.computer/v1/evidence/logs/status.json`
- `https://api.cathedral.computer/v1/evidence/logs/validator-events.jsonl`
- `https://api.cathedral.computer/v1/evidence/index.json`

## Immutable release

| Component | Revision or digest |
|---|---|
| SN39 validator | tag `sn39-mainnet-tdx-20260724`; the exact commit is bound by the root-signed public release |
| Cathedral Confidential producer | `fa39af97e738fdbed5c454f976b61246590b5794` |
| Registry key bundle | `sha256:5fb8f00cd2541606927373f596c2ba77d4ce485df0539f4afd5091858af48512` |
| Score-report key bundle | `sha256:30e438fff5b0508402b233eb5eec590a834882801a552edbbf7e62e45cf98c70` |
| Evidence-index key bundle | `sha256:1e35b9ce36b3da3362a88feb93dfa90f1fe03ab7c42e902b13ac3789324f7611` |
| Release-attestation key bundle | `sha256:1a60a22de160853d460b22853a426d0534fab4df0fe9f89e5859d60bb4ed3d12` |
| Reproduction dependency lock | `sha256:8a4d730778c37ef7cc47e2ffcba74e42dcdd19240283f688567dd06204181e5b` |
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

The environment is deliberately outside the checkout and bytecode is disabled:
the reproducer rejects modified, untracked, **or ignored** files before using
the repository revision. The direct runner binds imports to its own checkout
and then verifies that checkout against the signed release. The provenance
dependency still carries a legacy console-script name, so an older reused
environment can otherwise resolve `cathedral-validator` to the wrong package.
A fresh environment plus `python -m scaffold.cli` is deterministic.

## Install the reviewed release

The production services do not run a mutable checkout or editable package.
Install the exact reviewed release in a root-owned checkout and build its
versioned environment from the two committed hash locks. The first lock installs
the producer's build backend; the second disables build isolation, so Python
cannot download an unpinned build tool while installing the byte-pinned producer
archive. Install every reviewed config and unit before generating the manifest:

```bash
set -euo pipefail
release_sha="<reviewed-tag-commit>"
release="/opt/cathedral-sn39/releases/$release_sha"
venv="/opt/cathedral-sn39/venvs/$release_sha"

/usr/bin/python3 -m venv "$venv"
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
  "$release/deploy/sn39/cathedral-sn39.sysusers" \
  /etc/sysusers.d/cathedral-sn39.conf
install -D -o root -g root -m 0644 \
  "$release/deploy/sn39/cathedral-sn39.tmpfiles" \
  /etc/tmpfiles.d/cathedral-sn39.conf
install -D -o root -g root -m 0644 \
  "$release/config/validator-mainnet-sn39.toml" \
  /etc/cathedral/validator-mainnet-sn39.toml
install -D -o root -g root -m 0644 \
  "$release/config/validator-mainnet-sn39-launch.toml" \
  /etc/cathedral/validator-mainnet-sn39-launch.toml
systemd-sysusers /etc/sysusers.d/cathedral-sn39.conf
systemd-tmpfiles --create /etc/tmpfiles.d/cathedral-sn39.conf
systemctl mask cathedral-thin-validator.service
systemctl daemon-reload

manifest_tmp="$(mktemp /etc/cathedral/sn39-release-manifest.json.XXXXXX)"
/usr/bin/python3 -I -E -s \
  "$release/scripts/build_sn39_release_manifest.py" \
  --release "$release" \
  --release-sha "$release_sha" \
  --venv "$venv" \
  > "$manifest_tmp"
chown root:root "$manifest_tmp"
chmod 0644 "$manifest_tmp"
mv -f "$manifest_tmp" /etc/cathedral/sn39-release-manifest.json
```

Manifest schema v3 binds the pristine release, lock-created environment,
reviewed configs and units, verifier binary, and the resolved root-managed
`/usr/bin/python3` bootstrap interpreter. The systemd units start that absolute
interpreter in isolated, environment-ignoring mode. The launcher then passes a
fixed allowlisted child environment, so ambient variables cannot substitute
settings, Python imports, or the shared submission journal.

## Execute and seal the one-shot launch

Do not start the continuous writer first. The launch order is deliberately
one-way:

1. Confirm the reviewed tag and CI, install the immutable release above, and
   keep the previous validator stopped.
2. Start `cathedral-validator-sn39-launch.service`. It can reserve at most one
   launch attempt and writes only after rewarded-set raw TDX replay, exact
   vector agreement, finalized UID mapping, `min_allowed_weights=1`,
   `max_weight_limit=1.0`, and commit-reveal disabled all pass.
3. If the service does not return a named finalized extrinsic, stop. A pending
   journal is ambiguous and must never be retried automatically.
4. With the launch service stopped, run the root-only finalizer against the
   single `journal-<chain-and-hotkey-digest>.json`:

```bash
sudo "$venv/bin/python" -B "$release/scripts/finalize_sn39_public_release.py" \
  --release "$release" \
  --release-sha "$release_sha" \
  --journal /var/lib/cathedral-validator/journal-<64-hex-digest>.json
```

The finalizer re-reads the historical mapping and inclusion blocks from a
Finney archive, verifies the exact extrinsic and applied wire weights,
recomputes the frozen public evidence checkpoint, creates the content-addressed
positive-TDX replay result, checks the root-only private key against the
committed public key, and only then publishes `release.json` and its detached
signature exactly once. An idempotent rerun may confirm identical bytes, but
the finalizer rejects an attempt to replace an existing seal. The launch
journal must be owned by the validator service account in its mode-0700 runtime
directory; public release files and bounded evidence blobs remain root-owned
and non-writable by group or world. It never prints private key material.

5. Run the public reproduction from a pristine tagged checkout. An independent
   operator must run the same command for the external-reproduction gate.
6. Start `cathedral-validator-sn39-reconcile.service`. It independently
   re-verifies the public signature, archive record, frozen evidence, and local
   one-shot journal before setting the durable continuous-operation seal.
7. Only after reconciliation passes, enable
   `cathedral-validator-sn39.service` and
   `cathedral-sn39-public-status.timer`.

The public status card is operational telemetry, not launch authorization. It
reports authority `PASS` only for a fresh observed exact 90/10 submission. A
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

Do not switch `mode = "authority"` merely because one positive worker replays.
Authority refuses to submit unless the complete historically anchored
candidate epoch reaches FULL assurance. The launch configuration therefore
keeps thin authority and concurrent shadow recomputation.

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
