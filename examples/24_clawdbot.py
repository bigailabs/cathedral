#!/usr/bin/env python3
"""
Deploy Clawdbot AI agent platform on Basilica.

Usage:
    export ANTHROPIC_API_KEY="your-key"
    python3 24_clawdbot.py

See: https://github.com/clawdbot/clawdbot
"""
import os
import re
import sys

from basilica import BasilicaClient


def main():
    api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("Set ANTHROPIC_API_KEY or OPENAI_API_KEY")
        sys.exit(1)

    client = BasilicaClient()

    # Deploy Clawdbot
    env = {}
    if os.getenv("ANTHROPIC_API_KEY"):
        env["ANTHROPIC_API_KEY"] = os.getenv("ANTHROPIC_API_KEY")
    if os.getenv("OPENAI_API_KEY"):
        env["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY")

    deployment = client.deploy(
        name="clawdbot",
        image="ghcr.io/one-covenant/basilica-clawdbot:latest",
        port=18789,
        env=env,
        cpu="2",
        memory="4Gi",
    )

    # Extract gateway token from logs
    token = None
    try:
        logs = deployment.logs(tail=50)
        match = re.search(r"CLAWDBOT_GATEWAY_TOKEN=([a-f0-9]{64})", logs)
        if match:
            token = match.group(1)
    except Exception:
        pass

    # Print access info
    print(f"\nClawdbot deployed: {deployment.url}")
    if token:
        print(f"Control UI: {deployment.url}/chat?session=main&token={token}")
    else:
        print("Run: basilica deploy logs clawdbot  # to get the token")


if __name__ == "__main__":
    main()
