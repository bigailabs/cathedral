# AGENTS.md

This repository has two very different surfaces:

- `validator` / `miner` / subnet infrastructure
- Cathedral cloud customer operations via the `cathedral` CLI and Python SDK

If the user is asking how to **use Cathedral as a customer/operator** for compute, billing, rentals, deployments, inference, or OpenClaw-style apps, ignore most of the subnet docs and start here:

- CLI source: `crates/cathedral-cli/src/`
- Python SDK: `crates/cathedral-sdk-python/`
- user docs: `crates/cathedral-sdk-python/README.md`, `examples/`, `docs/GETTING-STARTED.md`

## What To Use

Use these repo-local skills for agent work:

- `cathedral/.claude/skills/cathedral-account-ops/SKILL.md`
  - login, device-code auth, API tokens, balance, TAO funding, deposit tracking
- `cathedral/.claude/skills/cathedral-rentals-ops/SKILL.md`
  - machine discovery, SSH keys, direct rentals, volumes, copy/exec/ssh, teardown
- `cathedral/.claude/skills/cathedral-serverless-ops/SKILL.md`
  - `cathedral deploy`, `summon`, vLLM, SGLang, OpenClaw, Tau, scale/logs/share-token
- `cathedral/.claude/skills/cathedral-sdk-ops/SKILL.md`
  - Python automation, SDK caveats, blocking deploy semantics, low-level API gaps

For a single end-to-end playbook with copyable flows, use:

- `cathedral/docs/agent-cloud-ops.md`
  - auth, funding, rentals, deploys, inference, OpenClaw, Tau, SDK, cleanup

## Routing

If the request is about:

- account setup, credits, deposits, or top-up: use `cathedral-account-ops`
- direct GPU/CPU machines, SSH access, training boxes, or persistent rented hosts: use `cathedral-rentals-ops`
- HTTP services, inference endpoints, public URLs, or self-hosted apps: use `cathedral-serverless-ops`
- Python code, notebooks, scripts, CI automation, or typed programmatic access: use `cathedral-sdk-ops`

## Canonical Agent Rules

### 1. Pick the right control plane

- Prefer the CLI for interactive operator workflows.
- Prefer the Python SDK for repeatable automation and code generation.
- Prefer direct rentals over serverless deploys when the workload needs SSH, custom system setup, distributed training, or huge models that may take too long to pass deployment health checks.

### 2. Use the real auth path

- For CLI usage, the canonical path is:

```bash
curl -sSL https://basilica.ai/install.sh | bash
cathedral login
```

- For headless terminals, SSH boxes, or remote shells, use:

```bash
cathedral login --device-code
```

- Use API tokens mainly for SDK/programmatic work:

```bash
cathedral tokens create my-agent-token
export BASILICA_API_TOKEN="cathedral_..."
```

### 3. Treat cost-bearing actions as explicit

Do not create chargeable resources unless the user asked for it. These usually incur cost:

- `cathedral up ...`
- `cathedral deploy ...`
- `cathedral deploy vllm ...`
- `cathedral deploy sglang ...`
- `cathedral summon ...`
- SDK equivalents like `start_secure_cloud_rental()` and `deploy()`

Safe read-mostly operations include:

- `cathedral balance`
- `cathedral fund`
- `cathedral fund list`
- `cathedral ls`
- `cathedral ps`
- `cathedral status <id>`
- `cathedral deploy ls`
- `cathedral deploy status <name>`
- SDK health/list/get calls

### 4. Always include cleanup intent

When you create resources, prefer cleanup-friendly defaults:

- set `--ttl` / `ttl_seconds` for deployments unless the user explicitly wants persistence
- call out whether the resource will persist after the current task
- tear down rentals with `cathedral down <rental-id>` when the task is finished unless the user asked to keep them

### 5. Use current command names

The current CLI uses:

- `cathedral deploy delete ...`
- not `cathedral deployments delete ...`

Be careful with stale examples in old docs or transcripts.

### 6. Understand default exposure

- deploys are public by default
- `--private` changes the deploy to share-token-gated access
- OpenClaw deployments are intentionally public and use their own gateway token flow

### 7. Know the current gaps

- CLI exposes balance + deposit flows, but not a rich spend-history surface
- SDK exposes balance + usage history, but does not cleanly expose the full deposit-account flow in the public Python wrapper
- for deposit address creation/history, prefer the CLI

## Repo Pointers

These files are the best source of truth for agent docs and command behavior:

- `README.md`
- `docs/GETTING-STARTED.md`
- `config/README.md`
- `examples/README.md`
- `examples/15_cli_deploy/README.md`
- `examples/inference/README.md`
- `crates/cathedral-cli/src/cli/commands.rs`
- `crates/cathedral-cli/src/cli/handlers/`
- `crates/cathedral-sdk-python/README.md`
- `crates/cathedral-sdk-python/python/cathedral/__init__.py`
- `crates/cathedral-sdk-python/python/cathedral/_deployment.py`

## Fast Decision Table

- User wants a machine and shell access: rentals
- User wants a public API or app URL: serverless deploy
- User wants to top up credits: account ops
- User wants programmatic provisioning in Python: SDK ops
- User wants OpenClaw or Tau specifically: serverless deploy skill
- User wants huge multi-GPU model serving with lots of manual control: rentals first, deploy second

## TODOs For Future Agent Docs

- add a dedicated skill for billing/usage-history analysis once the CLI grows first-class spend commands
- add a repo-local troubleshooting skill once deployment failure patterns stabilize around phases and events
- add shell-tested end-to-end transcripts for account -> fund -> deploy -> cleanup flows
