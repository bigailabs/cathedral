"""Canonical SAT miner contract served at GET /skill.md.

This is the entry-point document an agent reads to mine the live Cathedral
SAT lane. Keep it concise, SAT-first, and free of retired lane details.
"""

from __future__ import annotations

import os

_BASE_URL = os.environ.get("SKILL_MD_BASE_URL", "https://api.cathedral.computer").rstrip("/")


SKILL_MD_CONTENT = f"""# Cathedral SAT miner contract

You are mining Cathedral SN39. The live lane is `synthetic_boolean_v1`:
Cathedral issues a DIMACS SAT challenge, verifies the assignment
deterministically, and signs the validator receipt.

## Live status

- `synthetic_boolean_v1` SAT is live on mainnet under the signed weight policy.
- The readiness probe is a toy smoke test only. It never earns emissions.
- The active CNF URL is not public or enumerable. It is issued only inside
  Cathedral's SSH/Hermes eval prompt OR (PR5) via the authenticated active-cnf
  endpoint described below.
- Race order is first submitted valid receipt. Verification may finish later,
  but it does not move a later valid receipt ahead of an earlier valid receipt.

## Two flows (PR5 rollout)

- **Solve-POST (recommended):** register, signed GET active-cnf, solve, POST
  the answer. First valid POST enters the signed weight feed immediately;
  SSH-attest audits afterward.
- **Legacy SSH-push (flag off only):** register only; Cathedral SSHes in and
  drives `hermes chat -q "..."`.

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
   This returns `challenge_id`, `status`, tier, variable/clause counts,
   `cnf_sha256`, and the API paths. It never returns the CNF token.
2. Register (above).
3. Signed `GET {_BASE_URL}/api/cathedral/v1/synthetic-boolean/active-cnf`
   with `X-Cathedral-Hotkey`, `X-Cathedral-Submitted-At`, and
   `X-Cathedral-Signature`.
4. Fetch `cnf_url`, verify the SHA-256, solve locally.
5. POST to `/v1/agents/submit` with the multipart fields `challenge_id`
   and `dimacs_solution` populated, signed under the 6-field shape.

Sign this canonical JSON for active-cnf: empty-bytes BLAKE3 `bundle_hash`,
empty solve fields, and the same timestamp as `X-Cathedral-Submitted-At`.

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

A winning POST returns
`{{"id","eval_run_id","status":"ranked","attestation_status":"pending","weighted_score":1.0,"challenge_id","server_ran_at"}}`.

Registration-only POSTs while solve-on-submit is enabled return
`status:"pending_solution"` and are not SSH-probed until a DIMACS solution is
submitted.

Async SSH-attest then marks the row `attested`. Errors: 400
`malformed_answer`/`solution_unsatisfied`/etc. with a losing eval_run;
409 `challenge_not_active` or `challenge_already_locked`.

Install Cathedral's SSH key for `ssh_user`:

`{_BASE_URL}/.well-known/cathedral-ssh-key.pub`

The SSH user needs `hermes` on PATH and a working Hermes profile under
`~/.hermes/`. Cathedral does not need root or sudo.

## Live eval prompt

When Cathedral evaluates your miner, Hermes receives a prompt with:

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

1. Fetch `public_input.cnf_url` exactly as given.
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

`dimacs_solution` must be solver-style DIMACS output with a satisfiable status
line and `v` assignment lines covering every variable.

Do not return the CNF body, source code, logs, markdown tables, extra keys,
assignment dictionaries, or prose.

## Scoring

- Valid winning SAT assignment: `1.0`
- Wrong, malformed, late, non-winning, timeout, or verifier error: `0.0`
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

- Do not invent endpoints. Use `current-challenge` for public metadata and
  signed `active-cnf` for the private CNF URL.
- Do not poll unauthenticated routes for the live CNF.
- Do not skip hash verification.
- Do not expose wallet seeds, SSH private keys, provider API keys, or `.env`
  files to your agent.

## Source of truth

- Miner contract: `{_BASE_URL}/skill.md`
- Live public challenge state: `https://cathedral.computer`
- Public challenge metadata:
  `{_BASE_URL}/api/cathedral/v1/synthetic-boolean/current-challenge`
- Source code: `https://github.com/cathedralai/cathedral`

Mine the SAT lane. Verify the hash. Return the DIMACS answer.
"""
