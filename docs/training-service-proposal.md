# Training Service Proposal

This document outlines the architecture and implementation plan for building a managed GPU training service on top of the Basilica infrastructure.

## Executive Summary

The goal is to build a high-level training API that abstracts away GPU infrastructure management, allowing developers to focus on their training logic while Basilica handles distributed training, hardware failures, and GPU orchestration.

**Key Value Proposition**: Developers write simple Python scripts with calls like `forward_backward()` and `optim_step()`, while Basilica manages efficient distributed training of large models (Llama 70B, Qwen 235B, etc.).

**Technology Stack**:
- **Control Plane**: Rust (Basilica API Gateway, Operator) - unchanged from existing Basilica
- **Training Backend**: Python with HuggingFace Transformers + PEFT
- **Inference Backend**: Integrated sampling within training service
- **Protocol**: HTTP/REST

This architecture leverages battle-tested ML infrastructure while maintaining Basilica's Rust control plane for orchestration.

---

## Training Service vs Current Basilica

| Aspect | Training Service | Current Basilica |
|--------|------------------|------------------|
| **Abstraction Level** | High-level training API (`forward_backward()`, `sample()`) | Infrastructure-level (Jobs, Deployments, Rentals) |
| **GPU Management** | Shared worker pools with time-slicing | Dedicated GPU allocation per workload |
| **Model Hosting** | Pre-loaded base models (Llama, Qwen) | User provides complete container images |
| **Training State** | Managed checkpoints with resume | User-managed storage sync |
| **Billing Model** | Per-operation (clock cycles) | Per-time (rental hours) |

### What Training Service Provides

1. **Core API Functions** (TrainingClient):
   - `forward_backward()` - Computes and accumulates gradients
   - `forward_backward_custom()` - Custom loss functions on logprobs
   - `forward()` - Forward pass without gradient computation
   - `optim_step()` - Updates model using accumulated gradients (Adam optimizer)
   - `save_state()` / `load_state()` - Checkpoint management
   - `load_state_with_optimizer()` - Resume with optimizer state
   - `save_weights_for_sampler()` - Export weights for inference
   - `save_weights_and_get_sampling_client()` - Export and get sampler
   - `get_tokenizer()` - Access model tokenizer
   - `get_info()` - Get session configuration

2. **Sampling Functions** (SamplingClient):
   - `sample()` - Generate text completions
   - `sample_async()` - Async text generation
   - `compute_logprobs()` - Calculate log probabilities for prompts

3. **REST Operations** (RestClient):
   - `list_checkpoints()` / `delete_checkpoint()` - Manage checkpoints
   - `get_checkpoint_archive_url()` - Download checkpoint URLs
   - `list_training_runs()` / `get_training_run()` - Run metadata
   - `publish_checkpoint()` - Make checkpoints public

4. **Supported Models** (20+ models):
   - **Qwen3**: 4B, 8B, 30B, 32B, 235B (dense and MoE)
   - **Qwen3-VL**: Vision-language models (30B, 235B)
   - **Llama 3.x**: 1B, 3B, 8B, 70B
   - **DeepSeek V3.1**: Base and Instruct
   - **GPT-OSS**: 20B, 120B (MoE)
   - **Moonshot Kimi-K2**: Reasoning model

5. **LoRA Configuration**:
   - `rank` - LoRA rank (default 32)
   - `seed` - Reproducibility
   - `train_mlp` - Toggle MLP adaptation
   - `train_attn` - Toggle attention adaptation
   - `train_unembed` - Toggle unembedding adaptation

6. **Async Support**:
   - All methods have `*_async` variants
   - `APIFuture` class for non-blocking operations
   - `result()` / `result_async()` with timeout support

---

## Technology Stack

### Why HuggingFace + PEFT?

| Component | Purpose | Why This Choice |
|-----------|---------|-----------------|
| **HuggingFace Transformers** | Model loading, architecture | Industry standard, all models supported day-1 |
| **PEFT** | LoRA adapters | Gold standard for parameter-efficient fine-tuning |
| **HTTP/REST** | API protocol | Simple integration, well-understood |

### Comparison: HuggingFace vs Candle

| Aspect | HuggingFace | Candle |
|--------|-------------|--------|
| **Maturity** | 6+ years, 140k stars | ~2 years, 18k stars |
| **Model support** | Every model, day-1 | Good, but gaps (Qwen LoRA missing) |
| **LoRA support** | Full PEFT integration | Basic LoRA only |
| **Community** | Massive, easy to hire | Small, niche |
| **Production use** | Proven at scale | Limited |

**Decision**: Use HuggingFace ecosystem. The "native Rust" benefit of Candle doesn't outweigh the 6-month head start and complete model coverage of HuggingFace.

---

## Proposed Architecture

### High-Level Overview

> **Note**: TrainingSession follows the same architectural pattern as UserDeployment,
> leveraging the existing Envoy Gateway infrastructure for routing, rate limiting,
> authentication, and billing. See `docs/training-session-architecture.md` for details.

```
┌─────────────────────────────────────────────────────────────────┐
│                        Python SDK                                │
│  ServiceClient → TrainingClient / SamplingClient                │
└─────────────────────────────────────────────────────────────────┘
                              │
                         HTTP/REST
                              │
┌─────────────────────────────────────────────────────────────────┐
│                    Basilica API (Rust)                           │
│  Creates TrainingSession CRD + HTTPRoute for Envoy Gateway      │
└─────────────────────────────────────────────────────────────────┘
                              │
           ┌──────────────────┼──────────────────┐
           │                  │                  │
           ▼                  ▼                  ▼
┌──────────────────┐  ┌──────────────┐  ┌───────────────────┐
│  Envoy Gateway   │  │   Operator   │  │  TrainingSession  │
│  (Gateway API)   │  │   (Rust)     │  │  CRD              │
│                  │  │              │  │                   │
│ • Rate limiting  │  │ • Reconcile  │  │ • Session spec    │
│ • Auth (JWT/Key) │  │ • Pod mgmt   │  │ • Status tracking │
│ • HTTPRoute      │  │ • HTTPRoute  │  │ • Phase lifecycle │
│ • TLS            │  │   creation   │  │                   │
└────────┬─────────┘  └──────────────┘  └───────────────────┘
         │
         │ Routes to Training Service via HTTPRoute
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Training Service Pod                          │
│                    (HuggingFace + PEFT)                          │
├─────────────────────────────────────────────────────────────────┤
│  Training Operations        │  Sampling Operations              │
│  • forward_backward()       │  • sample()                       │
│  • forward_backward_custom()│  • sample_async()                 │
│  • forward()                │  • compute_logprobs()             │
│  • optim_step()             │                                   │
│  • save_state() / load_state()                                  │
│  • save_weights_for_sampler()                                   │
│  • get_tokenizer() / get_info()                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                    ┌─────────┴─────────┐
                    │   Shared Storage   │
                    │     (R2 / S3)      │
                    │                    │
                    │  • Base models     │
                    │  • LoRA adapters   │
                    │  • Checkpoints     │
                    └────────────────────┘
```

### Component Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                      CONTROL PLANE (Rust)                       │
├────────────────────────────────────────────────────────────────┤
│ Basilica API (crates/basilica-api)                             │
│ • Session lifecycle management (create/delete)                 │
│ • Creates TrainingSession CRD in user namespace                │
│ • Creates HTTPRoute for Envoy Gateway routing                  │
│ • User authentication (validates API key/JWT)                  │
│                                                                │
│ Basilica Operator (crates/basilica-operator)                   │
│ • TrainingPool CRD controller                                  │
│ • TrainingSession CRD controller                               │
│ • Pod scheduling and lifecycle                                 │
│ • Health monitoring                                            │
│                                                                │
│ Envoy Gateway (existing infrastructure)                        │
│ • Rate limiting via BackendTrafficPolicy                       │
│ • JWT/API key authentication via SecurityPolicy                │
│ • Dynamic routing via HTTPRoute                                │
│ • TLS termination                                              │
│ • Billing event emission                                       │
└────────────────────────────────────────────────────────────────┘
                              │
                         HTTP/REST
                              │
┌────────────────────────────────────────────────────────────────┐
│                       ML PLANE (Python)                         │
├────────────────────────────────────────────────────────────────┤
│ Training Service (HuggingFace + PEFT)                          │
│ • Model loading from HuggingFace Hub                           │
│ • LoRA adapter management                                      │
│ • Gradient computation (forward_backward, forward_backward_custom)│
│ • Forward pass without gradients (forward)                     │
│ • Optimizer step (AdamW with configurable params)              │
│ • Checkpoint save/load (safetensors)                           │
│ • Text generation (sample, compute_logprobs)                   │
│ • Tokenizer access (get_tokenizer)                             │
│ • Session info (get_info)                                      │
└────────────────────────────────────────────────────────────────┘
                              │
                        safetensors
                              │
┌────────────────────────────────────────────────────────────────┐
│                      STORAGE (R2/S3)                            │
├────────────────────────────────────────────────────────────────┤
│ • Base model weights (cached on GPU nodes)                     │
│ • LoRA adapters per user session                               │
│ • Optimizer state checkpoints                                  │
│ • Training logs and metrics                                    │
└────────────────────────────────────────────────────────────────┘
```

---

## Implementation Details

### 1. Training Service (HuggingFace + PEFT)

```python
# training_service/backend.py

from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from peft import get_peft_model, LoraConfig, TaskType, PeftModel
from accelerate import Accelerator
import torch
from typing import Optional, Dict, List
import asyncio

class TrainingBackend:
    """Core training backend using HuggingFace + PEFT."""

    def __init__(
        self,
        base_model: str,
        device_map: str = "auto",
        torch_dtype: torch.dtype = torch.bfloat16,
        quantization: Optional[str] = None,  # "4bit", "8bit", None
    ):
        self.base_model_name = base_model
        self.sessions: Dict[str, SessionState] = {}

        # Quantization config for QLoRA
        bnb_config = None
        if quantization == "4bit":
            bnb_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch_dtype,
                bnb_4bit_use_double_quant=True,
            )
        elif quantization == "8bit":
            bnb_config = BitsAndBytesConfig(load_in_8bit=True)

        # Load base model
        self.base_model = AutoModelForCausalLM.from_pretrained(
            base_model,
            torch_dtype=torch_dtype,
            device_map=device_map,
            quantization_config=bnb_config,
            trust_remote_code=True,
        )
        self.tokenizer = AutoTokenizer.from_pretrained(base_model)

        # Accelerator for distributed training
        self.accelerator = Accelerator()

    def create_session(
        self,
        session_id: str,
        lora_config: LoraConfig,
        optimizer_config: OptimizerConfig,
    ) -> str:
        """Create a new training session with LoRA adapter."""

        peft_config = LoraConfig(
            task_type=TaskType.CAUSAL_LM,
            r=lora_config.rank,
            lora_alpha=lora_config.alpha,
            lora_dropout=lora_config.dropout,
            target_modules=lora_config.target_modules or [
                "q_proj", "k_proj", "v_proj", "o_proj",
                "gate_proj", "up_proj", "down_proj",
            ],
            bias="none",
        )

        # Create PEFT model with new adapter
        if not hasattr(self.base_model, 'peft_config'):
            model = get_peft_model(self.base_model, peft_config)
        else:
            # Add adapter to existing PEFT model
            self.base_model.add_adapter(session_id, peft_config)
            model = self.base_model

        # Create optimizer for this session's parameters
        optimizer = torch.optim.AdamW(
            model.parameters(),
            lr=optimizer_config.learning_rate,
            weight_decay=optimizer_config.weight_decay,
            betas=(optimizer_config.beta1, optimizer_config.beta2),
            eps=optimizer_config.epsilon,
        )

        # Store session state
        self.sessions[session_id] = SessionState(
            model=model,
            optimizer=optimizer,
            config=lora_config,
            step_count=0,
            accumulated_gradients=None,
        )

        return session_id

    def forward_backward(
        self,
        session_id: str,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
        labels: torch.Tensor,
        loss_weights: Optional[torch.Tensor] = None,
        loss_fn: str = "cross_entropy",
    ) -> ForwardBackwardResult:
        """Compute forward pass and gradients."""

        session = self.sessions[session_id]
        model = session.model
        model.train()

        # Set active adapter
        if hasattr(model, 'set_adapter'):
            model.set_adapter(session_id)

        # Forward pass
        outputs = model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            labels=labels,
        )

        # Compute weighted loss if weights provided
        if loss_weights is not None:
            # Custom weighted cross-entropy
            logits = outputs.logits
            shift_logits = logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            shift_weights = loss_weights[..., 1:].contiguous()

            loss_fct = torch.nn.CrossEntropyLoss(reduction='none')
            loss = loss_fct(
                shift_logits.view(-1, shift_logits.size(-1)),
                shift_labels.view(-1)
            )
            loss = (loss.view(shift_labels.size()) * shift_weights).sum()
            loss = loss / shift_weights.sum()
        else:
            loss = outputs.loss

        # Handle different loss functions
        if loss_fn == "dpo":
            loss = self._compute_dpo_loss(outputs, labels, session.config)
        elif loss_fn == "kto":
            loss = self._compute_kto_loss(outputs, labels, session.config)

        # Backward pass
        loss.backward()

        # Compute logprobs for return
        with torch.no_grad():
            logprobs = self._compute_logprobs(outputs.logits, labels)

        return ForwardBackwardResult(
            loss=loss.item(),
            logprobs=logprobs.tolist(),
            tokens_processed=int(attention_mask.sum().item()),
        )

    def optim_step(
        self,
        session_id: str,
        grad_clip: Optional[float] = None,
    ) -> OptimStepResult:
        """Apply gradients and update weights."""

        session = self.sessions[session_id]

        # Gradient clipping
        if grad_clip is not None:
            torch.nn.utils.clip_grad_norm_(
                session.model.parameters(),
                grad_clip
            )

        # Optimizer step
        session.optimizer.step()
        session.optimizer.zero_grad()
        session.step_count += 1

        return OptimStepResult(step=session.step_count)

    def save_state(
        self,
        session_id: str,
        path: str,
        include_optimizer: bool = True,
    ):
        """Save LoRA weights and optionally optimizer state."""

        session = self.sessions[session_id]

        # Save adapter weights (safetensors format)
        session.model.save_pretrained(path)

        # Save optimizer state
        if include_optimizer:
            torch.save({
                'optimizer_state_dict': session.optimizer.state_dict(),
                'step_count': session.step_count,
            }, f"{path}/optimizer.pt")

    def load_state(
        self,
        session_id: str,
        path: str,
        load_optimizer: bool = True,
    ):
        """Load LoRA weights and optionally optimizer state."""

        session = self.sessions[session_id]

        # Load adapter weights
        session.model = PeftModel.from_pretrained(
            self.base_model,
            path,
        )

        # Load optimizer state
        if load_optimizer:
            checkpoint = torch.load(f"{path}/optimizer.pt")
            session.optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
            session.step_count = checkpoint['step_count']

    def _compute_logprobs(
        self,
        logits: torch.Tensor,
        labels: torch.Tensor,
    ) -> torch.Tensor:
        """Compute per-token log probabilities."""
        log_probs = torch.nn.functional.log_softmax(logits, dim=-1)
        return torch.gather(log_probs, -1, labels.unsqueeze(-1)).squeeze(-1)

    def _compute_dpo_loss(self, outputs, labels, config):
        """Direct Preference Optimization loss."""
        # Implementation follows DPO paper
        # https://arxiv.org/abs/2305.18290
        beta = getattr(config, 'dpo_beta', 0.1)
        # ... DPO implementation
        pass

    def _compute_kto_loss(self, outputs, labels, config):
        """Kahneman-Tversky Optimization loss."""
        # Implementation follows KTO paper
        # https://arxiv.org/abs/2402.01306
        pass
```

### 2. vLLM Inference Service

```python
# inference_service/vllm_backend.py

from vllm import LLM, SamplingParams
from vllm.lora.request import LoRARequest
from typing import Optional, List
import asyncio

class VLLMInferenceBackend:
    """High-throughput inference using vLLM with LoRA support."""

    def __init__(
        self,
        base_model: str,
        tensor_parallel_size: int = 1,
        max_loras: int = 32,
        max_lora_rank: int = 64,
        gpu_memory_utilization: float = 0.9,
    ):
        self.llm = LLM(
            model=base_model,
            tensor_parallel_size=tensor_parallel_size,
            enable_lora=True,
            max_loras=max_loras,
            max_lora_rank=max_lora_rank,
            gpu_memory_utilization=gpu_memory_utilization,
            trust_remote_code=True,
        )
        self.lora_cache: Dict[str, int] = {}  # session_id -> lora_int_id
        self._next_lora_id = 1

    def sample(
        self,
        session_id: str,
        prompts: List[str],
        lora_path: str,
        sampling_params: SamplingParams,
    ) -> List[SampleResult]:
        """Generate completions using user's LoRA adapter."""

        # Get or assign LoRA ID
        if session_id not in self.lora_cache:
            self.lora_cache[session_id] = self._next_lora_id
            self._next_lora_id += 1

        lora_request = LoRARequest(
            lora_name=session_id,
            lora_int_id=self.lora_cache[session_id],
            lora_local_path=lora_path,
        )

        # Generate with vLLM
        outputs = self.llm.generate(
            prompts,
            sampling_params=sampling_params,
            lora_request=lora_request,
        )

        results = []
        for output in outputs:
            for completion in output.outputs:
                results.append(SampleResult(
                    text=completion.text,
                    token_ids=list(completion.token_ids),
                    logprobs=[lp.logprob for lp in completion.logprobs] if completion.logprobs else None,
                    finish_reason=completion.finish_reason,
                ))

        return results

    def compute_logprobs(
        self,
        session_id: str,
        prompts: List[str],
        lora_path: str,
    ) -> List[List[float]]:
        """Compute log probabilities for sequences."""

        lora_request = LoRARequest(
            lora_name=session_id,
            lora_int_id=self.lora_cache.get(session_id, 0),
            lora_local_path=lora_path,
        )

        # Use vLLM's prompt logprobs feature
        sampling_params = SamplingParams(
            max_tokens=1,
            prompt_logprobs=1,
        )

        outputs = self.llm.generate(
            prompts,
            sampling_params=sampling_params,
            lora_request=lora_request,
        )

        return [
            [lp[token].logprob for token, lp in enumerate(output.prompt_logprobs) if lp]
            for output in outputs
        ]

    async def sample_streaming(
        self,
        session_id: str,
        prompt: str,
        lora_path: str,
        sampling_params: SamplingParams,
    ):
        """Stream generation tokens."""

        lora_request = LoRARequest(
            lora_name=session_id,
            lora_int_id=self.lora_cache.get(session_id, 0),
            lora_local_path=lora_path,
        )

        async for output in self.llm.generate(
            prompt,
            sampling_params=sampling_params,
            lora_request=lora_request,
            stream=True,
        ):
            yield output.outputs[0].text
```

### 3. SGLang Structured Generation Service

```python
# inference_service/sglang_backend.py

import sglang as sgl
from typing import Optional, Dict, Any

class SGLangInferenceBackend:
    """Structured generation using SGLang."""

    def __init__(
        self,
        base_model: str,
        port: int = 30000,
    ):
        # Start SGLang runtime
        self.runtime = sgl.Runtime(
            model_path=base_model,
            port=port,
        )
        sgl.set_default_backend(self.runtime)

    @sgl.function
    def generate_json(s, prompt: str, schema: Dict[str, Any]):
        """Generate JSON conforming to schema."""
        s += prompt
        s += sgl.gen("response", max_tokens=1024, json_schema=schema)

    @sgl.function
    def generate_with_grammar(s, prompt: str, grammar: str):
        """Generate text conforming to grammar."""
        s += prompt
        s += sgl.gen("response", max_tokens=1024, regex=grammar)

    def sample_json(
        self,
        prompt: str,
        output_schema: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Generate structured JSON output."""
        state = self.generate_json.run(prompt=prompt, schema=output_schema)
        return state["response"]

    def sample_constrained(
        self,
        prompt: str,
        regex_pattern: str,
    ) -> str:
        """Generate text matching regex pattern."""
        state = self.generate_with_grammar.run(prompt=prompt, grammar=regex_pattern)
        return state["response"]

    def batch_sample_json(
        self,
        prompts: List[str],
        output_schema: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Batch generate structured JSON outputs."""
        states = self.generate_json.run_batch(
            [{"prompt": p, "schema": output_schema} for p in prompts]
        )
        return [s["response"] for s in states]
```

### 4. Distributed Training with DeepSpeed

```python
# training_service/distributed.py

from transformers import Trainer, TrainingArguments
from peft import get_peft_model, LoraConfig
from accelerate import Accelerator
import deepspeed

DEEPSPEED_CONFIG = {
    "bf16": {"enabled": True},
    "zero_optimization": {
        "stage": 3,
        "offload_optimizer": {"device": "cpu", "pin_memory": True},
        "offload_param": {"device": "cpu", "pin_memory": True},
        "overlap_comm": True,
        "contiguous_gradients": True,
        "sub_group_size": 1e9,
        "reduce_bucket_size": "auto",
        "stage3_prefetch_bucket_size": "auto",
        "stage3_param_persistence_threshold": "auto",
        "stage3_max_live_parameters": 1e9,
        "stage3_max_reuse_distance": 1e9,
    },
    "gradient_accumulation_steps": "auto",
    "gradient_clipping": "auto",
    "steps_per_print": 100,
    "train_batch_size": "auto",
    "train_micro_batch_size_per_gpu": "auto",
    "wall_clock_breakdown": False,
}

class DistributedTrainingBackend:
    """Multi-GPU training with DeepSpeed ZeRO-3."""

    def __init__(
        self,
        base_model: str,
        deepspeed_config: dict = DEEPSPEED_CONFIG,
    ):
        self.base_model_name = base_model
        self.deepspeed_config = deepspeed_config
        self.accelerator = Accelerator()

    def train_session(
        self,
        session_id: str,
        dataset,
        lora_config: LoraConfig,
        training_args: TrainingArguments,
    ):
        """Run distributed training for a session."""

        # Load model with DeepSpeed
        model = AutoModelForCausalLM.from_pretrained(
            self.base_model_name,
            torch_dtype=torch.bfloat16,
        )

        # Add LoRA
        peft_config = LoraConfig(
            task_type=TaskType.CAUSAL_LM,
            r=lora_config.rank,
            lora_alpha=lora_config.alpha,
            target_modules=lora_config.target_modules,
        )
        model = get_peft_model(model, peft_config)

        # Training arguments with DeepSpeed
        training_args = TrainingArguments(
            output_dir=f"./checkpoints/{session_id}",
            deepspeed=self.deepspeed_config,
            bf16=True,
            gradient_checkpointing=True,
            per_device_train_batch_size=1,
            gradient_accumulation_steps=16,
            learning_rate=lora_config.learning_rate,
            num_train_epochs=1,
            logging_steps=10,
            save_steps=500,
        )

        # Create trainer
        trainer = Trainer(
            model=model,
            args=training_args,
            train_dataset=dataset,
        )

        # Train
        trainer.train()

        # Save final checkpoint
        model.save_pretrained(f"./checkpoints/{session_id}/final")
```

### 5. REST API Specification

> **Note**: This REST API provides a clean interface for training operations.

```yaml
# OpenAPI 3.0 specification (summary)

# === Session Management ===

POST /sessions
  Request:
    base_model: string          # e.g., "meta-llama/Llama-3.1-70B"
    rank: int = 32              # LoRA rank
    seed: int?                  # Optional random seed
    train_mlp: bool = true      # Apply LoRA to MLP layers
    train_attn: bool = true     # Apply LoRA to attention layers
    train_unembed: bool = true  # Apply LoRA to unembedding
    user_metadata: object?      # Custom metadata
  Response:
    session_id: string
    endpoint: string            # Direct endpoint for this session

POST /sessions/from_state
  Request:
    path: string                # Checkpoint path (e.g., "basilica://...")
    user_metadata: object?
  Response:
    session_id: string

POST /sessions/from_state_with_optimizer
  Request:
    path: string
    user_metadata: object?
  Response:
    session_id: string

GET /sessions/{session_id}
  Response:
    session_id: string
    base_model: string
    rank: int
    status: string
    created_at: datetime

DELETE /sessions/{session_id}
  Response: 204 No Content

# === Training Operations ===

POST /sessions/{session_id}/forward
  Request:
    data: Datum[]               # Training examples
  Response:
    logprobs: float[][]
    tokens_processed: int

POST /sessions/{session_id}/forward_backward
  Request:
    data: Datum[]
    loss_fn: string = "cross_entropy"
  Response:
    loss: float
    logprobs: float[][]
    tokens_processed: int

POST /sessions/{session_id}/optim_step
  Request:
    learning_rate: float?       # Override learning rate
    betas: [float, float]?      # Adam betas
    eps: float?                 # Adam epsilon
    weight_decay: float?        # L2 regularization
  Response:
    step: int

# === State Management ===

POST /sessions/{session_id}/save_state
  Request:
    name: string                # Checkpoint name
  Response:
    path: string                # Saved checkpoint path

POST /sessions/{session_id}/load_state
  Request:
    path: string
  Response: 200 OK

POST /sessions/{session_id}/load_state_with_optimizer
  Request:
    path: string
  Response: 200 OK

POST /sessions/{session_id}/save_weights_for_sampler
  Request:
    name: string
  Response:
    path: string

GET /sessions/{session_id}/info
  Response:
    base_model: string
    rank: int
    train_mlp: bool
    train_attn: bool
    train_unembed: bool
    user_metadata: object

# === Sampling ===

POST /sample
  Request:
    model_path: string?         # Fine-tuned weights path
    base_model: string?         # Or base model name
    prompt: ModelInput          # Token IDs
    num_samples: int = 1
    sampling_params: SamplingParams?
    include_prompt_logprobs: bool = false
    topk_prompt_logprobs: int?
  Response:
    samples: SampleResponse[]

POST /compute_logprobs
  Request:
    model_path: string?
    base_model: string?
    prompt: ModelInput
  Response:
    logprobs: float[]

# === REST Client Operations ===

GET /training_runs
  Query: limit=20
  Response:
    runs: TrainingRun[]

GET /training_runs/{run_id}
  Response:
    run_id: string
    base_model: string
    created_at: datetime
    checkpoints: Checkpoint[]

GET /checkpoints
  Query: run_id?, limit=100
  Response:
    checkpoints: Checkpoint[]

GET /checkpoints/{checkpoint_id}/download_url
  Response:
    url: string                 # Signed download URL

DELETE /checkpoints/{checkpoint_id}
  Response: 204 No Content

POST /checkpoints/publish
  Request:
    path: string
  Response:
    public_url: string

POST /checkpoints/unpublish
  Request:
    path: string
  Response: 200 OK

GET /capabilities
  Response:
    models: string[]
    max_batch_tokens: int
    max_sequence_length: int

# === Data Types ===

Datum:
  input_ids: int[]
  labels: int[]
  loss_weights: float[]?

ModelInput:
  token_ids: int[]

SamplingParams:
  max_tokens: int = 256
  temperature: float = 1.0
  top_p: float = 1.0
  top_k: int = 0
  stop_sequences: string[]?
  include_logprobs: bool = false

SampleResponse:
  text: string
  token_ids: int[]
  logprobs: float[]?
  finish_reason: string
```

---

## Kubernetes CRDs

### TrainingPool CRD

```yaml
apiVersion: basilica.ai/v1
kind: TrainingPool
metadata:
  name: llama-70b-pool
spec:
  # Base model configuration
  baseModel: "meta-llama/Llama-3.1-70B-Instruct"
  modelConfig:
    dtype: "bfloat16"
    quantization: null  # or "4bit" for QLoRA
    tensorParallelSize: 8

  # Pool sizing
  replicas: 4
  gpusPerReplica: 8
  gpuModel: "H100"

  # Multi-tenancy
  maxConcurrentSessions: 32
  maxLoraRank: 64

  # Service configuration
  services:
    training:
      enabled: true
      image: "basilica/training:latest"
      resources:
        memory: "64Gi"
        gpu: 8

    vllm:
      enabled: true
      image: "vllm/vllm-openai:latest"
      resources:
        memory: "64Gi"
        gpu: 8
      config:
        maxLoras: 32
        gpuMemoryUtilization: 0.9

    sglang:
      enabled: false  # Optional
      image: "lmsys/sglang:latest"

  # Storage
  modelStorage:
    backend: "r2"
    bucket: "base-models"
    cachePath: "/models"

  checkpointStorage:
    backend: "r2"
    bucket: "training-checkpoints"

status:
  phase: "Ready"
  readyReplicas: 4
  loadedModel: "meta-llama/Llama-3.1-70B-Instruct"
  activeSessions: 12
  gpuUtilization: 0.85
```

### TrainingSession CRD

```yaml
apiVersion: basilica.ai/v1
kind: TrainingSession
metadata:
  name: user-123-session-abc
spec:
  userId: "user-123"
  poolRef: "llama-70b-pool"

  loraConfig:
    rank: 32
    alpha: 64
    dropout: 0.05
    targetModules: ["q_proj", "k_proj", "v_proj", "o_proj"]
    loraType: "lora"  # or "qlora", "dora"

  optimizerConfig:
    type: "adamw"
    learningRate: 1e-4
    weightDecay: 0.01
    beta1: 0.9
    beta2: 0.999

  checkpointStorage:
    backend: "r2"
    bucket: "training-checkpoints"
    path: "user-123/session-abc"
    autoSaveSteps: 1000

  limits:
    maxSteps: 100000
    ttlSeconds: 86400

status:
  phase: "Active"
  stepsCompleted: 1500
  tokensProcessed: 48000000
  lastCheckpoint: "step-1500"
  lastActivity: "2024-01-15T10:30:00Z"
  billing:
    totalComputeSeconds: 3600
    estimatedCostUsd: 12.50
```

---

## Python SDK

> **Note**: This SDK provides a clean, Pythonic interface for training operations.

```python
# basilica/training/__init__.py

from dataclasses import dataclass
from typing import List, Optional, Dict, Any
import os
import httpx


# === Data Types ===

@dataclass
class SamplingParams:
    """Sampling parameters for text generation."""
    max_tokens: int = 256
    temperature: float = 1.0
    top_p: float = 1.0
    top_k: int = 0
    stop_sequences: List[str] = None
    include_logprobs: bool = False


@dataclass
class ModelInput:
    """Input tokens for the model."""
    token_ids: List[int]

    @classmethod
    def from_ints(cls, token_ids: List[int]) -> "ModelInput":
        return cls(token_ids=token_ids)


@dataclass
class Datum:
    """Training example with input and loss function targets."""
    input_ids: List[int]
    labels: List[int]
    loss_weights: Optional[List[float]] = None


@dataclass
class SampleResponse:
    """Generated sample from the model."""
    text: str
    token_ids: List[int]
    logprobs: Optional[List[float]] = None
    finish_reason: str = "stop"


@dataclass
class ForwardBackwardResult:
    """Result of forward-backward pass."""
    loss: float
    logprobs: List[float]
    tokens_processed: int


@dataclass
class GetServerCapabilitiesResponse:
    """Available models and server features."""
    models: List[str]
    max_batch_tokens: int
    max_sequence_length: int


# === APIFuture ===

class APIFuture:
    """Async handle for training operations.

    Supports both sync and async access patterns:
        # Sync
        result = future.result(timeout=30)

        # Async
        result = await future.result_async(timeout=30)
    """

    def __init__(self, future):
        self._future = future
        self._result = None

    def result(self, timeout: Optional[float] = None):
        """Block until operation completes (sync)."""
        if self._result is None:
            self._result = self._future.result(timeout=timeout)
        return self._result

    async def result_async(self, timeout: Optional[float] = None):
        """Wait for operation to complete (async)."""
        import asyncio
        return await asyncio.wait_for(
            asyncio.to_thread(self._future.result),
            timeout=timeout
        )

    def __await__(self):
        """Allow: result = await future"""
        return self.result_async().__await__()


# === ServiceClient ===

class ServiceClient:
    """Main entry point for the Basilica Training API.

    Example:
        >>> client = ServiceClient()
        >>> caps = client.get_server_capabilities()
        >>> print(caps.models)
        ['meta-llama/Llama-3.1-70B', 'Qwen/Qwen3-235B-A22B-Instruct', ...]

        >>> training = client.create_lora_training_client(
        ...     "meta-llama/Llama-3.1-8B-Instruct",
        ...     rank=32,
        ... )
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
    ):
        self.api_key = api_key or os.environ.get("BASILICA_API_KEY")
        self.endpoint = endpoint or os.environ.get(
            "BASILICA_ENDPOINT", "https://api.basilica.ai"
        )

        if not self.api_key:
            raise ValueError(
                "API key required. Set BASILICA_API_KEY or pass api_key parameter."
            )

        self._client = httpx.Client(
            base_url=self.endpoint,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=300.0,
        )

    def get_server_capabilities(self) -> GetServerCapabilitiesResponse:
        """Query available models, features, and limits."""
        response = self._client.get("/capabilities")
        response.raise_for_status()
        data = response.json()
        return GetServerCapabilitiesResponse(**data)

    async def get_server_capabilities_async(self) -> GetServerCapabilitiesResponse:
        """Query available models (async)."""
        async with httpx.AsyncClient(
            base_url=self.endpoint,
            headers={"Authorization": f"Bearer {self.api_key}"},
        ) as client:
            response = await client.get("/capabilities")
            response.raise_for_status()
            return GetServerCapabilitiesResponse(**response.json())

    def create_lora_training_client(
        self,
        base_model: str,
        rank: int = 32,
        seed: Optional[int] = None,
        train_mlp: bool = True,
        train_attn: bool = True,
        train_unembed: bool = True,
        user_metadata: Optional[Dict[str, str]] = None,
    ) -> "TrainingClient":
        """Initialize LoRA fine-tuning session.

        Args:
            base_model: Name of base model (e.g., "meta-llama/Llama-3.1-70B")
            rank: LoRA rank (default 32)
            seed: Random seed for reproducibility
            train_mlp: Apply LoRA to MLP layers (default True)
            train_attn: Apply LoRA to attention layers (default True)
            train_unembed: Apply LoRA to unembedding layer (default True)
            user_metadata: Custom metadata for tracking

        Returns:
            TrainingClient for performing training operations
        """
        response = self._client.post(
            "/sessions",
            json={
                "base_model": base_model,
                "rank": rank,
                "seed": seed,
                "train_mlp": train_mlp,
                "train_attn": train_attn,
                "train_unembed": train_unembed,
                "user_metadata": user_metadata or {},
            },
        )
        response.raise_for_status()
        data = response.json()
        return TrainingClient(data["session_id"], self._client)

    async def create_lora_training_client_async(
        self,
        base_model: str,
        rank: int = 32,
        **kwargs,
    ) -> "TrainingClient":
        """Initialize LoRA fine-tuning session (async)."""
        # Async implementation
        ...

    def create_training_client_from_state(
        self,
        path: str,
        user_metadata: Optional[Dict[str, str]] = None,
    ) -> "TrainingClient":
        """Resume training from checkpoint (weights only, optimizer resets)."""
        response = self._client.post(
            "/sessions/from_state",
            json={"path": path, "user_metadata": user_metadata or {}},
        )
        response.raise_for_status()
        data = response.json()
        return TrainingClient(data["session_id"], self._client)

    def create_training_client_from_state_with_optimizer(
        self,
        path: str,
        user_metadata: Optional[Dict[str, str]] = None,
    ) -> "TrainingClient":
        """Resume training from checkpoint (weights + optimizer state)."""
        response = self._client.post(
            "/sessions/from_state_with_optimizer",
            json={"path": path, "user_metadata": user_metadata or {}},
        )
        response.raise_for_status()
        data = response.json()
        return TrainingClient(data["session_id"], self._client)

    def create_sampling_client(
        self,
        model_path: Optional[str] = None,
        base_model: Optional[str] = None,
        retry_config: Optional[Dict] = None,
    ) -> "SamplingClient":
        """Create client for text generation.

        Args:
            model_path: Path to saved weights (e.g., 'basilica://...')
            base_model: Base model name (if no fine-tuned weights)
            retry_config: Optional retry configuration
        """
        if model_path is None and base_model is None:
            raise ValueError("Either model_path or base_model must be provided")

        return SamplingClient(
            client=self._client,
            model_path=model_path,
            base_model=base_model,
        )

    def create_rest_client(self) -> "RestClient":
        """Create REST client for checkpoint and run management."""
        return RestClient(self._client)


# === TrainingClient ===

class TrainingClient:
    """Client for training operations.

    Example:
        >>> training = client.create_lora_training_client("meta-llama/Llama-3.1-8B")
        >>>
        >>> # Training loop
        >>> for batch in dataloader:
        ...     result = training.forward_backward(batch.data).result()
        ...     print(f"Loss: {result.loss:.4f}")
        ...     training.optim_step().result()
        ...
        >>> training.save_state("checkpoint-final").result()
    """

    def __init__(self, session_id: str, client: httpx.Client):
        self._session_id = session_id
        self._client = client
        self._base_url = f"/sessions/{session_id}"

    @property
    def session_id(self) -> str:
        return self._session_id

    # --- Training Operations ---

    def forward(
        self,
        data: List[Datum],
    ) -> APIFuture:
        """Forward pass without gradient computation."""
        # Implementation returns APIFuture
        ...

    def forward_backward(
        self,
        data: List[Datum],
        loss_fn: str = "cross_entropy",
    ) -> APIFuture:
        """Compute forward pass and gradients.

        Args:
            data: List of training examples
            loss_fn: Loss function ("cross_entropy")

        Returns:
            APIFuture resolving to ForwardBackwardResult
        """
        # Implementation returns APIFuture
        ...

    def forward_backward_custom(
        self,
        data: List[Datum],
        loss_fn: callable,
    ) -> APIFuture:
        """Compute gradients with custom loss function on logprobs.

        Args:
            data: List of training examples
            loss_fn: Custom loss function operating on log probabilities
        """
        # Implementation returns APIFuture
        ...

    def optim_step(
        self,
        learning_rate: Optional[float] = None,
        betas: Optional[tuple] = None,
        eps: Optional[float] = None,
        weight_decay: Optional[float] = None,
    ) -> APIFuture:
        """Update model weights using accumulated gradients (Adam).

        Args:
            learning_rate: Override learning rate
            betas: Adam beta parameters (beta1, beta2)
            eps: Adam epsilon
            weight_decay: L2 regularization
        """
        # Implementation returns APIFuture
        ...

    # --- State Management ---

    def save_state(self, name: str) -> APIFuture:
        """Save checkpoint (weights + optimizer state) to storage."""
        ...

    def load_state(self, path: str) -> APIFuture:
        """Load weights only (optimizer state resets)."""
        ...

    def load_state_with_optimizer(self, path: str) -> APIFuture:
        """Load weights and optimizer state for seamless resume."""
        ...

    def save_weights_for_sampler(self, name: str) -> APIFuture:
        """Export weights formatted for sampling."""
        ...

    def save_weights_and_get_sampling_client(
        self,
        name: str,
    ) -> "SamplingClient":
        """Save weights and return a SamplingClient for inference."""
        ...

    # --- Utilities ---

    def get_tokenizer(self):
        """Get the model's tokenizer for encoding/decoding."""
        ...

    def get_info(self) -> Dict[str, Any]:
        """Get session info (base model, LoRA rank, metadata)."""
        response = self._client.get(f"{self._base_url}/info")
        response.raise_for_status()
        return response.json()

    def create_sampling_client(self, model_path: str) -> "SamplingClient":
        """Create SamplingClient from a saved checkpoint path."""
        ...

    def close(self):
        """Close the training session."""
        self._client.delete(self._base_url)

    # --- Async Variants ---

    async def forward_async(self, data: List[Datum]):
        """Forward pass (async)."""
        ...

    async def forward_backward_async(self, data: List[Datum], loss_fn: str = "cross_entropy"):
        """Compute gradients (async)."""
        ...

    async def optim_step_async(self, **kwargs):
        """Update weights (async)."""
        ...

    async def save_state_async(self, name: str):
        """Save checkpoint (async)."""
        ...


# === SamplingClient ===

class SamplingClient:
    """Client for text generation and inference.

    Example:
        >>> sampling = client.create_sampling_client(base_model="Qwen/Qwen3-8B")
        >>> result = sampling.sample(prompt, SamplingParams(max_tokens=100)).result()
        >>> print(result.text)
    """

    def __init__(
        self,
        client: httpx.Client,
        model_path: Optional[str] = None,
        base_model: Optional[str] = None,
    ):
        self._client = client
        self._model_path = model_path
        self._base_model = base_model

    def sample(
        self,
        prompt: ModelInput,
        num_samples: int = 1,
        sampling_params: Optional[SamplingParams] = None,
        include_prompt_logprobs: bool = False,
        topk_prompt_logprobs: Optional[int] = None,
    ) -> APIFuture:
        """Generate text completions.

        Args:
            prompt: Input tokens (ModelInput)
            num_samples: Number of independent samples
            sampling_params: Generation parameters
            include_prompt_logprobs: Include logprobs for prompt tokens
            topk_prompt_logprobs: Top-k logprobs per position
        """
        ...

    async def sample_async(
        self,
        prompt: ModelInput,
        num_samples: int = 1,
        sampling_params: Optional[SamplingParams] = None,
        **kwargs,
    ) -> SampleResponse:
        """Generate text completions (async)."""
        ...

    def compute_logprobs(
        self,
        prompt: ModelInput,
    ) -> APIFuture:
        """Compute log probabilities for prompt tokens."""
        ...

    async def compute_logprobs_async(
        self,
        prompt: ModelInput,
    ) -> List[Optional[float]]:
        """Compute log probabilities (async)."""
        ...


# === RestClient ===

class RestClient:
    """REST client for checkpoint and training run management.

    Example:
        >>> rest = client.create_rest_client()
        >>> runs = rest.list_training_runs().result()
        >>> checkpoints = rest.list_checkpoints(run_id).result()
    """

    def __init__(self, client: httpx.Client):
        self._client = client

    # --- Training Runs ---

    def get_training_run(self, run_id: str) -> APIFuture:
        """Get training run metadata by ID."""
        ...

    def get_training_run_by_path(self, path: str) -> APIFuture:
        """Get training run by path (e.g., 'basilica://...')."""
        ...

    def list_training_runs(self, limit: int = 20) -> APIFuture:
        """List training runs (paginated)."""
        ...

    # --- Checkpoints ---

    def list_checkpoints(self, run_id: str) -> APIFuture:
        """List checkpoints for a training run."""
        ...

    def list_user_checkpoints(self, limit: int = 100) -> APIFuture:
        """List all user's checkpoints across runs."""
        ...

    def get_checkpoint_archive_url(self, checkpoint_id: str) -> APIFuture:
        """Get signed download URL for checkpoint."""
        ...

    def delete_checkpoint(self, checkpoint_id: str) -> APIFuture:
        """Delete a checkpoint."""
        ...

    def get_weights_info_by_path(self, path: str) -> APIFuture:
        """Get checkpoint metadata (base model, LoRA rank)."""
        ...

    # --- Publishing ---

    def publish_checkpoint(self, path: str) -> APIFuture:
        """Make checkpoint publicly accessible."""
        ...

    def unpublish_checkpoint(self, path: str) -> APIFuture:
        """Revert checkpoint to private."""
        ...

    # --- Sessions ---

    def get_session(self, session_id: str) -> APIFuture:
        """Get session with associated runs and samplers."""
        ...

    def list_sessions(self, limit: int = 20) -> APIFuture:
        """List sessions (paginated)."""
        ...

    # Async variants for all methods...
```

---

## Implementation Phases

### Phase 1: Foundation

**Goal**: Basic training API with core functionality

1. **Kubernetes CRDs**
   - TrainingPool CRD
   - TrainingSession CRD
   - Validation webhooks

2. **Training Service (Python)**
   - HuggingFace model loading
   - PEFT LoRA integration
   - `forward_backward()`, `optim_step()` endpoints
   - REST/FastAPI server

3. **Basilica API Extensions (Rust)**
   - `/sessions` endpoints for session lifecycle
   - TrainingSession CRD creation
   - HTTPRoute creation for Envoy Gateway
   - Integration with existing auth middleware

4. **Python SDK**
   - ServiceClient, TrainingClient
   - `create_lora_training_client()` with train_mlp/train_attn/train_unembed
   - APIFuture for async operations

**Deliverables**:
- Single-GPU LoRA training on Llama-8B
- Checkpoint save/load (`save_state`, `load_state`, `load_state_with_optimizer`)
- Clean SDK API

### Phase 2: Sampling & Inference

**Goal**: Text generation with SamplingClient

1. **Sampling Endpoints**
   - `sample()` / `sample_async()`
   - `compute_logprobs()`
   - `include_prompt_logprobs`, `topk_prompt_logprobs` support

2. **SamplingClient**
   - Create from base model or checkpoint path
   - Consistent API with TrainingClient

3. **Integration with Training**
   - `save_weights_for_sampler()`
   - `save_weights_and_get_sampling_client()`

**Deliverables**:
- High-throughput text generation
- Logprob computation for prompts
- Seamless training → inference workflow

### Phase 3: RestClient & Checkpoint Management

**Goal**: Full checkpoint and run management with RestClient

1. **RestClient Implementation**
   - `list_training_runs()`, `get_training_run()`
   - `list_checkpoints()`, `delete_checkpoint()`
   - `get_checkpoint_archive_url()` for downloads

2. **Checkpoint Publishing**
   - `publish_checkpoint()`
   - `unpublish_checkpoint()`
   - `get_weights_info_by_path()`

3. **Session Management**
   - `list_sessions()`, `get_session()`
   - `create_training_client_from_state()`
   - `create_training_client_from_state_with_optimizer()`

**Deliverables**:
- Full checkpoint lifecycle management
- Checkpoint sharing/publishing
- Resume training from any checkpoint

### Phase 4: Advanced Training Features

**Goal**: Custom loss functions and training utilities

1. **Custom Loss Support**
   - `forward_backward_custom()` with custom loss functions on logprobs
   - `forward()` for inference-only passes

2. **Training Utilities**
   - `get_tokenizer()` for encoding/decoding
   - `get_info()` for session metadata

3. **Multi-GPU Training** (internal)
   - DeepSpeed/FSDP integration
   - Tensor parallelism for large models

**Deliverables**:
- Custom loss functions for advanced training (DPO, RLHF, etc.)
- Train Llama-70B, Qwen-235B on multi-GPU

### Phase 5: Multi-Tenancy & Pools

**Goal**: Shared GPU pools, efficient resource utilization

1. **Worker Pool Management**
   - TrainingPool controller
   - Session scheduling
   - Resource allocation

2. **Request Batching**
   - Clock-cycle aggregation
   - Fair scheduling
   - Priority support

3. **Billing Integration**
   - Per-operation metering
   - Usage tracking

**Deliverables**:
- 32+ concurrent sessions per pool
- Efficient GPU utilization
- Accurate billing

### Phase 6: Production Hardening

**Goal**: Production-ready service

1. **Observability**
   - Training metrics (loss curves)
   - GPU utilization dashboards
   - Alerting

2. **Reliability**
   - Checkpoint recovery
   - Node failure handling
   - Auto-scaling

3. **OpenAI-Compatible API**
   - /v1/completions
   - /v1/chat/completions

**Deliverables**:
- Production monitoring
- 99.9% availability
- Drop-in OpenAI replacement

---

## Directory Structure

```
crates/
├── basilica-api/
│   └── src/api/routes/
│       └── training.rs              # Session management, CRD/HTTPRoute creation
│
├── basilica-operator/
│   └── src/
│       ├── crd/
│       │   ├── training_pool.rs     # TrainingPool CRD
│       │   └── training_session.rs  # TrainingSession CRD
│       └── controllers/
│           └── training_session_controller.rs  # Reconciliation logic

services/
├── training-service/                # Python training backend
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── src/
│   │   ├── __init__.py
│   │   ├── backend.py              # HuggingFace + PEFT
│   │   ├── server.py               # FastAPI REST service
│   │   ├── sampling.py             # sample(), compute_logprobs()
│   │   ├── distributed.py          # Multi-GPU support (internal)
│   │   └── checkpoints.py          # RestClient operations
│   └── tests/

sdk/
└── python/
    └── basilica/
        ├── __init__.py
        └── training/
            ├── __init__.py
            ├── service_client.py   # ServiceClient (entry point)
            ├── training_client.py  # TrainingClient
            ├── sampling_client.py  # SamplingClient
            ├── rest_client.py      # RestClient (checkpoints, runs)
            └── types.py            # Datum, SamplingParams, APIFuture, etc.

# Envoy Gateway resources created dynamically:
# - HTTPRoute for /sessions/{id}/* → training-{session-id} Service
# - BackendTrafficPolicy for rate limiting
# - SecurityPolicy for JWT/API key auth (if per-session auth needed)
```

---

## Resource Requirements

### Development Environment

- 1x A100 or H100 GPU
- 64GB+ system RAM
- 200GB+ SSD for models
- CUDA 12.0+

### Production Environment (Per Pool)

| Model | GPUs | VRAM | Training | Sampling |
|-------|------|------|----------|----------|
| Llama-8B | 1x H100 | 80GB | Full model | 32 concurrent LoRAs |
| Llama-70B | 8x H100 | 640GB | Multi-GPU | Tensor parallel |
| Qwen-72B | 8x H100 | 640GB | Multi-GPU | Tensor parallel |
| Qwen-235B | 8x H100 | 640GB | MoE sharding | MoE inference |

### Cost Estimates

- H100 spot: ~$2/GPU/hour
- 8B model pool (1 GPU): ~$2/hour
- 70B model pool (8 GPUs): ~$16/hour
- Multi-tenant (32 users): ~$0.50/user/hour

---

## Technology Comparison Summary

| Aspect | This Proposal | Candle Approach |
|--------|---------------|-----------------|
| **API Surface** | REST with ServiceClient pattern | Custom |
| **Training framework** | HuggingFace + PEFT | Candle + candle-lora |
| **Protocol** | HTTP/REST | Native Rust |
| **Model support** | All models, day-1 | Subset, needs porting |
| **LoRA config** | train_mlp/train_attn/train_unembed | Custom targeting |
| **Distributed** | Internal (DeepSpeed/FSDP) | Custom NCCL |
| **Hiring difficulty** | Easy (Python ML) | Hard (Rust ML) |
| **Control plane** | Rust (unchanged) | Rust |
| **SDK style** | ServiceClient → TrainingClient/SamplingClient | Custom |

---

## References

- [HuggingFace Transformers](https://github.com/huggingface/transformers)
- [PEFT (Parameter-Efficient Fine-Tuning)](https://github.com/huggingface/peft)
- [vLLM](https://github.com/vllm-project/vllm)
- [SGLang](https://github.com/sgl-project/sglang)
- [DeepSpeed](https://github.com/microsoft/DeepSpeed)
- [QLoRA Paper](https://arxiv.org/abs/2305.14314)
- [DPO Paper](https://arxiv.org/abs/2305.18290)
- [LoRA Paper](https://arxiv.org/abs/2106.09685)
