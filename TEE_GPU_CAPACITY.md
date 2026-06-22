# TEE GPU Capacity Lane

Status: publisher-only MVP. Default off. No validator update.

This lane records candidate Intel TDX + NVIDIA confidential-compute GPU capacity
and turns accepted records into an operator-controlled provider listing. It is a
revenue/ops pathway, not a Cathedral emissions pathway.

## What It Does

- Accepts signed miner offers at `POST /v1/tee-gpu/offers`.
- Accepts operator-created/imported offers at `POST /v1/admin/tee-gpu/capacity`.
- Stores inventory, preflight results, admin status, and provider handoff fields in
  `tee_gpu_capacity`.
- Exposes admin inventory, metrics, and provider listing commands.
- Optionally exposes a production-ready public catalog.

It never writes `eval_runs`, `lane_challenge_solves`, or `per_miner_solves`.
That is the guardrail that keeps this lane outside validator scoring.

## Why This Shape

Provider onboarding is operational. A GPU server still has to be provisioned
into the provider stack and added by an operator. The provider then runs its own
verification. Cathedral should not pretend a self-reported GPU offer is
cryptographic proof of usable confidential compute.

The first useful platform move is therefore:

1. collect supply,
2. preflight obvious bad claims,
3. verify fresh TDX + NVIDIA GPU evidence before production acceptance,
4. let an operator review operational readiness,
5. export or execute the exact provider listing handoff,
6. track status and revenue ops separately from emissions.

For live traffic, prefer running this on an operator-controlled publisher backed
by Postgres. If it is enabled on a SQLite scoring publisher, offer/admin writes
share the same SQLite write lock as validator-facing feed activity.

## Environment

```bash
CATHEDRAL_TEE_GPU_ENABLED=1
CATHEDRAL_TEE_GPU_ADMIN_TOKEN=<long random token>
CATHEDRAL_TEE_GPU_REQUIRE_INTAKE_CODE=1
CATHEDRAL_TEE_GPU_INTAKE_CODE=<shared invite code>
# Optional bypass for hand-picked hotkeys. Comma or newline separated.
CATHEDRAL_TEE_GPU_INTAKE_ALLOWLIST=5HotkeyA,5HotkeyB

# Optional
CATHEDRAL_TEE_GPU_PUBLIC_CATALOG_ENABLED=1
CATHEDRAL_TEE_GPU_REQUIRE_EVIDENCE=1
CATHEDRAL_TEE_GPU_REQUIRE_CRYPTO_EVIDENCE=1
CATHEDRAL_TEE_GPU_VERIFY_CMD="<tdx-and-nvidia-gpu-verifier-command>"
CATHEDRAL_TEE_GPU_VERIFY_TIMEOUT_SECS=120
CATHEDRAL_TEE_GPU_CHUTES_VALIDATOR_HOTKEY=5Dt7HZ7Zpw4DppPxFM7Ke3Cm7sDAWhsZXmM5ZAmE7dSVJbcQ
CATHEDRAL_TEE_GPU_CHUTES_MINER_API=http://127.0.0.1:32000
CATHEDRAL_TEE_GPU_CHUTES_HOTKEY_PATH=/path/to/chutes/hotkey
CATHEDRAL_TEE_GPU_CHUTES_CLI=chutes-miner
CATHEDRAL_TEE_GPU_CHUTES_TIMEOUT_SECS=120

# Required before Cathedral will execute chutes-miner instead of dry-running.
CATHEDRAL_TEE_GPU_CHUTES_EXECUTE_ENABLED=1
```

When `CATHEDRAL_TEE_GPU_ENABLED` is off, all routes return `404`.
When it is on but no intake code or allowlist is configured, public miner
offer and evidence-request routes fail closed with `503`.

For gated live intake, set `CATHEDRAL_TEE_GPU_REQUIRE_INTAKE_CODE=1` and either
configure `CATHEDRAL_TEE_GPU_INTAKE_CODE` or place specific miner hotkeys in
`CATHEDRAL_TEE_GPU_INTAKE_ALLOWLIST`. Without one of those, public offer and
evidence-request entry fail closed. The code gates entry into the intake lane
only; it is not hardware proof, provider acceptance, or payment authority.

## Exact Chutes Eligibility

There are two Chutes checks, and they are not the same:

- General `add-node` support comes from `GET https://api.chutes.ai/nodes/supported`.
- TEE acceptance comes from `GET https://api.chutes.ai/servers/tee/measurements`.

As checked on 2026-06-19, the general Chutes short refs are:

```text
3090, 4090, 5090, a10, a100, a100_40gb, a100_40gb_sxm, a100_sxm,
a40, a4000, a4000_ada, a5000, a6000, a6000_ada, b200, b300,
h20, h100, h100_nvl, h100_sxm, h200, h800, l4, l40, l40s,
mi300x, pro_6000
```

For this TEE provider lane, Cathedral only preflights the currently published
TEE measurement profiles:

```text
8x h200, 8x pro_6000, 8x b200, 8x b300
```

That means `h100` is Chutes-supported generally, but not a current public TEE
measurement profile. Do not treat an H100 offer as immediately listable for the
TEE revenue lane unless Chutes publishes an H100 measurement profile.

These profiles are **not open for purchase yet**. Before asking miners to buy
production hardware, keep the accepted list narrow:

- 8x H200
- 8x B200
- 8x B300
- 8x RTX Pro 6000 only after the provider measurement path is confirmed live

Do not recommend A6000, AMD GPUs, CPU-only TEE machines, non-TEE H100/A100/4090
machines, or any single-GPU shape unless the provider publishes and accepts that
exact TEE measurement profile.

The private Polaris/Chutes runbook confirms a second operational constraint:
Chutes expects a two-node topology, with a control-plane node and a separate GPU
worker cluster/node. The older single-node scripts are stale; the runbook later
records Helm release, NodePort, and PriorityClass collisions. In the offer,
`agent_api` must be the Chutes worker agent URL that serves `/config/kubeconfig`
after the `chutes-miner-gpu` chart is installed, normally
`http://<worker-ip>:32000`. It is not a generic miner web endpoint.

## Signed Miner Offer

`POST /v1/tee-gpu/offers`

Headers:

- `X-Cathedral-Hotkey`
- `X-Cathedral-Signature`
- `X-Cathedral-Submitted-At`

The signature uses the existing Cathedral canonical-claim bytes with:

- `bundle_hash = blake3(b"")`
- `card_id = "cathedral-tee-gpu-capacity-v1"`
- `challenge_id = node_id`
- `dimacs_solution_sha256 = sha256(canonical JSON request body)`

Canonical JSON means the parsed request object serialized with sorted keys,
compact separators, and UTF-8 bytes. In Python:

```python
json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
```

Minimum JSON:

```json
{
  "node_id": "miner-picked-stable-node-id",
  "intake_code": "operator-provided-code",
  "gpu_short_ref": "h200",
  "gpu_count": 8,
  "hourly_cost": 2.75,
  "agent_api": "http://203.0.113.10:32000",
  "tee_kind": "intel_tdx",
  "tdx_claimed": true,
  "gpu_cc_claimed": true,
  "operator_use_authorized": true
}
```

`intake_code` may also be supplied as `invite_code` or `access_code`. It is part
of the signed request body and is used only to gate live intake. It is not stored
in the capacity row and does not make the machine eligible or payable.
The shared intake code is a bearer gate. If access must be miner-bound, use
`CATHEDRAL_TEE_GPU_INTAKE_ALLOWLIST`; do not treat the shared code as identity
proof.

`hourly_cost` is the cost per GPU per hour, matching Chutes'
`chutes-miner add-node --hourly-cost` semantics. An 8x H200 offer at `2.75`
therefore represents `22.00` USD/hour of active listed capacity in Cathedral's
admin metrics.

New offers start as `pending`; only operator-approved `active` rows are listable.
Resubmits preserve the current operator-reviewed status unless the miner changes
a review-relevant field such as GPU shape, endpoint, TEE claims, or price; those
changes demote an active or paused row back to `pending`. A signed offer means
only that the hotkey made the claim. It does not mean the machine is real,
exclusive, TEE-valid, or earning.

If a previously listed row is demoted by a material resubmit, Cathedral marks the
provider handoff state as `needs_relisting`. Re-approval can then produce a fresh
provider handoff instead of silently treating the old listing as current.
Material admin edits to an active or paused row follow the same rule unless the
operator explicitly changes the status in the same update.

`operator_use_authorized=true` is required. It means the miner authorizes
Cathedral to use the machine for secure compute and mining workloads while the
capacity offer is active. The authorization flag is part of the signed request
body.

To list your own offers, sign the same canonical claim with:

- `challenge_id = "list"`
- `dimacs_solution_sha256 = ""`

Then call `GET /v1/tee-gpu/offers` with the same three headers.

## Admin Routes

Use `Authorization: Bearer <CATHEDRAL_TEE_GPU_ADMIN_TOKEN>`.

- `POST /v1/admin/tee-gpu/capacity`
- `PATCH /v1/admin/tee-gpu/capacity/{capacity_id}`
- `GET /v1/admin/tee-gpu/capacity`
- `GET /v1/admin/tee-gpu/metrics`
- `GET /v1/admin/tee-gpu/dashboard`
- `GET /v1/admin/tee-gpu/chutes-manifest?status=active`
- `POST /v1/admin/tee-gpu/capacity/{capacity_id}/attestation-review`
- `POST /v1/admin/tee-gpu/capacity/{capacity_id}/verify-evidence`
- `POST /v1/admin/tee-gpu/capacity/{capacity_id}/provider-status`
- `POST /v1/admin/tee-gpu/capacity/{capacity_id}/health-receipt`
- `POST /v1/admin/tee-gpu/capacity/{capacity_id}/usage-receipt`
- `POST /v1/admin/tee-gpu/capacity/{capacity_id}/chutes-list`

Allowed statuses:

- `pending`
- `active`
- `paused`
- `rejected`
- `retired`

Activation fails if deterministic preflight is blocked. Preflight is not
cryptographic attestation; it only rejects obvious non-starters such as non-TEE
GPU refs, GPU counts outside the current Chutes TEE measurement profiles,
missing Intel TDX claim, missing NVIDIA CC claim, bad GPU count, or a non-HTTP
agent API.

For lab intake, `CATHEDRAL_TEE_GPU_REQUIRE_EVIDENCE=1` allows an operator to
mark submitted evidence as `operator_reviewed`. That is not production proof.

For production hardware asks, set:

```bash
CATHEDRAL_TEE_GPU_REQUIRE_CRYPTO_EVIDENCE=1
CATHEDRAL_TEE_GPU_VERIFY_CMD="<tdx-and-nvidia-gpu-verifier-command>"
```

Then only `cryptographically_verified` evidence can pass preflight. Manual
`attestation-review` cannot create that status. The verifier endpoint runs the
configured command and requires explicit checks for:

- TDX quote verification
- NVIDIA GPU confidential-compute attestation verification
- claimed GPU model/count match against the Cathedral capacity row
- Cathedral evidence request/report binding
- debug-disabled state

Before exporting a Chutes command, set the Chutes inventory short name:

```bash
PATCH /v1/admin/tee-gpu/capacity/<capacity_id>
{
  "chutes_server_name": "<chutes-inventory-short-name>",
  "status": "active"
}
```

## Provider Listing Handoff

For a quick operator view, open:

```bash
GET /v1/admin/tee-gpu/dashboard
```

The dashboard keeps the workflow simple: miners list compute with Cathedral;
Cathedral reviews eligible, authorized capacity; Cathedral lists approved
machines into Chutes. It uses the same readiness checks as the API. Blocked or
non-consented rows are visible for review but do not emit a copy-paste command.

The public catalog shows only production-ready rows: cryptographically verified,
provider-listed, healthy, and usage/revenue observed. The admin manifest endpoint
defaults to active rows, but only command-ready rows emit a Chutes command.
Pending rows can be inspected with `status=pending`, but they do not emit
listing commands. Blocked rows are omitted unless `include_blocked=true`.

The endpoint always returns a structured handoff item. With
`include_blocked=true`, blocked rows are visible for review but are not command
ready. A copy-paste command is only included when the row is active, passed
preflight, operator use is authorized, cryptographic TDX/GPU evidence is
verified, and these fields are present:

- `chutes_server_name` on the capacity row
- `CATHEDRAL_TEE_GPU_CHUTES_HOTKEY_PATH` in the operator environment

Manual `attestation-review` is not enough for provider handoff when crypto mode
is required. The row must reach `evidence.status = cryptographically_verified`
through the configured verifier command.

Before executing the command, the operator must have already provisioned the
control plane and worker according to Chutes' two-node topology. The `agent_api`
value must point at the worker's Chutes agent, which is what `add-node` uses to
fetch that worker's kubeconfig.

When ready, the command is:

```bash
chutes-miner add-node \
  --name <server-name> \
  --validator <chutes-validator-hotkey> \
  --hourly-cost <per-gpu-hourly-cost> \
  --gpu-short-ref <gpu-short-ref> \
  --hotkey <chutes-hotkey-path> \
  --agent-api <agent-api> \
  --miner-api <miner-api>
```

Run this from the provider control plane after the worker node has been
provisioned into that provider's Kubernetes/K3s setup. The first implemented
provider backend is Chutes.

To run the pipeline from Cathedral, call:

```bash
POST /v1/admin/tee-gpu/capacity/<capacity_id>/chutes-list
{}
```

That records a dry-run audit event and returns the command. To immediately list
the node from an operator-controlled service, first install/configure
`chutes-miner`, set `CATHEDRAL_TEE_GPU_CHUTES_EXECUTE_ENABLED=1`, then call:

```bash
POST /v1/admin/tee-gpu/capacity/<capacity_id>/chutes-list
{
  "execute": true
}
```

On success Cathedral marks `chutes_status = "listed"` and `status = "active"`.
On failure it records stdout/stderr tail output in the audit event and sets
`chutes_status = "list_failed"`.

## Provider, Health, And Revenue Receipts

After cryptographic evidence verification, the operator records the real-world
launch evidence:

```bash
POST /v1/admin/tee-gpu/capacity/<capacity_id>/provider-status
{
  "provider_status": "listed",
  "server_id": "<provider-server-id>",
  "server_name": "<provider-server-name>",
  "receipt_id": "<provider-receipt-id>"
}
```

```bash
POST /v1/admin/tee-gpu/capacity/<capacity_id>/health-receipt
{
  "ok": true,
  "probe_url": "http://<worker-ip>:32000/health",
  "response_digest": "sha256:<digest>"
}
```

```bash
POST /v1/admin/tee-gpu/capacity/<capacity_id>/usage-receipt
{
  "receipt_id": "<usage-or-revenue-receipt-id>",
  "usage_seconds": 60,
  "revenue_usd": 0.01,
  "workload_count": 1
}
```

These routes are admin-only and fail closed unless the capacity is already
`cryptographically_verified` and preflight eligible. The admin API exposes
`launch_evidence.production_compute_ready=true` only after all three receipts
are present.

## Security Position

Do not pay for this table alone.

Miners can fake screenshots, `nvidia-smi`, cloud metadata, benchmark output,
and stale exports. Provider verification, cryptographically verified fresh
attestation evidence, live heartbeats, and actual revenue/usage receipts must be
the source of truth before this becomes a paid capacity market.

Pausing, rejecting, retiring, or revoking authorization on a Cathedral capacity
row does not yet remove an already-listed node from the provider. Until a
provider inventory importer and unlist pipeline exist, provider-side removal is
a manual operator step.

The current strict verifier hook is command-based. It is only as good as the
configured verifier. Do not ask miners to buy hardware until that verifier has
accepted at least one real machine and `launch_evidence.production_compute_ready`
is true from real provider, health, and usage/revenue receipts.
