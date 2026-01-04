# verl Integration Implementation Plan

## Overview

This document provides a detailed, test-driven implementation plan for integrating verl SDK into Basilica's training API. Each task is gated by passing tests before proceeding.

### Testing Requirements

Every task MUST pass:
1. **Unit tests** for the specific component
2. **E2E test**: `./scripts/local-training-e2e.sh test`
3. **Example validation**: `python examples/training_example.py` (with new features)

### Test Commands

```bash
# Run full E2E test suite
./scripts/local-training-e2e.sh test

# Run specific example
python examples/training_example.py           # Main training example
python examples/training_example.py --rest    # RestClient demo
python examples/training_example.py --rl      # RL training demo (new)

# Run unit tests
cargo test -p basilica-api
cargo test -p basilica-operator
```

---

## Phase 1: CRD & API Schema Updates (Week 1)

### Task 1.1: Add TrainingMode to CRD

**Files to modify:**
- `crates/basilica-operator/src/crd/training_session.rs`

**Changes:**
```rust
// Add training mode enum
#[derive(Clone, Debug, Default, Deserialize, Serialize, JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum TrainingMode {
    #[default]
    Sft,
    Grpo,
    ReinforcePp,
    Rloo,
}

// Add to TrainingSessionSpec
pub training_mode: TrainingMode,
```

**Test gate:**
```bash
# 1. Unit test
cargo test -p basilica-operator training_session

# 2. E2E test (existing functionality unchanged)
./scripts/local-training-e2e.sh test

# Expected: All existing tests pass, CRD accepts new field
```

**Acceptance criteria:**
- [ ] `TrainingMode` enum compiles
- [ ] CRD schema includes `trainingMode` field
- [ ] Default value is `sft`
- [ ] Existing E2E tests pass unchanged

---

### Task 1.2: Add RLConfig to CRD

**Files to modify:**
- `crates/basilica-operator/src/crd/training_session.rs`

**Changes:**
```rust
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct RLConfig {
    #[serde(default = "default_kl_coef")]
    pub kl_coef: f64,
    
    #[serde(default = "default_num_samples")]
    pub num_samples: u32,
    
    #[serde(default = "default_max_response_tokens")]
    pub max_response_tokens: u32,
    
    #[serde(default = "default_temperature")]
    pub temperature: f64,
}

// Add to TrainingSessionSpec
pub rl_config: Option<RLConfig>,
```

**Test gate:**
```bash
cargo test -p basilica-operator training_session
./scripts/local-training-e2e.sh test
```

**Acceptance criteria:**
- [ ] `RLConfig` struct compiles with defaults
- [ ] CRD accepts `rlConfig` field
- [ ] Existing E2E tests pass unchanged

---

### Task 1.3: Add MultiTurnConfig to CRD

**Files to modify:**
- `crates/basilica-operator/src/crd/training_session.rs`

**Changes:**
```rust
#[derive(Clone, Debug, Deserialize, Serialize, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct MultiTurnConfig {
    #[serde(default)]
    pub enabled: bool,
    
    #[serde(default = "default_max_turns")]
    pub max_user_turns: u32,
    
    #[serde(default = "default_max_turns")]
    pub max_assistant_turns: u32,
    
    #[serde(default = "default_tokens_per_turn")]
    pub max_tokens_per_turn: u32,
}

// Add to TrainingSessionSpec
pub multi_turn: Option<MultiTurnConfig>,
```

**Test gate:**
```bash
cargo test -p basilica-operator training_session
./scripts/local-training-e2e.sh test
```

**Acceptance criteria:**
- [ ] `MultiTurnConfig` struct compiles
- [ ] CRD accepts `multiTurn` field
- [ ] Existing E2E tests pass unchanged

---

### Task 1.4: Update API Request Types

**Files to modify:**
- `crates/basilica-api/src/api/routes/training.rs`

**Changes:**
```rust
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CreateSessionRequest {
    // ... existing fields ...
    
    #[serde(default = "default_training_mode")]
    pub training_mode: String,
    
    #[serde(default)]
    pub rl_config: Option<RLConfigRequest>,
    
    #[serde(default)]
    pub multi_turn: Option<MultiTurnConfigRequest>,
    
    #[serde(default)]
    pub reward_function: Option<String>,
}
```

**Test gate:**
```bash
cargo test -p basilica-api
./scripts/local-training-e2e.sh test
```

**Acceptance criteria:**
- [ ] API accepts new request fields
- [ ] Default training_mode is "sft"
- [ ] Existing E2E tests pass unchanged

---

### Task 1.5: Update CRD Builder Function

**Files to modify:**
- `crates/basilica-api/src/api/routes/training.rs`

**Changes:**
Update `build_training_session_crd()` to include new fields:

```rust
fn build_training_session_crd(...) -> serde_json::Value {
    // ... existing code ...
    
    json!({
        // ... existing fields ...
        "spec": {
            // ... existing spec fields ...
            "trainingMode": req.training_mode,
            "rlConfig": req.rl_config,
            "multiTurn": req.multi_turn,
            "rewardFunction": req.reward_function,
        }
    })
}
```

**Test gate:**
```bash
cargo test -p basilica-api
./scripts/local-training-e2e.sh test
```

**Acceptance criteria:**
- [ ] CRD created with new fields
- [ ] Existing SFT flow unchanged
- [ ] E2E tests pass

---

### Task 1.6: Apply Updated CRD to Cluster

**Files to modify:**
- `orchestrator/k8s/training/training-session-crd.yaml`

**Test gate:**
```bash
# Regenerate CRD from Rust types
cargo run -p basilica-operator --bin gen-crd > orchestrator/k8s/training/training-session-crd.yaml

# Apply to cluster
./scripts/local-training-e2e.sh cluster-up
./scripts/local-training-e2e.sh deploy
./scripts/local-training-e2e.sh test
```

**Acceptance criteria:**
- [ ] CRD YAML includes new fields
- [ ] kubectl apply succeeds
- [ ] E2E tests pass

---

## Phase 1 Checkpoint

Before proceeding to Phase 2:

```bash
# Full validation
./scripts/local-training-e2e.sh cluster-up
./scripts/local-training-e2e.sh deploy
./scripts/local-training-e2e.sh api &
sleep 10
./scripts/local-training-e2e.sh gen-key
./scripts/local-training-e2e.sh test
python examples/training_example.py

# All must pass
```

---

## Phase 2: verl Backend Implementation (Week 2)

### Task 2.1: Create verl_backend.py Skeleton

**Files to create:**
- `services/training-service/src/verl_backend.py`

**Initial implementation:**
```python
"""verl-based training backend."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import torch

@dataclass
class VerlSessionState:
    session_id: str
    base_model: str
    training_mode: str
    model: Any
    optimizer: Any
    step_count: int = 0

class VerlTrainingBackend:
    def __init__(self, model_cache_dir: str = "/models", checkpoint_dir: str = "/checkpoints"):
        self.sessions: Dict[str, VerlSessionState] = {}
        # ... initialization
    
    def create_session(self, session_id: str, base_model: str, training_mode: str = "sft", **kwargs) -> str:
        # For now, delegate to existing implementation
        raise NotImplementedError("verl backend not yet implemented")
```

**Test gate:**
```bash
# Unit test - just import check
cd services/training-service
python -c "from src.verl_backend import VerlTrainingBackend; print('OK')"
```

**Acceptance criteria:**
- [ ] File created and importable
- [ ] No syntax errors

---

### Task 2.2: Implement SFT Mode in verl Backend

**Files to modify:**
- `services/training-service/src/verl_backend.py`

**Implementation:**
Port the existing `backend.py` SFT functionality to `VerlTrainingBackend`:
- `create_session()` for SFT mode
- `forward_backward()`
- `optim_step()`
- `save_state()`
- `load_state()`
- `sample()`

**Test gate:**
```bash
# Update server.py to use verl_backend for SFT mode
# Then run E2E test
./scripts/local-training-e2e.sh test
python examples/training_example.py
```

**Acceptance criteria:**
- [ ] All existing SFT tests pass
- [ ] forward_backward works
- [ ] optim_step works
- [ ] save/load checkpoint works
- [ ] sample generation works

---

### Task 2.3: Add forward() Method to verl Backend

**Files to modify:**
- `services/training-service/src/verl_backend.py`

**Implementation:**
```python
def forward(
    self,
    session_id: str,
    input_ids: torch.Tensor,
    attention_mask: torch.Tensor,
) -> ForwardResult:
    """Forward pass without gradient computation."""
    session = self._get_session(session_id)
    model = session.model
    model.eval()
    
    with torch.no_grad():
        outputs = model(input_ids=input_ids, attention_mask=attention_mask)
        log_probs = torch.nn.functional.log_softmax(outputs.logits, dim=-1)
        token_logprobs = torch.gather(
            log_probs[:, :-1, :], dim=-1, index=input_ids[:, 1:].unsqueeze(-1)
        ).squeeze(-1)
    
    return ForwardResult(
        logprobs=token_logprobs.cpu().tolist(),
        tokens_processed=int(attention_mask.sum().item()),
    )
```

**Test gate:**
```bash
./scripts/local-training-e2e.sh test
# Check the log for "Forward pass successful"
```

**Acceptance criteria:**
- [ ] forward() returns logprobs
- [ ] E2E test "forward-only pass" section passes

---

### Task 2.4: Add compute_logprobs() Method

**Files to modify:**
- `services/training-service/src/verl_backend.py`

**Implementation:**
```python
def compute_logprobs(self, session_id: str, token_ids: List[int]) -> List[Optional[float]]:
    """Compute per-token log probabilities."""
    session = self._get_session(session_id)
    model = session.model
    model.eval()
    
    input_ids = torch.tensor([token_ids], device=self.device)
    
    with torch.no_grad():
        outputs = model(input_ids=input_ids)
        log_probs = torch.nn.functional.log_softmax(outputs.logits, dim=-1)
        
        result = [None]  # First token has no context
        for i in range(len(token_ids) - 1):
            result.append(log_probs[0, i, token_ids[i + 1]].item())
    
    return result
```

**Test gate:**
```bash
./scripts/local-training-e2e.sh test
# Check the log for "Compute logprobs successful"

python examples/training_example.py
# Check "Logprobs Demo" section passes
```

**Acceptance criteria:**
- [ ] compute_logprobs() returns correct shape
- [ ] First element is None
- [ ] E2E test passes

---

### Task 2.5: Switch server.py to verl Backend

**Files to modify:**
- `services/training-service/src/server.py`

**Changes:**
```python
# Replace
from backend import TrainingBackend

# With
from verl_backend import VerlTrainingBackend as TrainingBackend
```

**Test gate:**
```bash
# Full E2E validation
./scripts/local-training-e2e.sh deploy
./scripts/local-training-e2e.sh test
python examples/training_example.py
python examples/training_example.py --rest
```

**Acceptance criteria:**
- [ ] All existing tests pass with verl backend
- [ ] No regressions in functionality
- [ ] Example runs successfully

---

## Phase 2 Checkpoint

Before proceeding to Phase 3:

```bash
# Full validation with verl backend
./scripts/local-training-e2e.sh cluster-down
./scripts/local-training-e2e.sh cluster-up
./scripts/local-training-e2e.sh deploy
./scripts/local-training-e2e.sh api &
sleep 10
./scripts/local-training-e2e.sh gen-key
./scripts/local-training-e2e.sh test
python examples/training_example.py
python examples/training_example.py --rest

# All must pass - no regressions
```

---

## Phase 3: RL Training Support (Week 3)

### Task 3.1: Add set_reward_function() to Backend

**Files to modify:**
- `services/training-service/src/verl_backend.py`
- `services/training-service/src/server.py`

**Backend implementation:**
```python
def set_reward_function(self, session_id: str, name: str, source_code: str) -> Dict[str, Any]:
    """Register custom reward function."""
    session = self._get_session(session_id)
    
    local_ns = {"torch": torch, "re": __import__("re"), "math": __import__("math")}
    exec(source_code, local_ns)
    
    if "compute_reward" not in local_ns:
        raise ValueError("source_code must define compute_reward()")
    
    session.reward_fn = local_ns["compute_reward"]
    return {"status": "registered", "name": name}
```

**Server endpoint:**
```python
@app.post("/sessions/{session_id}/set_reward_function")
async def set_reward_function(session_id: str, request: SetRewardFunctionRequest):
    return backend.set_reward_function(session_id, request.name, request.source_code)
```

**Test gate:**
```bash
# Add to E2E test script (run_training_steps function):
log_info "Testing set_reward_function..."
RESPONSE=$(curl -s -X POST "$TRAINING_URL/sessions/$INTERNAL_SESSION/set_reward_function" \
    -H "Content-Type: application/json" \
    -d '{
        "name": "test_reward",
        "source_code": "def compute_reward(prompt, response):\n    return 1.0"
    }')
echo "$RESPONSE" | jq .

# Run test
./scripts/local-training-e2e.sh test
```

**Acceptance criteria:**
- [ ] Endpoint accepts reward function code
- [ ] Function is compiled and stored
- [ ] E2E test passes

---

### Task 3.2: Add rl_train_step() to Backend

**Files to modify:**
- `services/training-service/src/verl_backend.py`
- `services/training-service/src/server.py`

**Backend implementation:**
```python
def rl_train_step(
    self,
    session_id: str,
    prompts: List[str],
    rewards: Optional[List[float]] = None,
) -> RLTrainStepResult:
    """Execute one RL training step using GRPO/REINFORCE++/RLOO."""
    session = self._get_session(session_id)
    
    if session.training_mode == "sft":
        raise ValueError("Cannot call rl_train_step on SFT session")
    
    # Generate responses
    responses, log_probs = self._generate_responses(session, prompts)
    
    # Compute rewards if not provided
    if rewards is None:
        if session.reward_fn is None:
            raise ValueError("No rewards and no reward function")
        rewards = [session.reward_fn(p, r) for p, r in zip(prompts, responses)]
    
    rewards_tensor = torch.tensor(rewards, device=self.device)
    
    # Compute policy loss based on algorithm
    if session.training_mode == "grpo":
        advantages = rewards_tensor - rewards_tensor.mean()
        policy_loss = -(log_probs * advantages).mean()
    elif session.training_mode == "reinforce_pp":
        advantages = (rewards_tensor - rewards_tensor.mean()) / (rewards_tensor.std() + 1e-8)
        policy_loss = -(log_probs * advantages).mean()
    else:  # rloo
        n = len(rewards_tensor)
        baselines = (rewards_tensor.sum() - rewards_tensor) / (n - 1)
        advantages = rewards_tensor - baselines
        policy_loss = -(log_probs * advantages).mean()
    
    policy_loss.backward()
    session.step_count += 1
    
    return RLTrainStepResult(
        policy_loss=policy_loss.item(),
        mean_reward=rewards_tensor.mean().item(),
        kl_divergence=0.0,
        step=session.step_count,
    )
```

**Server endpoint:**
```python
@app.post("/sessions/{session_id}/rl_train_step")
async def rl_train_step(session_id: str, request: RLTrainStepRequest):
    result = backend.rl_train_step(session_id, request.prompts, request.rewards)
    return {
        "policy_loss": result.policy_loss,
        "mean_reward": result.mean_reward,
        "kl_divergence": result.kl_divergence,
        "step": result.step,
    }
```

**Test gate:**
```bash
# Add to E2E test script:
log_info "Testing RL train step..."

# First create an RL session
RESPONSE=$(curl -s -X POST "$TRAINING_URL/sessions" \
    -H "Content-Type: application/json" \
    -d '{
        "session_id": "rl-session-1",
        "base_model": "facebook/opt-125m",
        "training_mode": "grpo",
        "lora_config": {"rank": 8}
    }')
echo "$RESPONSE" | jq .

# Set reward function
curl -s -X POST "$TRAINING_URL/sessions/rl-session-1/set_reward_function" \
    -H "Content-Type: application/json" \
    -d '{
        "name": "test",
        "source_code": "def compute_reward(prompt, response):\n    return 1.0 if len(response) > 10 else 0.0"
    }'

# Run RL train step
RESPONSE=$(curl -s -X POST "$TRAINING_URL/sessions/rl-session-1/rl_train_step" \
    -H "Content-Type: application/json" \
    -d '{"prompts": ["Hello world", "The quick brown"]}')
echo "$RESPONSE" | jq .

./scripts/local-training-e2e.sh test
```

**Acceptance criteria:**
- [ ] rl_train_step generates responses
- [ ] Computes rewards using reward function
- [ ] Returns policy_loss and mean_reward
- [ ] E2E test passes

---

### Task 3.3: Add API Proxy for RL Endpoints

**Files to modify:**
- `crates/basilica-api/src/api/routes/training.rs`
- `crates/basilica-api/src/api/mod.rs`

**Implementation:**
```rust
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
    ).await;
    
    apimetrics::record_request("training.rl_train_step", "POST", start, result.is_ok());
    result
}

pub async fn set_reward_function(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Path((session_id, internal_id)): Path<(String, String)>,
    Json(body): Json<serde_json::Value>,
) -> Result<axum::response::Response> {
    // Similar proxy implementation
}
```

**Route registration:**
```rust
.route("/sessions/:session_id/internal/:internal_id/rl_train_step", post(training::rl_train_step))
.route("/sessions/:session_id/internal/:internal_id/set_reward_function", post(training::set_reward_function))
```

**Test gate:**
```bash
cargo test -p basilica-api
./scripts/local-training-e2e.sh test
```

**Acceptance criteria:**
- [ ] API compiles with new endpoints
- [ ] Proxy works for RL operations
- [ ] E2E test passes

---

### Task 3.4: Add RL Example to training_example.py

**Files to modify:**
- `examples/training_example.py`

**Implementation:**
```python
def rl_example():
    """Example using RL training (GRPO/REINFORCE++)."""
    api_key = None
    if os.path.exists("build/api-token.txt"):
        api_key = open("build/api-token.txt").read().strip()

    endpoint = os.environ.get("BASILICA_API_URL", "http://localhost:8000")
    client = ServiceClient(api_key=api_key, endpoint=endpoint)

    print("=== RL Training Demo (GRPO) ===\n")

    # Create RL training session
    rl = client.create_rl_training_client(
        base_model="facebook/opt-125m",
        algorithm="grpo",
        rank=8,
        gpu_count=0,
    )

    try:
        print(f"Session: {rl.session_id}")
        print(f"Algorithm: GRPO\n")

        # Set custom reward function
        print("Setting reward function...")
        rl.set_reward_function('''
def compute_reward(prompt: str, response: str) -> float:
    # Simple reward: longer responses are better
    return min(len(response) / 50.0, 1.0)
''')
        print("Reward function set!\n")

        # RL training loop
        prompts = [
            "The meaning of life is",
            "Once upon a time",
            "The quick brown fox",
        ]

        for step in range(3):
            result = rl.train_step(prompts).result()
            rl.optim_step().result()
            print(f"Step {step + 1}: policy_loss={result.policy_loss:.4f}, mean_reward={result.mean_reward:.4f}")

        # Save checkpoint
        checkpoint = rl.save_state("rl-checkpoint").result()
        print(f"\nCheckpoint saved: {checkpoint}")

    finally:
        rl.close()

    print("\n=== RL Training Demo Complete ===")


# Add to argparse
parser.add_argument("--rl", action="store_true", help="Run RL training example")

# Add to main
if args.rl:
    rl_example()
```

**Test gate:**
```bash
python examples/training_example.py --rl
```

**Acceptance criteria:**
- [ ] --rl flag works
- [ ] Creates GRPO session
- [ ] Sets reward function
- [ ] Runs train_step
- [ ] Saves checkpoint

---

## Phase 3 Checkpoint

Before proceeding to Phase 4:

```bash
# Full validation with RL support
./scripts/local-training-e2e.sh test
python examples/training_example.py
python examples/training_example.py --rl
python examples/training_example.py --rest

# All must pass
```

---

## Phase 4: Multi-turn Support (Week 4)

### Task 4.1: Add Multi-turn State to Backend

**Files to modify:**
- `services/training-service/src/verl_backend.py`

**Implementation:**
```python
@dataclass
class VerlSessionState:
    # ... existing fields ...
    conversation_history: List[Dict] = field(default_factory=list)
    multi_turn_config: Optional[MultiTurnConfiguration] = None
```

**Test gate:**
```bash
python -c "from src.verl_backend import VerlSessionState; print('OK')"
```

---

### Task 4.2: Add multi_turn_reset() Method

**Files to modify:**
- `services/training-service/src/verl_backend.py`
- `services/training-service/src/server.py`

**Implementation:**
```python
def multi_turn_reset(self, session_id: str) -> Dict[str, Any]:
    """Reset multi-turn conversation."""
    session = self._get_session(session_id)
    session.conversation_history = []
    return {"status": "reset", "turn_number": 0}
```

**Server endpoint:**
```python
@app.post("/sessions/{session_id}/multi_turn/reset")
async def multi_turn_reset(session_id: str):
    return backend.multi_turn_reset(session_id)
```

**Test gate:**
```bash
# Add to E2E test:
log_info "Testing multi_turn_reset..."
RESPONSE=$(curl -s -X POST "$TRAINING_URL/sessions/$INTERNAL_SESSION/multi_turn/reset")
echo "$RESPONSE" | jq .

./scripts/local-training-e2e.sh test
```

**Acceptance criteria:**
- [ ] Endpoint returns {"status": "reset"}
- [ ] Conversation history cleared

---

### Task 4.3: Add multi_turn_step() Method

**Files to modify:**
- `services/training-service/src/verl_backend.py`
- `services/training-service/src/server.py`

**Implementation:**
```python
def multi_turn_step(self, session_id: str, user_input: str) -> MultiTurnStepResult:
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
    session.conversation_history.append({"role": "user", "content": user_input})
    session.conversation_history.append({"role": "assistant", "content": response})
    
    turn_number = len(session.conversation_history) // 2
    done = turn_number >= config.max_user_turns
    
    # Compute reward if function set
    reward = 0.0
    if session.reward_fn:
        reward = session.reward_fn(conversation=session.conversation_history, response=response)
    
    return MultiTurnStepResult(
        response=response,
        reward=reward,
        done=done,
        turn_number=turn_number,
        conversation_history=session.conversation_history.copy(),
    )
```

**Server endpoint:**
```python
@app.post("/sessions/{session_id}/multi_turn/step")
async def multi_turn_step(session_id: str, request: MultiTurnStepRequest):
    result = backend.multi_turn_step(session_id, request.user_input)
    return {
        "response": result.response,
        "reward": result.reward,
        "done": result.done,
        "turn_number": result.turn_number,
        "conversation_history": result.conversation_history,
    }
```

**Test gate:**
```bash
# Add to E2E test:
log_info "Testing multi_turn_step..."
curl -s -X POST "$TRAINING_URL/sessions/$INTERNAL_SESSION/multi_turn/reset"

RESPONSE=$(curl -s -X POST "$TRAINING_URL/sessions/$INTERNAL_SESSION/multi_turn/step" \
    -H "Content-Type: application/json" \
    -d '{"user_input": "Hello, how are you?"}')
echo "$RESPONSE" | jq .

RESPONSE=$(curl -s -X POST "$TRAINING_URL/sessions/$INTERNAL_SESSION/multi_turn/step" \
    -H "Content-Type: application/json" \
    -d '{"user_input": "Tell me a joke."}')
echo "$RESPONSE" | jq .

./scripts/local-training-e2e.sh test
```

**Acceptance criteria:**
- [ ] Returns response and conversation history
- [ ] turn_number increments
- [ ] done=true when max turns reached

---

### Task 4.4: Add API Proxy for Multi-turn Endpoints

**Files to modify:**
- `crates/basilica-api/src/api/routes/training.rs`
- `crates/basilica-api/src/api/mod.rs`

**Implementation:**
```rust
pub async fn multi_turn_reset(...) -> Result<axum::response::Response> { ... }
pub async fn multi_turn_step(...) -> Result<axum::response::Response> { ... }

// Routes
.route("/sessions/:session_id/internal/:internal_id/multi_turn/reset", post(training::multi_turn_reset))
.route("/sessions/:session_id/internal/:internal_id/multi_turn/step", post(training::multi_turn_step))
```

**Test gate:**
```bash
cargo test -p basilica-api
./scripts/local-training-e2e.sh test
```

---

### Task 4.5: Add Multi-turn Example

**Files to modify:**
- `examples/training_example.py`

**Implementation:**
```python
def multi_turn_example():
    """Example using multi-turn training."""
    api_key = None
    if os.path.exists("build/api-token.txt"):
        api_key = open("build/api-token.txt").read().strip()

    endpoint = os.environ.get("BASILICA_API_URL", "http://localhost:8000")
    client = ServiceClient(api_key=api_key, endpoint=endpoint)

    print("=== Multi-turn Training Demo ===\n")

    # Create session with multi-turn config
    rl = client.create_rl_training_client(
        base_model="facebook/opt-125m",
        algorithm="grpo",
        rank=8,
        multi_turn_config=MultiTurnConfig(
            max_user_turns=5,
            max_tokens_per_turn=50,
        ),
        gpu_count=0,
    )

    try:
        # Reset conversation
        rl.multi_turn_reset()
        print("Conversation reset.\n")

        # Multi-turn interaction
        user_messages = [
            "Hello! What's your name?",
            "Nice to meet you. What can you do?",
            "Tell me something interesting.",
        ]

        for msg in user_messages:
            print(f"User: {msg}")
            result = rl.multi_turn_step(msg).result()
            print(f"Assistant: {result.response}")
            print(f"  (turn {result.turn_number}, reward={result.reward:.2f}, done={result.done})\n")

            if result.done:
                break

    finally:
        rl.close()

    print("=== Multi-turn Demo Complete ===")


# Add to argparse
parser.add_argument("--multi-turn", action="store_true", help="Run multi-turn example")

# Add to main
if args.multi_turn:
    multi_turn_example()
```

**Test gate:**
```bash
python examples/training_example.py --multi-turn
```

**Acceptance criteria:**
- [ ] --multi-turn flag works
- [ ] Conversation progresses
- [ ] History maintained between turns

---

## Phase 4 Checkpoint

Before proceeding to Phase 5:

```bash
# Full validation with multi-turn support
./scripts/local-training-e2e.sh test
python examples/training_example.py
python examples/training_example.py --rl
python examples/training_example.py --multi-turn
python examples/training_example.py --rest

# All must pass
```

---

## Phase 5: SDK Updates (Week 4 continued)

### Task 5.1: Add RLTrainingClient to SDK

**Files to create:**
- `crates/basilica-sdk-python/python/basilica/training/rl_client.py`

**Implementation:** (See full implementation in gap analysis doc)

**Test gate:**
```bash
cd crates/basilica-sdk-python
python -c "from basilica.training.rl_client import RLTrainingClient; print('OK')"
```

---

### Task 5.2: Add create_rl_training_client() to ServiceClient

**Files to modify:**
- `crates/basilica-sdk-python/python/basilica/training/service_client.py`

**Test gate:**
```bash
python examples/training_example.py --rl
```

---

### Task 5.3: Add LifecycleManager to SDK

**Files to create:**
- `crates/basilica-sdk-python/python/basilica/training/lifecycle.py`

**Implementation:**
```python
import atexit
import signal

class LifecycleManager:
    _instance = None
    
    def __init__(self):
        self._sessions = []
        atexit.register(self._cleanup)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def register_session(self, client, session_id: str):
        self._sessions.append((client, session_id))
    
    def _cleanup(self):
        for client, session_id in self._sessions:
            try:
                client.delete_session(session_id)
            except:
                pass
    
    def _signal_handler(self, signum, frame):
        self._cleanup()
        raise SystemExit(128 + signum)

def get_lifecycle_manager():
    if LifecycleManager._instance is None:
        LifecycleManager._instance = LifecycleManager()
    return LifecycleManager._instance
```

**Test gate:**
```bash
python -c "from basilica.training.lifecycle import get_lifecycle_manager; print('OK')"
```

---

### Task 5.4: Update SDK __init__.py Exports

**Files to modify:**
- `crates/basilica-sdk-python/python/basilica/training/__init__.py`

**Implementation:**
```python
from .service_client import ServiceClient
from .training_client import TrainingClient
from .rl_client import RLTrainingClient
from .sampling_client import SamplingClient
from .rest_client import RestClient
from .types import (
    Datum, ModelInput, SamplingParams, 
    RLConfig, MultiTurnConfig, TrainingMode,
    ForwardResult, ForwardBackwardResult, RLTrainStepResult, MultiTurnStepResult,
)
from .lifecycle import get_lifecycle_manager

__all__ = [
    "ServiceClient",
    "TrainingClient", 
    "RLTrainingClient",
    "SamplingClient",
    "RestClient",
    "Datum",
    "ModelInput",
    "SamplingParams",
    "RLConfig",
    "MultiTurnConfig",
    "TrainingMode",
    "ForwardResult",
    "ForwardBackwardResult",
    "RLTrainStepResult",
    "MultiTurnStepResult",
    "get_lifecycle_manager",
]
```

**Test gate:**
```bash
python -c "from basilica.training import RLTrainingClient, MultiTurnConfig, TrainingMode; print('OK')"
```

---

## Final Validation

### Complete Test Suite

```bash
#!/bin/bash
# scripts/test-verl-integration.sh

set -e

echo "=== verl Integration Test Suite ==="

# 1. Start cluster if not running
if ! k3d cluster list | grep -q "basilica-training-local"; then
    ./scripts/local-training-e2e.sh cluster-up
fi

# 2. Deploy
./scripts/local-training-e2e.sh deploy

# 3. Start API in background
./scripts/local-training-e2e.sh api &
API_PID=$!
sleep 15

# 4. Generate API key
./scripts/local-training-e2e.sh gen-key

# 5. Run E2E tests
echo ""
echo "=== Running E2E Tests ==="
./scripts/local-training-e2e.sh test

# 6. Run Python examples
echo ""
echo "=== Running Python Examples ==="

echo "--- Main Example ---"
python examples/training_example.py

echo "--- RL Example ---"
python examples/training_example.py --rl

echo "--- Multi-turn Example ---"
python examples/training_example.py --multi-turn

echo "--- RestClient Example ---"
python examples/training_example.py --rest

echo "--- Async Example ---"
python examples/training_example.py --async

# 7. Cleanup
kill $API_PID 2>/dev/null || true

echo ""
echo "=== All Tests Passed! ==="
```

### Acceptance Criteria Summary

| Phase | Task | Test Gate |
|-------|------|-----------|
| 1.1 | TrainingMode enum | `cargo test`, E2E |
| 1.2 | RLConfig struct | `cargo test`, E2E |
| 1.3 | MultiTurnConfig struct | `cargo test`, E2E |
| 1.4 | API request types | `cargo test`, E2E |
| 1.5 | CRD builder | `cargo test`, E2E |
| 1.6 | CRD YAML | kubectl apply, E2E |
| 2.1 | verl_backend skeleton | import check |
| 2.2 | SFT in verl | E2E, `training_example.py` |
| 2.3 | forward() | E2E forward test |
| 2.4 | compute_logprobs() | E2E, example |
| 2.5 | Switch to verl | Full E2E, all examples |
| 3.1 | set_reward_function | E2E curl test |
| 3.2 | rl_train_step | E2E curl test |
| 3.3 | API proxy for RL | `cargo test`, E2E |
| 3.4 | RL example | `--rl` example |
| 4.1 | Multi-turn state | import check |
| 4.2 | multi_turn_reset | E2E curl test |
| 4.3 | multi_turn_step | E2E curl test |
| 4.4 | API proxy for multi-turn | `cargo test`, E2E |
| 4.5 | Multi-turn example | `--multi-turn` example |
| 5.1 | RLTrainingClient | import check |
| 5.2 | create_rl_training_client | `--rl` example |
| 5.3 | LifecycleManager | import check |
| 5.4 | SDK exports | import check |

---

## Files Modified/Created Summary

### New Files
```
services/training-service/src/verl_backend.py
crates/basilica-sdk-python/python/basilica/training/rl_client.py
crates/basilica-sdk-python/python/basilica/training/lifecycle.py
scripts/test-verl-integration.sh
```

### Modified Files
```
crates/basilica-operator/src/crd/training_session.rs
crates/basilica-api/src/api/routes/training.rs
crates/basilica-api/src/api/mod.rs
services/training-service/src/server.py
examples/training_example.py
scripts/local-training-e2e.sh
crates/basilica-sdk-python/python/basilica/training/__init__.py
crates/basilica-sdk-python/python/basilica/training/service_client.py
crates/basilica-sdk-python/python/basilica/training/types.py
orchestrator/k8s/training/training-session-crd.yaml
```

### Deleted Files
```
services/training-service/src/backend.py  # Replaced by verl_backend.py
```
