#!/usr/bin/env python3
"""
LobeChat - Self-hosted ChatGPT-style interface.

LobeChat is a modern, open-source AI chat framework supporting multiple
providers including OpenAI, Claude, Gemini, and 40+ other models.

Features:
  - Beautiful ChatGPT-like interface
  - Multi-model support (OpenAI, Anthropic, Google, local models)
  - Plugin ecosystem and function calling
  - File upload and vision capabilities

This example deploys LobeChat connected to an OpenAI-compatible API.

Usage:
    export BASILICA_API_TOKEN="your-token"
    export OPENAI_API_KEY="your-openai-key"
    python3 12_lobe_chat.py

Repository: https://github.com/lobehub/lobe-chat
"""
import os
import sys

from basilica import BasilicaClient


def main():
    openai_api_key = os.getenv("OPENAI_API_KEY")
    if not openai_api_key:
        print("Error: OPENAI_API_KEY not set")
        print("  export OPENAI_API_KEY=your-api-key")
        print()
        print("Supported providers: OpenAI, Together, Groq, etc.")
        sys.exit(1)

    client = BasilicaClient()

    print("Deploying LobeChat...")

    deployment = client.deploy(
        name="lobe-chat",
        image="lobehub/lobe-chat:latest",
        port=3210,
        env={
            "OPENAI_API_KEY": openai_api_key,
            "ACCESS_CODE": "basilica",
        },
        cpu="500m",
        memory="1Gi",
    )

    print()
    print("=" * 50)
    print("LobeChat Ready")
    print("=" * 50)
    print(f"URL: {deployment.url}")
    print(f"Access Code: basilica")
    print()
    print("Open the URL in your browser to start chatting!")


if __name__ == "__main__":
    main()
