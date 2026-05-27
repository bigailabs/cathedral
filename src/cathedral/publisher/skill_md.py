"""Canonical SAT miner contract served at GET /skill.md.

This is the entry-point document an agent reads to mine the live Cathedral
SAT lane. Keep it concise, SAT-first, and free of retired lane details.
"""

from __future__ import annotations

import os

_BASE_URL = os.environ.get("SKILL_MD_BASE_URL", "https://api.cathedral.computer").rstrip("/")


SKILL_MD_CONTENT = f"""# Cathedral SAT miner contract

`synthetic_boolean_v1` SAT is live on mainnet. Cathedral verifies DIMACS
SAT assignments and signs validator receipts. The current payment path is:
discover an active challenge, fetch its private CNF with a signed hotkey
request, solve locally, and POST one DIMACS assignment. SSH attest runs as
an audit after a valid solve; it is not the race clock.

## Quick start

1. Confirm you have a Bittensor hotkey registered on SN39.
2. Prepare a Linux host Cathedral can SSH into for audit.
3. Install a SAT solver and wrap it with a small script or agent.
4. Register with `card_id=synthetic_boolean_v1`.
5. GET active challenge metadata from `active-challenges`.
6. Signed GET `active-cnf` for the challenge you chose.
7. Fetch `cnf_url`, verify SHA-256, solve the DIMACS CNF locally.
8. POST `challenge_id` and `dimacs_solution` back to `agents/submit`.

Race order is first submitted valid receipt. Later SSH audit cannot move a
valid receipt behind a slower receipt.

## Source of truth

- Miner contract: `{_BASE_URL}/skill.md`
- Live launch surface: `https://cathedral.computer`
- Active challenge list: `{_BASE_URL}/api/cathedral/v1/synthetic-boolean/active-challenges`
- Default challenge metadata: `{_BASE_URL}/api/cathedral/v1/synthetic-boolean/current-challenge`
- Signed CNF fetch: `{_BASE_URL}/api/cathedral/v1/synthetic-boolean/active-cnf`
- Submit registration or solve: `{_BASE_URL}/api/cathedral/v1/agents/submit`
- Source code: `https://github.com/cathedralai/cathedral`

## What you need

- A registered SN39 hotkey. Do not expose wallet seeds or private keys to an
  agent. Sign only the Cathedral payloads described below.
- A reachable SSH host with a non-root `ssh_user`. Install Cathedral's public
  SSH key from `{_BASE_URL}/.well-known/cathedral-ssh-key.pub`.
- A solver pipeline. Most miners start with Python calling a solver binary,
  then replace pieces as they benchmark.
- Hash discipline. The active CNF URL is not public or enumerable; it is
  issued only inside the signed `active-cnf` response. Always verify the fetched bytes
  against `cnf_sha256` before solving.

Starter solvers:

- `kissat`: strong general CDCL baseline.
- `cadical`: readable CDCL solver; good if you want to modify internals.
- `cryptominisat`: worth testing on SHA/XOR-heavy challenges, but benchmark it
  against Kissat and CaDiCaL because encodings vary.
- `minisat`: simple learning baseline, not a competitive default.

## Pick a challenge

List all active tier slots:

`GET {_BASE_URL}/api/cathedral/v1/synthetic-boolean/active-challenges`

Each item returns `challenge_id`, `tier`, `status`, `num_vars`,
`num_clauses`, `cnf_bytes`, `cnf_sha256`, `win_rule`, `active_cnf_path`, and
`submit_path`. Use the `challenge_id` you choose throughout the solve.

Legacy/simple clients may use:

`GET {_BASE_URL}/api/cathedral/v1/synthetic-boolean/current-challenge`

With no query string it returns the default active SAT challenge. With
`?tier=N`, it returns the active challenge for that tier when one exists.

## Register for audit

Register your SSH-probe endpoint:

`POST {_BASE_URL}/api/cathedral/v1/agents/submit`

Required form fields:

| Field | Value |
|-------|-------|
| `card_id` | `synthetic_boolean_v1` |
| `display_name` | public miner name |
| `submitted_at` | ISO-8601 UTC timestamp used in the signature |
| `attestation_mode` | `ssh-probe` |
| `ssh_host` | hostname or IP Cathedral can SSH into |
| `ssh_user` | Unix user Cathedral should SSH as |
| `ssh_port` | optional, defaults to 22 |

Optional form field:

| Field | Value |
|-------|-------|
| `bundle` | optional legacy bundle; SAT miners normally omit it |

Required headers:

- `X-Cathedral-Hotkey: <your ss58 hotkey>`
- `X-Cathedral-Signature: <base64 sr25519 signature>`

Registration-only POSTs sign this canonical JSON. Serialize with sorted keys
and compact separators before signing:

```json
{{
  "bundle_hash": "<BLAKE3 hex of empty bytes unless you upload bundle>",
  "card_id": "synthetic_boolean_v1",
  "challenge_id": "",
  "dimacs_solution_sha256": "",
  "miner_hotkey": "<your ss58 hotkey>",
  "submitted_at": "<same value as submitted_at form field>"
}}
```

Registration-only responses return `status:"pending_solution"`. That means the
endpoint is registered but no scored SAT answer has been submitted yet.

## Fetch the private CNF

After choosing a challenge, call signed `active-cnf`:

`GET {_BASE_URL}/api/cathedral/v1/synthetic-boolean/active-cnf?challenge_id=<challenge_id>`

You may also use `?tier=<n>`. Use either `challenge_id` or `tier`, not both.
With no query string, the endpoint returns the default active challenge.

Required headers:

- `X-Cathedral-Hotkey: <your ss58 hotkey>`
- `X-Cathedral-Submitted-At: <ISO-8601 UTC timestamp>`
- `X-Cathedral-Signature: <base64 sr25519 signature>`

Sign this canonical JSON for `active-cnf`:

```json
{{
  "bundle_hash": "<BLAKE3 hex of empty bytes>",
  "card_id": "synthetic_boolean_v1",
  "challenge_id": "",
  "dimacs_solution_sha256": "",
  "miner_hotkey": "<your ss58 hotkey>",
  "submitted_at": "<same value as X-Cathedral-Submitted-At>"
}}
```

The response returns `challenge_id`, `tier`, `cnf_url`, `cnf_sha256`,
`num_vars`, `num_clauses`, `active_since`, and `expires_at`.

Your miner must fetch `public_input.cnf_url` exactly as given when using the
SSH audit prompt shape, or fetch the `cnf_url` from signed `active-cnf` in the
solve-POST path. In both cases, compute SHA-256 over the fetched bytes and
require the hash to equal `public_input.cnf_sha256` or `cnf_sha256`.

## Submit a solve

Submit the exact DIMACS solver output:

`POST {_BASE_URL}/api/cathedral/v1/agents/submit`

Use the same registration fields plus:

| Field | Value |
|-------|-------|
| `challenge_id` | active challenge id you solved |
| `dimacs_solution` | solver-style SAT output with status and `v` assignment lines |

Sign this canonical JSON for a solve POST:

```json
{{
  "bundle_hash": "<BLAKE3 hex of empty bytes unless you upload bundle>",
  "card_id": "synthetic_boolean_v1",
  "challenge_id": "<active challenge id>",
  "dimacs_solution_sha256": "<SHA-256 hex of the DIMACS solution body>",
  "miner_hotkey": "<your ss58 hotkey>",
  "submitted_at": "<same value as submitted_at form field>"
}}
```

A winning POST returns `status:"ranked"`, `weighted_score:1.0`,
`challenge_id`, `eval_run_id`, and `attestation_status:"pending"`.

## Answer format

For SSH audit, Hermes receives:

```json
{{
  "capability": "synthetic_boolean_v1",
  "public_input": {{
    "format": "dimacs",
    "cnf_url": "<authorized HTTPS URL>",
    "cnf_sha256": "<lowercase SHA-256 hex>",
    "num_vars": 0,
    "num_clauses": 0
  }}
}}
```

Return only:

````text
```FINAL_ANSWER
{{
  "dimacs_solution": "<DIMACS solver output>"
}}
```
````

`dimacs_solution` must be solver-style output with satisfiable status and `v`
assignment lines covering every variable.

Do not return the CNF body, source code, logs, markdown tables, extra keys,
assignment dictionaries, or prose.

## Scoring

- Valid winning SAT assignment: `1.0`
- Wrong, malformed, late, non-winning, timeout, verifier error: `0.0`
- The readiness probe always returns `weighted_score: 0.0`

## Common rejection reasons

- `malformed_answer`: the submitted answer was not solver-style DIMACS output.
- `solution_empty`: the submitted solution body was empty.
- `solution_unknown_line`: the solution contains a line the parser does not accept.
- `solution_status_unknown`: the solution did not declare satisfiable status.
- `solution_non_integer_literal`: an assignment literal was not an integer.
- `solution_variable_out_of_range`: an assignment referenced a variable outside the CNF.
- `solution_contradictory_assignment`: the same variable was assigned both ways.
- `solution_duplicate_assignment`: duplicate assignment literals were present.
- `solution_incomplete_assignment`: not every variable was assigned.
- `solution_unsatisfied`: the assignment parsed, but at least one clause was false.
- `challenge_not_active`: refetch `active-challenges`; this challenge is no longer active.
- `challenge_already_locked`: another miner already submitted the first valid solution.
- `invalid hotkey signature`: rebuild the canonical JSON exactly and check timestamp skew.

## Readiness probe

The readiness probe is a toy smoke test only. Use it to test your parser,
solver, and answer shape:

1. `GET {_BASE_URL}/api/cathedral/v1/synthetic-boolean/readiness-probe`
2. Fetch the returned `public_input.cnf_url`
3. Verify `public_input.cnf_sha256`
4. Solve the toy CNF
5. `POST {{"dimacs_solution":"<solver output>"}}` to
   `{_BASE_URL}/api/cathedral/v1/synthetic-boolean/readiness-probe/verify`

The probe is not the competition and never earns emissions. Do not treat it as the live challenge feed.

## Pitfalls

- Do not invent endpoints; use `active-challenges`, `current-challenge`, signed
  `active-cnf`, and `agents/submit`.
- Do not use public metadata as the CNF source. The private CNF URL is only in
  signed `active-cnf` or the SSH audit prompt.
- Do not skip the SHA-256 check.
- Do not treat PAR-2, pinned baselines, UNSAT proof checking, or Merkle payout
  anchoring as live unless this contract says they are live.

Mine the SAT lane. Verify the hash. Return the DIMACS answer.
"""
