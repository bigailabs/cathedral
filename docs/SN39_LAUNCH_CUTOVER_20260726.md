# SN39 launch cutover: producer revision and deploy contract

This document covers two changes that must land together and be applied to the
live producer host in a specific order. Neither has been applied to any host.
Nothing here authorizes a chain write, infrastructure spend, or a public claim.

## 1. Producer revision reconciliation

### What disagreed

Three values claimed to name the code that produced SN39 evidence, and they
did not agree:

| Source | Value | Kind of claim |
|---|---|---|
| Host exporter `/usr/local/sbin/cathedral-sn39-export-evidence` | `b77c7cfacab34de75b1102360f6e3fc1edf5b796` | Hardcoded constant stamped into every signed manifest |
| This repository (validator pin and configs) | `fa39af97e738fdbed5c454f976b61246590b5794` | Hardcoded constant the validator compares against |
| The producer's installed controlled venv `/opt/cathedral-sn39/venvs/9540de44...` | `655c264421a1f5f2e625a372a40f595aa1e114ab` | The code that actually ran |

The consequence was not cosmetic. `scaffold/provenance_audit.py:935-936` fails
the audit when the manifest's `source_revision` differs from the operator pin,
and `scaffold/validator_thin.py:4731` requires the launch reservation's
`source_revision` to equal `SN39_PRODUCER_REVISION`. With the exporter stamping
one value and the validator pinning a different one, and neither matching the
installed venv, signed evidence misstated what produced it and FULL provenance
could never match.

### Why the venv basename is the truthful value

The installed venv path is the only one of the three that is an observation
rather than an assertion. The exporter constant and the validator constant are
both editable claims about a fact recorded somewhere else, and each can drift
independently. The venv basename is the identity of the interpreter and package
tree that actually executed the export, so it cannot drift from the code that
produced the evidence without the export itself moving. Every pin in this
repository is now `655c264421a1f5f2e625a372a40f595aa1e114ab`.

The upstream archive for that revision was verified independently rather than
trusted:

```
curl -sSL -o /tmp/c.tar.gz \
  https://github.com/cathedralai/cathedralconfidential/archive/655c264421a1f5f2e625a372a40f595aa1e114ab.tar.gz
sha256sum /tmp/c.tar.gz
# befc572f459c2d80af7ce18013cb4d3649716f143da0a6a86a4a8b96f84b88fb
```

The same procedure applied to the superseded `fa39af97` archive reproduces its
previously pinned `356080e1...` digest exactly, which is what establishes that
the method is sound and that only the revision changed.

### Sites updated

The revision is load-bearing in more places than the constant. All of these
moved in one commit, because bumping any subset leaves the release internally
inconsistent and fails the two-mode tests:

| Site | What it binds |
|---|---|
| `scaffold/validator_thin.py` `SN39_PRODUCER_REVISION` | Launch reservation equality check |
| `scaffold/validator_thin.py` STARTUP event `provenance_source_revision` | Published startup assertion |
| `scaffold/sn39_public_reproduction.py` `EXPECTED_PRODUCER_REVISION` | Public reproducer pin |
| `scaffold/sn39_public_reproduction.py` `EXPECTED_STARTUP` | Startup event the reproducer requires |
| `scaffold/sn39_public_reproduction.py` `EXPECTED_RELEASE_PINS["reproduction_dependencies"]` | Digest of the reproduction lock, which changes because the lock changes |
| `config/validator-mainnet-sn39.toml` | Operator provenance pin |
| `config/validator-mainnet-sn39-launch.toml` | Launch-mode provenance pin |
| `config/validator-thin-sn39-relay.toml` | Relay provenance pin |
| `config/validator.toml` | Commented reference pin |
| `requirements/sn39-reproduction.lock` | Archive URL and its `--hash` |
| `pyproject.toml` | `provenance` extra archive URL and `#sha256=` |
| `scripts/build_sn39_release_manifest.py` | `EXPECTED_CATHEDRAL_URL`, `EXPECTED_CATHEDRAL_ARCHIVE_SHA256` |
| `docs/SN39_MAINNET_RELEASE_20260724.md` | Component table and reproduction lock digest |
| `scaffold/publisher/tests/test_validator_two_mode.py` | Two-mode config assertion and cross-binding fixture |

The reproduction lock digest moved from
`sha256:8a4d730778c37ef7cc47e2ffcba74e42dcdd19240283f688567dd06204181e5b` to
`sha256:4c8155b0f3af5d2df254e1680b574ed51d6d9b9a36078469cc9bc5a1f13c84d8`.
The build lock digest is unchanged, which confirms only the cathedral archive
line moved.

### Host exporter change (NOT applied)

`/usr/local/sbin/cathedral-sn39-export-evidence` currently hardcodes
`SOURCE_REVISION`. Replace the hardcoded assignment with a derivation from the
controlled venv that is executing the export:

```sh
# Derive the stamped revision from the controlled venv actually running this
# export. PRODUCER_ROOT is /opt/cathedral-sn39/venvs/<sha>, installed one
# directory per reviewed revision, so its basename IS the revision that
# produced the evidence.
#
# A hardcoded constant is a second, independently editable claim about the same
# fact. It drifts the moment a new venv is installed and nobody remembers to
# edit this file, and the drift is invisible from the host because the only
# place the value is ever read back is the manifest this script just wrote:
# the stamp agrees with itself no matter how wrong it is. Deriving makes the
# stamp an observation of the running install instead of an assertion about it,
# so the failure mode changes from silently signing false provenance to
# refusing to export.
: "${PRODUCER_ROOT:?cathedral-sn39-export-evidence: PRODUCER_ROOT is not set}"

if [ ! -d "$PRODUCER_ROOT" ]; then
    printf '%s\n' "cathedral-sn39-export-evidence: refusing to export, PRODUCER_ROOT='${PRODUCER_ROOT}' is not a directory" >&2
    exit 1
fi

SOURCE_REVISION="$(basename "$PRODUCER_ROOT")"

# Refuse rather than stamp a value that cannot be a git revision. Without this
# guard a relocated or mistyped PRODUCER_ROOT (for example a "current" symlink
# parent, or a trailing slash) would quietly stamp a plausible-looking string
# into signed evidence.
if ! printf '%s' "$SOURCE_REVISION" | grep -Eq '^[0-9a-f]{40}$'; then
    printf '%s\n' "cathedral-sn39-export-evidence: refusing to export, derived source revision '${SOURCE_REVISION}' from PRODUCER_ROOT='${PRODUCER_ROOT}' is not a 40-character lowercase hex sha" >&2
    exit 1
fi
```

This fragment is POSIX sh and assumes only that `PRODUCER_ROOT` is already set
by the surrounding script to the installed venv root. The exporter file itself
was not read while preparing this change, so confirm the variable name and the
location of the existing `SOURCE_REVISION=` assignment before applying.

After the exporter is redeployed, the next signed manifest must stamp
`655c264421a1f5f2e625a372a40f595aa1e114ab`. Until that happens the Producer
revision boundary in `SN39_MAINNET_RELEASE_20260724.md` stays **FAIL**, because
published evidence still carries `b77c7cf...` and will not match the pin.

### Host exporter change (APPLIED live 2026-07-27T02:48:44Z): policy reissue fix

Preserve this on any exporter redeploy, including the launch-window one above.
Dropping it re-introduces a chronic ~12-hourly outage.

`runtime export-evidence` requires the frozen epoch's signed report
`policy_digest` to equal the sha256 of the file passed as `--policy-registry`.
The exporter passed the live `/etc/cathedral/policy-registry-sn39.json`
unconditionally, but `cathedral-sn39-policy-republisher.timer` reissues that
file roughly every 12 hours. Every reissue therefore permanently deadlocked the
epoch loop's reconcile-before-admit gate (observed 2026-07-26 at both the
10:25:09Z and 22:31:37Z reissues; the second one degraded the chain vector to
a 100% burn until repaired).

The applied fix resolves the registry per epoch: use the live file when its
hash equals the report's pinned digest, otherwise use the content-addressed
archive the republisher writes to
`/var/lib/cathedral-confidential-sn39/policy-history/` (hash re-verified before
use; the CLI's own digest check remains the gate, so unknown digests still fail
closed). The registry install path is archive-then-install under
`policy-writer.lock`, so no missing-archive window exists. Inserted between
`export-score-class` and `export-evidence`:

```sh
readonly POLICY_HISTORY=/var/lib/cathedral-confidential-sn39/policy-history
pinned_digest="$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["policy_digest"])' \
  "${report_path}")"
pinned_hex="${pinned_digest#sha256:}"
epoch_registry="${REGISTRY}"
if [[ "${pinned_hex}" =~ ^[0-9a-f]{64}$ ]] \
  && [[ -r "${REGISTRY}" ]] \
  && [[ "$(sha256sum "${REGISTRY}" | cut -d" " -f1)" != "${pinned_hex}" ]]; then
  for candidate in "${POLICY_HISTORY}"/release-*-"${pinned_hex}".json; do
    if [[ -f "${candidate}" ]] \
      && [[ "$(sha256sum "${candidate}" | cut -d" " -f1)" == "${pinned_hex}" ]]; then
      epoch_registry="${candidate}"
      printf '%s reconciling frozen epoch against archived policy %s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(basename "${candidate}")" >&2
      break
    fi
  done
fi
```

with `--policy-registry "${epoch_registry}"` on the `export-evidence` call.

Rollback: `/usr/local/sbin/cathedral-sn39-export-evidence.pre-rotationfix-20260727T024844Z`
(restores the deadlock-on-reissue behavior; only useful if the fix itself
misbehaves). Durable package-level fix is tracked in cathedralconfidential
(export-evidence should accept a policy-history directory natively).

## 2. Deploy contract migration safety

### The hazard, measured

The previously shipped `deploy/sn39/cathedral-sn39.tmpfiles` could not be
applied to the live producer host. Reproducing the host's ownership state in a
disposable container and running `systemd-tmpfiles --create` against the
shipped contract produced this:

```
-/var/lib/cathedral-public-evidence polaris:polaris 755
-/var/lib/cathedral-public-evidence/blobs polaris:polaris 755
-/var/lib/cathedral-public-evidence/blobs/sha256 polaris:polaris 755
+/var/lib/cathedral-public-evidence root:root 755
+/var/lib/cathedral-public-evidence/blobs root:root 755
+/var/lib/cathedral-public-evidence/blobs/sha256 root:root 755
-/var/lib/cathedral-public-evidence/logs polaris:polaris 755
+/var/lib/cathedral-public-evidence/logs cathedral-status:cathedral-status 755
```

The whole evidence tree is chowned away from the producer, not just the `logs`
directory, and `logs` is written every few minutes by the running producer.
systemd also runs `systemd-tmpfiles --create` at boot, so this would recur.

A second hazard was the filename. The host already carries
`/etc/sysusers.d/cathedral-sn39.conf` declaring the evidence PRODUCER
identities, installed from the producer's deploy tree. This repository shipped
a file of the same name declaring VALIDATOR identities. Installing it
overwrites the producer's declaration. `systemd-sysusers` never deletes
accounts, so the producer identities survive on the running host and nothing
appears to break, until the host is rebuilt from its configuration and they are
silently absent. That the two trees collide at all records something worth
keeping: this host was provisioned from something other than this repository's
deploy tree.

### What changed

Both files are renamed so validator identities can never overwrite producer
identities:

- `deploy/sn39/cathedral-sn39.sysusers` to `deploy/sn39/cathedral-sn39-validator.sysusers`, installed as `/etc/sysusers.d/cathedral-sn39-validator.conf`
- `deploy/sn39/cathedral-sn39.tmpfiles` to `deploy/sn39/cathedral-sn39-validator.tmpfiles`, installed as `/etc/tmpfiles.d/cathedral-sn39-validator.conf`

The validator sysusers file already matches what is installed by hand on the
host today, so the repository now converges on the host rather than fighting it.

Every mode and ownership field in the tmpfiles contract is now `:`-prefixed.
Per `tmpfiles.d(5)`, a `:`-prefixed mode or user/group "is only applied when
creating new inodes, and if the inode the line refers to already exists, its
access mode / user/group is left in place unmodified". The `z` lines were
deleted outright: `z` creates nothing and exists only to force mode and
ownership onto an inode that already exists, which is precisely the unsafe
half of the contract.

### Proof

`systemd-tmpfiles --create` was run for real against a reconstruction of the
live host state (evidence tree owned `polaris:polaris`, a `logs/status.json`
the producer is appending to) on three systemd versions:

| systemd | Live-host state | Fresh host |
|---|---|---|
| 249 (Ubuntu 22.04) | No change to any owner, group or mode | Converges to the reviewed layout |
| 255 (Ubuntu 24.04) | No change to any owner, group or mode | Converges to the reviewed layout |
| 257 (Debian 13) | No change to any owner, group or mode | Converges to the reviewed layout |

Fresh-host convergence is unchanged in shape from the old contract: the tree is
created `root:root 0755`. The `logs` owner has since changed from
`cathedral-status` to `polaris` for the reason given in section 5, and four
subdirectories the live host carries were added, so a fresh host now converges
on the whole tree rather than part of it. The contract still fully describes a
new host; it simply no longer rewrites an established one.

`test_immutable_install_binds_venv_and_masks_legacy_writer` now asserts the
property rather than only the three known lines. It parses every directive in
the file and fails if any line is not a `d` line, or if any mode, user or group
field is missing its `:` prefix. A future line cannot reintroduce the hazard
without failing the test.

### Read-only verification on the live host

**`--dry-run` is not available on this host.** The producer host runs
systemd 255 (`255.4-1ubuntu8.16`) and `systemd-tmpfiles` gained `--dry-run` in
256. Running it there fails with `unrecognized option '--dry-run'` and exits
without checking anything, which is easy to misread as a clean result. Check
the version before relying on it:

```sh
systemctl --version | head -1
```

On systemd 256 or newer, this needs no root and changes nothing. `--dry-run`
prints `Would create directory` for directories that already exist, which is a
dry-run artifact and not a hazard. The signal that matters is whether any
`Would change` line appears:

```sh
systemd-tmpfiles --dry-run --create \
  /path/to/release/deploy/sn39/cathedral-sn39-validator.tmpfiles 2>&1 \
  | grep 'Would change' \
  && echo 'UNSAFE: contract would modify existing ownership or mode' \
  || echo 'SAFE: no ownership or mode change on this host'
```

Expected output is eight `Would create directory` lines and nothing else. Run
the same command against the old contract to see the hazard the change removes:
it prints eight `Would change` lines.

On systemd 255, which is what this host runs, verify by observation instead.
The property being checked is that no existing inode changes owner, group or
mode, so record the tree, apply, and compare:

```sh
find /var/lib/cathedral-public-evidence -maxdepth 2 \
  -printf '%p %u:%g %m\n' | sort > /tmp/evidence-tree.before
# ... install the file and run systemd-tmpfiles --create on it (step 3) ...
find /var/lib/cathedral-public-evidence -maxdepth 2 \
  -printf '%p %u:%g %m\n' | sort > /tmp/evidence-tree.after
diff /tmp/evidence-tree.before /tmp/evidence-tree.after \
  && echo 'SAFE: no ownership or mode change on this host'
```

An empty diff is the same verdict the dry-run would have given. If the diff is
non-empty, the rollback is `rm /etc/tmpfiles.d/cathedral-sn39-validator.conf`
plus restoring the recorded ownership.

## 3. Weight-policy contract version: the publisher and validator move together

This is the tightest coupling in the cutover and the one with no rollback if it
is sequenced wrong. The launch-locked validator and the live publisher disagree
about the shape of the signed `validated_supply` policy block, and the
disagreement is invisible until the validator is upgraded.

### Measured, both sides

The live publisher emits contract **v1**. Read from the host with a plain GET
against the loopback feed the epoch loop already uses:

```sh
curl -sk https://127.0.0.1:8012/v1/validator/weights/next \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["policy_metadata"]["validated_supply"])'
```

```
{"contract_version": "v1", "intel_tdx_allocation": 0.9,
 "verified_gpu_allocation": 0.1, "verified_gpu_admitted": false,
 "burn_hotkey": "5G3qVaXzKMPDm5AJ3dpzbpUC27kpccBvDwzSWXrq8M6qMmbC"}
```

The launch-locked validator requires contract **v2**.
`scaffold/validator_thin.py::_validated_supply_meta` compares the field set
exactly and then checks the version:

```python
expected = {
    "contract_version",
    "intel_tdx_allocation",
    "fixed_burn_allocation",
    "burn_hotkey",
}
if set(policy) != expected:
    raise wire.VectorError("validated_supply metadata fields mismatch")
if policy["contract_version"] != "v2":
    raise wire.VectorError("validated_supply unsupported contract_version")
```

Both `config/validator-mainnet-sn39.toml` and
`config/validator-mainnet-sn39-launch.toml` set
`require_policy = "validated_supply_v1"`, which is the mode name that routes
into this function. The mode is named v1; the payload contract it demands is
v2. They are different version numbers and the naming collision is worth
knowing before reading the code.

A v1 payload fails on the field-set comparison before the version check is
ever reached, because `verified_gpu_allocation` and `verified_gpu_admitted` are
not in `expected` and `fixed_burn_allocation` is missing.

### Why the running validator does not already fail

The live validator runs revision `98b862b` from
`/home/polaris/cathedral-scorer-98b862b`, which accepts v1. That is the only
reason production submits the 90/10 split today. Upgrading the validator
without upgrading the publisher removes that acceptance.

### What actually happens if they are sequenced wrong

The validator submits **nothing**. It does not fall back to an all-burn vector.
`vector_to_uid_weights` raises, the tick emits

```
VECTOR rejected stage=map reason=VectorError
VECTOR_REJECTED stage=map status=FAIL
  remediation="The signed vector failed UID mapping; nothing was submitted."
```

and the continuous loop's handler records `TICK_FAILED` and breaks. There is no
burn fallback anywhere on this path; the code comment at the burn application
site states the signed burn is applied only after a fully successful map.

The consequence is therefore not a 100% burn but total silence: no weight
extrinsic is emitted at all. The previously set weights stay on chain and go
stale, and once they pass the subnet's expiry the validator stops asserting the
90/10 split entirely. That failure is quieter than a burn and takes longer to
notice, which is why the verification step below runs BEFORE the validator is
cut over rather than after.

### Required sequencing

The publisher upgrade to a v2-emitting revision belongs in the SAME maintenance
window as the exporter redeploy and the validator install. Verify first, then
cut over:

```sh
curl -sk https://127.0.0.1:8012/v1/validator/weights/next \
  | python3 -c '
import json, sys
p = json.load(sys.stdin)["policy_metadata"]["validated_supply"]
expected = {"contract_version", "intel_tdx_allocation",
            "fixed_burn_allocation", "burn_hotkey"}
assert p["contract_version"] == "v2", f"contract_version={p[\"contract_version\"]}"
assert set(p) == expected, f"field set mismatch: {sorted(set(p) ^ expected)}"
print("OK: publisher emits v2 with the exact field set the validator requires")
'
```

Do not proceed to the validator cutover step until this prints `OK`. Run it
against port 8012, which is the feed the epoch loop consumes. Port 8010 is
served by `cathedral-scorer-canary.service` and currently has no live consumer,
so a v2 answer there proves nothing about what the validator will fetch.

## 4. Staged cutover

Apply in this order. Each step is independently reversible.

1. **Verify, change nothing.** Run the read-only `--dry-run` command above and
   confirm `SAFE`. Record the current ownership for rollback:
   `find /var/lib/cathedral-public-evidence -maxdepth 2 -printf '%p %u:%g %m\n' | sort`.
2. **Install the sysusers file under its new name.** Install
   `cathedral-sn39-validator.sysusers` to
   `/etc/sysusers.d/cathedral-sn39-validator.conf` and run `systemd-sysusers`
   on that path only. Do not touch `/etc/sysusers.d/cathedral-sn39.conf`, which
   belongs to the producer. This is a no-op on the current host because the
   identities are already present.
3. **Install the tmpfiles file under its new name** to
   `/etc/tmpfiles.d/cathedral-sn39-validator.conf`, then run
   `systemd-tmpfiles --create /etc/tmpfiles.d/cathedral-sn39-validator.conf`.
   Re-run the `find` from step 1 and confirm the output is byte-identical.
4. **Redeploy the exporter** with the derivation change from section 1. Confirm
   the next exported manifest stamps `9540de44...`, and that the exporter
   refuses to run if `PRODUCER_ROOT` is unset or not a 40-hex basename.
5. **Upgrade the weight-policy publisher to a v2-emitting revision.** See
   section 3. This must happen before step 7 and inside the same window,
   because the validator being installed there rejects v1 outright.
6. **Verify the publisher emits v2** with the exact required field set, using
   the assertion command in section 3 against port 8012. Do not continue until
   it prints `OK`. This is the gate for step 7.
7. **Install the validator release** with the bumped pins. Provenance can only
   reach FULL after step 4 has produced at least one manifest stamped
   `9540de44...`; before that the pin and the evidence still disagree.
8. **Replace the pre-cutover status publisher.** The live host runs its own
   `cathedral-sn39-public-status.service` against the current validator's event
   stream. The shipped unit is paired with `cathedral-validator-sn39.service`
   and reads `/var/log/cathedral-validator/validator-events.jsonl`, which does
   not exist until step 7 has run. Install it only after step 7, and confirm it
   actually ran: its `ConditionPathExists=` makes systemd SKIP it silently if
   the log is absent, so a skipped unit and a working one look identical in
   `systemctl status`. Check `journalctl -u cathedral-sn39-public-status` for a
   real execution, not just an absence of errors.
9. **Leave the three validator units disabled and inactive** until the launch
   window. Their single-writer guards are unchanged: each names the other SN39
   writers in `Conflicts=` and refuses to start via `ExecStartPre` while any of
   them is active.

### Rollback

| Step | Rollback |
|---|---|
| 2 | `rm /etc/sysusers.d/cathedral-sn39-validator.conf`. Accounts already created remain, which is harmless and matches the current host. |
| 3 | `rm /etc/tmpfiles.d/cathedral-sn39-validator.conf`. No ownership was changed, so there is nothing to restore. This is the property proven above. |
| 4 | Reinstall the previous exporter. Evidence returns to stamping `b77c7cf...`, which is wrong but is the current production behavior. |
| 5 | Reinstall the previous publisher revision. It returns to emitting v1, which the pre-cutover validator accepts. Roll back step 7 with it or the validator has nothing it will accept. |
| 7 | Reinstall the previous validator release. The pins revert together because they moved together. |
| 8 | Reinstall the pre-cutover status publisher unit. Nothing else depends on the shipped one. |

Steps 1 through 3 do not touch the producer and can be done outside a
maintenance window. Step 4 restarts the exporter and should be done between
export cycles.

Steps 5 through 7 are one transaction. Rolling back the validator without
rolling back the publisher, or the reverse, reproduces the version mismatch in
section 3 from the opposite direction: a v2 publisher feeding the `98b862b`
validator fails the same exact-field-set comparison. Roll them back together.

## 5. Status publisher identity: resolved

The previous revision of this document left open which account owns
`/var/lib/cathedral-public-evidence/logs`. It is now resolved in favour of
`polaris`, the account that demonstrably writes it.

The deciding evidence is that `cathedral-status` has never existed. `id
cathedral-status` on the producer host returns "no such user", the logs
directory is `polaris:polaris 0755`, and the publisher that writes it every
minute already runs as `polaris` and is healthy:

```
cathedral-sn39-publish-validator-status[118036]:
  {"events_published": 200, "status": "sanitized_validator_stream_published"}
```

So the contract was not describing a configuration that had drifted. It was
describing one that had never been applied. The `cathedral-status` identity is
removed from the sysusers file, the tmpfiles `logs` line, and the status unit,
which now runs `User=polaris`. Nothing is chowned, and the option that would
have required chowning a live producer-owned tree is off the table.

The alternative remains available and is a one-line change in review if
preferred: **give the status publisher its own directory**, for example
`/var/lib/cathedral-validator-status/logs` owned by a real `cathedral-status`
account, and publish from there. That removes the shared-directory coupling
entirely rather than naming the existing owner. It costs a new directory, a
sysusers entry, a tmpfiles entry, and a path change in the publish script, and
it does not match what runs today, which is why it is not the default here.

## 6. Inventory and operator notes

These are recorded because a new operator trips over them, not because any of
them blocks the launch. None proposes a host deletion.

### Retained but unreferenced under /etc/cathedral

No unit or script under `/etc/systemd/system`, `/usr/local/sbin`,
`/usr/local/libexec` or `/home/polaris/cathedral-sn39/scripts` references any of
these. They are retained deliberately; deleting key material belongs in an
explicit operator transaction, not a release.

| Path | Note |
|---|---|
| `confidential-spot-worker.env` | No referencing unit |
| `registry-reissues-sn39.jsonl` | No referencing unit |
| `receipt-signing-sn39.key` | Superseded by `receipt-signing-sn39-20260724.key`, which IS referenced |
| `release-attestation-signing-sn39-20260724.key` | Key material, no referencing unit |
| `confidential-validator-sn39-provenance-canary.toml` | No referencing unit |
| `confidential-sn39-rollback-anchor` | No referencing unit |
| `scorer-canary.env.sh.bak.20260713T111941Z` | Backup of a live file |
| `rollback-20260713T184411Z/` | Snapshot directory |
| `rollback-canonical-feed-20260723T0738Z/` | Snapshot directory |
| `rollback-validated-supply-20260722T2343Z/` | Snapshot directory |

`confidential-measurements.json` and `confidential-tokens.json` are referenced
only by `/usr/local/sbin/cathedral-confidential-validator-loop`, which is the
`ExecStart` of `cathedral-confidential-validator.service`. That unit is disabled
and inactive, so both files are reachable only through a dead path.

### The scorer unit names do not match their ports

This reverses the intuition and has cost review time already:

| Unit | Port | Consumer |
|---|---|---|
| `cathedral-scorer-sn39.service` | 8012 | The live epoch loop and `cathedral-sn39-fresh-evidence-epoch` |
| `cathedral-scorer-canary.service` | 8010 | None |

Both serve the same `scaffold.publisher.server:app` from the same venv with the
same TLS pair, and both source `/etc/cathedral/scorer-canary.env.sh`, which sets
`PORT=8010` for both. The only substantive difference is that the `sn39` unit
additionally exports `CATHEDRAL_WEIGHT_POLICY_NETWORK=finney` and `NETUID=39`.

Port 8010's only reference anywhere is
`cathedral-confidential-validator-loop:10`
(`PUBLISHER=https://127.0.0.1:8010/v1/external-scores/violet`), and that script
belongs to the disabled `cathedral-confidential-validator.service`. Any
verification command that talks to a scorer must target **8012**.

### The canary worker runs a development flag on a routable bind

`cathedral-confidential-canary.service` runs:

```
cathedral worker serve --hotkey cathedral-tdx-canary-7e93d5de \
  --host 0.0.0.0 --port 8081 --development-allow-non-loopback
```

`MINING.md` says of that flag: "Use the flag only here, never on a worker
anything else can reach." A `0.0.0.0` bind is reachable. This is not a chain
writer and not a launch blocker, but it is a documented rule the host
contradicts, so it is a decision to make in the launch window rather than a
detail to leave undisturbed.
