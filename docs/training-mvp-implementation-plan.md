# Training Service MVP Implementation Plan

This document provides a detailed, actionable implementation plan for building an MVP of the training service with single-node training support.

## MVP Scope

### In Scope
- Single-node LoRA training on Llama models (8B, 70B)
- HuggingFace + PEFT backend
- Basic inference via HuggingFace (vLLM in Phase 2)
- TrainingSession CRD and controller
- Python SDK with `forward_backward()`, `optim_step()`, `sample()`, `save_state()`
- Checkpoint persistence to R2/S3
- Single-tenant (one session per GPU)

### Out of Scope (Phase 2+)
- Multi-tenant worker pools (clock-cycle batching)
- vLLM inference backend
- SGLang structured generation
- Distributed training (DeepSpeed)
- QLoRA, DoRA
- DPO, KTO loss functions
- Billing integration

---

## Architecture Overview (MVP)

> **Note**: TrainingSession follows the same architectural pattern as UserDeployment,
> leveraging the existing Envoy Gateway infrastructure for routing, rate limiting,
> and authentication. See `docs/training-session-architecture.md` for full details.

```
┌─────────────────────────────────────────────────────────────────┐
│                        Python SDK                                │
│  ServiceClient → TrainingClient                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                         HTTP/REST
                              │
┌─────────────────────────────────────────────────────────────────┐
│                      Basilica API (Rust)                         │
│  POST /sessions → Creates TrainingSession CRD + HTTPRoute       │
│  GET/DELETE /sessions/{id} → Manages session lifecycle          │
└─────────────────────────────────────────────────────────────────┘
                              │
       ┌──────────────────────┼──────────────────────┐
       │                      │                      │
       ▼                      ▼                      ▼
┌─────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   K8s API   │    │   Envoy Gateway  │    │    Operator     │
│             │    │                  │    │                 │
│ Creates CRD │    │ Routes requests  │    │ Reconciles      │
│ + HTTPRoute │    │ to Training Pod  │    │ TrainingSession │
└─────────────┘    └────────┬─────────┘    └─────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                  Training Pod (Python)                           │
│  HuggingFace + PEFT                                             │
│  • forward_backward()                                           │
│  • optim_step()                                                 │
│  • sample()                                                     │
│  • save_state() / load_state()                                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Storage (R2/S3)                             │
│  • Base model cache                                             │
│  • LoRA checkpoints                                             │
└─────────────────────────────────────────────────────────────────┘
```

### Request Flow

1. **Session Creation**: SDK → Basilica API → Create TrainingSession CRD + HTTPRoute
2. **Training Operations**: SDK → Envoy Gateway → HTTPRoute → Training Pod
3. **Session Deletion**: SDK → Basilica API → Delete CRD (cascade deletes Pod, Service, HTTPRoute)

---

## Implementation Tasks

### Week 1: Python Training Service

#### Task 1.1: Project Setup

**Create directory structure:**

```bash
mkdir -p services/training-service/src
mkdir -p services/training-service/tests
mkdir -p services/training-service/proto
```

**Create `services/training-service/pyproject.toml`:**

```toml
[project]
name = "basilica-training-service"
version = "0.1.0"
description = "GPU training service for Basilica"
requires-python = ">=3.10"
dependencies = [
    "torch>=2.1.0",
    "transformers>=4.36.0",
    "peft>=0.7.0",
    "accelerate>=0.25.0",
    "safetensors>=0.4.0",
    "tokenizers>=0.15.0",
    "grpcio>=1.60.0",
    "grpcio-tools>=1.60.0",
    "protobuf>=4.25.0",
    "boto3>=1.34.0",  # For S3/R2
    "fastapi>=0.109.0",
    "uvicorn>=0.27.0",
    "pydantic>=2.5.0",
    "structlog>=24.1.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=7.4.0",
    "pytest-asyncio>=0.23.0",
    "black>=24.1.0",
    "ruff>=0.1.0",
    "mypy>=1.8.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src"]

[tool.black]
line-length = 100

[tool.ruff]
line-length = 100
select = ["E", "F", "I", "N", "W"]
```

**Create `services/training-service/Dockerfile`:**

```dockerfile
FROM nvidia/cuda:12.1-devel-ubuntu22.04

# Install Python
RUN apt-get update && apt-get install -y \
    python3.11 \
    python3.11-venv \
    python3-pip \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create venv
RUN python3.11 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install dependencies
WORKDIR /app
COPY pyproject.toml .
RUN pip install --upgrade pip && \
    pip install -e ".[dev]"

# Copy source
COPY src/ src/
COPY proto/ proto/

# Generate gRPC stubs
RUN python -m grpc_tools.protoc \
    -I proto \
    --python_out=src \
    --grpc_python_out=src \
    proto/training.proto

# Run service
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "uvicorn", "src.server:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

#### Task 1.2: Core Training Backend

**Create `services/training-service/src/backend.py`:**

```python
"""Core training backend using HuggingFace + PEFT."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from pathlib import Path
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, PreTrainedModel
from peft import get_peft_model, LoraConfig, TaskType, PeftModel
import structlog

logger = structlog.get_logger()


@dataclass
class LoraConfiguration:
    """LoRA adapter configuration."""
    rank: int = 32
    alpha: int = 64
    dropout: float = 0.05
    target_modules: List[str] = field(default_factory=lambda: [
        "q_proj", "k_proj", "v_proj", "o_proj",
        "gate_proj", "up_proj", "down_proj",
    ])


@dataclass
class OptimizerConfiguration:
    """Optimizer configuration."""
    learning_rate: float = 1e-4
    weight_decay: float = 0.01
    beta1: float = 0.9
    beta2: float = 0.999
    epsilon: float = 1e-8
    grad_clip: Optional[float] = 1.0


@dataclass
class SessionState:
    """State for a training session."""
    session_id: str
    model: PeftModel
    optimizer: torch.optim.AdamW
    lora_config: LoraConfiguration
    optimizer_config: OptimizerConfiguration
    step_count: int = 0
    tokens_processed: int = 0


@dataclass
class ForwardBackwardResult:
    """Result of forward-backward pass."""
    loss: float
    logprobs: List[float]
    tokens_processed: int


@dataclass
class SampleResult:
    """Result of text generation."""
    text: str
    token_ids: List[int]
    logprobs: Optional[List[float]] = None
    finish_reason: str = "stop"


class TrainingBackend:
    """Single-node training backend using HuggingFace + PEFT."""

    def __init__(
        self,
        model_cache_dir: str = "/models",
        checkpoint_dir: str = "/checkpoints",
        device: str = "cuda",
        dtype: torch.dtype = torch.bfloat16,
    ):
        self.model_cache_dir = Path(model_cache_dir)
        self.checkpoint_dir = Path(checkpoint_dir)
        self.device = device
        self.dtype = dtype

        self.sessions: Dict[str, SessionState] = {}
        self.base_models: Dict[str, PreTrainedModel] = {}
        self.tokenizers: Dict[str, Any] = {}

        logger.info(
            "training_backend_initialized",
            device=device,
            dtype=str(dtype),
            cache_dir=str(model_cache_dir),
        )

    def _load_base_model(self, model_name: str) -> PreTrainedModel:
        """Load or retrieve cached base model."""
        if model_name not in self.base_models:
            logger.info("loading_base_model", model=model_name)

            model = AutoModelForCausalLM.from_pretrained(
                model_name,
                torch_dtype=self.dtype,
                device_map="auto",
                cache_dir=self.model_cache_dir,
                trust_remote_code=True,
            )
            tokenizer = AutoTokenizer.from_pretrained(
                model_name,
                cache_dir=self.model_cache_dir,
                trust_remote_code=True,
            )

            # Ensure pad token exists
            if tokenizer.pad_token is None:
                tokenizer.pad_token = tokenizer.eos_token

            self.base_models[model_name] = model
            self.tokenizers[model_name] = tokenizer

            logger.info("base_model_loaded", model=model_name)

        return self.base_models[model_name]

    def create_session(
        self,
        session_id: str,
        base_model: str,
        lora_config: LoraConfiguration,
        optimizer_config: OptimizerConfiguration,
        seed: Optional[int] = None,
    ) -> str:
        """Create a new training session with LoRA adapter."""

        if session_id in self.sessions:
            raise ValueError(f"Session {session_id} already exists")

        if seed is not None:
            torch.manual_seed(seed)

        logger.info(
            "creating_session",
            session_id=session_id,
            base_model=base_model,
            lora_rank=lora_config.rank,
        )

        # Load base model
        base = self._load_base_model(base_model)

        # Create PEFT config
        peft_config = LoraConfig(
            task_type=TaskType.CAUSAL_LM,
            r=lora_config.rank,
            lora_alpha=lora_config.alpha,
            lora_dropout=lora_config.dropout,
            target_modules=lora_config.target_modules,
            bias="none",
        )

        # Wrap with LoRA
        model = get_peft_model(base, peft_config)
        model.print_trainable_parameters()

        # Create optimizer (only for LoRA parameters)
        trainable_params = [p for p in model.parameters() if p.requires_grad]
        optimizer = torch.optim.AdamW(
            trainable_params,
            lr=optimizer_config.learning_rate,
            weight_decay=optimizer_config.weight_decay,
            betas=(optimizer_config.beta1, optimizer_config.beta2),
            eps=optimizer_config.epsilon,
        )

        # Store session
        self.sessions[session_id] = SessionState(
            session_id=session_id,
            model=model,
            optimizer=optimizer,
            lora_config=lora_config,
            optimizer_config=optimizer_config,
        )

        logger.info("session_created", session_id=session_id)
        return session_id

    def forward_backward(
        self,
        session_id: str,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
        labels: torch.Tensor,
        loss_weights: Optional[torch.Tensor] = None,
    ) -> ForwardBackwardResult:
        """Compute forward pass and gradients."""

        session = self._get_session(session_id)
        model = session.model
        model.train()

        # Move to device
        input_ids = input_ids.to(self.device)
        attention_mask = attention_mask.to(self.device)
        labels = labels.to(self.device)
        if loss_weights is not None:
            loss_weights = loss_weights.to(self.device)

        # Forward pass
        outputs = model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            labels=labels,
        )

        # Compute loss (with optional weighting)
        if loss_weights is not None:
            # Custom weighted cross-entropy
            logits = outputs.logits
            shift_logits = logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            shift_weights = loss_weights[..., 1:].contiguous()

            loss_fct = torch.nn.CrossEntropyLoss(reduction='none')
            token_losses = loss_fct(
                shift_logits.view(-1, shift_logits.size(-1)),
                shift_labels.view(-1)
            )
            token_losses = token_losses.view(shift_labels.size())
            loss = (token_losses * shift_weights).sum() / shift_weights.sum().clamp(min=1)
        else:
            loss = outputs.loss

        # Backward pass
        loss.backward()

        # Compute logprobs for return
        with torch.no_grad():
            logprobs = self._compute_logprobs(outputs.logits, labels)

        tokens_processed = int(attention_mask.sum().item())
        session.tokens_processed += tokens_processed

        logger.debug(
            "forward_backward_complete",
            session_id=session_id,
            loss=loss.item(),
            tokens=tokens_processed,
        )

        return ForwardBackwardResult(
            loss=loss.item(),
            logprobs=logprobs.cpu().tolist(),
            tokens_processed=tokens_processed,
        )

    def optim_step(self, session_id: str) -> int:
        """Apply gradients and update weights."""

        session = self._get_session(session_id)

        # Gradient clipping
        if session.optimizer_config.grad_clip is not None:
            torch.nn.utils.clip_grad_norm_(
                session.model.parameters(),
                session.optimizer_config.grad_clip,
            )

        # Optimizer step
        session.optimizer.step()
        session.optimizer.zero_grad()
        session.step_count += 1

        logger.debug(
            "optim_step_complete",
            session_id=session_id,
            step=session.step_count,
        )

        return session.step_count

    def sample(
        self,
        session_id: str,
        prompt: str,
        max_tokens: int = 256,
        temperature: float = 1.0,
        top_p: float = 1.0,
        top_k: int = 0,
        include_logprobs: bool = False,
    ) -> SampleResult:
        """Generate text completion."""

        session = self._get_session(session_id)
        model = session.model
        model.eval()

        # Get tokenizer for base model
        base_model_name = list(self.tokenizers.keys())[0]  # MVP: single model
        tokenizer = self.tokenizers[base_model_name]

        # Tokenize prompt
        inputs = tokenizer(prompt, return_tensors="pt").to(self.device)
        prompt_length = inputs.input_ids.shape[1]

        # Generate
        with torch.no_grad():
            outputs = model.generate(
                **inputs,
                max_new_tokens=max_tokens,
                temperature=temperature if temperature > 0 else 1.0,
                top_p=top_p,
                top_k=top_k if top_k > 0 else None,
                do_sample=temperature > 0,
                pad_token_id=tokenizer.pad_token_id,
                eos_token_id=tokenizer.eos_token_id,
                output_scores=include_logprobs,
                return_dict_in_generate=True,
            )

        # Decode
        generated_ids = outputs.sequences[0, prompt_length:]
        text = tokenizer.decode(generated_ids, skip_special_tokens=True)

        # Compute logprobs if requested
        logprobs = None
        if include_logprobs and outputs.scores:
            logprobs = []
            for i, scores in enumerate(outputs.scores):
                log_probs = torch.nn.functional.log_softmax(scores[0], dim=-1)
                token_id = generated_ids[i].item()
                logprobs.append(log_probs[token_id].item())

        # Determine finish reason
        finish_reason = "length"
        if generated_ids[-1].item() == tokenizer.eos_token_id:
            finish_reason = "stop"

        return SampleResult(
            text=text,
            token_ids=generated_ids.cpu().tolist(),
            logprobs=logprobs,
            finish_reason=finish_reason,
        )

    def save_state(
        self,
        session_id: str,
        checkpoint_name: str,
        include_optimizer: bool = True,
    ) -> str:
        """Save LoRA weights and optimizer state."""

        session = self._get_session(session_id)

        # Create checkpoint directory
        checkpoint_path = self.checkpoint_dir / session_id / checkpoint_name
        checkpoint_path.mkdir(parents=True, exist_ok=True)

        # Save adapter weights (safetensors)
        session.model.save_pretrained(checkpoint_path)

        # Save optimizer state
        if include_optimizer:
            torch.save({
                'optimizer_state_dict': session.optimizer.state_dict(),
                'step_count': session.step_count,
                'tokens_processed': session.tokens_processed,
                'lora_config': session.lora_config,
                'optimizer_config': session.optimizer_config,
            }, checkpoint_path / "training_state.pt")

        logger.info(
            "checkpoint_saved",
            session_id=session_id,
            checkpoint=checkpoint_name,
            path=str(checkpoint_path),
        )

        return str(checkpoint_path)

    def load_state(
        self,
        session_id: str,
        checkpoint_path: str,
        load_optimizer: bool = True,
    ) -> None:
        """Load LoRA weights and optimizer state."""

        session = self._get_session(session_id)
        checkpoint_path = Path(checkpoint_path)

        # Load adapter weights
        session.model = PeftModel.from_pretrained(
            session.model.base_model,
            checkpoint_path,
        )

        # Load optimizer state
        if load_optimizer and (checkpoint_path / "training_state.pt").exists():
            state = torch.load(checkpoint_path / "training_state.pt")
            session.optimizer.load_state_dict(state['optimizer_state_dict'])
            session.step_count = state['step_count']
            session.tokens_processed = state['tokens_processed']

        logger.info(
            "checkpoint_loaded",
            session_id=session_id,
            path=str(checkpoint_path),
        )

    def get_session_status(self, session_id: str) -> Dict[str, Any]:
        """Get session status."""
        session = self._get_session(session_id)
        return {
            "session_id": session_id,
            "step_count": session.step_count,
            "tokens_processed": session.tokens_processed,
            "lora_rank": session.lora_config.rank,
            "learning_rate": session.optimizer_config.learning_rate,
        }

    def delete_session(self, session_id: str) -> None:
        """Delete a training session."""
        if session_id in self.sessions:
            del self.sessions[session_id]
            logger.info("session_deleted", session_id=session_id)

    def _get_session(self, session_id: str) -> SessionState:
        """Get session or raise error."""
        if session_id not in self.sessions:
            raise ValueError(f"Session {session_id} not found")
        return self.sessions[session_id]

    def _compute_logprobs(
        self,
        logits: torch.Tensor,
        labels: torch.Tensor,
    ) -> torch.Tensor:
        """Compute per-token log probabilities."""
        # Shift for next-token prediction
        shift_logits = logits[..., :-1, :].contiguous()
        shift_labels = labels[..., 1:].contiguous()

        # Compute log probs
        log_probs = torch.nn.functional.log_softmax(shift_logits, dim=-1)

        # Gather log probs for actual tokens
        token_logprobs = torch.gather(
            log_probs,
            dim=-1,
            index=shift_labels.unsqueeze(-1)
        ).squeeze(-1)

        return token_logprobs
```

---

#### Task 1.3: REST API Server

**Create `services/training-service/src/server.py`:**

```python
"""FastAPI server for training service."""

from contextlib import asynccontextmanager
from typing import List, Optional
import os

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field
import torch
import structlog

from .backend import (
    TrainingBackend,
    LoraConfiguration,
    OptimizerConfiguration,
    ForwardBackwardResult,
    SampleResult,
)

logger = structlog.get_logger()

# Global backend instance
backend: Optional[TrainingBackend] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize backend on startup."""
    global backend

    backend = TrainingBackend(
        model_cache_dir=os.getenv("MODEL_CACHE_DIR", "/models"),
        checkpoint_dir=os.getenv("CHECKPOINT_DIR", "/checkpoints"),
        device="cuda" if torch.cuda.is_available() else "cpu",
    )

    logger.info("training_service_started")
    yield
    logger.info("training_service_stopped")


app = FastAPI(
    title="Basilica Training Service",
    version="0.1.0",
    lifespan=lifespan,
)


# === Request/Response Models ===

class LoraConfigRequest(BaseModel):
    rank: int = Field(default=32, ge=1, le=256)
    alpha: int = Field(default=64, ge=1, le=512)
    dropout: float = Field(default=0.05, ge=0.0, le=0.5)
    target_modules: Optional[List[str]] = None


class OptimizerConfigRequest(BaseModel):
    learning_rate: float = Field(default=1e-4, gt=0)
    weight_decay: float = Field(default=0.01, ge=0)
    beta1: float = Field(default=0.9, ge=0, le=1)
    beta2: float = Field(default=0.999, ge=0, le=1)
    epsilon: float = Field(default=1e-8, gt=0)
    grad_clip: Optional[float] = Field(default=1.0, gt=0)


class CreateSessionRequest(BaseModel):
    session_id: str
    base_model: str
    lora_config: Optional[LoraConfigRequest] = None
    optimizer_config: Optional[OptimizerConfigRequest] = None
    seed: Optional[int] = None


class CreateSessionResponse(BaseModel):
    session_id: str
    status: str = "created"


class ForwardBackwardRequest(BaseModel):
    input_ids: List[List[int]]
    attention_mask: List[List[int]]
    labels: List[List[int]]
    loss_weights: Optional[List[List[float]]] = None


class ForwardBackwardResponse(BaseModel):
    loss: float
    logprobs: List[List[float]]
    tokens_processed: int


class OptimStepResponse(BaseModel):
    step: int


class SampleRequest(BaseModel):
    prompt: str
    max_tokens: int = Field(default=256, ge=1, le=4096)
    temperature: float = Field(default=1.0, ge=0, le=2.0)
    top_p: float = Field(default=1.0, ge=0, le=1.0)
    top_k: int = Field(default=0, ge=0)
    include_logprobs: bool = False


class SampleResponse(BaseModel):
    text: str
    token_ids: List[int]
    logprobs: Optional[List[float]] = None
    finish_reason: str


class SaveStateRequest(BaseModel):
    checkpoint_name: str
    include_optimizer: bool = True


class SaveStateResponse(BaseModel):
    checkpoint_path: str


class LoadStateRequest(BaseModel):
    checkpoint_path: str
    load_optimizer: bool = True


class SessionStatusResponse(BaseModel):
    session_id: str
    step_count: int
    tokens_processed: int
    lora_rank: int
    learning_rate: float


# === Health Check ===

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "cuda_available": torch.cuda.is_available(),
        "gpu_count": torch.cuda.device_count() if torch.cuda.is_available() else 0,
    }


# === Session Management ===

@app.post("/sessions", response_model=CreateSessionResponse)
async def create_session(request: CreateSessionRequest):
    """Create a new training session."""
    try:
        lora_config = LoraConfiguration(
            **(request.lora_config.model_dump() if request.lora_config else {})
        )
        optimizer_config = OptimizerConfiguration(
            **(request.optimizer_config.model_dump() if request.optimizer_config else {})
        )

        backend.create_session(
            session_id=request.session_id,
            base_model=request.base_model,
            lora_config=lora_config,
            optimizer_config=optimizer_config,
            seed=request.seed,
        )

        return CreateSessionResponse(session_id=request.session_id)

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.exception("create_session_failed", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@app.get("/sessions/{session_id}", response_model=SessionStatusResponse)
async def get_session(session_id: str):
    """Get session status."""
    try:
        status = backend.get_session_status(session_id)
        return SessionStatusResponse(**status)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a training session."""
    try:
        backend.delete_session(session_id)
        return {"status": "deleted"}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


# === Training Operations ===

@app.post("/sessions/{session_id}/forward_backward", response_model=ForwardBackwardResponse)
async def forward_backward(session_id: str, request: ForwardBackwardRequest):
    """Compute forward pass and gradients."""
    try:
        # Convert to tensors
        input_ids = torch.tensor(request.input_ids)
        attention_mask = torch.tensor(request.attention_mask)
        labels = torch.tensor(request.labels)
        loss_weights = torch.tensor(request.loss_weights) if request.loss_weights else None

        result = backend.forward_backward(
            session_id=session_id,
            input_ids=input_ids,
            attention_mask=attention_mask,
            labels=labels,
            loss_weights=loss_weights,
        )

        return ForwardBackwardResponse(
            loss=result.loss,
            logprobs=[result.logprobs],  # Wrap in list for batch dimension
            tokens_processed=result.tokens_processed,
        )

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except Exception as e:
        logger.exception("forward_backward_failed", error=str(e))
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@app.post("/sessions/{session_id}/optim_step", response_model=OptimStepResponse)
async def optim_step(session_id: str):
    """Apply gradients and update weights."""
    try:
        step = backend.optim_step(session_id)
        return OptimStepResponse(step=step)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


# === Inference ===

@app.post("/sessions/{session_id}/sample", response_model=SampleResponse)
async def sample(session_id: str, request: SampleRequest):
    """Generate text completion."""
    try:
        result = backend.sample(
            session_id=session_id,
            prompt=request.prompt,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            top_p=request.top_p,
            top_k=request.top_k,
            include_logprobs=request.include_logprobs,
        )

        return SampleResponse(
            text=result.text,
            token_ids=result.token_ids,
            logprobs=result.logprobs,
            finish_reason=result.finish_reason,
        )

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


# === Checkpoints ===

@app.post("/sessions/{session_id}/save", response_model=SaveStateResponse)
async def save_state(session_id: str, request: SaveStateRequest):
    """Save checkpoint."""
    try:
        path = backend.save_state(
            session_id=session_id,
            checkpoint_name=request.checkpoint_name,
            include_optimizer=request.include_optimizer,
        )
        return SaveStateResponse(checkpoint_path=path)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@app.post("/sessions/{session_id}/load")
async def load_state(session_id: str, request: LoadStateRequest):
    """Load checkpoint."""
    try:
        backend.load_state(
            session_id=session_id,
            checkpoint_path=request.checkpoint_path,
            load_optimizer=request.load_optimizer,
        )
        return {"status": "loaded"}
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
```

---

#### Task 1.4: Unit Tests

**Create `services/training-service/tests/test_backend.py`:**

```python
"""Tests for training backend."""

import pytest
import torch
from src.backend import (
    TrainingBackend,
    LoraConfiguration,
    OptimizerConfiguration,
)


@pytest.fixture
def backend(tmp_path):
    """Create backend with temp directories."""
    return TrainingBackend(
        model_cache_dir=str(tmp_path / "models"),
        checkpoint_dir=str(tmp_path / "checkpoints"),
        device="cuda" if torch.cuda.is_available() else "cpu",
    )


@pytest.fixture
def session_id():
    return "test-session-001"


@pytest.fixture
def small_model():
    # Use a small model for testing
    return "facebook/opt-125m"


class TestTrainingBackend:

    def test_create_session(self, backend, session_id, small_model):
        """Test session creation."""
        result = backend.create_session(
            session_id=session_id,
            base_model=small_model,
            lora_config=LoraConfiguration(rank=8),
            optimizer_config=OptimizerConfiguration(),
        )

        assert result == session_id
        assert session_id in backend.sessions

    def test_forward_backward(self, backend, session_id, small_model):
        """Test forward-backward pass."""
        backend.create_session(
            session_id=session_id,
            base_model=small_model,
            lora_config=LoraConfiguration(rank=8),
            optimizer_config=OptimizerConfiguration(),
        )

        # Create dummy batch
        input_ids = torch.randint(0, 1000, (1, 32))
        attention_mask = torch.ones_like(input_ids)
        labels = input_ids.clone()

        result = backend.forward_backward(
            session_id=session_id,
            input_ids=input_ids,
            attention_mask=attention_mask,
            labels=labels,
        )

        assert result.loss > 0
        assert result.tokens_processed == 32

    def test_optim_step(self, backend, session_id, small_model):
        """Test optimizer step."""
        backend.create_session(
            session_id=session_id,
            base_model=small_model,
            lora_config=LoraConfiguration(rank=8),
            optimizer_config=OptimizerConfiguration(),
        )

        # Do forward-backward first
        input_ids = torch.randint(0, 1000, (1, 32))
        attention_mask = torch.ones_like(input_ids)
        labels = input_ids.clone()

        backend.forward_backward(
            session_id=session_id,
            input_ids=input_ids,
            attention_mask=attention_mask,
            labels=labels,
        )

        # Optimizer step
        step = backend.optim_step(session_id)
        assert step == 1

    def test_sample(self, backend, session_id, small_model):
        """Test text generation."""
        backend.create_session(
            session_id=session_id,
            base_model=small_model,
            lora_config=LoraConfiguration(rank=8),
            optimizer_config=OptimizerConfiguration(),
        )

        result = backend.sample(
            session_id=session_id,
            prompt="Hello, world!",
            max_tokens=10,
            temperature=0.7,
        )

        assert len(result.text) > 0
        assert len(result.token_ids) > 0

    def test_save_load_checkpoint(self, backend, session_id, small_model):
        """Test checkpoint save/load."""
        backend.create_session(
            session_id=session_id,
            base_model=small_model,
            lora_config=LoraConfiguration(rank=8),
            optimizer_config=OptimizerConfiguration(),
        )

        # Do some training
        input_ids = torch.randint(0, 1000, (1, 32))
        attention_mask = torch.ones_like(input_ids)
        labels = input_ids.clone()

        backend.forward_backward(session_id, input_ids, attention_mask, labels)
        backend.optim_step(session_id)

        # Save
        path = backend.save_state(session_id, "checkpoint-1")
        assert "checkpoint-1" in path

        # Load
        backend.load_state(session_id, path)
        status = backend.get_session_status(session_id)
        assert status["step_count"] == 1
```

---

### Week 2: Kubernetes Integration

#### Task 2.1: TrainingSession CRD

**Create `crates/basilica-operator/src/crd/training_session.rs`:**

```rust
//! TrainingSession CRD for managing training workloads.

use kube::CustomResource;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

/// LoRA configuration for the training session.
#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct LoraConfig {
    /// LoRA rank (default: 32)
    #[serde(default = "default_rank")]
    pub rank: u32,

    /// LoRA alpha scaling factor (default: 64)
    #[serde(default = "default_alpha")]
    pub alpha: u32,

    /// Dropout rate (default: 0.05)
    #[serde(default = "default_dropout")]
    pub dropout: f32,

    /// Target modules for LoRA
    #[serde(default = "default_target_modules")]
    pub target_modules: Vec<String>,
}

fn default_rank() -> u32 { 32 }
fn default_alpha() -> u32 { 64 }
fn default_dropout() -> f32 { 0.05 }
fn default_target_modules() -> Vec<String> {
    vec![
        "q_proj".into(), "k_proj".into(), "v_proj".into(), "o_proj".into(),
    ]
}

/// Optimizer configuration.
#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct OptimizerConfig {
    /// Learning rate (default: 1e-4)
    #[serde(default = "default_learning_rate")]
    pub learning_rate: f64,

    /// Weight decay (default: 0.01)
    #[serde(default = "default_weight_decay")]
    pub weight_decay: f64,

    /// Gradient clipping (default: 1.0)
    #[serde(default = "default_grad_clip")]
    pub grad_clip: Option<f64>,
}

fn default_learning_rate() -> f64 { 1e-4 }
fn default_weight_decay() -> f64 { 0.01 }
fn default_grad_clip() -> Option<f64> { Some(1.0) }

/// Checkpoint storage configuration.
#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct CheckpointStorage {
    /// Storage backend: "r2", "s3", "gcs"
    pub backend: String,

    /// Bucket name
    pub bucket: String,

    /// Path prefix within bucket
    pub path: String,

    /// Credentials secret name
    #[serde(default)]
    pub credentials_secret: Option<String>,
}

/// GPU resource requirements.
#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct GpuResources {
    /// Number of GPUs (default: 1)
    #[serde(default = "default_gpu_count")]
    pub count: u32,

    /// GPU model filter
    #[serde(default)]
    pub model: Vec<String>,

    /// Minimum GPU memory in GB
    #[serde(default)]
    pub min_memory_gb: Option<u32>,
}

fn default_gpu_count() -> u32 { 1 }

/// TrainingSession spec.
#[derive(CustomResource, Clone, Debug, Default, Deserialize, Serialize, JsonSchema)]
#[kube(
    group = "basilica.ai",
    version = "v1",
    kind = "TrainingSession",
    namespaced,
    status = "TrainingSessionStatus",
    printcolumn = r#"{"name":"Phase", "type":"string", "jsonPath":".status.phase"}"#,
    printcolumn = r#"{"name":"Steps", "type":"integer", "jsonPath":".status.stepsCompleted"}"#,
    printcolumn = r#"{"name":"Model", "type":"string", "jsonPath":".spec.baseModel"}"#,
)]
#[serde(rename_all = "camelCase")]
pub struct TrainingSessionSpec {
    /// User ID owning this session
    pub user_id: String,

    /// Base model to fine-tune (HuggingFace model ID)
    pub base_model: String,

    /// LoRA configuration
    #[serde(default)]
    pub lora_config: LoraConfig,

    /// Optimizer configuration
    #[serde(default)]
    pub optimizer_config: OptimizerConfig,

    /// Checkpoint storage configuration
    pub checkpoint_storage: CheckpointStorage,

    /// GPU resource requirements
    #[serde(default)]
    pub gpu_resources: GpuResources,

    /// Training service image
    #[serde(default = "default_image")]
    pub image: String,

    /// Session TTL in seconds (default: 86400 = 24 hours)
    #[serde(default = "default_ttl")]
    pub ttl_seconds: u64,

    /// Random seed for reproducibility
    #[serde(default)]
    pub seed: Option<i64>,
}

fn default_image() -> String { "basilica/training:latest".into() }
fn default_ttl() -> u64 { 86400 }

/// Training session phase.
#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema, PartialEq, Eq)]
pub enum TrainingSessionPhase {
    #[default]
    Pending,
    Scheduling,
    Initializing,
    Ready,
    Suspended,
    Failed,
    Terminated,
}

/// TrainingSession status.
#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct TrainingSessionStatus {
    /// Current phase
    #[serde(default)]
    pub phase: TrainingSessionPhase,

    /// Training steps completed
    #[serde(default)]
    pub steps_completed: u64,

    /// Tokens processed
    #[serde(default)]
    pub tokens_processed: u64,

    /// Last checkpoint name
    #[serde(default)]
    pub last_checkpoint: Option<String>,

    /// Pod name
    #[serde(default)]
    pub pod_name: Option<String>,

    /// Service endpoint
    #[serde(default)]
    pub endpoint: Option<String>,

    /// Last activity timestamp (RFC 3339)
    #[serde(default)]
    pub last_activity: Option<String>,

    /// Error message if failed
    #[serde(default)]
    pub error: Option<String>,
}
```

---

#### Task 2.2: TrainingSession Controller

**Create `crates/basilica-operator/src/controllers/training_session_controller.rs`:**

```rust
//! Controller for TrainingSession CRD.

use std::sync::Arc;
use std::time::Duration;

use futures::StreamExt;
use k8s_openapi::api::core::v1::{Pod, PodSpec, Container, Service, ServiceSpec};
use k8s_openapi::api::core::v1::{ResourceRequirements, EnvVar};
use k8s_openapi::apimachinery::pkg::api::resource::Quantity;
use kube::{
    api::{Api, ListParams, Patch, PatchParams, PostParams},
    runtime::controller::{Action, Controller},
    runtime::watcher::Config,
    Client, Resource, ResourceExt,
};
use tracing::{info, warn, error, instrument};

use crate::crd::training_session::{
    TrainingSession, TrainingSessionPhase, TrainingSessionStatus,
};
use crate::error::Error;

/// Controller context.
pub struct Context {
    pub client: Client,
}

/// Reconcile a TrainingSession.
#[instrument(skip(ctx), fields(name = %session.name_any(), namespace = session.namespace()))]
pub async fn reconcile(
    session: Arc<TrainingSession>,
    ctx: Arc<Context>,
) -> Result<Action, Error> {
    let namespace = session.namespace().unwrap_or_default();
    let name = session.name_any();

    info!("reconciling training session");

    let sessions: Api<TrainingSession> = Api::namespaced(ctx.client.clone(), &namespace);
    let pods: Api<Pod> = Api::namespaced(ctx.client.clone(), &namespace);
    let services: Api<Service> = Api::namespaced(ctx.client.clone(), &namespace);

    // Get current status
    let phase = session.status.as_ref()
        .map(|s| s.phase.clone())
        .unwrap_or_default();

    match phase {
        TrainingSessionPhase::Pending => {
            // Create pod and service
            create_training_pod(&pods, &session).await?;
            create_training_service(&services, &session).await?;

            // Update status to Scheduling
            update_status(&sessions, &name, |status| {
                status.phase = TrainingSessionPhase::Scheduling;
            }).await?;

            Ok(Action::requeue(Duration::from_secs(5)))
        }

        TrainingSessionPhase::Scheduling => {
            // Check if pod is running
            let pod_name = format!("training-{}", name);
            match pods.get(&pod_name).await {
                Ok(pod) => {
                    let pod_phase = pod.status
                        .as_ref()
                        .and_then(|s| s.phase.clone())
                        .unwrap_or_default();

                    if pod_phase == "Running" {
                        // Update to Initializing
                        update_status(&sessions, &name, |status| {
                            status.phase = TrainingSessionPhase::Initializing;
                            status.pod_name = Some(pod_name);
                        }).await?;
                    }
                }
                Err(_) => {
                    // Pod not found, recreate
                    create_training_pod(&pods, &session).await?;
                }
            }

            Ok(Action::requeue(Duration::from_secs(5)))
        }

        TrainingSessionPhase::Initializing => {
            // Check if service is ready (health check)
            let endpoint = format!("http://training-{}.{}.svc:8000", name, namespace);

            // TODO: Actual health check
            // For now, just transition to Ready
            update_status(&sessions, &name, |status| {
                status.phase = TrainingSessionPhase::Ready;
                status.endpoint = Some(endpoint);
            }).await?;

            Ok(Action::requeue(Duration::from_secs(30)))
        }

        TrainingSessionPhase::Ready => {
            // Monitor session health
            // Check TTL
            // Update metrics
            Ok(Action::requeue(Duration::from_secs(60)))
        }

        TrainingSessionPhase::Suspended | TrainingSessionPhase::Failed | TrainingSessionPhase::Terminated => {
            // No action needed
            Ok(Action::requeue(Duration::from_secs(300)))
        }
    }
}

/// Create the training pod.
async fn create_training_pod(
    pods: &Api<Pod>,
    session: &TrainingSession,
) -> Result<(), Error> {
    let name = session.name_any();
    let spec = &session.spec;

    let pod_name = format!("training-{}", name);

    // Check if pod already exists
    if pods.get(&pod_name).await.is_ok() {
        return Ok(());
    }

    // Build GPU resource requirements
    let mut resources = ResourceRequirements::default();
    let mut limits = std::collections::BTreeMap::new();
    limits.insert(
        "nvidia.com/gpu".to_string(),
        Quantity(spec.gpu_resources.count.to_string()),
    );
    resources.limits = Some(limits.clone());
    resources.requests = Some(limits);

    // Environment variables
    let env_vars = vec![
        EnvVar {
            name: "MODEL_CACHE_DIR".to_string(),
            value: Some("/models".to_string()),
            ..Default::default()
        },
        EnvVar {
            name: "CHECKPOINT_DIR".to_string(),
            value: Some("/checkpoints".to_string()),
            ..Default::default()
        },
        EnvVar {
            name: "BASE_MODEL".to_string(),
            value: Some(spec.base_model.clone()),
            ..Default::default()
        },
    ];

    let pod = Pod {
        metadata: kube::api::ObjectMeta {
            name: Some(pod_name.clone()),
            namespace: session.namespace(),
            labels: Some(std::collections::BTreeMap::from([
                ("app".to_string(), "basilica-training".to_string()),
                ("session".to_string(), name.clone()),
            ])),
            owner_references: Some(vec![session.controller_owner_ref(&()).unwrap()]),
            ..Default::default()
        },
        spec: Some(PodSpec {
            containers: vec![Container {
                name: "training".to_string(),
                image: Some(spec.image.clone()),
                ports: Some(vec![k8s_openapi::api::core::v1::ContainerPort {
                    container_port: 8000,
                    name: Some("http".to_string()),
                    ..Default::default()
                }]),
                resources: Some(resources),
                env: Some(env_vars),
                ..Default::default()
            }],
            restart_policy: Some("Never".to_string()),
            ..Default::default()
        }),
        ..Default::default()
    };

    pods.create(&PostParams::default(), &pod).await?;
    info!(pod = %pod_name, "created training pod");

    Ok(())
}

/// Create the training service.
async fn create_training_service(
    services: &Api<Service>,
    session: &TrainingSession,
) -> Result<(), Error> {
    let name = session.name_any();
    let svc_name = format!("training-{}", name);

    // Check if service already exists
    if services.get(&svc_name).await.is_ok() {
        return Ok(());
    }

    let service = Service {
        metadata: kube::api::ObjectMeta {
            name: Some(svc_name.clone()),
            namespace: session.namespace(),
            owner_references: Some(vec![session.controller_owner_ref(&()).unwrap()]),
            ..Default::default()
        },
        spec: Some(ServiceSpec {
            selector: Some(std::collections::BTreeMap::from([
                ("session".to_string(), name.clone()),
            ])),
            ports: Some(vec![k8s_openapi::api::core::v1::ServicePort {
                port: 8000,
                target_port: Some(k8s_openapi::apimachinery::pkg::util::intstr::IntOrString::Int(8000)),
                name: Some("http".to_string()),
                ..Default::default()
            }]),
            ..Default::default()
        }),
        ..Default::default()
    };

    services.create(&PostParams::default(), &service).await?;
    info!(service = %svc_name, "created training service");

    Ok(())
}

/// Update session status.
async fn update_status<F>(
    sessions: &Api<TrainingSession>,
    name: &str,
    update_fn: F,
) -> Result<(), Error>
where
    F: FnOnce(&mut TrainingSessionStatus),
{
    let session = sessions.get(name).await?;
    let mut status = session.status.clone().unwrap_or_default();
    update_fn(&mut status);

    let patch = serde_json::json!({
        "status": status
    });

    sessions.patch_status(
        name,
        &PatchParams::default(),
        &Patch::Merge(&patch),
    ).await?;

    Ok(())
}

/// Handle errors.
fn error_policy(
    _session: Arc<TrainingSession>,
    error: &Error,
    _ctx: Arc<Context>,
) -> Action {
    error!(%error, "reconciliation error");
    Action::requeue(Duration::from_secs(30))
}

/// Start the controller.
pub async fn run(client: Client) -> Result<(), Error> {
    let sessions: Api<TrainingSession> = Api::all(client.clone());
    let pods: Api<Pod> = Api::all(client.clone());
    let services: Api<Service> = Api::all(client.clone());

    let ctx = Arc::new(Context { client });

    Controller::new(sessions, Config::default())
        .owns(pods, Config::default())
        .owns(services, Config::default())
        .run(reconcile, error_policy, ctx)
        .for_each(|res| async move {
            match res {
                Ok(o) => info!("reconciled {:?}", o),
                Err(e) => error!("reconcile failed: {:?}", e),
            }
        })
        .await;

    Ok(())
}
```

---

### Week 3: API & SDK

#### Task 3.1: Basilica API Session Routes

> **Note**: The API creates TrainingSession CRDs and HTTPRoutes. Actual training
> operations (forward_backward, sample, etc.) are routed by Envoy Gateway directly
> to the Training Pod via HTTPRoute, not proxied through the API.

**Create `crates/basilica-api/src/api/routes/training.rs`:**

```rust
//! Training Session API routes.
//!
//! These routes handle session lifecycle (create/delete) by creating
//! TrainingSession CRDs and HTTPRoutes for Envoy Gateway.
//!
//! Training operations (forward_backward, sample, etc.) are NOT proxied
//! through this API - they go directly through Envoy Gateway to the
//! Training Service pod via the HTTPRoute.

use axum::{
    extract::{Path, State},
    http::StatusCode,
    routing::{delete, get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use tracing::{info, error};

use crate::state::AppState;
use crate::error::ApiError;

pub fn routes() -> Router<AppState> {
    Router::new()
        // Session lifecycle management (creates CRD + HTTPRoute)
        .route("/sessions", post(create_session))
        .route("/sessions", get(list_sessions))
        .route("/sessions/:session_id", get(get_session))
        .route("/sessions/:session_id", delete(delete_session))
    // Note: /sessions/:id/forward_backward, /sample, etc. are routed
    // by Envoy Gateway via HTTPRoute directly to the Training Pod
}

// === Request/Response Types ===

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CreateSessionRequest {
    pub base_model: String,
    pub lora_config: Option<LoraConfigRequest>,
    pub optimizer_config: Option<OptimizerConfigRequest>,
    pub seed: Option<i64>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LoraConfigRequest {
    pub rank: Option<u32>,
    pub alpha: Option<u32>,
    pub dropout: Option<f32>,
    pub target_modules: Option<Vec<String>>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct OptimizerConfigRequest {
    pub learning_rate: Option<f64>,
    pub weight_decay: Option<f64>,
    pub grad_clip: Option<f64>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CreateSessionResponse {
    pub session_id: String,
    pub status: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ForwardBackwardRequest {
    pub input_ids: Vec<Vec<i32>>,
    pub attention_mask: Vec<Vec<i32>>,
    pub labels: Vec<Vec<i32>>,
    pub loss_weights: Option<Vec<Vec<f32>>>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ForwardBackwardResponse {
    pub loss: f32,
    pub logprobs: Vec<Vec<f32>>,
    pub tokens_processed: u64,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct OptimStepResponse {
    pub step: u64,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SampleRequest {
    pub prompt: String,
    pub max_tokens: Option<u32>,
    pub temperature: Option<f32>,
    pub top_p: Option<f32>,
    pub top_k: Option<u32>,
    pub include_logprobs: Option<bool>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SampleResponse {
    pub text: String,
    pub token_ids: Vec<i32>,
    pub logprobs: Option<Vec<f32>>,
    pub finish_reason: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct SaveStateRequest {
    pub checkpoint_name: String,
    pub include_optimizer: Option<bool>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct SaveStateResponse {
    pub checkpoint_path: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LoadStateRequest {
    pub checkpoint_path: String,
    pub load_optimizer: Option<bool>,
}

// === Handlers ===
// Note: Only session lifecycle handlers are here.
// Training operations go through Envoy Gateway directly to the Training Pod.

async fn create_session(
    State(state): State<AppState>,
    Json(request): Json<CreateSessionRequest>,
) -> Result<Json<CreateSessionResponse>, ApiError> {
    let user = state.get_authenticated_user()?;
    let namespace = format!("u-{}", user.id);

    info!(model = %request.base_model, user = %user.id, "creating training session");

    let session_id = uuid::Uuid::new_v4().to_string();

    // 1. Create TrainingSession CRD
    let training_session = TrainingSession {
        metadata: ObjectMeta {
            name: Some(session_id.clone()),
            namespace: Some(namespace.clone()),
            ..Default::default()
        },
        spec: TrainingSessionSpec {
            user_id: user.id.clone(),
            base_model: request.base_model.clone(),
            lora_config: request.lora_config.clone().unwrap_or_default().into(),
            optimizer_config: request.optimizer_config.clone().unwrap_or_default().into(),
            // ... other fields
        },
        status: None,
    };

    state.k8s_client
        .create_training_session(&namespace, &training_session)
        .await
        .map_err(|e| ApiError::internal(format!("Failed to create session: {}", e)))?;

    // 2. Create HTTPRoute for Envoy Gateway
    let http_route = create_training_http_route(&session_id, &namespace);
    state.k8s_client
        .create_http_route(&namespace, &http_route)
        .await
        .map_err(|e| ApiError::internal(format!("Failed to create HTTPRoute: {}", e)))?;

    Ok(Json(CreateSessionResponse {
        session_id,
        status: "pending".to_string(),
        // Include the endpoint URL that SDK should use for training operations
        endpoint: format!("https://api.basilica.ai/sessions/{}/", session_id),
    }))
}

async fn list_sessions(
    State(state): State<AppState>,
) -> Result<Json<ListSessionsResponse>, ApiError> {
    let user = state.get_authenticated_user()?;
    let namespace = format!("u-{}", user.id);

    let sessions = state.k8s_client
        .list_training_sessions(&namespace)
        .await
        .map_err(|e| ApiError::internal(format!("Failed to list sessions: {}", e)))?;

    Ok(Json(ListSessionsResponse { sessions }))
}

async fn get_session(
    State(state): State<AppState>,
    Path(session_id): Path<String>,
) -> Result<Json<SessionStatusResponse>, ApiError> {
    let user = state.get_authenticated_user()?;
    let namespace = format!("u-{}", user.id);

    let session = state.k8s_client
        .get_training_session(&namespace, &session_id)
        .await
        .map_err(|e| ApiError::not_found(format!("Session not found: {}", e)))?;

    Ok(Json(SessionStatusResponse::from(session)))
}

async fn delete_session(
    State(state): State<AppState>,
    Path(session_id): Path<String>,
) -> Result<StatusCode, ApiError> {
    let user = state.get_authenticated_user()?;
    let namespace = format!("u-{}", user.id);

    // Delete CRD - K8s will cascade delete Pod, Service, HTTPRoute via ownerRefs
    state.k8s_client
        .delete_training_session(&namespace, &session_id)
        .await
        .map_err(|e| ApiError::internal(format!("Failed to delete session: {}", e)))?;

    Ok(StatusCode::NO_CONTENT)
}

// Helper to create HTTPRoute for a training session
fn create_training_http_route(session_id: &str, namespace: &str) -> HttpRoute {
    HttpRoute {
        metadata: ObjectMeta {
            name: Some(format!("training-{}", session_id)),
            namespace: Some(namespace.to_string()),
            ..Default::default()
        },
        spec: HttpRouteSpec {
            parent_refs: vec![ParentReference {
                name: "basilica-gateway".to_string(),
                namespace: Some("envoy-gateway-system".to_string()),
                ..Default::default()
            }],
            hostnames: vec!["api.basilica.ai".to_string()],
            rules: vec![HttpRouteRule {
                matches: vec![HttpRouteMatch {
                    path: Some(HttpPathMatch::PathPrefix {
                        value: format!("/sessions/{}/", session_id),
                    }),
                    ..Default::default()
                }],
                backend_refs: vec![HttpBackendRef {
                    name: format!("training-{}", session_id),
                    port: Some(8000),
                    ..Default::default()
                }],
                ..Default::default()
            }],
        },
        status: None,
    }
}
```

> **Note**: Training operations (`forward_backward`, `optim_step`, `sample`, `save`, `load`)
> are routed directly by Envoy Gateway to the Training Pod via HTTPRoute.
> The SDK calls these endpoints at `https://api.basilica.ai/sessions/{id}/forward_backward`, etc.

---

#### Task 3.2: Python SDK

**Create `sdk/python/basilica/training/__init__.py`:**

```python
"""Basilica Training SDK."""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
import os
import httpx


@dataclass
class LoraConfig:
    """LoRA adapter configuration."""
    rank: int = 32
    alpha: int = 64
    dropout: float = 0.05
    target_modules: List[str] = field(default_factory=lambda: [
        "q_proj", "k_proj", "v_proj", "o_proj",
    ])


@dataclass
class OptimizerConfig:
    """Optimizer configuration."""
    learning_rate: float = 1e-4
    weight_decay: float = 0.01
    grad_clip: Optional[float] = 1.0


@dataclass
class SamplingParams:
    """Sampling parameters."""
    max_tokens: int = 256
    temperature: float = 1.0
    top_p: float = 1.0
    top_k: int = 0
    include_logprobs: bool = False


@dataclass
class Datum:
    """Training example."""
    input_ids: List[int]
    labels: List[int]
    loss_weights: Optional[List[float]] = None


@dataclass
class ForwardBackwardResult:
    """Result of forward-backward pass."""
    loss: float
    logprobs: List[List[float]]
    tokens_processed: int


@dataclass
class Sample:
    """Generated sample."""
    text: str
    token_ids: List[int]
    logprobs: Optional[List[float]] = None
    finish_reason: str = "stop"


class ServiceClient:
    """Main entry point for Basilica Training API.

    Example:
        >>> client = ServiceClient()
        >>> training = client.create_lora_training_client(
        ...     "meta-llama/Llama-3.1-8B-Instruct",
        ...     rank=32,
        ... )
        >>>
        >>> # Training loop
        >>> for batch in dataloader:
        ...     result = training.forward_backward(batch)
        ...     print(f"Loss: {result.loss:.4f}")
        ...     training.optim_step()
        ...
        >>> training.save_state("checkpoint-final")
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        timeout: float = 300.0,
    ):
        self.api_key = api_key or os.environ.get("BASILICA_API_KEY")
        self.endpoint = endpoint or os.environ.get(
            "BASILICA_ENDPOINT", "http://localhost:8080"
        )
        self.timeout = timeout

        if not self.api_key:
            raise ValueError(
                "API key required. Set BASILICA_API_KEY or pass api_key parameter."
            )

        self._client = httpx.Client(
            base_url=self.endpoint,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=timeout,
        )

    def create_lora_training_client(
        self,
        base_model: str,
        rank: int = 32,
        alpha: int = 64,
        dropout: float = 0.05,
        target_modules: Optional[List[str]] = None,
        learning_rate: float = 1e-4,
        seed: Optional[int] = None,
    ) -> "TrainingClient":
        """Create a new LoRA training session.

        This creates a TrainingSession CRD and HTTPRoute in K8s. The returned
        TrainingClient will route training operations through Envoy Gateway
        directly to the Training Pod.

        Args:
            base_model: HuggingFace model ID (e.g., "meta-llama/Llama-3.1-8B")
            rank: LoRA rank
            alpha: LoRA alpha scaling factor
            dropout: LoRA dropout rate
            target_modules: Modules to apply LoRA to
            learning_rate: Initial learning rate
            seed: Random seed for reproducibility

        Returns:
            TrainingClient for the new session
        """
        # API creates TrainingSession CRD + HTTPRoute
        response = self._client.post(
            "/sessions",
            json={
                "baseModel": base_model,
                "loraConfig": {
                    "rank": rank,
                    "alpha": alpha,
                    "dropout": dropout,
                    "targetModules": target_modules or [
                        "q_proj", "k_proj", "v_proj", "o_proj",
                    ],
                },
                "optimizerConfig": {
                    "learningRate": learning_rate,
                },
                "seed": seed,
            },
        )
        response.raise_for_status()
        data = response.json()

        # The endpoint is the URL where training ops are routed via Envoy Gateway
        training_endpoint = data.get("endpoint", f"{self.endpoint}/sessions/{data['sessionId']}")

        return TrainingClient(
            session_id=data["sessionId"],
            api_client=self._client,
            training_endpoint=training_endpoint,
        )

    def create_training_client_from_checkpoint(
        self,
        checkpoint_path: str,
        load_optimizer: bool = True,
    ) -> "TrainingClient":
        """Resume training from a checkpoint.

        Args:
            checkpoint_path: Path to checkpoint in storage
            load_optimizer: Whether to load optimizer state

        Returns:
            TrainingClient for the restored session
        """
        # First create a new session, then load the checkpoint
        # This is MVP behavior - production would be more sophisticated
        raise NotImplementedError("Checkpoint resume not yet implemented in MVP")


class TrainingClient:
    """Client for a training session.

    Training operations (forward_backward, sample, etc.) are routed through
    Envoy Gateway directly to the Training Pod, not through the Basilica API.

    Example:
        >>> # Training loop
        >>> for batch in dataloader:
        ...     result = training.forward_backward(batch)
        ...     print(f"Loss: {result.loss:.4f}")
        ...     training.optim_step()
        ...
        >>> # Generate sample
        >>> sample = training.sample("Hello, world!", max_tokens=100)
        >>> print(sample.text)
        ...
        >>> # Save checkpoint
        >>> training.save_state("checkpoint-1000")
    """

    def __init__(
        self,
        session_id: str,
        api_client: httpx.Client,
        training_endpoint: str,
    ):
        """
        Args:
            session_id: The session ID
            api_client: Client for API calls (get_status, close)
            training_endpoint: Endpoint for training ops (routed via Envoy Gateway)
        """
        self._session_id = session_id
        self._api_client = api_client  # For session management calls
        # Training operations go through Envoy Gateway
        self._training_client = httpx.Client(
            base_url=training_endpoint,
            timeout=300.0,
        )

    @property
    def session_id(self) -> str:
        """Session ID."""
        return self._session_id

    def forward_backward(
        self,
        data: List[Datum],
        loss_fn: str = "cross_entropy",
    ) -> ForwardBackwardResult:
        """Compute forward pass and gradients.

        Note: This request goes through Envoy Gateway directly to the Training Pod.

        Args:
            data: List of training examples
            loss_fn: Loss function (currently only "cross_entropy")

        Returns:
            ForwardBackwardResult with loss and logprobs
        """
        # Batch the data
        input_ids = [d.input_ids for d in data]
        labels = [d.labels for d in data]
        loss_weights = [d.loss_weights for d in data] if data[0].loss_weights else None

        # Pad to same length
        max_len = max(len(ids) for ids in input_ids)
        input_ids = [ids + [0] * (max_len - len(ids)) for ids in input_ids]
        labels = [lbl + [-100] * (max_len - len(lbl)) for lbl in labels]
        attention_mask = [[1] * len(ids) + [0] * (max_len - len(ids)) for ids in input_ids]

        if loss_weights:
            loss_weights = [w + [0.0] * (max_len - len(w)) for w in loss_weights]

        # Training ops go through Envoy Gateway → Training Pod
        response = self._training_client.post(
            "/forward_backward",
            json={
                "inputIds": input_ids,
                "attentionMask": attention_mask,
                "labels": labels,
                "lossWeights": loss_weights,
            },
        )
        response.raise_for_status()
        data = response.json()

        return ForwardBackwardResult(
            loss=data["loss"],
            logprobs=data["logprobs"],
            tokens_processed=data["tokensProcessed"],
        )

    def optim_step(self) -> int:
        """Apply gradients and update weights.

        Note: This request goes through Envoy Gateway directly to the Training Pod.

        Returns:
            Current step count
        """
        # Training ops go through Envoy Gateway → Training Pod
        response = self._training_client.post("/optim_step")
        response.raise_for_status()
        data = response.json()
        return data["step"]

    def sample(
        self,
        prompt: str,
        max_tokens: int = 256,
        temperature: float = 1.0,
        top_p: float = 1.0,
        top_k: int = 0,
        include_logprobs: bool = False,
    ) -> Sample:
        """Generate text completion.

        Note: This request goes through Envoy Gateway directly to the Training Pod.

        Args:
            prompt: Input prompt
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            top_p: Nucleus sampling parameter
            top_k: Top-k sampling parameter
            include_logprobs: Whether to return log probabilities

        Returns:
            Generated sample
        """
        # Training ops go through Envoy Gateway → Training Pod
        response = self._training_client.post(
            "/sample",
            json={
                "prompt": prompt,
                "maxTokens": max_tokens,
                "temperature": temperature,
                "topP": top_p,
                "topK": top_k,
                "includeLogprobs": include_logprobs,
            },
        )
        response.raise_for_status()
        data = response.json()

        return Sample(
            text=data["text"],
            token_ids=data["tokenIds"],
            logprobs=data.get("logprobs"),
            finish_reason=data["finishReason"],
        )

    def save_state(
        self,
        checkpoint_name: str,
        include_optimizer: bool = True,
    ) -> str:
        """Save checkpoint.

        Note: This request goes through Envoy Gateway directly to the Training Pod.

        Args:
            checkpoint_name: Name for the checkpoint
            include_optimizer: Whether to save optimizer state

        Returns:
            Checkpoint path
        """
        # Training ops go through Envoy Gateway → Training Pod
        response = self._training_client.post(
            "/save",
            json={
                "checkpointName": checkpoint_name,
                "includeOptimizer": include_optimizer,
            },
        )
        response.raise_for_status()
        data = response.json()
        return data["checkpointPath"]

    def load_state(
        self,
        checkpoint_path: str,
        load_optimizer: bool = True,
    ) -> None:
        """Load checkpoint.

        Note: This request goes through Envoy Gateway directly to the Training Pod.

        Args:
            checkpoint_path: Path to checkpoint
            load_optimizer: Whether to load optimizer state
        """
        # Training ops go through Envoy Gateway → Training Pod
        response = self._training_client.post(
            "/load",
            json={
                "checkpointPath": checkpoint_path,
                "loadOptimizer": load_optimizer,
            },
        )
        response.raise_for_status()

    def get_status(self) -> Dict[str, Any]:
        """Get session status from the API.

        Note: This goes through the Basilica API, not Envoy Gateway.

        Returns:
            Status dictionary
        """
        # Session management goes through Basilica API
        response = self._api_client.get(
            f"/sessions/{self._session_id}",
        )
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        """Delete the training session.

        Note: This goes through the Basilica API, which deletes the CRD
        and cascades to delete Pod, Service, and HTTPRoute.
        """
        # Session management goes through Basilica API
        response = self._api_client.delete(
            f"/sessions/{self._session_id}",
        )
        response.raise_for_status()
        self._training_client.close()  # Clean up training client
```

---

### Week 4: Integration & Testing

#### Task 4.1: End-to-End Test

**Create `examples/training_example.py`:**

```python
"""Example training script using Basilica Training SDK."""

import os
from basilica.training import ServiceClient, Datum, SamplingParams

def main():
    # Initialize client
    client = ServiceClient(
        api_key=os.getenv("BASILICA_API_KEY", "test-key"),
        endpoint=os.getenv("BASILICA_ENDPOINT", "http://localhost:8080"),
    )

    # Create training session
    print("Creating training session...")
    training = client.create_lora_training_client(
        base_model="meta-llama/Llama-3.1-8B-Instruct",
        rank=32,
        alpha=64,
        learning_rate=1e-4,
    )
    print(f"Session created: {training.session_id}")

    # Example training data
    # In practice, you'd use a tokenizer to create these
    example_data = [
        Datum(
            input_ids=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
            labels=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
            loss_weights=[0, 0, 0, 0, 0, 1, 1, 1, 1, 1],  # Only train on completion
        )
    ]

    # Training loop
    print("\nTraining...")
    for step in range(10):
        # Forward-backward
        result = training.forward_backward(example_data)
        print(f"Step {step + 1}: loss={result.loss:.4f}, tokens={result.tokens_processed}")

        # Optimizer step
        training.optim_step()

    # Save checkpoint
    print("\nSaving checkpoint...")
    path = training.save_state("checkpoint-final")
    print(f"Saved to: {path}")

    # Generate sample
    print("\nGenerating sample...")
    sample = training.sample(
        prompt="Hello, world!",
        max_tokens=50,
        temperature=0.7,
    )
    print(f"Generated: {sample.text}")

    # Get status
    status = training.get_status()
    print(f"\nFinal status: {status}")

    # Cleanup
    print("\nCleaning up...")
    training.close()
    print("Done!")


if __name__ == "__main__":
    main()
```

---

## Deployment

### Docker Compose (Local Development)

> **Note**: For local development without K8s/Envoy Gateway, the SDK can
> connect directly to the training service. In production, requests flow
> through Envoy Gateway via HTTPRoute.

**Create `docker-compose.training.yml`:**

```yaml
version: '3.8'

services:
  training-service:
    build:
      context: services/training-service
      dockerfile: Dockerfile
    ports:
      - "8001:8000"
    environment:
      - MODEL_CACHE_DIR=/models
      - CHECKPOINT_DIR=/checkpoints
      - NVIDIA_VISIBLE_DEVICES=all
    volumes:
      - ./models:/models
      - ./checkpoints:/checkpoints
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 120s  # Model loading takes time

  # For local dev, SDK connects directly to training-service:8001
  # In production:
  #   - Basilica API creates TrainingSession CRD + HTTPRoute
  #   - Envoy Gateway routes /sessions/{id}/* to training-{id} Service
  #   - Operator reconciles CRD → creates Pod + Service
```

### Kubernetes Deployment (Production)

In production, the architecture uses:

1. **Basilica API** - Creates TrainingSession CRD and HTTPRoute on session creation
2. **Operator** - Reconciles TrainingSession CRD → creates Pod, Service
3. **Envoy Gateway** - Routes `/sessions/{id}/*` to `training-{id}` Service via HTTPRoute

See `docs/training-session-architecture.md` for the complete K8s resource flow.

### Kubernetes Manifest

**Create `orchestrator/k8s/training/training-session-example.yaml`:**

```yaml
apiVersion: basilica.ai/v1
kind: TrainingSession
metadata:
  name: example-training
  namespace: default
spec:
  userId: "user-123"
  baseModel: "meta-llama/Llama-3.1-8B-Instruct"
  loraConfig:
    rank: 32
    alpha: 64
    dropout: 0.05
  optimizerConfig:
    learningRate: 0.0001
    weightDecay: 0.01
    gradClip: 1.0
  checkpointStorage:
    backend: "r2"
    bucket: "training-checkpoints"
    path: "user-123/example-training"
  gpuResources:
    count: 1
    model:
      - "H100"
      - "A100"
  image: "basilica/training:latest"
  ttlSeconds: 86400
```

---

## Success Criteria

### MVP Complete When:

- [ ] Training service runs on single GPU
- [ ] `create_session()` creates LoRA adapter on base model
- [ ] `forward_backward()` computes loss and gradients
- [ ] `optim_step()` updates LoRA weights
- [ ] `sample()` generates text with current LoRA
- [ ] `save_state()` persists checkpoint to storage
- [ ] `load_state()` restores from checkpoint
- [ ] TrainingSession CRD creates and manages training pods
- [ ] Python SDK can run complete training loop
- [ ] Example script runs end-to-end

### Performance Targets (MVP):

- Session creation: < 60 seconds (model loading)
- Forward-backward: < 1 second per batch (8B model, batch size 1)
- Checkpoint save: < 30 seconds
- Sample generation: < 5 seconds for 100 tokens

---

## Next Steps (Post-MVP)

1. **vLLM Integration** - High-throughput inference
2. **Multi-GPU** - DeepSpeed ZeRO-3 for larger models
3. **QLoRA** - 4-bit quantization for memory efficiency
4. **Worker Pools** - Multi-tenant GPU sharing
5. **Billing** - Per-operation metering
6. **DPO/KTO** - Preference learning loss functions
