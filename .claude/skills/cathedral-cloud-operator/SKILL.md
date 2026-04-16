---
name: cathedral-cloud-operator
description: Use when the user broadly wants to operate Cathedral as a cloud platform customer, including account setup, funding, rentals, deployments, inference, OpenClaw, Tau, or SDK automation.
---

# Cathedral Cloud Operator

This is the top-level routing skill for Cathedral customer operations.

Use it when the user says things like:

- "use Cathedral"
- "deploy this on Cathedral"
- "rent a GPU"
- "fund my account"
- "check my balance"
- "spin up OpenClaw"
- "do this from Python with Cathedral"

## Routing

Delegate to the narrower skill that matches the task:

- `cathedral/.claude/skills/cathedral-account-ops/SKILL.md`
  - auth, tokens, balance, funding, deposits
- `cathedral/.claude/skills/cathedral-rentals-ops/SKILL.md`
  - direct machines, SSH, volumes, file copy, teardown
- `cathedral/.claude/skills/cathedral-serverless-ops/SKILL.md`
  - managed deploys, inference endpoints, OpenClaw, Tau, logs, scale, delete
- `cathedral/.claude/skills/cathedral-sdk-ops/SKILL.md`
  - Python automation, scripts, notebooks, SDK caveats

## First Decision

Pick one of these control planes first:

- balance/funding/account question -> account ops
- wants an SSH box -> rentals ops
- wants a URL or HTTP API -> serverless ops
- wants Python code or automation -> SDK ops

## Guardrails

- treat `cathedral up`, `cathedral deploy`, `cathedral summon`, and SDK create calls as chargeable actions
- prefer read-only inspection first: balance, ls, ps, deploy status, logs
- when creating a deployment, set a TTL unless the user explicitly wants persistence
- when creating a rental, tear it down after the task unless the user explicitly wants to keep it

## Canonical Source Paths

- `AGENTS.md`
- `crates/cathedral-cli/src/cli/`
- `crates/cathedral-sdk-python/`
- `examples/`

## TODOs

- add a task-oriented checklist once there are stable end-to-end scripts for fund -> deploy -> cleanup
