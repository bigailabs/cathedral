# Miner Quickstart

This is the primary miner guide for the SAT launch path.

Cathedral uses your own infrastructure. Cathedral SSHs into your declared host, invokes Hermes, sends the active DIMACS CNF challenge, and reads your final answer from stdout. Your solver stays private.

## Status

The codebase includes the SAT lane. Mainnet SAT is disabled until operators deploy the feed and validators opt in to the signed weight path.

## Prerequisites

- Bittensor coldkey and hotkey registered on the target subnet.
- Linux host reachable by SSH from Cathedral.
- Dedicated unprivileged SSH user for Cathedral runs.
- Hermes installed and on `PATH` for that SSH user.
- A solver or wrapper available to Hermes on your host.
- Enough local CPU, memory, disk, and timeout budget for your solver.

You do not need to publish your solver source or upload a model by default.

## How Mining Works

1. Cathedral selects one active SAT formula.
2. All eligible miners race the same active formula.
3. Cathedral SSHs into each miner's host through the existing Hermes path.
4. Hermes receives a prompt containing a DIMACS CNF problem.
5. Your Hermes profile runs any private command, script, solver, or wrapper you choose.
6. Your run prints one final JSON answer.
7. Cathedral parses the DIMACS solution, checks every clause, and scores:
   - `1.0` for a valid satisfying assignment.
   - `0.0` for malformed, incomplete, contradictory, out-of-range, unsatisfied, or missing answers.
8. The first answer Cathedral verifies and locks wins the active challenge. Later answers for that challenge do not score.
9. The operator advances to the next formula.

Cathedral verifies the result, not your method.

## Migration From The Current Miner Path

Existing miners do not need to stop mining the agent pipeline while SAT is staged. Migration is additive:

1. Keep the current registered hotkey and agent submission path running.
2. Add a SAT wrapper on the same host or a separate Linux host.
3. Install Hermes for the SSH user Cathedral will invoke.
4. Dry-run the wrapper against toy DIMACS locally before exposing the host.
5. Register the host, SSH user, display name, hotkey, and hardware line with Cathedral operators.
6. Join SAT shadow rounds while `synthetic_boolean_v1` weight remains `0.0`.
7. Move to scored SAT rounds only after the feed, verifier, and signed-weight path are stable.

The miner contract is the public answer shape and the hotkey identity. Solver code, solver strategy, and infrastructure details stay private.

## Expected Wrapper Behavior

Your wrapper should:

- Read the CNF from the Hermes prompt.
- Run your solver privately.
- Preserve solver-style output.
- Return only one fenced `FINAL_ANSWER` JSON block.
- Cover every variable in the `v` lines.
- End DIMACS solution lines with `0`.

The accepted JSON shape is:

```json
{
  "dimacs_solution": "s SATISFIABLE\nv 1 -2 3 0\n"
}
```

The `dimacs_solution` value should look like normal SAT solver output:

```text
s SATISFIABLE
v 1 -2 3 0
```

Multiple `v` lines are allowed. Cathedral combines them during parsing.

## Answer Format

Print exactly one final answer block:

````text
```FINAL_ANSWER
{
  "dimacs_solution": "s SATISFIABLE\nv 1 -2 3 0\n"
}
```
````

Do not include explanations, markdown tables, logs, or extra JSON keys in the final answer.

## What Not To Submit

Do not submit:

- Solver source code.
- Raw formula files.
- Separate `.cnf`, `.dimacs`, or `.sol` files.
- Private corpus material.
- Your logs or infrastructure details.
- Multiple answer objects.
- An `assignment` dictionary. That draft shape is retired.

## Common Rejections

| Rejection | Meaning | Fix |
|---|---|---|
| `answer_missing_dimacs_solution` | Final JSON did not include `dimacs_solution`. | Return the exact key. |
| `answer_unexpected_keys` | Final JSON included extra keys. | Return only `dimacs_solution`. |
| `solution_unparseable` | DIMACS solver output could not be parsed. | Keep `s SATISFIABLE` and `v ... 0` lines. |
| `solution_incomplete_assignment` | Not every variable was assigned. | Emit a complete assignment. |
| `solution_variable_out_of_range` | Assignment referenced a variable outside the CNF range. | Check variable ids before returning. |
| `solution_unsatisfied` | At least one clause is false. | Re-run verification locally before printing. |
| `challenge_already_locked` | Another miner already solved this active challenge. | Wait for the next challenge. |

## Troubleshooting

- If Cathedral cannot connect, check SSH reachability, the SSH user, and firewall rules.
- If Hermes is not found, make sure `hermes` is on `PATH` for the SSH user, not only your login shell.
- If your run times out, make your wrapper fail fast and print no final answer unless it has a valid solution.
- If your answer parses locally but scores `0.0`, verify that every variable is assigned exactly once and every clause is satisfied.
- If you see no SAT challenge, the lane may not be enabled on the deployed publisher yet.

## Support

- Public site: <https://cathedral.computer>
- Publisher API: <https://api.cathedral.computer>
- Release state: [../../RELEASES.md](../../RELEASES.md)
