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

Fresh-host convergence is unchanged from the old contract: the tree is created
`root:root 0755` with `logs` as `cathedral-status:cathedral-status 0755`. The
contract still fully describes a new host; it simply no longer rewrites an
established one.

`test_immutable_install_binds_venv_and_masks_legacy_writer` now asserts the
property rather than only the three known lines. It parses every directive in
the file and fails if any line is not a `d` line, or if any mode, user or group
field is missing its `:` prefix. A future line cannot reintroduce the hazard
without failing the test.

### Read-only verification on the live host

This needs no root and changes nothing. `--dry-run` prints
`Would create directory` for directories that already exist, which is a dry-run
artifact and not a hazard. The signal that matters is whether any
`Would change` line appears:

```sh
systemd-tmpfiles --dry-run --create \
  /path/to/release/deploy/sn39/cathedral-sn39-validator.tmpfiles
```

Expected output on the current host is four `Would create directory` lines and
nothing else. To reduce it to a single verdict:

```sh
systemd-tmpfiles --dry-run --create \
  /path/to/release/deploy/sn39/cathedral-sn39-validator.tmpfiles 2>&1 \
  | grep 'Would change' \
  && echo 'UNSAFE: contract would modify existing ownership or mode' \
  || echo 'SAFE: no ownership or mode change on this host'
```

Run the same command against the old contract to see the hazard the change
removes: it prints eight `Would change` lines.

## 3. Staged cutover

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
5. **Install the validator release** with the bumped pins. Provenance can only
   reach FULL after step 4 has produced at least one manifest stamped
   `9540de44...`; before that the pin and the evidence still disagree.
6. **Leave the three validator units disabled and inactive** until the launch
   window. Their single-writer guards are unchanged: each names the other SN39
   writers in `Conflicts=` and refuses to start via `ExecStartPre` while any of
   them is active.

### Rollback

| Step | Rollback |
|---|---|
| 2 | `rm /etc/sysusers.d/cathedral-sn39-validator.conf`. Accounts already created remain, which is harmless and matches the current host. |
| 3 | `rm /etc/tmpfiles.d/cathedral-sn39-validator.conf`. No ownership was changed, so there is nothing to restore. This is the property proven above. |
| 4 | Reinstall the previous exporter. Evidence returns to stamping `b77c7cf...`, which is wrong but is the current production behavior. |
| 5 | Reinstall the previous validator release. The pins revert together because they moved together. |

Steps 1 through 3 do not touch the producer and can be done outside a
maintenance window. Step 4 restarts the exporter and should be done between
export cycles.

## 4. Open decisions

**The status publisher cannot currently write the directory it is configured to
write.** `cathedral-sn39-public-status.service` runs as `cathedral-status` with
`ReadWritePaths=/var/lib/cathedral-public-evidence/logs`, but on the live host
that directory is `polaris:polaris 0755`. `ReadWritePaths=` controls the mount
namespace, not file permissions, so the unit will get `EACCES`. Making the
tmpfiles contract migration-safe deliberately does not fix this, because the
only way a tmpfiles line could fix it is the chown that would break the
producer. Options, in the order they are recommended:

1. **Give the status publisher its own directory** (for example
   `/var/lib/cathedral-validator-status/logs`, owned `cathedral-status`) and
   publish from there. Removes the shared-directory coupling entirely and
   touches no producer-owned path. Requires changing the unit and the publish
   script paths.
2. **Add `cathedral-status` to the producer's group** and set the `logs`
   directory to `2775 polaris:polaris`. The producer keeps ownership and the
   publisher gains write access. This is additive but still a `chmod`/`chgrp`
   on a live directory, so it belongs in a maintenance window.
3. **Chown `logs` to `cathedral-status` and add the producer to that group.**
   Inverts the current owner on a directory a running service writes. Not
   recommended.

This is a design decision about which service owns the published evidence path,
not a packaging detail, so it is left open rather than chosen here.
