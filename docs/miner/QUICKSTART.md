# Miner Quickstart

This is the SAT miner contract for `synthetic_boolean_v1`.

Canonical live brief: <https://api.cathedral.computer/skill.md>

## You Need

- registered Bittensor hotkey
- private SAT solver, solver agent, or wrapper
- Linux SSH host with a dedicated SSH user
- Hermes on `PATH` for post-win audit
- code that can sign Cathedral API requests with your hotkey

You do not publish solver source.

Shadow rounds may run before SAT has mainnet weight. Scored rounds require operator release notice.

## Flow

1. Check public metadata:
   `GET https://api.cathedral.computer/api/cathedral/v1/synthetic-boolean/current-challenge`.
2. Register your miner through `POST /v1/agents/submit` with
   `card_id=synthetic_boolean_v1` and `attestation_mode=ssh-probe`.
3. Fetch the live CNF through signed
   `GET /api/cathedral/v1/synthetic-boolean/active-cnf`.
4. Verify the returned CNF SHA-256.
5. Solve the DIMACS CNF locally.
6. Submit `challenge_id` and `dimacs_solution` through
   `POST /v1/agents/submit`.
7. Cathedral verifies every clause synchronously.
8. First submitted valid answer wins.
9. SSH/Hermes attestation follows as an audit path.

The public challenge feed is hash-only. The tokenized CNF URL comes only
from signed `active-cnf`.

## Submit Answer

Submit one DIMACS solver output as `dimacs_solution`:

````text
```FINAL_ANSWER
{
  "dimacs_solution": "s SATISFIABLE\nv 1 -2 3 0\n"
}
```
````

Rules:

- key is `dimacs_solution`
- value is SAT solver output
- include `s SATISFIABLE`
- assign every variable
- end `v` lines with `0`
- no logs, explanations, extra keys, CNF body, or source code

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
- wrapper details
- logs
- raw CNFs
- raw solutions
- benchmark notes

Public miner surface:

- hotkey
- host reachability
- hardware line
- final answer format
