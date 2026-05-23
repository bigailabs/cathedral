# Miner Quickstart

This is the SAT miner contract for `synthetic_boolean_v1`.

## Status

- SAT mainnet weight is `0.0`.
- Shadow rounds may run.
- Scored rounds need a signed policy release.

## You Need

- registered Bittensor hotkey
- Linux SSH host
- dedicated SSH user
- Hermes on `PATH`
- private solver or wrapper

You do not publish solver source.

## Flow

1. Cathedral picks one active CNF.
2. Eligible miners race it.
3. Cathedral SSHs into your host.
4. Hermes gives your wrapper:
   - `public_input.cnf_url`
   - `public_input.cnf_sha256`
5. Your wrapper fetches the CNF.
6. Your wrapper checks SHA-256.
7. Your solver runs privately.
8. Your wrapper prints one final answer.
9. Cathedral verifies every clause.
10. First submitted valid answer wins.

## Final Answer

Print exactly:

````text
```FINAL_ANSWER
{
  "dimacs_solution": "s SATISFIABLE\nv 1 -2 3 0\n"
}
```
````

Rules:

- one JSON key only
- key is `dimacs_solution`
- value is SAT solver output
- include `s SATISFIABLE`
- assign every variable
- end `v` lines with `0`
- no logs
- no explanations
- no extra keys

## Score

| Case | Score |
|---|---:|
| First valid satisfying assignment | `1.0` |
| Invalid answer | `0.0` |
| Malformed answer | `0.0` |
| Incomplete assignment | `0.0` |
| Late after challenge lock | `0.0` |

## Common Rejections

| Rejection | Meaning |
|---|---|
| `answer_missing_dimacs_solution` | Missing required key. |
| `answer_unexpected_keys` | Extra key returned. |
| `solution_unparseable` | DIMACS could not be parsed. |
| `solution_incomplete_assignment` | Not every variable was assigned. |
| `solution_variable_out_of_range` | Variable id is outside the CNF. |
| `solution_unsatisfied` | At least one clause is false. |
| `challenge_already_locked` | Another miner already won. |

## Private

Keep private:

- solver source
- wrapper internals
- logs
- raw CNFs
- raw solutions
- benchmark notes

Public miner surface:

- hotkey
- host reachability
- hardware line
- final answer format
