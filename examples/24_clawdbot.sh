#!/usr/bin/env bash
#
# Deploy Clawdbot AI agent platform on Basilica using the CLI.
#
# Usage:
#   export ANTHROPIC_API_KEY="your-key"
#   ./24_clawdbot.sh
#
# See: https://github.com/clawdbot/clawdbot

set -euo pipefail

if [[ -z "${ANTHROPIC_API_KEY:-}" ]] && [[ -z "${OPENAI_API_KEY:-}" ]]; then
    echo "Set ANTHROPIC_API_KEY or OPENAI_API_KEY"
    exit 1
fi

# Build env args
ENV_ARGS=""
[[ -n "${ANTHROPIC_API_KEY:-}" ]] && ENV_ARGS="$ENV_ARGS -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY"
[[ -n "${OPENAI_API_KEY:-}" ]] && ENV_ARGS="$ENV_ARGS -e OPENAI_API_KEY=$OPENAI_API_KEY"

# Deploy Clawdbot
basilica deploy ghcr.io/one-covenant/basilica-clawdbot:latest \
    --name clawdbot \
    --port 18789 \
    --cpu 2 \
    --memory 4Gi \
    $ENV_ARGS

# Get the token from logs
echo ""
echo "To get your access token, run:"
echo "  basilica deploy logs clawdbot | grep CLAWDBOT_GATEWAY_TOKEN"
