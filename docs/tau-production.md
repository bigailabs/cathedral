# Tau Production Deployment Guide

This guide covers deploying and operating Tau on a production Basilica cluster.

## Prerequisites

- `basilica` CLI installed and authenticated
- Access to the production Basilica API
- `docker` access for image build/push
- Access to the `basilica-backend` repo (for Tau image build scripts)

## Required Secrets

Set these before deployment:

```bash
export BASILICA_API_TOKEN="basilica_..."          # Basilica API token
export TAU_BOT_TOKEN="123456789:telegram_token"   # Telegram bot token from @BotFather
export CHUTES_API_TOKEN="cpk_..."                 # Chutes token for Tau LLM/voice
```

Optional:

```bash
export TAU_CHAT_MODEL="nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16"
```

## 1) Build and Push Tau Image

From `basilica-backend/`:

```bash
bash scripts/tau/build.sh --image-name basilica/basilica-tau --image-tag latest
bash scripts/tau/push.sh --source-image basilica/basilica-tau --target-image ghcr.io/one-covenant/basilica-tau --tag latest
```

## 2) Deploy Tau (Production Cluster)

From `basilica/`:

```bash
basilica deploy tau --name tau-prod --detach
```

Notes:
- The Tau template reads `TAU_BOT_TOKEN`, `CHUTES_API_TOKEN`, and optional `TAU_CHAT_MODEL` from env.
- Deployment uses image `ghcr.io/one-covenant/basilica-tau:latest`.

## 3) Verify Deployment

```bash
basilica summon status tau-prod
basilica summon logs tau-prod --tail 200
```

Healthy startup logs should include:
- `FUSE mount ready, starting application`
- `Tau import OK`
- `TAU STARTING`

## 4) First Bot Initialization

Send `/start` to the bot in Telegram. This initializes persistent `chat_id.txt`.

## 5) Updating Tau in Production

For each update:

1. Rebuild image
2. Push image
3. Redeploy Tau (or recreate the deployment)
4. Check logs/status

Recommended update sequence:

```bash
# in basilica-backend/
bash scripts/tau/build.sh --image-name basilica/basilica-tau --image-tag latest
bash scripts/tau/push.sh --source-image basilica/basilica-tau --target-image ghcr.io/one-covenant/basilica-tau --tag latest

# in basilica/
basilica summon delete tau-prod --yes
basilica deploy tau --name tau-prod --detach
basilica summon logs tau-prod --tail 200
```

## Troubleshooting

- `CHUTES_API_TOKEN is required`
  - Ensure `CHUTES_API_TOKEN` is exported in the shell where `basilica deploy tau` is run.

- Telegram `409 Conflict: terminated by other getUpdates request`
  - Another running bot process is polling with the same `TAU_BOT_TOKEN`. Stop duplicate deployments/processes.

- Tau not starting after image update
  - Confirm pushed image digest in GHCR.
  - Recreate deployment to force a fresh pod.
