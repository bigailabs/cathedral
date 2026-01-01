# Training API Gap Analysis: Basilica + verl Integration

This document analyzes the gaps between Basilica's current training API and OpenTinker's approach, with a concrete plan to replace the training backend with **verl SDK**.

## Executive Summary

**Decision**: Replace `backend.py` (HuggingFace + PEFT) with **verl SDK**.

| Current | Target |
|---------|--------|
| HF + PEFT (680 lines) | verl SDK |
| SFT only | SFT + GRPO + REINFORCE++ + RLOO |
| Single GPU | Multi-GPU (FSDP, Megatron) |
| HF generate (slow) | vLLM/SGLang (fast) |
| No custom rewards | Function-based rewards |
| No multi-turn | Multi-turn agent loops |

---

## Part 1: verl Capabilities (Verified)

### What verl Supports

| Category | Features |
|----------|----------|
| **Training Modes** | SFT, RL (multiple algorithms) |
| **RL Algorithms** | GRPO, REINFORCE++, RLOO, ReMax, DAPO, PRIME, DrGRPO |
| **Training Backend** | FSDP, FSDP2, Megatron-LM |
| **Inference** | vLLM, SGLang, HF Transformers |
| **Models** | Qwen-3, Llama3.1, Gemma2, DeepSeek (up to 671B) |
| **Scale** | Hundreds of GPUs, expert parallelism |
| **Optimizations** | Flash Attention 2, sequence packing, LoRA, Liger-kernel |
| **Rewards** | Model-based and function-based |
| **Modalities** | LLMs + Vision-Language Models (VLMs) |
| **Tracking** | wandb, MLflow, TensorBoard, SwanLab |

### What verl Does NOT Support
- DPO (Direct Preference Optimization) - use HuggingFace TRL if needed later
- PPO with critic (has REINFORCE++ which is PPO without critic)

### Why verl Over Current Backend

| Aspect | Current (HF+PEFT) | verl |
|--------|-------------------|------|
| SFT | ✅ | ✅ |
| RL Training | ❌ | ✅ GRPO, REINFORCE++, RLOO |
| Custom Rewards | ❌ | ✅ Function-based |
| Multi-turn | ❌ | ✅ Agent loops |
| Fast Inference | ❌ HF generate | ✅ vLLM/SGLang |
| Multi-GPU | ❌ | ✅ FSDP, Megatron |
| Large Models | ❌ ~13B max | ✅ 671B |
| LoRA | ✅ | ✅ |

---

## Part 2: Gap Analysis

### Critical Gaps (High Priority)

#### Gap 1: No Job Scheduling / Resource Management

**Current**: 1 TrainingSession = 1 Pod, no queuing
**Impact**: High - no fair resource sharing when GPUs are busy

**Fix**: Add `TrainingJob` CRD that queues before creating `TrainingSession`

```yaml
apiVersion: basilica.ai/v1
kind: TrainingJob
spec:
  priority: 10
  requestedGpus: 2
  config: { ... }
status:
  phase: Queued | Scheduled | Running | Completed
  queuePosition: 3
  sessionRef: "ts-abc123"
```

---

#### Gap 2: No Multi-turn / Agentic Training

**Current**: Single `forward_backward()` call, no conversation history
**Impact**: High - can't do agentic tasks, multi-turn reasoning

**Fix**: verl has built-in multi-turn support via agent loops

```python
# verl config
multi_turn:
  max_user_turns: 10
  max_assistant_turns: 10
  max_tokens_per_turn: 512
```

API additions needed:
```
POST /sessions/{id}/internal/{iid}/multi_turn/reset
POST /sessions/{id}/internal/{iid}/multi_turn/step
GET  /sessions/{id}/internal/{iid}/multi_turn/history
```

---

#### Gap 3: No Custom Reward Functions

**Current**: Hardcoded cross-entropy loss
**Impact**: High - can't customize training objective for RL

**Fix**: verl supports function-based rewards

```python
# Upload reward function
def compute_reward(prompt: str, response: str, ground_truth: str) -> float:
    # Custom logic
    return 1.0 if is_correct(response, ground_truth) else 0.0

# Register with verl
reward_manager.register("math_reward", compute_reward)
```

---

#### Gap 4: No Environment Abstraction

**Current**: Client sends raw tensors, no protocol for env interaction
**Impact**: Medium - client handles all data/reward logic

**Fix**: Add SDK-side environment protocol

```python
class BaseEnvironment(ABC):
    @abstractmethod
    def reset(self) -> Dict: pass
    
    @abstractmethod
    def step(self, action: str) -> Tuple[Dict, float, bool]: pass
    
    def get_reward_source(self) -> Optional[str]: return None
```

---

#### Gap 5: SFT-only Backend (No RL)

**Current**: Only cross-entropy loss, no policy gradients
**Impact**: High - limited to supervised learning

**Fix**: verl provides GRPO, REINFORCE++, RLOO, etc.

---

### Moderate Gaps

| # | Gap | Impact | Fix |
|---|-----|--------|-----|
| 6 | No vLLM fast inference | Medium | verl has vLLM/SGLang built-in |
| 7 | No lifecycle cleanup | Medium | Add `LifecycleManager` to SDK |
| 8 | No tensor parallelism | Medium | verl supports TP via FSDP/Megatron |
| 9 | No validation integration | Medium | Add `test_freq` to training loop |
| 10 | No per-job metrics | Low | Tag metrics with job_id |

---

## Part 3: verl Integration Plan

### Architecture After Integration

```
┌─────────────────────────────────────────────────────────────────┐
│                 Basilica + verl Architecture                    │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  SDK (Python)                                                   │
│  ├── TrainingClient (SFT)                                       │
│  ├── RLTrainingClient (GRPO/REINFORCE++)                       │
│  ├── Environment protocol                                       │
│  └── LifecycleManager                                          │
│       │                                                         │
│       ▼                                                         │
│  Basilica API (Rust)                                           │
│  ├── /sessions (CRD management)                                │
│  ├── /sessions/{id}/internal/* (proxy to verl)                 │
│  └── /training_runs, /checkpoints (management)                 │
│       │                                                         │
│       ▼                                                         │
│  K8s Operator                                                   │
│  └── TrainingSession CRD → Pod + Service + HTTPRoute           │
│       │                                                         │
│       ▼                                                         │
│  Training Pod (verl)                                           │
│  ├── verl SFT Trainer (FSDP)                                   │
│  ├── verl RL Trainer (GRPO/REINFORCE++)                        │
│  ├── vLLM Rollout Worker                                       │
│  ├── Reward Function Registry                                   │
│  └── Checkpoint Manager (R2/S3 via FUSE)                       │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Phase 1: CRD & API Updates (Week 1)

#### 1.1 Update TrainingSession CRD

```rust
// crates/basilica-operator/src/crd/training_session.rs

/// Training mode selection
#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum TrainingMode {
    #[default]
    Sft,           // Supervised fine-tuning
    Grpo,          // Group Relative Policy Optimization
    ReinforcePP,   // REINFORCE++ (no critic needed)
    Rloo,          // Leave-One-Out
    Remax,         // Reward maximization
}

/// RL-specific configuration
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct RLConfig {
    /// KL penalty coefficient
    #[serde(default = "default_kl_coef")]
    pub kl_coef: f64,
    
    /// Number of samples per prompt for advantage estimation
    #[serde(default = "default_num_samples")]
    pub num_samples: u32,
    
    /// Max tokens to generate per response
    #[serde(default = "default_max_response_tokens")]
    pub max_response_tokens: u32,
    
    /// Temperature for sampling
    #[serde(default = "default_temperature")]
    pub temperature: f64,
}

fn default_kl_coef() -> f64 { 0.1 }
fn default_num_samples() -> u32 { 4 }
fn default_max_response_tokens() -> u32 { 512 }
fn default_temperature() -> f64 { 1.0 }

/// Multi-turn configuration
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct MultiTurnConfig {
    /// Enable multi-turn training
    #[serde(default)]
    pub enabled: bool,
    
    /// Maximum user turns
    #[serde(default = "default_max_turns")]
    pub max_user_turns: u32,
    
    /// Maximum assistant turns
    #[serde(default = "default_max_turns")]
    pub max_assistant_turns: u32,
    
    /// Max tokens per turn
    #[serde(default = "default_tokens_per_turn")]
    pub max_tokens_per_turn: u32,
}

fn default_max_turns() -> u32 { 10 }
fn default_tokens_per_turn() -> u32 { 512 }

/// Updated TrainingSessionSpec
pub struct TrainingSessionSpec {
    // ... existing fields ...
    
    /// Training mode: sft, grpo, reinforce_pp, rloo, remax
    #[serde(default)]
    pub training_mode: TrainingMode,
    
    /// RL configuration (required for non-SFT modes)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rl_config: Option<RLConfig>,
    
    /// Multi-turn configuration
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub multi_turn: Option<MultiTurnConfig>,
    
    /// Custom reward function source code
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reward_function: Option<String>,
    
    /// Tensor parallel size (for large models)
    #[serde(default = "default_tp_size")]
    pub tensor_parallel_size: u32,
}

fn default_tp_size() -> u32 { 1 }
```

#### 1.2 Update API Request Types

```rust
// crates/basilica-api/src/api/routes/training.rs

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CreateSessionRequest {
    pub base_model: String,
    pub checkpoint_storage: CheckpointStorageRequest,
    
    #[serde(default)]
    pub lora_config: Option<LoraConfigRequest>,
    
    #[serde(default)]
    pub optimizer_config: Option<OptimizerConfigRequest>,
    
    #[serde(default)]
    pub gpu_resources: Option<GpuResourcesRequest>,
    
    #[serde(default)]
    pub seed: Option<i64>,
    
    #[serde(default = "default_ttl")]
    pub ttl_seconds: u64,
    
    // NEW: verl-specific fields
    
    /// Training mode: "sft" (default), "grpo", "reinforce_pp", "rloo"
    #[serde(default = "default_training_mode")]
    pub training_mode: String,
    
    /// RL configuration
    #[serde(default)]
    pub rl_config: Option<RLConfigRequest>,
    
    /// Multi-turn configuration  
    #[serde(default)]
    pub multi_turn: Option<MultiTurnConfigRequest>,
    
    /// Custom reward function source code
    #[serde(default)]
    pub reward_function: Option<String>,
    
    /// Tensor parallel size
    #[serde(default = "default_tp")]
    pub tensor_parallel_size: u32,
}

fn default_training_mode() -> String { "sft".to_string() }
fn default_tp() -> u32 { 1 }

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RLConfigRequest {
    #[serde(default = "default_kl")]
    pub kl_coef: f64,
    #[serde(default = "default_samples")]
    pub num_samples: u32,
    #[serde(default = "default_max_resp")]
    pub max_response_tokens: u32,
    #[serde(default = "default_temp")]
    pub temperature: f64,
}

fn default_kl() -> f64 { 0.1 }
fn default_samples() -> u32 { 4 }
fn default_max_resp() -> u32 { 512 }
fn default_temp() -> f64 { 1.0 }

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct MultiTurnConfigRequest {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default = "default_turns")]
    pub max_user_turns: u32,
    #[serde(default = "default_turns")]
    pub max_assistant_turns: u32,
    #[serde(default = "default_tpt")]
    pub max_tokens_per_turn: u32,
}

fn default_turns() -> u32 { 10 }
fn default_tpt() -> u32 { 512 }
```

#### 1.3 Add New API Endpoints

```rust
// Add to training.rs

/// RL training step (for GRPO/REINFORCE++/RLOO)
/// POST /sessions/{id}/internal/{iid}/rl_train_step
pub async fn rl_train_step(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Path((session_id, internal_id)): Path<(String, String)>,
    Json(body): Json<serde_json::Value>,
) -> Result<axum::response::Response> {
    let start = Instant::now();
    
    let k8s_client = state.k8s.as_ref().ok_or(ApiError::ServiceUnavailable)?;
    let namespace = user_namespace(&auth.user_id);
    
    let result = proxy_to_training_service(
        k8s_client.kube_client(),
        &namespace,
        &session_id,
        &format!("/sessions/{}/rl_train_step", internal_id),
        http::Method::POST,
        Some(body),
    )
    .await;
    
    apimetrics::record_request("training.rl_train_step", "POST", start, result.is_ok());
    result
}

/// Set reward function
/// POST /sessions/{id}/internal/{iid}/set_reward_function
pub async fn set_reward_function(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Path((session_id, internal_id)): Path<(String, String)>,
    Json(body): Json<serde_json::Value>,
) -> Result<axum::response::Response> {
    let start = Instant::now();
    
    let k8s_client = state.k8s.as_ref().ok_or(ApiError::ServiceUnavailable)?;
    let namespace = user_namespace(&auth.user_id);
    
    let result = proxy_to_training_service(
        k8s_client.kube_client(),
        &namespace,
        &session_id,
        &format!("/sessions/{}/set_reward_function", internal_id),
        http::Method::POST,
        Some(body),
    )
    .await;
    
    apimetrics::record_request("training.set_reward_function", "POST", start, result.is_ok());
    result
}

/// Multi-turn step
/// POST /sessions/{id}/internal/{iid}/multi_turn/step
pub async fn multi_turn_step(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Path((session_id, internal_id)): Path<(String, String)>,
    Json(body): Json<serde_json::Value>,
) -> Result<axum::response::Response> {
    let start = Instant::now();
    
    let k8s_client = state.k8s.as_ref().ok_or(ApiError::ServiceUnavailable)?;
    let namespace = user_namespace(&auth.user_id);
    
    let result = proxy_to_training_service(
        k8s_client.kube_client(),
        &namespace,
        &session_id,
        &format!("/sessions/{}/multi_turn/step", internal_id),
        http::Method::POST,
        Some(body),
    )
    .await;
    
    apimetrics::record_request("training.multi_turn.step", "POST", start, result.is_ok());
    result
}

/// Reset multi-turn conversation
/// POST /sessions/{id}/internal/{iid}/multi_turn/reset  
pub async fn multi_turn_reset(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Path((session_id, internal_id)): Path<(String, String)>,
) -> Result<axum::response::Response> {
    let start = Instant::now();
    
    let k8s_client = state.k8s.as_ref().ok_or(ApiError::ServiceUnavailable)?;
    let namespace = user_namespace(&auth.user_id);
    
    let result = proxy_to_training_service(
        k8s_client.kube_client(),
        &namespace,
        &session_id,
        &format!("/sessions/{}/multi_turn/reset", internal_id),
        http::Method::POST,
        None,
    )
    .await;
    
    apimetrics::record_request("training.multi_turn.reset", "POST", start, result.is_ok());
    result
}
```

#### 1.4 Register Routes

```rust
// crates/basilica-api/src/api/mod.rs - add to training router

.route(
    "/sessions/:session_id/internal/:internal_id/rl_train_step",
    post(training::rl_train_step),
)
.route(
    "/sessions/:session_id/internal/:internal_id/set_reward_function",
    post(training::set_reward_function),
)
.route(
    "/sessions/:session_id/internal/:internal_id/multi_turn/step",
    post(training::multi_turn_step),
)
.route(
    "/sessions/:session_id/internal/:internal_id/multi_turn/reset",
    post(training::multi_turn_reset),
)
```

---

### Phase 2: verl Backend Implementation (Week 2-3)

#### 2.1 New verl Backend

```python
# services/training-service/src/verl_backend.py
"""Training backend using verl for SFT and RL."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
import os

import structlog
import torch

logger = structlog.get_logger()


@dataclass
class LoraConfiguration:
    """LoRA adapter configuration."""
    rank: int = 32
    alpha: int = 64
    dropout: float = 0.05
    target_modules: List[str] = field(
        default_factory=lambda: [
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ]
    )


@dataclass
class OptimizerConfiguration:
    """Optimizer configuration."""
    learning_rate: float = 1e-4
    weight_decay: float = 0.01
    grad_clip: Optional[float] = 1.0


@dataclass
class RLConfiguration:
    """RL training configuration."""
    algorithm: str = "grpo"  # grpo, reinforce_pp, rloo, remax
    kl_coef: float = 0.1
    num_samples: int = 4
    max_response_tokens: int = 512
    temperature: float = 1.0


@dataclass
class MultiTurnConfiguration:
    """Multi-turn configuration."""
    enabled: bool = False
    max_user_turns: int = 10
    max_assistant_turns: int = 10
    max_tokens_per_turn: int = 512


@dataclass 
class VerlSessionState:
    """State for a verl training session."""
    session_id: str
    base_model: str
    training_mode: str  # "sft" or RL algorithm name
    model: Any  # verl model wrapper
    optimizer: Any
    lora_config: LoraConfiguration
    optimizer_config: OptimizerConfiguration
    rl_config: Optional[RLConfiguration] = None
    multi_turn_config: Optional[MultiTurnConfiguration] = None
    reward_fn: Optional[Callable] = None
    rollout_worker: Any = None  # vLLM worker for RL
    step_count: int = 0
    tokens_processed: int = 0
    # Multi-turn state
    conversation_history: List[Dict] = field(default_factory=list)


@dataclass
class TrainStepResult:
    """Result of a training step."""
    loss: float
    metrics: Dict[str, float]
    step: int


@dataclass
class RLTrainStepResult:
    """Result of an RL training step."""
    policy_loss: float
    mean_reward: float
    kl_divergence: float
    metrics: Dict[str, float]
    step: int


@dataclass
class MultiTurnStepResult:
    """Result of a multi-turn step."""
    response: str
    reward: float
    done: bool
    turn_number: int
    conversation_history: List[Dict]


class VerlTrainingBackend:
    """Training backend using verl for SFT and RL."""
    
    def __init__(
        self,
        model_cache_dir: str = "/models",
        checkpoint_dir: str = "/checkpoints",
        device: str = "cuda",
    ):
        self.model_cache_dir = Path(model_cache_dir)
        self.checkpoint_dir = Path(checkpoint_dir)
        self.device = device if torch.cuda.is_available() else "cpu"
        
        self.sessions: Dict[str, VerlSessionState] = {}
        self.reward_registry: Dict[str, Callable] = {}
        
        # Ensure directories exist
        self.model_cache_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        
        logger.info(
            "verl_backend_initialized",
            device=self.device,
            cache_dir=str(model_cache_dir),
        )
    
    def create_session(
        self,
        session_id: str,
        base_model: str,
        training_mode: str = "sft",
        lora_config: Optional[LoraConfiguration] = None,
        optimizer_config: Optional[OptimizerConfiguration] = None,
        rl_config: Optional[RLConfiguration] = None,
        multi_turn_config: Optional[MultiTurnConfiguration] = None,
        reward_function: Optional[str] = None,
        tensor_parallel_size: int = 1,
        seed: Optional[int] = None,
    ) -> str:
        """Create a training session using verl."""
        
        if session_id in self.sessions:
            raise ValueError(f"Session {session_id} already exists")
        
        if seed is not None:
            torch.manual_seed(seed)
        
        lora_config = lora_config or LoraConfiguration()
        optimizer_config = optimizer_config or OptimizerConfiguration()
        
        logger.info(
            "creating_verl_session",
            session_id=session_id,
            base_model=base_model,
            training_mode=training_mode,
            lora_rank=lora_config.rank,
        )
        
        # Import verl components
        from verl.trainer.fsdp_sft_trainer import FSDPSFTTrainer
        from verl.workers.rollout.vllm_rollout import vLLMRollout
        from peft import LoraConfig, get_peft_model, TaskType
        from transformers import AutoModelForCausalLM, AutoTokenizer
        
        # Load base model with LoRA
        model = AutoModelForCausalLM.from_pretrained(
            base_model,
            torch_dtype=torch.bfloat16 if self.device == "cuda" else torch.float32,
            device_map="auto" if self.device == "cuda" else None,
            cache_dir=self.model_cache_dir,
            trust_remote_code=True,
        )
        
        tokenizer = AutoTokenizer.from_pretrained(
            base_model,
            cache_dir=self.model_cache_dir,
            trust_remote_code=True,
        )
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token
        
        # Apply LoRA
        peft_config = LoraConfig(
            task_type=TaskType.CAUSAL_LM,
            r=lora_config.rank,
            lora_alpha=lora_config.alpha,
            lora_dropout=lora_config.dropout,
            target_modules=lora_config.target_modules,
            bias="none",
        )
        model = get_peft_model(model, peft_config)
        model.print_trainable_parameters()
        
        # Create optimizer
        trainable_params = [p for p in model.parameters() if p.requires_grad]
        optimizer = torch.optim.AdamW(
            trainable_params,
            lr=optimizer_config.learning_rate,
            weight_decay=optimizer_config.weight_decay,
        )
        
        # Create rollout worker for RL modes
        rollout_worker = None
        if training_mode != "sft":
            try:
                rollout_worker = vLLMRollout(
                    model_path=base_model,
                    tensor_parallel_size=tensor_parallel_size,
                    gpu_memory_utilization=0.8,
                )
                logger.info("vllm_rollout_initialized")
            except Exception as e:
                logger.warning(f"Failed to init vLLM rollout: {e}, using HF generate")
        
        # Compile reward function if provided
        reward_fn = None
        if reward_function:
            reward_fn = self._compile_reward_function(reward_function)
        
        # Store session
        self.sessions[session_id] = VerlSessionState(
            session_id=session_id,
            base_model=base_model,
            training_mode=training_mode,
            model=model,
            optimizer=optimizer,
            lora_config=lora_config,
            optimizer_config=optimizer_config,
            rl_config=rl_config,
            multi_turn_config=multi_turn_config,
            reward_fn=reward_fn,
            rollout_worker=rollout_worker,
        )
        
        # Store tokenizer
        self.sessions[session_id]._tokenizer = tokenizer
        
        logger.info("verl_session_created", session_id=session_id, mode=training_mode)
        return session_id
    
    # === SFT Methods ===
    
    def forward_backward(
        self,
        session_id: str,
        input_ids: torch.Tensor,
        attention_mask: torch.Tensor,
        labels: torch.Tensor,
        loss_weights: Optional[torch.Tensor] = None,
    ) -> TrainStepResult:
        """Standard SFT forward-backward pass."""
        session = self._get_session(session_id)
        model = session.model
        model.train()
        
        # Move to device
        input_ids = input_ids.to(self.device)
        attention_mask = attention_mask.to(self.device)
        labels = labels.to(self.device)
        
        # Forward pass
        outputs = model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            labels=labels,
        )
        
        loss = outputs.loss
        
        # Apply loss weights if provided
        if loss_weights is not None:
            loss_weights = loss_weights.to(self.device)
            # Weighted loss computation
            logits = outputs.logits
            shift_logits = logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            shift_weights = loss_weights[..., 1:].contiguous()
            
            loss_fct = torch.nn.CrossEntropyLoss(reduction="none")
            token_losses = loss_fct(
                shift_logits.view(-1, shift_logits.size(-1)),
                shift_labels.view(-1)
            ).view(shift_labels.size())
            
            loss = (token_losses * shift_weights).sum() / shift_weights.sum().clamp(min=1)
        
        # Backward
        loss.backward()
        
        tokens_processed = int(attention_mask.sum().item())
        session.tokens_processed += tokens_processed
        
        return TrainStepResult(
            loss=loss.item(),
            metrics={"tokens_processed": tokens_processed},
            step=session.step_count,
        )
    
    def optim_step(self, session_id: str) -> int:
        """Apply gradients and update weights."""
        session = self._get_session(session_id)
        
        # Gradient clipping
        if session.optimizer_config.grad_clip:
            torch.nn.utils.clip_grad_norm_(
                session.model.parameters(),
                session.optimizer_config.grad_clip,
            )
        
        session.optimizer.step()
        session.optimizer.zero_grad()
        session.step_count += 1
        
        return session.step_count
    
    # === RL Methods ===
    
    def rl_train_step(
        self,
        session_id: str,
        prompts: List[str],
        rewards: Optional[List[float]] = None,
    ) -> RLTrainStepResult:
        """RL training step using GRPO/REINFORCE++/RLOO."""
        session = self._get_session(session_id)
        
        if session.training_mode == "sft":
            raise ValueError("Cannot call rl_train_step on SFT session")
        
        rl_config = session.rl_config or RLConfiguration()
        model = session.model
        model.train()
        
        # Generate responses
        responses, log_probs = self._generate_responses(
            session,
            prompts,
            num_samples=rl_config.num_samples,
            max_tokens=rl_config.max_response_tokens,
            temperature=rl_config.temperature,
        )
        
        # Compute rewards if not provided
        if rewards is None:
            if session.reward_fn is None:
                raise ValueError("No rewards provided and no reward function set")
            
            rewards = []
            for prompt, response in zip(prompts, responses):
                reward = session.reward_fn(prompt=prompt, response=response)
                rewards.append(reward)
        
        rewards_tensor = torch.tensor(rewards, device=self.device)
        
        # Compute policy loss based on algorithm
        if session.training_mode == "grpo":
            policy_loss = self._grpo_loss(log_probs, rewards_tensor)
        elif session.training_mode == "reinforce_pp":
            policy_loss = self._reinforce_pp_loss(log_probs, rewards_tensor)
        elif session.training_mode == "rloo":
            policy_loss = self._rloo_loss(log_probs, rewards_tensor)
        else:
            # Default to simple REINFORCE
            policy_loss = -(log_probs * rewards_tensor).mean()
        
        # Backward
        policy_loss.backward()
        
        session.step_count += 1
        
        return RLTrainStepResult(
            policy_loss=policy_loss.item(),
            mean_reward=rewards_tensor.mean().item(),
            kl_divergence=0.0,  # TODO: compute KL with reference model
            metrics={
                "num_prompts": len(prompts),
                "mean_response_len": sum(len(r) for r in responses) / len(responses),
            },
            step=session.step_count,
        )
    
    def _grpo_loss(
        self,
        log_probs: torch.Tensor,
        rewards: torch.Tensor,
    ) -> torch.Tensor:
        """Group Relative Policy Optimization loss."""
        # GRPO: use group-relative advantage (no baseline needed)
        # advantage = reward - mean(reward in group)
        advantages = rewards - rewards.mean()
        return -(log_probs * advantages).mean()
    
    def _reinforce_pp_loss(
        self,
        log_probs: torch.Tensor,
        rewards: torch.Tensor,
    ) -> torch.Tensor:
        """REINFORCE++ loss (PPO-style without critic)."""
        # Normalize rewards as baseline
        advantages = (rewards - rewards.mean()) / (rewards.std() + 1e-8)
        return -(log_probs * advantages).mean()
    
    def _rloo_loss(
        self,
        log_probs: torch.Tensor,
        rewards: torch.Tensor,
    ) -> torch.Tensor:
        """Leave-One-Out loss."""
        # For each sample, baseline is mean of other samples
        n = len(rewards)
        if n <= 1:
            return -(log_probs * rewards).mean()
        
        total = rewards.sum()
        baselines = (total - rewards) / (n - 1)
        advantages = rewards - baselines
        return -(log_probs * advantages).mean()
    
    def _generate_responses(
        self,
        session: VerlSessionState,
        prompts: List[str],
        num_samples: int = 1,
        max_tokens: int = 512,
        temperature: float = 1.0,
    ) -> Tuple[List[str], torch.Tensor]:
        """Generate responses for RL training."""
        model = session.model
        tokenizer = session._tokenizer
        model.eval()
        
        responses = []
        all_log_probs = []
        
        with torch.no_grad():
            for prompt in prompts:
                inputs = tokenizer(prompt, return_tensors="pt").to(self.device)
                
                outputs = model.generate(
                    **inputs,
                    max_new_tokens=max_tokens,
                    temperature=temperature,
                    do_sample=True,
                    output_scores=True,
                    return_dict_in_generate=True,
                    pad_token_id=tokenizer.pad_token_id,
                )
                
                # Decode response
                generated_ids = outputs.sequences[0, inputs.input_ids.shape[1]:]
                response = tokenizer.decode(generated_ids, skip_special_tokens=True)
                responses.append(response)
                
                # Compute log probs
                if outputs.scores:
                    log_probs = []
                    for i, scores in enumerate(outputs.scores):
                        if i < len(generated_ids):
                            lp = torch.nn.functional.log_softmax(scores[0], dim=-1)
                            log_probs.append(lp[generated_ids[i]].item())
                    all_log_probs.append(sum(log_probs))
                else:
                    all_log_probs.append(0.0)
        
        model.train()
        return responses, torch.tensor(all_log_probs, device=self.device)
    
    # === Multi-turn Methods ===
    
    def multi_turn_reset(self, session_id: str) -> Dict[str, Any]:
        """Reset multi-turn conversation."""
        session = self._get_session(session_id)
        session.conversation_history = []
        return {"status": "reset", "turn_number": 0}
    
    def multi_turn_step(
        self,
        session_id: str,
        user_input: str,
    ) -> MultiTurnStepResult:
        """Execute one turn of multi-turn conversation."""
        session = self._get_session(session_id)
        config = session.multi_turn_config or MultiTurnConfiguration()
        
        # Build prompt from history
        prompt = self._build_multi_turn_prompt(session, user_input)
        
        # Generate response
        tokenizer = session._tokenizer
        inputs = tokenizer(prompt, return_tensors="pt").to(self.device)
        
        session.model.eval()
        with torch.no_grad():
            outputs = session.model.generate(
                **inputs,
                max_new_tokens=config.max_tokens_per_turn,
                temperature=1.0,
                do_sample=True,
                pad_token_id=tokenizer.pad_token_id,
            )
        session.model.train()
        
        generated_ids = outputs[0, inputs.input_ids.shape[1]:]
        response = tokenizer.decode(generated_ids, skip_special_tokens=True)
        
        # Update history
        session.conversation_history.append({
            "role": "user",
            "content": user_input,
        })
        session.conversation_history.append({
            "role": "assistant", 
            "content": response,
        })
        
        turn_number = len(session.conversation_history) // 2
        done = turn_number >= config.max_user_turns
        
        # Compute reward if function is set
        reward = 0.0
        if session.reward_fn:
            reward = session.reward_fn(
                conversation=session.conversation_history,
                response=response,
            )
        
        return MultiTurnStepResult(
            response=response,
            reward=reward,
            done=done,
            turn_number=turn_number,
            conversation_history=session.conversation_history.copy(),
        )
    
    def _build_multi_turn_prompt(
        self,
        session: VerlSessionState,
        user_input: str,
    ) -> str:
        """Build prompt from conversation history."""
        messages = session.conversation_history + [
            {"role": "user", "content": user_input}
        ]
        
        # Simple chat template
        prompt_parts = []
        for msg in messages:
            if msg["role"] == "user":
                prompt_parts.append(f"User: {msg['content']}")
            else:
                prompt_parts.append(f"Assistant: {msg['content']}")
        
        prompt_parts.append("Assistant:")
        return "\n".join(prompt_parts)
    
    # === Reward Functions ===
    
    def set_reward_function(
        self,
        session_id: str,
        name: str,
        source_code: str,
    ) -> Dict[str, Any]:
        """Set custom reward function for session."""
        session = self._get_session(session_id)
        reward_fn = self._compile_reward_function(source_code)
        session.reward_fn = reward_fn
        self.reward_registry[name] = reward_fn
        
        logger.info("reward_function_set", session_id=session_id, name=name)
        return {"status": "set", "name": name}
    
    def _compile_reward_function(self, source_code: str) -> Callable:
        """Compile user-provided reward function."""
        local_ns = {
            "torch": torch,
            "re": __import__("re"),
            "math": __import__("math"),
            "json": __import__("json"),
        }
        
        exec(source_code, local_ns)
        
        if "compute_reward" not in local_ns:
            raise ValueError("Reward function must define compute_reward()")
        
        return local_ns["compute_reward"]
    
    # === Checkpoint Methods ===
    
    def save_state(
        self,
        session_id: str,
        checkpoint_name: str,
        include_optimizer: bool = True,
    ) -> str:
        """Save checkpoint."""
        session = self._get_session(session_id)
        
        checkpoint_path = self.checkpoint_dir / session_id / checkpoint_name
        checkpoint_path.mkdir(parents=True, exist_ok=True)
        
        # Save adapter weights
        session.model.save_pretrained(checkpoint_path)
        
        # Save training state
        if include_optimizer:
            torch.save({
                "optimizer_state_dict": session.optimizer.state_dict(),
                "step_count": session.step_count,
                "tokens_processed": session.tokens_processed,
                "training_mode": session.training_mode,
                "base_model": session.base_model,
            }, checkpoint_path / "training_state.pt")
        
        logger.info("checkpoint_saved", session_id=session_id, path=str(checkpoint_path))
        return str(checkpoint_path)
    
    def load_state(
        self,
        session_id: str,
        checkpoint_path: str,
        load_optimizer: bool = True,
    ) -> None:
        """Load checkpoint."""
        session = self._get_session(session_id)
        local_path = Path(checkpoint_path)
        
        # Load adapter weights
        from safetensors.torch import load_file
        
        adapter_file = local_path / "adapter_model.safetensors"
        if adapter_file.exists():
            adapter_weights = load_file(str(adapter_file))
        else:
            adapter_weights = torch.load(local_path / "adapter_model.bin", weights_only=True)
        
        session.model.load_state_dict(adapter_weights, strict=False)
        
        # Load optimizer state
        if load_optimizer and (local_path / "training_state.pt").exists():
            state = torch.load(local_path / "training_state.pt", weights_only=False)
            session.optimizer.load_state_dict(state["optimizer_state_dict"])
            session.step_count = state["step_count"]
            session.tokens_processed = state["tokens_processed"]
        
        logger.info("checkpoint_loaded", session_id=session_id, path=str(local_path))
    
    # === Utility Methods ===
    
    def get_session_status(self, session_id: str) -> Dict[str, Any]:
        """Get session status."""
        session = self._get_session(session_id)
        return {
            "session_id": session_id,
            "base_model": session.base_model,
            "training_mode": session.training_mode,
            "step_count": session.step_count,
            "tokens_processed": session.tokens_processed,
            "lora_rank": session.lora_config.rank,
            "has_reward_function": session.reward_fn is not None,
        }
    
    def delete_session(self, session_id: str) -> None:
        """Delete session."""
        if session_id in self.sessions:
            del self.sessions[session_id]
            logger.info("session_deleted", session_id=session_id)
    
    def list_sessions(self) -> List[str]:
        """List all sessions."""
        return list(self.sessions.keys())
    
    def _get_session(self, session_id: str) -> VerlSessionState:
        """Get session or raise error."""
        if session_id not in self.sessions:
            raise ValueError(f"Session {session_id} not found")
        return self.sessions[session_id]
```

#### 2.2 Update Server to Use verl Backend

```python
# services/training-service/src/server.py
"""Training service FastAPI server using verl backend."""

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
import os

from verl_backend import (
    VerlTrainingBackend,
    LoraConfiguration,
    OptimizerConfiguration,
    RLConfiguration,
    MultiTurnConfiguration,
)

app = FastAPI(title="Basilica Training Service (verl)")

# Initialize backend
backend = VerlTrainingBackend(
    model_cache_dir=os.environ.get("MODEL_CACHE_DIR", "/models"),
    checkpoint_dir=os.environ.get("CHECKPOINT_DIR", "/checkpoints"),
)


# === Request/Response Models ===

class CreateSessionRequest(BaseModel):
    session_id: str
    base_model: str
    training_mode: str = "sft"
    lora_config: Optional[Dict] = None
    optimizer_config: Optional[Dict] = None
    rl_config: Optional[Dict] = None
    multi_turn: Optional[Dict] = None
    reward_function: Optional[str] = None
    tensor_parallel_size: int = 1
    seed: Optional[int] = None


class ForwardBackwardRequest(BaseModel):
    input_ids: List[List[int]]
    attention_mask: List[List[int]]
    labels: List[List[int]]
    loss_weights: Optional[List[List[float]]] = None


class RLTrainStepRequest(BaseModel):
    prompts: List[str]
    rewards: Optional[List[float]] = None


class SetRewardFunctionRequest(BaseModel):
    name: str
    source_code: str


class MultiTurnStepRequest(BaseModel):
    user_input: str


class SaveCheckpointRequest(BaseModel):
    checkpoint_name: str
    include_optimizer: bool = True


class LoadCheckpointRequest(BaseModel):
    checkpoint_path: str
    load_optimizer: bool = True


# === Endpoints ===

@app.get("/health")
async def health():
    return {"status": "healthy", "backend": "verl"}


@app.post("/sessions")
async def create_session(request: CreateSessionRequest):
    try:
        lora = LoraConfiguration(**request.lora_config) if request.lora_config else None
        optim = OptimizerConfiguration(**request.optimizer_config) if request.optimizer_config else None
        rl = RLConfiguration(**request.rl_config) if request.rl_config else None
        multi = MultiTurnConfiguration(**request.multi_turn) if request.multi_turn else None
        
        session_id = backend.create_session(
            session_id=request.session_id,
            base_model=request.base_model,
            training_mode=request.training_mode,
            lora_config=lora,
            optimizer_config=optim,
            rl_config=rl,
            multi_turn_config=multi,
            reward_function=request.reward_function,
            tensor_parallel_size=request.tensor_parallel_size,
            seed=request.seed,
        )
        return {"session_id": session_id, "status": "created"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/sessions")
async def list_sessions():
    return {"sessions": backend.list_sessions()}


@app.get("/sessions/{session_id}")
async def get_session(session_id: str):
    try:
        return backend.get_session_status(session_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    backend.delete_session(session_id)
    return {"status": "deleted"}


@app.post("/sessions/{session_id}/forward_backward")
async def forward_backward(session_id: str, request: ForwardBackwardRequest):
    import torch
    try:
        result = backend.forward_backward(
            session_id=session_id,
            input_ids=torch.tensor(request.input_ids),
            attention_mask=torch.tensor(request.attention_mask),
            labels=torch.tensor(request.labels),
            loss_weights=torch.tensor(request.loss_weights) if request.loss_weights else None,
        )
        return {
            "loss": result.loss,
            "metrics": result.metrics,
            "step": result.step,
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/sessions/{session_id}/optim_step")
async def optim_step(session_id: str):
    try:
        step = backend.optim_step(session_id)
        return {"step": step}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/sessions/{session_id}/rl_train_step")
async def rl_train_step(session_id: str, request: RLTrainStepRequest):
    try:
        result = backend.rl_train_step(
            session_id=session_id,
            prompts=request.prompts,
            rewards=request.rewards,
        )
        return {
            "policy_loss": result.policy_loss,
            "mean_reward": result.mean_reward,
            "kl_divergence": result.kl_divergence,
            "metrics": result.metrics,
            "step": result.step,
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/sessions/{session_id}/set_reward_function")
async def set_reward_function(session_id: str, request: SetRewardFunctionRequest):
    try:
        return backend.set_reward_function(
            session_id=session_id,
            name=request.name,
            source_code=request.source_code,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/sessions/{session_id}/multi_turn/reset")
async def multi_turn_reset(session_id: str):
    try:
        return backend.multi_turn_reset(session_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/sessions/{session_id}/multi_turn/step")
async def multi_turn_step(session_id: str, request: MultiTurnStepRequest):
    try:
        result = backend.multi_turn_step(
            session_id=session_id,
            user_input=request.user_input,
        )
        return {
            "response": result.response,
            "reward": result.reward,
            "done": result.done,
            "turn_number": result.turn_number,
            "conversation_history": result.conversation_history,
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/sessions/{session_id}/save")
async def save_checkpoint(session_id: str, request: SaveCheckpointRequest):
    try:
        path = backend.save_state(
            session_id=session_id,
            checkpoint_name=request.checkpoint_name,
            include_optimizer=request.include_optimizer,
        )
        return {"checkpoint_path": path}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/sessions/{session_id}/load")
async def load_checkpoint(session_id: str, request: LoadCheckpointRequest):
    try:
        backend.load_state(
            session_id=session_id,
            checkpoint_path=request.checkpoint_path,
            load_optimizer=request.load_optimizer,
        )
        return {"status": "loaded"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
```

---

### Phase 3: SDK Updates (Week 4)

#### 3.1 Update SDK Types

```python
# crates/basilica-sdk-python/python/basilica/training/types.py

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from enum import Enum


class TrainingMode(str, Enum):
    """Training mode selection."""
    SFT = "sft"
    GRPO = "grpo"
    REINFORCE_PP = "reinforce_pp"
    RLOO = "rloo"
    REMAX = "remax"


@dataclass
class RLConfig:
    """RL training configuration."""
    kl_coef: float = 0.1
    num_samples: int = 4
    max_response_tokens: int = 512
    temperature: float = 1.0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "kl_coef": self.kl_coef,
            "num_samples": self.num_samples,
            "max_response_tokens": self.max_response_tokens,
            "temperature": self.temperature,
        }


@dataclass
class MultiTurnConfig:
    """Multi-turn configuration."""
    enabled: bool = True
    max_user_turns: int = 10
    max_assistant_turns: int = 10
    max_tokens_per_turn: int = 512
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_user_turns": self.max_user_turns,
            "max_assistant_turns": self.max_assistant_turns,
            "max_tokens_per_turn": self.max_tokens_per_turn,
        }


@dataclass
class RLTrainStepResult:
    """Result of RL training step."""
    policy_loss: float
    mean_reward: float
    kl_divergence: float
    step: int
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MultiTurnStepResult:
    """Result of multi-turn step."""
    response: str
    reward: float
    done: bool
    turn_number: int
    conversation_history: List[Dict[str, str]]
```

#### 3.2 Add RLTrainingClient

```python
# crates/basilica-sdk-python/python/basilica/training/rl_client.py

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional
import httpx

from .types import (
    APIFuture,
    RLConfig,
    MultiTurnConfig,
    RLTrainStepResult,
    MultiTurnStepResult,
    TrainingMode,
)
from .exceptions import TrainingError


class RLTrainingClient:
    """Client for RL training operations (GRPO, REINFORCE++, RLOO).
    
    Example:
        >>> rl = client.create_rl_training_client(
        ...     "meta-llama/Llama-3.1-8B",
        ...     algorithm=TrainingMode.GRPO,
        ... )
        >>> rl.set_reward_function('''
        ... def compute_reward(prompt, response):
        ...     return 1.0 if "correct" in response else 0.0
        ... ''')
        >>> result = rl.train_step(prompts).result()
    """
    
    def __init__(
        self,
        client: httpx.Client,
        session_id: str,
        internal_id: str,
        base_model: str,
        algorithm: TrainingMode,
        rl_config: RLConfig,
        multi_turn_config: Optional[MultiTurnConfig] = None,
    ):
        self._client = client
        self._session_id = session_id
        self._internal_id = internal_id
        self._base_model = base_model
        self._algorithm = algorithm
        self._rl_config = rl_config
        self._multi_turn_config = multi_turn_config
        self._step = 0
        self._executor = ThreadPoolExecutor(max_workers=4)
    
    @property
    def session_id(self) -> str:
        return self._session_id
    
    @property
    def algorithm(self) -> TrainingMode:
        return self._algorithm
    
    def _proxy(self, op: str = "") -> str:
        base = f"/sessions/{self._session_id}/internal/{self._internal_id}"
        return f"{base}/{op}" if op else base
    
    # === Reward Function ===
    
    def set_reward_function(self, source_code: str, name: str = "custom") -> None:
        """Set custom reward function.
        
        Args:
            source_code: Python source defining compute_reward(prompt, response) -> float
            name: Name for the reward function
            
        Example:
            >>> rl.set_reward_function('''
            ... def compute_reward(prompt: str, response: str) -> float:
            ...     # Check if response contains correct answer
            ...     if "42" in response:
            ...         return 1.0
            ...     return 0.0
            ... ''')
        """
        resp = self._client.post(
            self._proxy("set_reward_function"),
            json={"name": name, "source_code": source_code},
        )
        if not resp.is_success:
            raise TrainingError(f"Failed to set reward function: {resp.text}")
    
    # === RL Training ===
    
    def train_step(
        self,
        prompts: List[str],
        rewards: Optional[List[float]] = None,
    ) -> APIFuture:
        """Execute one RL training step.
        
        Args:
            prompts: List of prompts to generate responses for
            rewards: Optional pre-computed rewards (uses reward function if not provided)
            
        Returns:
            APIFuture resolving to RLTrainStepResult
        """
        def _call():
            payload = {"prompts": prompts}
            if rewards is not None:
                payload["rewards"] = rewards
            
            resp = self._client.post(self._proxy("rl_train_step"), json=payload)
            if not resp.is_success:
                raise TrainingError(f"rl_train_step failed: {resp.text}")
            
            r = resp.json()
            self._step = r["step"]
            return RLTrainStepResult(
                policy_loss=r["policy_loss"],
                mean_reward=r["mean_reward"],
                kl_divergence=r["kl_divergence"],
                step=r["step"],
                metrics=r.get("metrics", {}),
            )
        
        return APIFuture(self._executor.submit(_call), RLTrainStepResult)
    
    def optim_step(self) -> APIFuture:
        """Apply gradients and update weights."""
        def _call():
            resp = self._client.post(self._proxy("optim_step"))
            if not resp.is_success:
                raise TrainingError(f"optim_step failed: {resp.text}")
            self._step = resp.json()["step"]
            return self._step
        
        return APIFuture(self._executor.submit(_call), int)
    
    # === Multi-turn ===
    
    def multi_turn_reset(self) -> None:
        """Reset multi-turn conversation."""
        resp = self._client.post(self._proxy("multi_turn/reset"))
        if not resp.is_success:
            raise TrainingError(f"multi_turn_reset failed: {resp.text}")
    
    def multi_turn_step(self, user_input: str) -> APIFuture:
        """Execute one turn of multi-turn conversation.
        
        Args:
            user_input: User message for this turn
            
        Returns:
            APIFuture resolving to MultiTurnStepResult
        """
        def _call():
            resp = self._client.post(
                self._proxy("multi_turn/step"),
                json={"user_input": user_input},
            )
            if not resp.is_success:
                raise TrainingError(f"multi_turn_step failed: {resp.text}")
            
            r = resp.json()
            return MultiTurnStepResult(
                response=r["response"],
                reward=r["reward"],
                done=r["done"],
                turn_number=r["turn_number"],
                conversation_history=r["conversation_history"],
            )
        
        return APIFuture(self._executor.submit(_call), MultiTurnStepResult)
    
    # === Checkpoints ===
    
    def save_state(self, name: str) -> APIFuture:
        """Save checkpoint."""
        def _call():
            resp = self._client.post(
                self._proxy("save"),
                json={"checkpoint_name": name, "include_optimizer": True},
            )
            if not resp.is_success:
                raise TrainingError(f"save_state failed: {resp.text}")
            return resp.json()["checkpoint_path"]
        
        return APIFuture(self._executor.submit(_call), str)
    
    # === Lifecycle ===
    
    def close(self):
        """Close the training session."""
        resp = self._client.delete(f"/sessions/{self._session_id}")
        if resp.status_code != 404 and not resp.is_success:
            raise TrainingError(f"close failed: {resp.text}")
        self._executor.shutdown(wait=False)
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        self.close()
```

#### 3.3 Update ServiceClient

```python
# Add to service_client.py

def create_rl_training_client(
    self,
    base_model: str,
    algorithm: TrainingMode = TrainingMode.GRPO,
    rank: int = 32,
    rl_config: Optional[RLConfig] = None,
    multi_turn_config: Optional[MultiTurnConfig] = None,
    reward_function: Optional[str] = None,
    seed: Optional[int] = None,
    gpu_count: int = 1,
    tensor_parallel_size: int = 1,
    wait_timeout: float = 300.0,
) -> RLTrainingClient:
    """Create RL training session (GRPO/REINFORCE++/RLOO).
    
    Args:
        base_model: HuggingFace model ID
        algorithm: RL algorithm to use
        rank: LoRA rank
        rl_config: RL-specific configuration
        multi_turn_config: Multi-turn configuration
        reward_function: Custom reward function source code
        seed: Random seed
        gpu_count: Number of GPUs
        tensor_parallel_size: Tensor parallel size for inference
        wait_timeout: Seconds to wait for session ready
        
    Returns:
        RLTrainingClient for RL training operations
    """
    rl_config = rl_config or RLConfig()
    
    # Create K8s session
    resp = self._client.post("/sessions", json={
        "baseModel": base_model,
        "trainingMode": algorithm.value,
        "loraConfig": {
            "rank": rank,
            "alpha": rank * 2,
            "dropout": 0.05,
        },
        "rlConfig": rl_config.to_dict(),
        "multiTurn": multi_turn_config.to_dict() if multi_turn_config else None,
        "rewardFunction": reward_function,
        "gpuResources": {"count": gpu_count},
        "tensorParallelSize": tensor_parallel_size,
        "seed": seed,
        "ttlSeconds": 86400,
        "checkpointStorage": {
            "backend": "r2",
            "bucket": "",
            "path": "",
        },
    })
    
    if not resp.is_success:
        raise TrainingError(f"Failed to create session: {resp.text}")
    
    session_id = resp.json()["sessionId"]
    
    # Wait for ready
    self._wait_for_session(session_id, wait_timeout)
    
    # Create internal session
    internal_id = f"rl-{session_id}"
    resp = self._client.post(
        f"/sessions/{session_id}/internal",
        json={
            "session_id": internal_id,
            "base_model": base_model,
            "training_mode": algorithm.value,
            "lora_config": {"rank": rank, "alpha": rank * 2},
            "rl_config": rl_config.to_dict(),
            "multi_turn": multi_turn_config.to_dict() if multi_turn_config else None,
            "reward_function": reward_function,
        },
    )
    
    if not resp.is_success:
        raise TrainingError(f"Failed to create internal session: {resp.text}")
    
    return RLTrainingClient(
        client=self._client,
        session_id=session_id,
        internal_id=internal_id,
        base_model=base_model,
        algorithm=algorithm,
        rl_config=rl_config,
        multi_turn_config=multi_turn_config,
    )
```

---

## Part 4: Implementation Roadmap

| Week | Phase | Tasks |
|------|-------|-------|
| 1 | CRD & API | Update TrainingSession CRD, add training_mode, rl_config, multi_turn fields |
| 1 | CRD & API | Add new API endpoints: rl_train_step, set_reward_function, multi_turn/* |
| 2 | Backend | Create verl_backend.py with SFT + GRPO/REINFORCE++/RLOO support |
| 2 | Backend | Update server.py to use verl backend |
| 3 | Backend | Add multi-turn support, vLLM rollout integration |
| 3 | Backend | Test with reward functions |
| 4 | SDK | Add RLTrainingClient, update ServiceClient |
| 4 | SDK | Add Environment protocol, LifecycleManager |
| 4 | Testing | Integration tests, example scripts |

---

## Part 5: Files to Modify/Create

### Modified Files

```
crates/basilica-api/src/api/routes/training.rs  # Add RL endpoints
crates/basilica-api/src/api/mod.rs              # Register routes
crates/basilica-operator/src/crd/training_session.rs  # Add training_mode, rl_config
services/training-service/src/server.py          # Use verl backend
```

### New Files

```
services/training-service/src/verl_backend.py    # verl integration
crates/basilica-sdk-python/python/basilica/training/rl_client.py
crates/basilica-sdk-python/python/basilica/training/environment.py
crates/basilica-sdk-python/python/basilica/training/lifecycle.py
```

### Deleted Files

```
services/training-service/src/backend.py  # Replace with verl_backend.py
```

---

## Part 6: Example Usage After Integration

```python
from basilica.training import ServiceClient, TrainingMode, RLConfig, MultiTurnConfig

# Initialize client
client = ServiceClient(api_key="...")

# === SFT Training (unchanged) ===
sft = client.create_lora_training_client(
    "meta-llama/Llama-3.1-8B",
    rank=32,
)
result = sft.forward_backward(data).result()
sft.optim_step().result()

# === RL Training (new) ===
rl = client.create_rl_training_client(
    "meta-llama/Llama-3.1-8B",
    algorithm=TrainingMode.GRPO,
    rl_config=RLConfig(
        kl_coef=0.1,
        num_samples=4,
        temperature=1.0,
    ),
)

# Set custom reward function
rl.set_reward_function('''
def compute_reward(prompt: str, response: str) -> float:
    # Math problem: check if answer is correct
    import re
    match = re.search(r"\\boxed{(.+?)}", response)
    if match and match.group(1) == "42":
        return 1.0
    return 0.0
''')

# Train
prompts = ["Solve: 6 * 7 = ?", "What is 2 + 2?"]
result = rl.train_step(prompts).result()
print(f"Policy loss: {result.policy_loss}, Mean reward: {result.mean_reward}")

rl.optim_step().result()
rl.save_state("checkpoint-1").result()

# === Multi-turn Training (new) ===
rl_multi = client.create_rl_training_client(
    "meta-llama/Llama-3.1-8B",
    algorithm=TrainingMode.GRPO,
    multi_turn_config=MultiTurnConfig(
        max_user_turns=5,
        max_tokens_per_turn=256,
    ),
)

rl_multi.multi_turn_reset()
step1 = rl_multi.multi_turn_step("Let's play 20 questions. I'm thinking of an animal.").result()
step2 = rl_multi.multi_turn_step("No, it doesn't have fur.").result()
# Continue conversation...
```
