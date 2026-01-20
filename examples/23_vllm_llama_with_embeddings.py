#!/usr/bin/env python3
"""
Deploy Llama-3.1-8B-Instruct with a dedicated Embeddings service.

This example deploys a complete LLM + Embeddings stack:
  1. Llama-3.1-8B-Instruct for chat/completions (vLLM)
  2. E5-Mistral-7B-Instruct for embeddings (vLLM with --task embed)

This is the standard architecture for RAG applications:
  - Use the embedding model to vectorize documents and queries
  - Use the LLM to generate responses based on retrieved context

Requirements:
    - HuggingFace account with access to meta-llama/Llama-3.1-8B-Instruct
    - Set HF_TOKEN environment variable for gated model access
    - 2x GPUs with 24GB+ VRAM each (or 1x GPU with 48GB+ if running sequentially)

Usage:
    export BASILICA_API_TOKEN="your-token"
    export HF_TOKEN="your-huggingface-token"
    python3 23_vllm_llama_with_embeddings.py

Endpoints:
    LLM:        /v1/chat/completions, /v1/completions
    Embeddings: /v1/embeddings
"""
import os

import basilica


def deploy_embeddings(client: basilica.BasilicaClient) -> basilica.Deployment:
    """Deploy E5-Mistral-7B as embedding model."""
    print("Deploying E5-Mistral-7B embedding service...")

    cache = basilica.Volume.from_name("e5-mistral-cache", create_if_missing=True)

    @basilica.deployment(
        name="embeddings-e5",
        image="vllm/vllm-openai:latest",
        port=8000,
        gpu_count=1,
        min_gpu_memory_gb=24,
        memory="32Gi",
        volumes={"/root/.cache/huggingface": cache},
        ttl_seconds=3600,
        timeout=900,
    )
    def serve():
        import subprocess
        cmd = [
            "vllm", "serve", "intfloat/e5-mistral-7b-instruct",
            "--task", "embed",
            "--host", "0.0.0.0",
            "--port", "8000",
            "--max-model-len", "4096",
            "--trust-remote-code",
        ]
        subprocess.Popen(cmd).wait()

    return serve()


def deploy_llama(client: basilica.BasilicaClient, hf_token: str) -> basilica.Deployment:
    """Deploy Llama-3.1-8B-Instruct for chat/completions."""
    print("Deploying Llama-3.1-8B-Instruct inference service...")

    cache = basilica.Volume.from_name("llama-31-cache", create_if_missing=True)

    @basilica.deployment(
        name="llama-31-8b",
        image="vllm/vllm-openai:latest",
        port=8000,
        gpu_count=1,
        min_gpu_memory_gb=24,
        memory="32Gi",
        env={
            "HF_TOKEN": hf_token,
            "HF_HUB_ENABLE_HF_TRANSFER": "1",
        },
        volumes={"/root/.cache/huggingface": cache},
        ttl_seconds=3600,
        timeout=900,
    )
    def serve():
        import subprocess
        cmd = [
            "vllm", "serve", "meta-llama/Llama-3.1-8B-Instruct",
            "--host", "0.0.0.0",
            "--port", "8000",
            "--max-model-len", "4096",
            "--dtype", "bfloat16",
        ]
        subprocess.Popen(cmd).wait()

    return serve()


def main():
    hf_token = os.environ.get("HF_TOKEN")
    if not hf_token:
        print("ERROR: HF_TOKEN environment variable not set.")
        print("Llama models require HuggingFace authentication.")
        print("Set it with: export HF_TOKEN='your-huggingface-token'")
        return

    client = basilica.BasilicaClient()

    # Deploy both services
    embeddings = deploy_embeddings(client)
    print(f"  Embeddings API: {embeddings.url}/v1/embeddings")

    llama = deploy_llama(client, hf_token)
    print(f"  Llama API: {llama.url}/v1/chat/completions")

    # Print summary
    print()
    print("=" * 60)
    print("LLM + Embeddings Stack Ready")
    print("=" * 60)
    print()
    print("Endpoints:")
    print(f"  Embeddings:  {embeddings.url}/v1/embeddings")
    print(f"  Chat:        {llama.url}/v1/chat/completions")
    print(f"  Completions: {llama.url}/v1/completions")
    print()
    print("Models:")
    print(f"  Embeddings: intfloat/e5-mistral-7b-instruct")
    print(f"  LLM:        meta-llama/Llama-3.1-8B-Instruct")
    print()
    print("To delete deployments:")
    print("  embeddings.delete()")
    print("  llama.delete()")


if __name__ == "__main__":
    main()
