# AgentGym Custom Build and Deploy

Build and deploy AgentGym evaluation environments to Basilica.

AgentGym is a framework for evaluating AI agents across diverse interactive tasks. This example provides a self-contained environment for building custom AgentGym Docker images and deploying them to Basilica for LLM evaluation.

## Directory Structure

```
17_agentgym_custom/
  deploy.py           # Build and deploy script
  README.md           # This file
  agentgym/           # AgentGym environment files
    Dockerfile        # Container build instructions
    env.py            # FastAPI evaluation server
    config.py         # Build argument resolver
    preprocess_env.sh # Pre-installation setup
    postprocess_env.sh# Post-installation setup
```

## Supported Environments

| Environment | Python | Description |
|-------------|--------|-------------|
| webshop | 3.8 | E-commerce web navigation |
| alfworld | 3.11 | Text-based household tasks |
| babyai | 3.11 | Grid-world language instructions |
| sciworld | 3.8 | Scientific reasoning tasks |
| textcraft | 3.11 | Interactive fiction games |
| sqlgym | 3.11 | SQL query generation |
| maze | 3.9.12 | Maze navigation game |
| wordle | 3.9.12 | Word guessing game |
| weather | 3.8.13 | Weather API tool use |
| todo | 3.8.13 | Todo list tool use |
| movie | 3.8.13 | Movie database tool use |
| sheet | 3.8.13 | Spreadsheet tool use |
| academia | 3.8.13 | Academic search tool use |

## Prerequisites

1. Docker installed and running
2. Basilica API token: `export BASILICA_API_TOKEN="your-token"`
3. LLM API key (optional): `export CHUTES_API_KEY="your-api-key"`

## Quick Start

### Option 1: Use Pre-built Image

```bash
# Deploy webshop environment with pre-built image
python3 deploy.py --image epappas/agentgym:webshop webshop
```

### Option 2: Build and Deploy

```bash
# Build and deploy webshop environment
python3 deploy.py webshop

# Build, push to registry, and deploy
python3 deploy.py --push --registry epappas webshop
```

### Option 3: Build Only

```bash
# Build image without deploying
python3 deploy.py --skip-deploy webshop

# Later, deploy the built image
python3 deploy.py --image agentgym:webshop webshop
```

## Usage

```
usage: deploy.py [-h] [--image IMAGE] [--push] [--registry REGISTRY]
                 [--cpu CPU] [--memory MEMORY] [--ttl TTL] [--timeout TIMEOUT]
                 [--skip-deploy]
                 {webshop,sciworld,alfworld,babyai,textcraft,sqlgym,maze,wordle,weather,todo,movie,sheet,academia}

Build and deploy AgentGym environment to Basilica

positional arguments:
  env_name              AgentGym environment to deploy

optional arguments:
  -h, --help            show this help message and exit
  --image IMAGE         Pre-built Docker image (skips build step)
  --push                Push built image to registry
  --registry REGISTRY   Registry URL for pushing (e.g., epappas)
  --cpu CPU             CPU allocation (default: 500m)
  --memory MEMORY       Memory allocation (default: 1Gi)
  --ttl TTL             Auto-delete after N seconds (default: 3600)
  --timeout TIMEOUT     Deployment wait timeout in seconds (default: 300)
  --skip-deploy         Only build image, don't deploy
```

## API Endpoints

Once deployed, the environment exposes:

### Health Check

```bash
curl https://your-deployment.basilica.ai/health
```

### Evaluate Model

```bash
curl -X POST https://your-deployment.basilica.ai/evaluate \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen2.5-72B-Instruct",
    "base_url": "https://llm.chutes.ai/v1",
    "task_id": 0,
    "max_round": 10,
    "api_key": "YOUR_CHUTES_API_KEY"
  }'
```

### Evaluation Request Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| model | string | Yes | - | LLM model identifier |
| base_url | string | No | https://llm.chutes.ai/v1 | LLM API endpoint |
| task_id | int | Yes | - | Task index to evaluate |
| max_round | int | No | 10 | Maximum evaluation rounds |
| api_key | string | No | env var | LLM API key (overrides CHUTES_API_KEY) |
| temperature | float | No | 0.7 | Sampling temperature |
| top_p | float | No | 1.0 | Top-p sampling |
| max_tokens | int | No | None | Maximum tokens per response |
| timeout | int | No | 2400 | Request timeout in seconds |
| seed | int | No | random | Random seed for reproducibility |

### Evaluation Response

```json
{
  "task_name": "webshop",
  "score": 0.85,
  "success": true,
  "time_taken": 45.2,
  "extra": {
    "seed": 12345,
    "conversation": [...]
  },
  "error": null
}
```

## Build Process

The build uses the files in the `agentgym/` directory:

1. `Dockerfile` clones the AgentGym repository from GitHub
2. `preprocess_env.sh` installs environment-specific dependencies via Miniconda
3. `postprocess_env.sh` applies environment-specific fixes
4. `env.py` provides the FastAPI evaluation server

Build time varies by environment (10-30 minutes depending on dependencies).

## Resource Recommendations

| Environment | CPU | Memory | Notes |
|-------------|-----|--------|-------|
| webshop | 500m | 1Gi | Standard |
| alfworld | 500m | 1Gi | Standard |
| babyai | 500m | 512Mi | Lightweight |
| sciworld | 1 | 2Gi | Requires Java |
| sqlgym | 500m | 1Gi | Standard |
| maze, wordle | 500m | 512Mi | Lightweight |
| tool-based | 500m | 512Mi | Lightweight |

## Architecture Notes

AgentGym environments run as HTTP-based services with:

- **Single worker mode**: Required because AgentGym maintains stateful agent sessions
- **Memory monitoring**: Background task exits at 90% memory to trigger container restart
- **Async execution**: Evaluation runs in thread pool to avoid blocking
