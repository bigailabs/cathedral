# Migrating Miners To SAT

This guide is for current Cathedral miners who want to prepare for `synthetic_boolean_v1`.

SAT migration is additive. Keep the current agent pipeline running while SAT is staged.

## Current State

- The live production path remains the agent pipeline.
- The SAT lane runs through Cathedral's SSH/Hermes execution path.
- Mainnet SAT weight is `0.0` until operators enable the lane and intentionally change validator-local SAT weighting.
- Validators verify Cathedral signatures over eval rows. They do not receive raw formulas.

## What Changes For Miners

Current path:

1. Submit an agent or Polaris bundle identifier.
2. Cathedral evaluates the agent path.
3. Validators pull signed rows and set weights.

SAT path:

1. Register the same hotkey for SAT.
2. Provide a reachable Linux host and SSH user.
3. Install Hermes and your private solver wrapper on that host.
4. Cathedral sends one DIMACS challenge through Hermes.
5. Your wrapper prints one `FINAL_ANSWER` JSON block with `dimacs_solution`.
6. Cathedral verifies the answer and signs the public row.

## Migration Steps

1. Keep the existing miner path live.
2. Pick the host that will run SAT work.
3. Create a dedicated unprivileged SSH user for Cathedral.
4. Install Hermes for that user.
5. Put your solver or portfolio wrapper on the host.
6. Test the wrapper locally with toy DIMACS input.
7. Register display name, hotkey, host, SSH user, and hardware line with Cathedral operators.
8. Join shadow rounds while SAT weight remains `0.0`.
9. Move into scored rounds after the feed, verifier, and validator-local weight path are stable.

## Wrapper Contract

Your wrapper returns exactly one final block:

````text
```FINAL_ANSWER
{
  "dimacs_solution": "s SATISFIABLE\nv 1 -2 3 0\n"
}
```
````

Do not return solver source, raw formula files, logs, markdown tables, or extra JSON keys.

## What Stays Private

- Solver source.
- Solver strategy.
- Benchmark notes.
- Infrastructure details beyond the public hardware line.
- Raw formula files.
- Raw solution files.
- Private logs.

## Operator Checklist

- Confirm SSH reachability.
- Confirm Hermes is on `PATH`.
- Confirm the wrapper handles timeout and malformed input.
- Confirm toy DIMACS succeeds locally.
- Confirm public rows are hash-only.
- Confirm the miner understands that shadow SAT rounds do not move mainnet weight.
