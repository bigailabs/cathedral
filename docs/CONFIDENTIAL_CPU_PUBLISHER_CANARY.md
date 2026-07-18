# Confidential CPU publisher acceptance canary

This local canary proves the complete confidential CPU reward path without
connecting to a chain or touching a deployed publisher:

```text
exact producer report bytes
  -> dedicated bearer + raw-body HMAC intake
  -> fresh registered-hotkey filter
  -> confidential-primary signed vector
  -> Ed25519 verification + rollback fence
  -> confidential_primary_v1 policy pin
  -> hotkey-to-UID mapping
  -> later complete empty report
  -> signed report digest/epoch bound to that revocation
  -> prior miner mass revoked to the signed burn UID
  -> older report and vector replay rejected
```

The command requires two fresh, complete reports from the confidential scorer.
It does not change their timestamps or synthesize revocation evidence. The
second report must have a newer integer epoch and an empty `scores` list. Both
reports must explicitly name the exact `network` and `netuid` supplied to the
command; missing or cross-subnet audience fields fail before intake.

```bash
python scripts/confidential_cpu_publisher_canary.py \
  --positive-report /path/to/fresh-positive-report.json \
  --revoke-report /path/to/fresh-revoke-report.json \
  --uid-map '5RegisteredWorkerHotkey=17' \
  --network finney \
  --netuid 39 \
  --burn-uid 0
```

Repeat `--uid-map` for every positive hotkey that the supplied metagraph
snapshot says is currently registered. A positive report hotkey omitted from
the map is treated as an unregistered control and must be absent from the signed
vector. At least one positive hotkey must map successfully.

The parent launches the canary in a clean child process with Python isolated
mode and automatic `site` startup disabled (`-I -S`), plus an explicit bootstrap
environment. That prevents startup-time `sitecustomize` and `.pth` execution.
Installed dependency directories are added directly, without `site.addsitedir`,
only after the child replaces its environment with the canary allowlist.
Publisher modules are first imported after those steps. The child creates a
temporary SQLite database and blob directory plus ephemeral Ed25519 signing
key, source bearer token, source HMAC secret, and local CNF secrets. It builds
the publisher in submit-only mode, so read-side background workers never start.

The child installs a Python audit hook before publisher imports. It blocks
network-address socket creation, every socket connect/send path (including Unix
domain sockets), and process-launch escape paths. Local AF_UNIX socketpairs used
inside TestClient remain allowed because they have no connect/send operation.
On macOS the child is also launched under an OS `sandbox-exec` policy that
denies every network operation.
On platforms without that OS facility, the JSON says
`unavailable-python-guard-only`; the Python guard is deliberately reported as a
bounded application canary, not an OS security boundary. No untrusted code is
executed by this command.

The database is explicitly closed, the publisher's process-global CNF-store
registration is removed, app-held private state is cleared, and the whole
temporary root is verified absent before success is reported. The child then
exits, discarding its audit hook and any remaining imported module state.

The final JSON contains input digests, accepted publisher digests, vector IDs,
monotonic policy versions, registered/unregistered filter results, the exact
`epoch_too_old` replay verdict, proof that the latest revoke row was unchanged,
cleanup state, and positive/revoked UID weights. The command checks
that none of its generated signing, bearer, or HMAC values appear in that
evidence.

This is a code-level acceptance gate. Launch acceptance additionally requires a
fresh report naming a currently registered target-subnet hotkey and a separately
authorized on-chain positive-then-zero observation.
