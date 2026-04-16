#!/usr/bin/env python3
"""
AgentGym - Deploy RL agent evaluation environments.

AgentGym provides standardized environments for evaluating AI agents across
diverse tasks like web navigation, text games, and interactive simulations.

This example deploys:
  1. An LLM evaluation service for reasoning tasks
  2. An AgentGym environment (webshop, alfworld, babyai, etc.)

Usage:
    export BASILICA_API_TOKEN="your-token"
    export CHUTES_API_KEY="your-chutes-api-key"
    python3 11_agentgym.py
"""
import os
import sys

from cathedral import CathedralClient, Deployment


def deploy_llm_evaluator(client: CathedralClient, llm_api_key: str) -> Deployment:
    """Deploy an LLM-powered evaluation service.

    Args:
        client: Cathedral client instance
        llm_api_key: API key for LLM provider

    Returns:
        Deployment instance for the evaluation service
    """
    print("Deploying LLM evaluation service...")

    deployment = client.deploy(
        name="llm-evaluator",
        image="bignickeye/affine:latest",
        port=8000,
        env={"CHUTES_API_KEY": llm_api_key},
        cpu="250m",
        memory="512Mi",
        ttl_seconds=3600,
        timeout=120,
    )

    print(f"  URL: {deployment.url}")
    return deployment


def deploy_agentgym(
    client: CathedralClient,
    llm_api_key: str,
    env_name: str = "webshop",
) -> Deployment:
    """Deploy an AgentGym environment for agent evaluation.

    Available environments:
      - webshop: E-commerce web navigation
      - alfworld: Text-based household tasks
      - babyai: Grid-world language instructions
      - textworld: Interactive fiction games
      - sciworld: Scientific reasoning tasks

    Args:
        client: Cathedral client instance
        llm_api_key: API key for LLM provider
        env_name: AgentGym environment name

    Returns:
        Deployment instance for the AgentGym environment
    """
    print(f"Deploying AgentGym ({env_name})...")

    deployment = client.deploy(
        name=f"agentgym-{env_name}",
        image=f"bignickeye/agentgym:{env_name}",
        port=8000,
        env={
            "ENV_NAME": env_name,
            "CHUTES_API_KEY": llm_api_key,
        },
        cpu="500m",
        memory="1Gi",
        ttl_seconds=3600,
        timeout=180,
    )

    print(f"  URL: {deployment.url}")
    return deployment


def main():
    llm_api_key = os.getenv("CHUTES_API_KEY")
    if not llm_api_key:
        print("Error: CHUTES_API_KEY not set")
        print("  export CHUTES_API_KEY=your-api-key")
        sys.exit(1)

    client = CathedralClient()

    evaluator = deploy_llm_evaluator(client, llm_api_key)
    agentgym = deploy_agentgym(client, llm_api_key, env_name="webshop")

    print("\n" + "=" * 50)
    print("Deployments Ready")
    print("=" * 50)
    print(f"LLM Evaluator: {evaluator.url}")
    print(f"AgentGym:      {agentgym.url}")
    print()
    print("Test health endpoints:")
    print(f"  curl {evaluator.url}/health")
    print(f"  curl {agentgym.url}/health")


if __name__ == "__main__":
    main()
