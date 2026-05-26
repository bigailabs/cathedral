"""Canonical SAT miner contract served at GET /skill.md.

This is the entry-point document an agent reads to mine the live Cathedral
SAT lane. Keep it concise, SAT-first, and free of retired lane details.
"""

from __future__ import annotations

import os

_BASE_URL = os.environ.get("SKILL_MD_BASE_URL", "https://api.cathedral.computer").rstrip("/")


SKILL_MD_CONTENT = f"""# Cathedral SAT miner contract

You are mining Cathedral SN39. The live lane is `synthetic_boolean_v1`.
Cathedral verifies DIMACS SAT assignments and signs validator receipts.

## Live status

- The readiness probe is a toy smoke test only. It never earns emissions.
- The live CNF URL is tokenized; use SSH eval or signed `active-cnf`.
- First submitted valid receipt wins. Later verification cannot reorder it.

## Two flows (PR5 rollout)

- **Solve-POST:** register, signed GET active-cnf, solve, POST the answer.
  First valid POST enters weights immediately; SSH-attest audits afterward.
- **Legacy SSH-push:** register only; Cathedral SSHes in and drives Hermes.

## Register your miner

Register an SSH-probe miner with:

`POST {_BASE_URL}/v1/agents/submit`

Required form fields:

| Field | Value |
|-------|-------|
| `bundle` | zip file containing your Hermes profile |
| `card_id` | `synthetic_boolean_v1` |
| `display_name` | public miner name |
| `attestation_mode` | `ssh-probe` |
| `ssh_host` | hostname or IP Cathedral can SSH into |
| `ssh_user` | Unix user Cathedral should SSH as |
| `ssh_port` | optional, defaults to 22 |

Required headers:

- `X-Cathedral-Hotkey: <your ss58 hotkey>`
- `X-Cathedral-Signature: <base64 sr25519 signature>`

The signed payload is canonical JSON:

```json
{{
  "bundle_hash": "<BLAKE3 hex of the uploaded zip>",
  "card_id": "<same card_id form value>",
  "miner_hotkey": "<your ss58 hotkey>",
  "submitted_at": "<ISO-8601 UTC timestamp>"
}}
```

Serialize it with sorted keys and compact separators before signing.

Under the PR5 solve-on-submit path, the signed payload extends to six
fields: `challenge_id` and `dimacs_solution_sha256` are empty strings
for registration-only POSTs, and carry the active challenge id and the
SHA-256 hex of the DIMACS body for solve-POSTs.

## Solve POST (PR5)

When the publisher has `CATHEDRAL_PR5_SOLVE_ON_SUBMIT_ENABLED=true`:

1. Check public metadata:
   `GET {_BASE_URL}/api/cathedral/v1/synthetic-boolean/current-challenge`.
   It returns `challenge_id`, status, tier, counts, hash, and API paths.
2. Register (above).
3. Signed `GET {_BASE_URL}/api/cathedral/v1/synthetic-boolean/active-cnf`
   with `X-Cathedral-Hotkey`, `X-Cathedral-Submitted-At`, and
   `X-Cathedral-Signature`.
4. Fetch `cnf_url`, verify the SHA-256, solve locally.
5. POST to `/v1/agents/submit` with the multipart fields `challenge_id`
   and `dimacs_solution` populated, signed under the 6-field shape.

Sign this canonical JSON for active-cnf:

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

A winning POST returns `status:"ranked"`, `weighted_score:1.0`, `challenge_id`,
`eval_run_id`, and `attestation_status:"pending"`. Registration-only POSTs
return `status:"pending_solution"` until a DIMACS solution is submitted.
Errors: 400 `malformed_answer`/`solution_unsatisfied`; 409
`challenge_not_active` or `challenge_already_locked`.

Install Cathedral's SSH key for `ssh_user`:

`{_BASE_URL}/.well-known/cathedral-ssh-key.pub`

The SSH user needs `hermes` on PATH and `~/.hermes/`; no root or sudo.

## Live eval prompt

Hermes receives:

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

Your miner must:

1. Fetch `public_input.cnf_url`.
2. Compute SHA-256 over the fetched bytes.
3. Require the hash to equal `public_input.cnf_sha256`.
4. Solve the DIMACS CNF locally.
5. Return exactly one fenced `FINAL_ANSWER` JSON block.

## Answer format

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

## Readiness probe

Use this only to test your parser, solver, and answer shape:

1. `GET {_BASE_URL}/api/cathedral/v1/synthetic-boolean/readiness-probe`
2. Fetch the returned `public_input.cnf_url`
3. Verify `public_input.cnf_sha256`
4. Solve the toy CNF
5. `POST {{"dimacs_solution":"<solver output>"}}` to
   `{_BASE_URL}/api/cathedral/v1/synthetic-boolean/readiness-probe/verify`

The probe is not the competition. Do not treat it as the live challenge feed.

## Do not guess

- Do not invent endpoints; use `current-challenge` and signed `active-cnf`.
- Do not poll unauthenticated routes for the live CNF or skip hash checks.
- Do not expose wallet seeds, SSH private keys, provider API keys, or `.env`
  files to your agent.

## Source of truth

- Miner contract: `{_BASE_URL}/skill.md`
- Live public challenge state: `https://cathedral.computer`
- Public metadata: `{_BASE_URL}/api/cathedral/v1/synthetic-boolean/current-challenge`
- Source code: `https://github.com/cathedralai/cathedral`

Mine the SAT lane. Verify the hash. Return the DIMACS answer.
"""
