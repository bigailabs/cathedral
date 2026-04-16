# torchrun-ddp - Cathedral Deployment Example

PyTorch Distributed Data Parallel (DDP) training deployed to Cathedral GPU infrastructure.

## Overview

This example demonstrates how to deploy a PyTorch DDP training job to Cathedral as a custom Docker image. The training runs LLaMA fine-tuning with:

- Distributed Data Parallel training via torchrun
- DCT gradient compression (~50% bandwidth reduction)
- Streaming C4 dataset with automatic sharding
- Linear warmup with cosine annealing LR scheduling

## Prerequisites

- Docker installed locally
- Container registry access (ghcr.io, Docker Hub, etc.)
- Cathedral API token
- HuggingFace token with LLaMA model access

## Deployment Steps

### 1. Build the Docker image

```bash
cd examples/18_torchrun_ddp

docker build -t ghcr.io/<username>/torchrun-ddp:latest .
```

### 2. Push to registry

```bash
docker push ghcr.io/<username>/torchrun-ddp:latest
```

### 3. Set environment variables

```bash
export BASILICA_API_TOKEN="your-cathedral-token"
export HF_TOKEN="your-huggingface-token"
```

### 4. Deploy to Cathedral

```bash
python3 deploy.py ghcr.io/<username>/torchrun-ddp:latest
```

## Monitoring

Once deployed, you can monitor the training:

- **Health check**: `curl https://<deployment-url>/health`
- **Training status**: `curl https://<deployment-url>/status`

Status response example:
```json
{
  "status": "running",
  "message": "Training in progress",
  "start_time": 1702234567.89,
  "elapsed_seconds": 120.5
}
```

## Project Structure

```
18_torchrun_ddp/
  deploy.py          # Cathedral deployment script
  Dockerfile         # Container image definition
  entrypoint.py      # Health server + training runner
  train.py           # Training entry point
  run.sh             # Local torchrun wrapper
  pyproject.toml     # Dependencies
  src/torchrun_ddp/
      __init__.py
      main.py        # Core DDP training loop
      model.py       # Model definitions
      optimizer.py   # AdamW + LR scheduling
      training_data.py  # Dataset loading
      compression.py # DCT gradient compression
```

## Configuration

Training parameters can be modified in `train.py`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `dataset_name` | `meta-llama/Llama-2-7b-hf` | HuggingFace model |
| `learning_rate` | `1e-5` | Base learning rate |
| `seq_length` | `512` | Maximum sequence length |
| `batch_size` | `4` | Per-GPU batch size |
| `epochs` | `1` | Training epochs |

## Local Development

To run locally without Cathedral:

```bash
# Single GPU
torchrun --standalone --nproc_per_node=1 train.py

# Multiple GPUs
torchrun --standalone --nproc_per_node=4 train.py
```
