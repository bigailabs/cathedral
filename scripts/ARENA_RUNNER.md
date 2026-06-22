# Arena Runner Operator Notes

`CATHEDRAL_ARENA_RUNNER_CMD` is the production seam between the Cathedral
publisher and an operator-owned solver runner. The publisher creates a temporary
CNF, writes a manifest JSON file, runs the configured command under the sandbox,
and parses normal DIMACS solver output from stdout.

## Safe Default

Prefer the manifest interface:

```bash
export CATHEDRAL_ARENA_REQUIRE_CONTAINMENT=1
export CATHEDRAL_ARENA_RUNNER_CMD='/opt/cathedral/bin/arena-runner --manifest {manifest_path}'
```

The manifest contains:

```json
{
  "schema_version": 1,
  "cnf_path": "/tmp/.../problem.cnf",
  "cnf_sha256": "...",
  "timeout_ms": 5000,
  "timeout_s": 5.0,
  "solver": {
    "source_url": "...",
    "container_digest": "sha256:...",
    "source_sha256": "...",
    "owner_hotkey": "...",
    "commitment_id": "..."
  }
}
```

The wrapper should treat every manifest field as untrusted miner input. Pin by
`container_digest`, enforce a local allow/pull policy, disable network during the
solve, respect the timeout, and print only DIMACS-style solver output:

```text
s SATISFIABLE
v 1 -2 3 0
```

or:

```text
s UNSATISFIABLE
```

## Direct Placeholder Rules

Supported placeholders are:

- `{manifest_path}`
- `{cnf_path}`
- `{timeout_ms}`
- `{timeout_s}`
- `{container_digest}`
- `{source_url}`
- `{source_sha256}`
- `{owner_hotkey}`

Direct miner placeholders are rejected as bare argv tokens before a literal `--`.
This is unsafe:

```bash
CATHEDRAL_ARENA_RUNNER_CMD='/opt/runner {source_url} {cnf_path}'
```

This is allowed, but the wrapper must deliberately parse positional args after
`--` as data:

```bash
CATHEDRAL_ARENA_RUNNER_CMD='/opt/runner -- {source_url} {container_digest} {owner_hotkey} {cnf_path}'
```

This is also allowed because the placeholder is embedded in an operator-owned
option token:

```bash
CATHEDRAL_ARENA_RUNNER_CMD='/opt/runner --digest={container_digest} --manifest {manifest_path}'
```

## Failure Semantics

- No runner config means no adapter; solvers remain pending.
- Unsafe or unknown placeholders mean no adapter; solvers remain pending.
- Spawn failure, non-zero runner exit, malformed output, or no solver output is a
  non-solve.
- Host-observed timeout overrides any solver claim.
- The publisher measures wall time; miner output cannot set elapsed time.

Do not configure this command to pull arbitrary tags, mount host paths, expose
host networking, or trust the miner's `source_url` without an explicit policy.
