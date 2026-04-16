---
name: cathedral-account-ops
description: Use when the user wants to log into Cathedral, create API tokens, check balance, fund credits with TAO, or inspect deposit history.
---

# Cathedral Account Ops

Use this skill for the account-level Cathedral operator surface:

- CLI install and login
- device-code auth for remote/headless shells
- API token creation/revocation
- checking credit balance
- funding via TAO deposit address
- reviewing deposit history

## Canonical CLI Setup

Install the CLI:

```bash
curl -sSL https://basilica.ai/install.sh | bash
```

Authenticate:

```bash
cathedral login
```

For headless terminals:

```bash
cathedral login --device-code
```

Log out:

```bash
cathedral logout
```

## API Tokens

Use tokens for SDK/programmatic work, CI, and scripts.

Create:

```bash
cathedral tokens create my-agent-token
```

List:

```bash
cathedral tokens list
```

Revoke:

```bash
cathedral tokens revoke my-agent-token --yes
```

Export for SDK usage:

```bash
export BASILICA_API_TOKEN="cathedral_..."
```

## Balance And Funding

Check current balance:

```bash
cathedral balance
```

Get or create the user deposit address:

```bash
cathedral fund
```

List deposit history:

```bash
cathedral fund list --limit 100 --offset 0
```

## Funding Flow Agents Should Follow

If the user says "top up", "fund", "add credits", or "what address do I send TAO to", the operational flow is:

1. run `cathedral fund`
2. return the deposit address
3. tell the user to send TAO to that address
4. use `cathedral fund list` to confirm deposit arrival
5. use `cathedral balance` to confirm credits updated

Important details currently exposed by the CLI implementation:

- funding method is TAO
- minimum deposit is `0.1 TAO`
- funds settle after `12` confirmations

There is no separate `top-up` command. `cathedral fund` is the top-up entrypoint.

## What To Prefer

- Prefer `cathedral login` for CLI work.
- Prefer API tokens only when the task needs SDK calls or automation.
- Prefer the CLI over the Python SDK for deposit-address creation and deposit-history inspection.

## Safe Read-Only Commands

These are safe to run without creating billable resources:

```bash
cathedral balance
cathedral fund
cathedral fund list --limit 20 --offset 0
cathedral tokens list
```

## Common Agent Responses

### Check Whether The User Is Ready To Spend

```bash
cathedral balance
```

If the balance is too low for rentals or deploys, direct the user to:

```bash
cathedral fund
```

### Prepare A Script Environment

```bash
cathedral tokens create my-script-token
export BASILICA_API_TOKEN="cathedral_..."
```

### Rotate A Leaked Or Old Token

```bash
cathedral tokens list
cathedral tokens revoke old-token-name --yes
cathedral tokens create replacement-token
```

## Caveats

- The CLI is the authoritative operator surface for deposits.
- The public Python `CathedralClient` currently exposes `get_balance()` and `list_usage_history()`, but not a clean first-class deposit-account workflow.
- If the user asks for billing/spend analysis, combine CLI balance checks with SDK usage-history access.

## File Pointers

- `crates/cathedral-cli/src/cli/handlers/auth.rs`
- `crates/cathedral-cli/src/cli/handlers/tokens.rs`
- `crates/cathedral-cli/src/cli/handlers/fund.rs`
- `crates/cathedral-cli/src/cli/handlers/balance.rs`
- `crates/cathedral-sdk-python/README.md`

## TODOs

- add a repo-local example that shows `cathedral fund` output and the expected deposit-history shape
- document whether future CLI releases expose spend history or per-resource usage directly
