#!/usr/bin/env python3
"""
Deploy Tau agent on Basilica.

Usage:
    export TAU_BOT_TOKEN="your-telegram-token"
    export CHUTES_API_TOKEN="your-chutes-token"
    export TAU_CHAT_MODEL="optional-model-override"
    python3 30_tau.py
"""
import os
import sys

from basilica import BasilicaClient

bot_token = os.getenv("TAU_BOT_TOKEN")
chutes_token = os.getenv("CHUTES_API_TOKEN")
chat_model = os.getenv("TAU_CHAT_MODEL")

if not bot_token:
    sys.exit("Set TAU_BOT_TOKEN")
if not chutes_token:
    sys.exit("Set CHUTES_API_TOKEN")

env = {
    "TAU_BOT_TOKEN": bot_token,
    "CHUTES_API_TOKEN": chutes_token,
}
if chat_model:
    env["TAU_CHAT_MODEL"] = chat_model

client = BasilicaClient()

deployment = client.deploy(
    name="tau",
    image="ghcr.io/one-covenant/basilica-tau:latest",
    port=8080,
    env=env,
    cpu="2",
    memory="4Gi",
    timeout=600,
    storage=True,
)

print(f"Tau deployed: {deployment.url}")
print("Send a message to your Telegram bot to initialize chat_id.txt")
