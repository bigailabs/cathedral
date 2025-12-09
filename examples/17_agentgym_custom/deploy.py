#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "basilica-sdk>=0.9.0",
# ]
# ///
"""
Deploy AgentGym environment to Basilica from custom-built Docker image.

AgentGym provides standardized environments for evaluating AI agents across
diverse tasks like web navigation, text games, and interactive simulations.

This example shows how to:
  1. Build an AgentGym Docker image from the included environment files
  2. Push to a container registry
  3. Deploy to Basilica for LLM evaluation

Supported environments:
  - webshop: E-commerce web navigation
  - alfworld: Text-based household tasks
  - babyai: Grid-world language instructions
  - sciworld: Scientific reasoning tasks
  - textcraft: Interactive fiction games
  - sqlgym: SQL query generation
  - maze, wordle: Game environments (lmrlgym)
  - weather, todo, movie, sheet, academia: Tool-use environments

Prerequisites:
    1. Docker installed and running
    2. BASILICA_API_TOKEN environment variable set
    3. CHUTES_API_KEY for LLM API access (optional, can be set at runtime)

Usage:
    # Build and deploy webshop environment
    python3 deploy.py webshop

    # Deploy with pre-built image
    python3 deploy.py --image epappas/agentgym:webshop webshop
"""

import argparse
import os
import subprocess
from pathlib import Path
from typing import Optional

from basilica import BasilicaClient, Deployment

AGENTGYM_ENV_PATH = Path(__file__).parent / "agentgym"

ENVIRONMENT_CONFIG = {
    "webshop": {
        "base_image": "python:3.8-slim",
        "env_name": "webshop",
        "tool_name": "",
    },
    "sciworld": {
        "base_image": "python:3.8-slim",
        "env_name": "sciworld",
        "tool_name": "",
    },
    "alfworld": {
        "base_image": "python:3.11-slim",
        "env_name": "alfworld",
        "tool_name": "",
    },
    "babyai": {"base_image": "python:3.11-slim", "env_name": "babyai", "tool_name": ""},
    "textcraft": {
        "base_image": "python:3.11-slim",
        "env_name": "textcraft",
        "tool_name": "",
    },
    "sqlgym": {"base_image": "python:3.11-slim", "env_name": "sqlgym", "tool_name": ""},
    "maze": {
        "base_image": "python:3.9.12-slim",
        "env_name": "lmrlgym",
        "tool_name": "maze",
    },
    "wordle": {
        "base_image": "python:3.9.12-slim",
        "env_name": "lmrlgym",
        "tool_name": "wordle",
    },
    "weather": {
        "base_image": "python:3.8.13-slim",
        "env_name": "tool",
        "tool_name": "weather",
    },
    "todo": {
        "base_image": "python:3.8.13-slim",
        "env_name": "tool",
        "tool_name": "todo",
    },
    "movie": {
        "base_image": "python:3.8.13-slim",
        "env_name": "tool",
        "tool_name": "movie",
    },
    "sheet": {
        "base_image": "python:3.8.13-slim",
        "env_name": "tool",
        "tool_name": "sheet",
    },
    "academia": {
        "base_image": "python:3.8.13-slim",
        "env_name": "tool",
        "tool_name": "academia",
    },
}


def build_agentgym_image(
    env_name: str,
    image_tag: str,
    push: bool = False,
    registry: Optional[str] = None,
) -> str:
    """
    Build AgentGym Docker image from local environment files.

    Args:
        env_name: AgentGym environment name (webshop, alfworld, etc.)
        image_tag: Docker image tag (e.g., "agentgym:webshop")
        push: Push image to registry after build
        registry: Registry URL for pushing (e.g., "epappas")

    Returns:
        Final image tag (with registry if pushed)

    Raises:
        ValueError: If environment name is not supported
        subprocess.CalledProcessError: If Docker build fails
    """
    if env_name not in ENVIRONMENT_CONFIG:
        raise ValueError(
            f"Unknown environment: {env_name}. "
            f"Supported: {', '.join(ENVIRONMENT_CONFIG.keys())}"
        )

    config = ENVIRONMENT_CONFIG[env_name]

    if not AGENTGYM_ENV_PATH.exists():
        raise FileNotFoundError(
            f"AgentGym environment not found at {AGENTGYM_ENV_PATH}. "
            "Ensure the agentgym/ directory exists in this example."
        )

    print(f"Building AgentGym image for environment: {env_name}")
    print(f"  Base image: {config['base_image']}")
    print(f"  ENV_NAME: {config['env_name']}")
    if config["tool_name"]:
        print(f"  TOOL_NAME: {config['tool_name']}")

    build_args = [
        "--build-arg",
        f"BASE_IMAGE={config['base_image']}",
        "--build-arg",
        f"ENV_NAME={config['env_name']}",
        "--build-arg",
        f"TOOL_NAME={config['tool_name']}",
    ]

    cmd = [
        "docker",
        "build",
        "-t",
        image_tag,
        *build_args,
        "-f",
        str(AGENTGYM_ENV_PATH / "Dockerfile"),
        str(AGENTGYM_ENV_PATH),
    ]

    print(f"\nRunning: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)

    print(f"\nImage built: {image_tag}")

    if push:
        final_tag = image_tag
        if registry:
            final_tag = f"{registry}/{image_tag}"
            subprocess.run(["docker", "tag", image_tag, final_tag], check=True)

        print(f"\nPushing image: {final_tag}")
        subprocess.run(["docker", "push", final_tag], check=True)
        print(f"Image pushed: {final_tag}")
        return final_tag

    return image_tag


def deploy_agentgym(
    client: BasilicaClient,
    env_name: str,
    image: str,
    llm_api_key: Optional[str] = None,
    cpu: str = "500m",
    memory: str = "1Gi",
    ttl_seconds: int = 3600,
    timeout: int = 300,
) -> Deployment:
    """
    Deploy AgentGym environment to Basilica.

    Args:
        client: Basilica client instance
        env_name: AgentGym environment name
        image: Docker image to deploy
        llm_api_key: API key for LLM provider (optional)
        cpu: CPU allocation (default: "500m")
        memory: Memory allocation (default: "1Gi")
        ttl_seconds: Auto-delete timeout (default: 3600)
        timeout: Deployment wait timeout (default: 300)

    Returns:
        Deployment instance
    """
    if env_name not in ENVIRONMENT_CONFIG:
        raise ValueError(f"Unknown environment: {env_name}")

    config = ENVIRONMENT_CONFIG[env_name]

    env_vars = {
        "ENV_NAME": config["env_name"],
    }

    if config["tool_name"]:
        env_vars["TOOL_NAME"] = config["tool_name"]

    if llm_api_key:
        env_vars["CHUTES_API_KEY"] = llm_api_key

    print(f"\nDeploying AgentGym ({env_name})...")
    print(f"  Image: {image}")
    print(f"  CPU: {cpu}, Memory: {memory}")

    deployment = client.deploy(
        name=f"agentgym-{env_name}",
        image=image,
        port=8000,
        env=env_vars,
        cpu=cpu,
        memory=memory,
        ttl_seconds=ttl_seconds,
        timeout=timeout,
    )

    print(f"  URL: {deployment.url}")
    return deployment


def main():
    """
    Command-line interface for building and deploying AgentGym environments.
    1. Parse arguments
    2. Build Docker image (if not provided)
    3. Deploy to Basilica
    4. Print deployment details
    5. Optionally skip deployment after build
    6. Provide curl commands for health check and evaluation
    """
    parser = argparse.ArgumentParser(
        description="Build and deploy AgentGym environment to Basilica",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Build and deploy webshop locally
    python3 deploy.py webshop

    # Use pre-built image
    python3 deploy.py --image bignickeye/agentgym:webshop webshop

    # Build, push to registry, and deploy
    python3 deploy.py --push --registry ghcr.io/myuser webshop

    # Deploy with custom resources
    python3 deploy.py --cpu 1 --memory 2Gi webshop
        """,
    )

    parser.add_argument(
        "env_name",
        choices=list(ENVIRONMENT_CONFIG.keys()),
        help="AgentGym environment to deploy",
    )
    parser.add_argument(
        "--image",
        help="Pre-built Docker image (skips build step)",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Push built image to registry",
    )
    parser.add_argument(
        "--registry",
        help="Registry URL for pushing (e.g., epappas)",
    )
    parser.add_argument(
        "--cpu",
        default="500m",
        help="CPU allocation (default: 500m)",
    )
    parser.add_argument(
        "--memory",
        default="1Gi",
        help="Memory allocation (default: 1Gi)",
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=3600,
        help="Auto-delete after N seconds (default: 3600)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Deployment wait timeout in seconds (default: 300)",
    )
    parser.add_argument(
        "--skip-deploy",
        action="store_true",
        help="Only build image, don't deploy",
    )

    args = parser.parse_args()

    llm_api_key = os.getenv("CHUTES_API_KEY")
    if not llm_api_key and not args.skip_deploy:
        print("Warning: CHUTES_API_KEY not set. LLM evaluation will fail unless")
        print("         API key is provided in the /evaluate request.")
        print()

    if args.image:
        image = args.image
        print(f"Using pre-built image: {image}")
    else:
        image_tag = f"agentgym:{args.env_name}"
        image = build_agentgym_image(
            env_name=args.env_name,
            image_tag=image_tag,
            push=args.push,
            registry=args.registry,
        )

    if args.skip_deploy:
        print("\n" + "=" * 50)
        print("Build Complete (deployment skipped)")
        print("=" * 50)
        print(f"Image: {image}")
        print()
        print("To deploy manually:")
        print(f"  python3 deploy.py --image {image} {args.env_name}")
        return

    client = BasilicaClient()

    deployment = deploy_agentgym(
        client=client,
        env_name=args.env_name,
        image=image,
        llm_api_key=llm_api_key,
        cpu=args.cpu,
        memory=args.memory,
        ttl_seconds=args.ttl,
        timeout=args.timeout,
    )

    api_key_display = llm_api_key or "YOUR_CHUTES_API_KEY"

    print("\n" + "=" * 50)
    print("Deployment Ready")
    print("=" * 50)
    print(f"Environment: {args.env_name}")
    print(f"URL:         {deployment.url}")
    print()
    print("Test health endpoint:")
    print(f"  curl {deployment.url}/health")
    print()
    print("Run evaluation:")
    print(
        f"""  curl -X POST {deployment.url}/evaluate \\
    -H "Content-Type: application/json" \\
    -d '{{
      "model": "Qwen/Qwen2.5-72B-Instruct",
      "base_url": "https://llm.chutes.ai/v1",
      "task_id": 0,
      "max_round": 10,
      "api_key": "{api_key_display}"
    }}'"""
    )


if __name__ == "__main__":
    main()
