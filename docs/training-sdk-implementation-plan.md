# Training SDK Implementation Plan

This document provides a detailed implementation plan for the Basilica Training SDK.

## Overview

**Current State**: Basic SDK with `Client` → `TrainingSession` pattern, core training ops working
**Target State**: SDK with `ServiceClient` → `TrainingClient`/`SamplingClient`/`RestClient` pattern

**Files to Modify**:
- `crates/basilica-sdk-python/python/basilica/training/__init__.py` - SDK
- `services/training-service/src/server.py` - REST API
- `services/training-service/src/backend.py` - Training backend
- `crates/basilica-api/src/api/routes/training.rs` - Basilica API (for RestClient endpoints)
- `examples/training_example.py` - Example code

---

## Basilica API (Rust) - Current State Analysis

### Current Implementation

**File**: `crates/basilica-api/src/api/routes/training.rs` (869 lines)

#### Existing Endpoints

| Endpoint | Handler | Purpose |
|----------|---------|---------|
| `POST /sessions` | `create_session()` | Create TrainingSession CRD |
| `GET /sessions` | `list_sessions()` | List user's sessions |
| `GET /sessions/{id}` | `get_session()` | Get session status from CRD |
| `DELETE /sessions/{id}` | `delete_session()` | Delete TrainingSession CRD |
| `POST /sessions/{id}/internal` | `create_internal_session()` | Create session in training pod |
| `GET /sessions/{id}/internal/{iid}` | `get_internal_session()` | Get internal session status |
| `POST /sessions/{id}/internal/{iid}/forward_backward` | `forward_backward()` | Proxy to training pod |
| `POST /sessions/{id}/internal/{iid}/optim_step` | `optim_step()` | Proxy to training pod |
| `POST /sessions/{id}/internal/{iid}/sample` | `sample()` | Proxy to training pod |
| `POST /sessions/{id}/internal/{iid}/save` | `save_checkpoint()` | Proxy to training pod |
| `POST /sessions/{id}/internal/{iid}/load` | `load_checkpoint()` | Proxy to training pod |

#### Existing Request Types

```rust
// Current fields
CreateSessionRequest {
    base_model: String,
    checkpoint_storage: CheckpointStorageRequest,
    lora_config: Option<LoraConfigRequest>,      // Has: rank, alpha, dropout, target_modules
    optimizer_config: Option<OptimizerConfigRequest>,
    gpu_resources: Option<GpuResourcesRequest>,
    seed: Option<i64>,
    ttl_seconds: u64,
}
```

#### Architecture Notes

- **No Database**: Training state lives entirely in K8s CRDs (TrainingSession)
- **Proxy Pattern**: API proxies training ops to K8s service via `proxy_to_training_service()`
- **User Isolation**: Sessions in namespace `u-{user_id}`
- **CRD Lifecycle**: Operator watches CRD, creates Pod/Service/HTTPRoute

---

## Basilica API - Required Changes

### Phase 1: Update CreateSessionRequest

**File**: `crates/basilica-api/src/api/routes/training.rs`

#### Add New Fields to LoraConfigRequest

```rust
// BEFORE
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LoraConfigRequest {
    #[serde(default = "default_rank")]
    pub rank: u32,
    #[serde(default = "default_alpha")]
    pub alpha: u32,
    #[serde(default = "default_dropout")]
    pub dropout: f32,
    #[serde(default)]
    pub target_modules: Option<Vec<String>>,
}

// AFTER - Add module toggle flags
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LoraConfigRequest {
    #[serde(default = "default_rank")]
    pub rank: u32,
    #[serde(default = "default_alpha")]
    pub alpha: u32,
    #[serde(default = "default_dropout")]
    pub dropout: f32,
    #[serde(default)]
    pub target_modules: Option<Vec<String>>,

    // NEW: Module toggles for LoRA target selection
    #[serde(default = "default_true")]
    pub train_mlp: bool,
    #[serde(default = "default_true")]
    pub train_attn: bool,
    #[serde(default = "default_true")]
    pub train_unembed: bool,
}

fn default_true() -> bool { true }
```

#### Update build_training_session_crd()

```rust
fn build_training_session_crd(
    session_id: &str,
    user_id: &str,
    req: &CreateSessionRequest,
) -> serde_json::Value {
    let lora = req.lora_config.clone().unwrap_or_default();

    // NEW: Build target_modules from flags if not explicitly provided
    let target_modules = lora.target_modules.unwrap_or_else(|| {
        let mut modules = Vec::new();
        if lora.train_attn {
            modules.extend(["q_proj", "k_proj", "v_proj", "o_proj"].map(String::from));
        }
        if lora.train_mlp {
            modules.extend(["gate_proj", "up_proj", "down_proj"].map(String::from));
        }
        if lora.train_unembed {
            modules.push("lm_head".to_string());
        }
        modules
    });

    // ... rest of CRD building with target_modules
}
```

#### Add user_metadata to CreateSessionRequest

```rust
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

    // NEW: User metadata for tracking
    #[serde(default)]
    pub user_metadata: Option<std::collections::HashMap<String, String>>,
}
```

---

### Phase 2: Add New Proxy Endpoints

**File**: `crates/basilica-api/src/api/routes/training.rs`

#### Add /forward Endpoint (inference-only pass)

```rust
/// Forward pass without gradients (proxy to training pod).
pub async fn forward(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Path((session_id, internal_id)): Path<(String, String)>,
    Json(req): Json<serde_json::Value>,
) -> Result<Json<serde_json::Value>> {
    let start = Instant::now();

    let k8s_client = state.k8s.as_ref().ok_or(ApiError::ServiceUnavailable)?;
    let namespace = user_namespace(&auth.user_id);

    let response = proxy_to_training_service(
        k8s_client.kube_client(),
        &namespace,
        &session_id,
        &format!("/sessions/{}/forward", internal_id),
        http::Method::POST,
        Some(req),
    )
    .await?;

    apimetrics::record_request("training.forward", "POST", start, true);
    Ok(Json(response))
}
```

#### Add /compute_logprobs Endpoint

```rust
/// Compute log probabilities for token sequence.
pub async fn compute_logprobs(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Path((session_id, internal_id)): Path<(String, String)>,
    Json(req): Json<serde_json::Value>,
) -> Result<Json<serde_json::Value>> {
    let start = Instant::now();

    let k8s_client = state.k8s.as_ref().ok_or(ApiError::ServiceUnavailable)?;
    let namespace = user_namespace(&auth.user_id);

    let response = proxy_to_training_service(
        k8s_client.kube_client(),
        &namespace,
        &session_id,
        &format!("/sessions/{}/compute_logprobs", internal_id),
        http::Method::POST,
        Some(req),
    )
    .await?;

    apimetrics::record_request("training.compute_logprobs", "POST", start, true);
    Ok(Json(response))
}
```

#### Add /save_for_sampler Endpoint

```rust
/// Save weights formatted for sampling.
pub async fn save_for_sampler(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Path((session_id, internal_id)): Path<(String, String)>,
    Json(req): Json<serde_json::Value>,
) -> Result<Json<serde_json::Value>> {
    let start = Instant::now();

    let k8s_client = state.k8s.as_ref().ok_or(ApiError::ServiceUnavailable)?;
    let namespace = user_namespace(&auth.user_id);

    let response = proxy_to_training_service(
        k8s_client.kube_client(),
        &namespace,
        &session_id,
        &format!("/sessions/{}/save_for_sampler", internal_id),
        http::Method::POST,
        Some(req),
    )
    .await?;

    apimetrics::record_request("training.save_for_sampler", "POST", start, true);
    Ok(Json(response))
}
```

#### Update Route Registration

**File**: `crates/basilica-api/src/api/mod.rs`

```rust
// Add new routes to the training router
.route(
    "/sessions/:session_id/internal/:internal_id/forward",
    post(training::forward),
)
.route(
    "/sessions/:session_id/internal/:internal_id/compute_logprobs",
    post(training::compute_logprobs),
)
.route(
    "/sessions/:session_id/internal/:internal_id/save_for_sampler",
    post(training::save_for_sampler),
)
```

---

### Phase 3: Add RestClient Endpoints (Checkpoint Management)

**File**: `crates/basilica-api/src/api/routes/training.rs`

#### Add Request/Response Types

```rust
// === RestClient Types ===

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct TrainingRun {
    pub run_id: String,
    pub session_id: String,
    pub base_model: String,
    pub lora_rank: u32,
    pub created_at: String,
    pub checkpoints: Vec<CheckpointSummary>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CheckpointSummary {
    pub checkpoint_id: String,
    pub name: String,
    pub step: u64,
    pub created_at: String,
    pub path: String,
    pub is_public: bool,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ListTrainingRunsResponse {
    pub runs: Vec<TrainingRun>,
    pub total: u64,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ListCheckpointsResponse {
    pub checkpoints: Vec<CheckpointSummary>,
    pub total: u64,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ListParams {
    #[serde(default = "default_limit")]
    pub limit: u32,
    #[serde(default)]
    pub offset: u32,
    #[serde(default)]
    pub run_id: Option<String>,
}

fn default_limit() -> u32 { 20 }

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DownloadUrlResponse {
    pub url: String,
    pub expires_at: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct PublishCheckpointRequest {
    pub path: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PublishCheckpointResponse {
    pub public_url: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CapabilitiesResponse {
    pub models: Vec<String>,
    pub max_batch_tokens: u32,
    pub max_sequence_length: u32,
}
```

#### Add /capabilities Endpoint

```rust
/// Get server capabilities (available models, limits).
pub async fn get_capabilities(
    State(state): State<AppState>,
) -> Result<Json<CapabilitiesResponse>> {
    // TODO: Load from config or discover dynamically
    let models = vec![
        "meta-llama/Llama-3.1-8B-Instruct".to_string(),
        "meta-llama/Llama-3.1-70B-Instruct".to_string(),
        "Qwen/Qwen3-8B".to_string(),
        "Qwen/Qwen3-32B".to_string(),
        "facebook/opt-125m".to_string(),  // For testing
    ];

    Ok(Json(CapabilitiesResponse {
        models,
        max_batch_tokens: 4096,
        max_sequence_length: 8192,
    }))
}
```

#### Add /training_runs Endpoints

```rust
/// List training runs for the authenticated user.
///
/// Training runs are derived from TrainingSession CRDs - each session
/// represents one training run.
pub async fn list_training_runs(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Query(params): Query<ListParams>,
) -> Result<Json<ListTrainingRunsResponse>> {
    let start = Instant::now();

    let k8s_client = state.k8s.as_ref().ok_or(ApiError::ServiceUnavailable)?;
    let kube_client = k8s_client.kube_client();
    let namespace = user_namespace(&auth.user_id);

    let api = get_training_session_api(&kube_client, &namespace);

    let sessions = api
        .list(&kube::api::ListParams::default())
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("Failed to list sessions: {}", e),
        })?;

    let runs: Vec<TrainingRun> = sessions
        .items
        .iter()
        .skip(params.offset as usize)
        .take(params.limit as usize)
        .filter_map(|s| {
            let spec = s.data.get("spec")?;
            let status = s.data.get("status").cloned().unwrap_or_else(|| json!({}));
            let name = s.metadata.name.clone()?;

            Some(TrainingRun {
                run_id: name.clone(),
                session_id: name,
                base_model: spec.get("baseModel")?.as_str()?.to_string(),
                lora_rank: spec.get("loraConfig")?.get("rank")?.as_u64()? as u32,
                created_at: s.metadata.creation_timestamp.as_ref()?.0.to_rfc3339(),
                checkpoints: extract_checkpoints(&status),
            })
        })
        .collect();

    let total = sessions.items.len() as u64;

    apimetrics::record_request("training.list_runs", "GET", start, true);

    Ok(Json(ListTrainingRunsResponse { runs, total }))
}

/// Get a specific training run.
pub async fn get_training_run(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Path(run_id): Path<String>,
) -> Result<Json<TrainingRun>> {
    let start = Instant::now();

    let k8s_client = state.k8s.as_ref().ok_or(ApiError::ServiceUnavailable)?;
    let kube_client = k8s_client.kube_client();
    let namespace = user_namespace(&auth.user_id);

    let api = get_training_session_api(&kube_client, &namespace);

    let session = api.get(&run_id).await.map_err(|e| {
        if e.to_string().contains("404") {
            ApiError::NotFound {
                message: format!("Training run {} not found", run_id),
            }
        } else {
            ApiError::Internal {
                message: format!("Failed to get training run: {}", e),
            }
        }
    })?;

    let spec = session.data.get("spec").ok_or(ApiError::Internal {
        message: "Invalid session: missing spec".to_string(),
    })?;
    let status = session.data.get("status").cloned().unwrap_or_else(|| json!({}));

    let run = TrainingRun {
        run_id: run_id.clone(),
        session_id: run_id,
        base_model: spec.get("baseModel")
            .and_then(|v| v.as_str())
            .unwrap_or("unknown")
            .to_string(),
        lora_rank: spec.get("loraConfig")
            .and_then(|v| v.get("rank"))
            .and_then(|v| v.as_u64())
            .unwrap_or(32) as u32,
        created_at: session.metadata.creation_timestamp
            .as_ref()
            .map(|t| t.0.to_rfc3339())
            .unwrap_or_default(),
        checkpoints: extract_checkpoints(&status),
    };

    apimetrics::record_request("training.get_run", "GET", start, true);

    Ok(Json(run))
}

fn extract_checkpoints(status: &serde_json::Value) -> Vec<CheckpointSummary> {
    // Extract checkpoint info from CRD status
    // For now, just return last checkpoint if exists
    let mut checkpoints = Vec::new();

    if let (Some(name), Some(path)) = (
        status.get("lastCheckpoint").and_then(|v| v.as_str()),
        status.get("lastCheckpointPath").and_then(|v| v.as_str()),
    ) {
        checkpoints.push(CheckpointSummary {
            checkpoint_id: format!("cp-{}", name),
            name: name.to_string(),
            step: status.get("stepsCompleted").and_then(|v| v.as_u64()).unwrap_or(0),
            created_at: chrono::Utc::now().to_rfc3339(),  // TODO: Store actual time
            path: path.to_string(),
            is_public: false,
        });
    }

    checkpoints
}
```

#### Add /checkpoints Endpoints

```rust
/// List checkpoints.
pub async fn list_checkpoints(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Query(params): Query<ListParams>,
) -> Result<Json<ListCheckpointsResponse>> {
    let start = Instant::now();

    // If run_id specified, get checkpoints for that run
    // Otherwise, list all user checkpoints across all runs
    let k8s_client = state.k8s.as_ref().ok_or(ApiError::ServiceUnavailable)?;
    let kube_client = k8s_client.kube_client();
    let namespace = user_namespace(&auth.user_id);

    let api = get_training_session_api(&kube_client, &namespace);

    let sessions = if let Some(run_id) = &params.run_id {
        vec![api.get(run_id).await.map_err(|e| ApiError::Internal {
            message: format!("Failed to get session: {}", e),
        })?]
    } else {
        api.list(&kube::api::ListParams::default())
            .await
            .map_err(|e| ApiError::Internal {
                message: format!("Failed to list sessions: {}", e),
            })?
            .items
    };

    let checkpoints: Vec<CheckpointSummary> = sessions
        .iter()
        .flat_map(|s| {
            let status = s.data.get("status").cloned().unwrap_or_else(|| json!({}));
            extract_checkpoints(&status)
        })
        .skip(params.offset as usize)
        .take(params.limit as usize)
        .collect();

    let total = checkpoints.len() as u64;

    apimetrics::record_request("training.list_checkpoints", "GET", start, true);

    Ok(Json(ListCheckpointsResponse { checkpoints, total }))
}

/// Get download URL for a checkpoint.
pub async fn get_checkpoint_download_url(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Path(checkpoint_id): Path<String>,
) -> Result<Json<DownloadUrlResponse>> {
    let start = Instant::now();

    // TODO: Look up checkpoint path from storage metadata
    // For now, assume checkpoint_id contains the path info

    let storage = state.storage.as_ref().ok_or(ApiError::ServiceUnavailable)?;

    // Generate presigned URL (1 hour expiry)
    let url = storage
        .generate_presigned_url(&checkpoint_id, std::time::Duration::from_secs(3600))
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("Failed to generate download URL: {}", e),
        })?;

    let expires_at = (chrono::Utc::now() + chrono::Duration::hours(1)).to_rfc3339();

    apimetrics::record_request("training.checkpoint_url", "GET", start, true);

    Ok(Json(DownloadUrlResponse { url, expires_at }))
}

/// Delete a checkpoint.
pub async fn delete_checkpoint(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Path(checkpoint_id): Path<String>,
) -> Result<StatusCode> {
    let start = Instant::now();

    let storage = state.storage.as_ref().ok_or(ApiError::ServiceUnavailable)?;

    // TODO: Verify ownership before deleting
    storage
        .delete(&checkpoint_id)
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("Failed to delete checkpoint: {}", e),
        })?;

    apimetrics::record_request("training.delete_checkpoint", "DELETE", start, true);

    Ok(StatusCode::NO_CONTENT)
}

/// Publish a checkpoint (make publicly accessible).
pub async fn publish_checkpoint(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Json(req): Json<PublishCheckpointRequest>,
) -> Result<Json<PublishCheckpointResponse>> {
    let start = Instant::now();

    let storage = state.storage.as_ref().ok_or(ApiError::ServiceUnavailable)?;

    // TODO: Verify ownership and update visibility
    let public_url = storage
        .make_public(&req.path)
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("Failed to publish checkpoint: {}", e),
        })?;

    apimetrics::record_request("training.publish_checkpoint", "POST", start, true);

    Ok(Json(PublishCheckpointResponse { public_url }))
}

/// Unpublish a checkpoint (revert to private).
pub async fn unpublish_checkpoint(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Json(req): Json<PublishCheckpointRequest>,
) -> Result<StatusCode> {
    let start = Instant::now();

    let storage = state.storage.as_ref().ok_or(ApiError::ServiceUnavailable)?;

    storage
        .make_private(&req.path)
        .await
        .map_err(|e| ApiError::Internal {
            message: format!("Failed to unpublish checkpoint: {}", e),
        })?;

    apimetrics::record_request("training.unpublish_checkpoint", "POST", start, true);

    Ok(StatusCode::NO_CONTENT)
}
```

#### Update Route Registration for RestClient

**File**: `crates/basilica-api/src/api/mod.rs`

```rust
// RestClient routes (no auth required for capabilities)
.route("/capabilities", get(training::get_capabilities))

// RestClient routes (auth required)
.route("/training_runs", get(training::list_training_runs))
.route("/training_runs/:run_id", get(training::get_training_run))
.route("/checkpoints", get(training::list_checkpoints))
.route("/checkpoints/:checkpoint_id/download_url", get(training::get_checkpoint_download_url))
.route("/checkpoints/:checkpoint_id", delete(training::delete_checkpoint))
.route("/checkpoints/publish", post(training::publish_checkpoint))
.route("/checkpoints/unpublish", post(training::unpublish_checkpoint))
```

---

### Phase 4: Add /sessions/from_state Endpoint

**File**: `crates/basilica-api/src/api/routes/training.rs`

```rust
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CreateFromStateRequest {
    pub path: String,
    #[serde(default)]
    pub load_optimizer: bool,
    #[serde(default)]
    pub user_metadata: Option<std::collections::HashMap<String, String>>,
}

/// Create training session from checkpoint.
///
/// Loads weights (and optionally optimizer state) from a saved checkpoint
/// to resume training.
pub async fn create_session_from_state(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Json(req): Json<CreateFromStateRequest>,
) -> Result<Json<CreateSessionResponse>> {
    let start = Instant::now();

    info!(
        user_id = %auth.user_id,
        path = %req.path,
        load_optimizer = %req.load_optimizer,
        "Creating training session from checkpoint"
    );

    // TODO:
    // 1. Load checkpoint metadata to get base_model, lora_config, etc.
    // 2. Create session with those params
    // 3. After session is ready, call load_state on the training pod

    // For now, return NotImplemented
    Err(ApiError::NotImplemented {
        message: "create_session_from_state not yet implemented".to_string(),
    })
}
```

---

### Summary: All Basilica API Changes

#### New Endpoints to Add

| Endpoint | Method | Handler | Phase |
|----------|--------|---------|-------|
| `/capabilities` | GET | `get_capabilities()` | 3 |
| `/sessions/from_state` | POST | `create_session_from_state()` | 4 |
| `/sessions/{id}/internal/{iid}/forward` | POST | `forward()` | 2 |
| `/sessions/{id}/internal/{iid}/compute_logprobs` | POST | `compute_logprobs()` | 2 |
| `/sessions/{id}/internal/{iid}/save_for_sampler` | POST | `save_for_sampler()` | 2 |
| `/training_runs` | GET | `list_training_runs()` | 3 |
| `/training_runs/{run_id}` | GET | `get_training_run()` | 3 |
| `/checkpoints` | GET | `list_checkpoints()` | 3 |
| `/checkpoints/{id}/download_url` | GET | `get_checkpoint_download_url()` | 3 |
| `/checkpoints/{id}` | DELETE | `delete_checkpoint()` | 3 |
| `/checkpoints/publish` | POST | `publish_checkpoint()` | 3 |
| `/checkpoints/unpublish` | POST | `unpublish_checkpoint()` | 3 |

#### Modified Types

| Type | Change | Phase |
|------|--------|-------|
| `LoraConfigRequest` | Add `train_mlp`, `train_attn`, `train_unembed` | 1 |
| `CreateSessionRequest` | Add `user_metadata` | 1 |
| `build_training_session_crd()` | Build target_modules from flags | 1 |

#### New Types

| Type | Purpose | Phase |
|------|---------|-------|
| `TrainingRun` | Training run metadata | 3 |
| `CheckpointSummary` | Checkpoint metadata | 3 |
| `ListTrainingRunsResponse` | Paginated runs | 3 |
| `ListCheckpointsResponse` | Paginated checkpoints | 3 |
| `DownloadUrlResponse` | Presigned URL | 3 |
| `PublishCheckpointRequest` | Publish request | 3 |
| `PublishCheckpointResponse` | Public URL | 3 |
| `CapabilitiesResponse` | Server capabilities | 3 |
| `CreateFromStateRequest` | Resume from checkpoint | 4 |

---

## Phase 1: SDK Structure Alignment

**Goal**: Restructure SDK classes to match target pattern
**Effort**: 1-2 days

### 1.1 Create New SDK File Structure

```
crates/basilica-sdk-python/python/basilica/training/
├── __init__.py              # Re-exports all public symbols
├── service_client.py        # ServiceClient (entry point)
├── training_client.py       # TrainingClient
├── sampling_client.py       # SamplingClient
├── rest_client.py           # RestClient
├── types.py                 # Datum, SamplingParams, ModelInput, APIFuture
└── exceptions.py            # TrainingError, SessionNotFoundError, etc.
```

### 1.2 Implement Types Module

**File**: `types.py`

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
from concurrent.futures import Future
import asyncio

@dataclass
class Datum:
    """Training example."""
    input_ids: List[int]
    labels: Optional[List[int]] = None
    loss_weights: Optional[List[float]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {"input_ids": self.input_ids}
        if self.labels is not None:
            d["labels"] = self.labels
        if self.loss_weights is not None:
            d["loss_weights"] = self.loss_weights
        return d


@dataclass
class ModelInput:
    """Input tokens for sampling."""
    token_ids: List[int]

    @classmethod
    def from_ints(cls, token_ids: List[int]) -> "ModelInput":
        return cls(token_ids=token_ids)

    @classmethod
    def from_string(cls, text: str, tokenizer) -> "ModelInput":
        return cls(token_ids=tokenizer.encode(text))


@dataclass
class SamplingParams:
    """Sampling parameters."""
    max_tokens: int = 256
    temperature: float = 1.0
    top_p: float = 1.0
    top_k: int = 0
    stop_sequences: Optional[List[str]] = None
    include_logprobs: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "stop_sequences": self.stop_sequences or [],
            "include_logprobs": self.include_logprobs,
        }


@dataclass
class SampleResponse:
    """Generated sample."""
    text: str
    token_ids: List[int]
    logprobs: Optional[List[float]] = None
    finish_reason: str = "stop"


@dataclass
class ForwardBackwardResult:
    """Result of forward-backward pass."""
    loss: float
    logprobs: List[List[float]]
    tokens_processed: int


@dataclass
class ForwardResult:
    """Result of forward-only pass."""
    logprobs: List[List[float]]
    tokens_processed: int


@dataclass
class GetServerCapabilitiesResponse:
    """Server capabilities."""
    models: List[str]
    max_batch_tokens: int
    max_sequence_length: int


class APIFuture:
    """Async handle for operations.

    Supports both sync and async patterns:
        result = future.result(timeout=30)
        result = await future.result_async(timeout=30)
    """

    def __init__(self, future: Future, result_type: type = None):
        self._future = future
        self._result = None
        self._result_type = result_type

    def result(self, timeout: Optional[float] = None):
        """Block until complete (sync)."""
        if self._result is None:
            self._result = self._future.result(timeout=timeout)
        return self._result

    async def result_async(self, timeout: Optional[float] = None):
        """Wait for completion (async)."""
        loop = asyncio.get_event_loop()
        return await asyncio.wait_for(
            loop.run_in_executor(None, self._future.result),
            timeout=timeout
        )

    def __await__(self):
        return self.result_async().__await__()
```

### 1.3 Implement ServiceClient

**File**: `service_client.py`

```python
import os
from typing import Dict, List, Optional
import httpx

from .types import GetServerCapabilitiesResponse
from .training_client import TrainingClient
from .sampling_client import SamplingClient
from .rest_client import RestClient
from .exceptions import TrainingError


class ServiceClient:
    """Main entry point for Basilica Training API.

    Example:
        >>> client = ServiceClient()
        >>> training = client.create_lora_training_client(
        ...     "meta-llama/Llama-3.1-8B-Instruct",
        ...     rank=32,
        ... )
        >>> result = training.forward_backward(data).result()
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        timeout: float = 300.0,
    ):
        self.api_key = api_key or os.environ.get("BASILICA_API_KEY") or os.environ.get("BASILICA_API_TOKEN")
        self.endpoint = endpoint or os.environ.get("BASILICA_ENDPOINT") or os.environ.get("BASILICA_API_URL", "https://api.basilica.ai")

        if not self.api_key:
            raise ValueError("API key required. Set BASILICA_API_KEY or pass api_key=")

        self._client = httpx.Client(
            base_url=self.endpoint,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=timeout,
        )
        self._async_client = None  # Lazy init

    def get_server_capabilities(self) -> GetServerCapabilitiesResponse:
        """Query available models and limits."""
        resp = self._client.get("/capabilities")
        resp.raise_for_status()
        return GetServerCapabilitiesResponse(**resp.json())

    async def get_server_capabilities_async(self) -> GetServerCapabilitiesResponse:
        """Query capabilities (async)."""
        client = await self._get_async_client()
        resp = await client.get("/capabilities")
        resp.raise_for_status()
        return GetServerCapabilitiesResponse(**resp.json())

    def create_lora_training_client(
        self,
        base_model: str,
        rank: int = 32,
        seed: Optional[int] = None,
        train_mlp: bool = True,
        train_attn: bool = True,
        train_unembed: bool = True,
        learning_rate: float = 1e-4,
        weight_decay: float = 0.01,
        user_metadata: Optional[Dict[str, str]] = None,
        gpu_count: int = 1,
        gpu_type: Optional[List[str]] = None,
        wait_timeout: float = 300.0,
    ) -> TrainingClient:
        """Create LoRA fine-tuning session.

        Args:
            base_model: HuggingFace model ID
            rank: LoRA rank (default 32)
            seed: Random seed for reproducibility
            train_mlp: Apply LoRA to MLP layers
            train_attn: Apply LoRA to attention layers
            train_unembed: Apply LoRA to unembedding layer
            learning_rate: Optimizer learning rate
            weight_decay: L2 regularization
            user_metadata: Custom metadata for tracking
            gpu_count: Number of GPUs (0 for CPU)
            gpu_type: Acceptable GPU types
            wait_timeout: Seconds to wait for session ready

        Returns:
            TrainingClient for training operations
        """
        # Build target modules from flags
        target_modules = []
        if train_attn:
            target_modules.extend(["q_proj", "k_proj", "v_proj", "o_proj"])
        if train_mlp:
            target_modules.extend(["gate_proj", "up_proj", "down_proj"])
        if train_unembed:
            target_modules.append("lm_head")

        if not target_modules:
            raise ValueError("At least one of train_mlp, train_attn, train_unembed must be True")

        # Create K8s session via Basilica API
        resp = self._client.post("/sessions", json={
            "baseModel": base_model,
            "loraConfig": {
                "rank": rank,
                "alpha": rank * 2,  # Standard alpha = 2 * rank
                "dropout": 0.05,
                "targetModules": target_modules,
            },
            "optimizerConfig": {
                "learningRate": learning_rate,
                "weightDecay": weight_decay,
                "gradClip": 1.0,
            },
            "gpuResources": {"count": gpu_count, "model": gpu_type or []},
            "seed": seed,
            "userMetadata": user_metadata or {},
            "ttlSeconds": 86400,
        })

        if not resp.is_success:
            raise TrainingError(f"Failed to create session: {resp.text}")

        session_id = resp.json()["sessionId"]

        # Wait for ready
        import time
        start = time.time()
        while time.time() - start < wait_timeout:
            resp = self._client.get(f"/sessions/{session_id}")
            if not resp.is_success:
                raise TrainingError(f"Failed to get session: {resp.text}")

            data = resp.json()
            phase = data.get("phase", "pending")

            if phase == "ready":
                break
            elif phase == "failed":
                raise TrainingError(f"Session failed: {data.get('error')}")

            time.sleep(5)
        else:
            raise TrainingError(f"Session not ready after {wait_timeout}s")

        # Create internal training session
        internal_id = f"train-{session_id}"
        resp = self._client.post(
            f"/sessions/{session_id}/internal",
            json={
                "session_id": internal_id,
                "base_model": base_model,
                "lora_config": {
                    "rank": rank,
                    "alpha": rank * 2,
                    "dropout": 0.05,
                    "target_modules": target_modules,
                },
                "optimizer_config": {
                    "learning_rate": learning_rate,
                    "weight_decay": weight_decay,
                },
                "seed": seed,
            },
        )

        if not resp.is_success:
            raise TrainingError(f"Failed to create internal session: {resp.text}")

        return TrainingClient(
            client=self._client,
            session_id=session_id,
            internal_id=internal_id,
            base_model=base_model,
            train_mlp=train_mlp,
            train_attn=train_attn,
            train_unembed=train_unembed,
        )

    def create_training_client_from_state(
        self,
        path: str,
        user_metadata: Optional[Dict[str, str]] = None,
    ) -> TrainingClient:
        """Resume training from checkpoint (weights only, optimizer resets)."""
        resp = self._client.post("/sessions/from_state", json={
            "path": path,
            "user_metadata": user_metadata or {},
            "load_optimizer": False,
        })
        resp.raise_for_status()
        data = resp.json()
        return TrainingClient(
            client=self._client,
            session_id=data["session_id"],
            internal_id=data["internal_id"],
            base_model=data["base_model"],
        )

    def create_training_client_from_state_with_optimizer(
        self,
        path: str,
        user_metadata: Optional[Dict[str, str]] = None,
    ) -> TrainingClient:
        """Resume training from checkpoint (weights + optimizer state)."""
        resp = self._client.post("/sessions/from_state", json={
            "path": path,
            "user_metadata": user_metadata or {},
            "load_optimizer": True,
        })
        resp.raise_for_status()
        data = resp.json()
        return TrainingClient(
            client=self._client,
            session_id=data["session_id"],
            internal_id=data["internal_id"],
            base_model=data["base_model"],
        )

    def create_sampling_client(
        self,
        model_path: Optional[str] = None,
        base_model: Optional[str] = None,
    ) -> SamplingClient:
        """Create client for text generation.

        Args:
            model_path: Path to fine-tuned weights
            base_model: Base model name (if no fine-tuned weights)
        """
        if model_path is None and base_model is None:
            raise ValueError("Either model_path or base_model required")

        return SamplingClient(
            client=self._client,
            model_path=model_path,
            base_model=base_model,
        )

    def create_rest_client(self) -> RestClient:
        """Create REST client for checkpoint management."""
        return RestClient(self._client)

    async def _get_async_client(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(
                base_url=self.endpoint,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=300.0,
            )
        return self._async_client

    def close(self):
        """Close the client."""
        self._client.close()
        if self._async_client:
            # Note: async client should be closed in async context
            pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# Backwards compatibility alias
Client = ServiceClient
```

### 1.4 Implement TrainingClient

**File**: `training_client.py`

```python
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Union
import httpx

from .types import (
    APIFuture,
    Datum,
    ForwardBackwardResult,
    ForwardResult,
    SamplingParams,
)
from .sampling_client import SamplingClient
from .exceptions import TrainingError


class TrainingClient:
    """Client for training operations.

    Example:
        >>> training = client.create_lora_training_client("llama-8b")
        >>> for batch in dataloader:
        ...     result = training.forward_backward(batch).result()
        ...     training.optim_step().result()
        >>> training.save_state("checkpoint-final").result()
    """

    def __init__(
        self,
        client: httpx.Client,
        session_id: str,
        internal_id: str,
        base_model: str,
        train_mlp: bool = True,
        train_attn: bool = True,
        train_unembed: bool = True,
    ):
        self._client = client
        self._session_id = session_id
        self._internal_id = internal_id
        self._base_model = base_model
        self._train_mlp = train_mlp
        self._train_attn = train_attn
        self._train_unembed = train_unembed
        self._step = 0
        self._executor = ThreadPoolExecutor(max_workers=4)

    @property
    def session_id(self) -> str:
        return self._session_id

    def _proxy(self, op: str = "") -> str:
        """Build proxy path to training service."""
        base = f"/sessions/{self._session_id}/internal/{self._internal_id}"
        return f"{base}/{op}" if op else base

    def _normalize_data(self, data: Union[List[Datum], List[Dict]]) -> List[Dict]:
        """Convert data to list of dicts."""
        result = []
        for d in data:
            if isinstance(d, Datum):
                result.append(d.to_dict())
            else:
                result.append(d)
        return result

    # --- Training Operations ---

    def forward(self, data: List[Datum]) -> APIFuture:
        """Forward pass without gradient computation.

        Returns:
            APIFuture resolving to ForwardResult
        """
        def _call():
            normalized = self._normalize_data(data)
            resp = self._client.post(self._proxy("forward"), json={"data": normalized})
            if not resp.is_success:
                raise TrainingError(f"forward failed: {resp.text}")
            r = resp.json()
            return ForwardResult(logprobs=r["logprobs"], tokens_processed=r["tokens_processed"])

        return APIFuture(self._executor.submit(_call), ForwardResult)

    def forward_backward(
        self,
        data: List[Datum],
        loss_fn: str = "cross_entropy",
    ) -> APIFuture:
        """Compute forward pass and gradients.

        Args:
            data: Training examples
            loss_fn: Loss function ("cross_entropy")

        Returns:
            APIFuture resolving to ForwardBackwardResult
        """
        def _call():
            normalized = self._normalize_data(data)

            # Pad sequences
            max_len = max(len(d["input_ids"]) for d in normalized)
            input_ids = []
            labels = []
            attention_mask = []
            loss_weights = []

            for d in normalized:
                ids = d["input_ids"]
                pad_len = max_len - len(ids)
                input_ids.append(ids + [0] * pad_len)
                labels.append(d.get("labels", ids) + [-100] * pad_len)
                attention_mask.append([1] * len(ids) + [0] * pad_len)
                if d.get("loss_weights"):
                    loss_weights.append(d["loss_weights"] + [0.0] * pad_len)

            payload = {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "labels": labels,
                "loss_fn": loss_fn,
            }
            if loss_weights:
                payload["loss_weights"] = loss_weights

            resp = self._client.post(self._proxy("forward_backward"), json=payload)
            if not resp.is_success:
                raise TrainingError(f"forward_backward failed: {resp.text}")

            r = resp.json()
            return ForwardBackwardResult(
                loss=r["loss"],
                logprobs=r["logprobs"],
                tokens_processed=r["tokens_processed"],
            )

        return APIFuture(self._executor.submit(_call), ForwardBackwardResult)

    def forward_backward_custom(
        self,
        data: List[Datum],
        loss_fn: Callable,
    ) -> APIFuture:
        """Compute gradients with custom loss function.

        The custom loss function receives logprobs and should return a scalar loss.

        Args:
            data: Training examples
            loss_fn: Custom loss function operating on logprobs
        """
        # Note: Custom loss requires special handling - logprobs sent back to client
        # for loss computation, then gradients sent back to server
        raise NotImplementedError("forward_backward_custom requires server-side support")

    def optim_step(
        self,
        learning_rate: Optional[float] = None,
        betas: Optional[tuple] = None,
        eps: Optional[float] = None,
        weight_decay: Optional[float] = None,
    ) -> APIFuture:
        """Update weights using accumulated gradients (Adam).

        Args:
            learning_rate: Override learning rate
            betas: Adam beta parameters
            eps: Adam epsilon
            weight_decay: L2 regularization
        """
        def _call():
            payload = {}
            if learning_rate is not None:
                payload["learning_rate"] = learning_rate
            if betas is not None:
                payload["beta1"], payload["beta2"] = betas
            if eps is not None:
                payload["eps"] = eps
            if weight_decay is not None:
                payload["weight_decay"] = weight_decay

            resp = self._client.post(self._proxy("optim_step"), json=payload if payload else None)
            if not resp.is_success:
                raise TrainingError(f"optim_step failed: {resp.text}")

            self._step = resp.json()["step"]
            return self._step

        return APIFuture(self._executor.submit(_call), int)

    # --- State Management ---

    def save_state(self, name: str) -> APIFuture:
        """Save checkpoint (weights + optimizer state)."""
        def _call():
            resp = self._client.post(
                self._proxy("save"),
                json={"checkpoint_name": name, "include_optimizer": True},
            )
            if not resp.is_success:
                raise TrainingError(f"save_state failed: {resp.text}")
            return resp.json()["checkpoint_path"]

        return APIFuture(self._executor.submit(_call), str)

    def load_state(self, path: str) -> APIFuture:
        """Load weights only (optimizer resets)."""
        def _call():
            resp = self._client.post(
                self._proxy("load"),
                json={"checkpoint_path": path, "load_optimizer": False},
            )
            if not resp.is_success:
                raise TrainingError(f"load_state failed: {resp.text}")

        return APIFuture(self._executor.submit(_call))

    def load_state_with_optimizer(self, path: str) -> APIFuture:
        """Load weights and optimizer state."""
        def _call():
            resp = self._client.post(
                self._proxy("load"),
                json={"checkpoint_path": path, "load_optimizer": True},
            )
            if not resp.is_success:
                raise TrainingError(f"load_state_with_optimizer failed: {resp.text}")

        return APIFuture(self._executor.submit(_call))

    def save_weights_for_sampler(self, name: str) -> APIFuture:
        """Export weights formatted for sampling."""
        def _call():
            resp = self._client.post(
                self._proxy("save_for_sampler"),
                json={"name": name},
            )
            if not resp.is_success:
                raise TrainingError(f"save_weights_for_sampler failed: {resp.text}")
            return resp.json()["path"]

        return APIFuture(self._executor.submit(_call), str)

    def save_weights_and_get_sampling_client(self, name: str) -> SamplingClient:
        """Save weights and return SamplingClient."""
        path = self.save_weights_for_sampler(name).result()
        return SamplingClient(client=self._client, model_path=path)

    # --- Utilities ---

    def get_tokenizer(self):
        """Get the model's tokenizer.

        Returns a HuggingFace tokenizer for encoding/decoding.
        """
        from transformers import AutoTokenizer
        return AutoTokenizer.from_pretrained(self._base_model)

    def get_info(self) -> Dict[str, Any]:
        """Get session configuration."""
        resp = self._client.get(self._proxy())
        if not resp.is_success:
            raise TrainingError(f"get_info failed: {resp.text}")

        data = resp.json()
        return {
            "session_id": self._session_id,
            "base_model": self._base_model,
            "train_mlp": self._train_mlp,
            "train_attn": self._train_attn,
            "train_unembed": self._train_unembed,
            "step": data.get("step_count", self._step),
            "tokens_processed": data.get("tokens_processed", 0),
        }

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

    # --- Async Variants ---

    async def forward_async(self, data: List[Datum]) -> ForwardResult:
        """Forward pass (async)."""
        return await self.forward(data).result_async()

    async def forward_backward_async(self, data: List[Datum], loss_fn: str = "cross_entropy") -> ForwardBackwardResult:
        """Compute gradients (async)."""
        return await self.forward_backward(data, loss_fn).result_async()

    async def optim_step_async(self, **kwargs) -> int:
        """Update weights (async)."""
        return await self.optim_step(**kwargs).result_async()

    async def save_state_async(self, name: str) -> str:
        """Save checkpoint (async)."""
        return await self.save_state(name).result_async()

    async def load_state_async(self, path: str):
        """Load weights (async)."""
        return await self.load_state(path).result_async()


# Backwards compatibility
TrainingSession = TrainingClient
```

### 1.5 Acceptance Criteria - Phase 1

- [ ] SDK restructured into separate modules
- [ ] `ServiceClient` with `create_lora_training_client(train_mlp, train_attn, train_unembed)`
- [ ] `TrainingClient` with `forward()`, `forward_backward()`, `optim_step()`
- [ ] `APIFuture` class working with `.result()` and `.result_async()`
- [ ] Backwards compatibility: `Client` alias works
- [ ] Example updated and working

---

## Phase 2: SamplingClient & Logprobs

**Goal**: Separate sampling, add logprob computation
**Effort**: 2-3 days

### 2.1 Implement SamplingClient

**File**: `sampling_client.py`

```python
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional
import httpx

from .types import APIFuture, ModelInput, SampleResponse, SamplingParams
from .exceptions import TrainingError


class SamplingClient:
    """Client for text generation.

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
        session_id: Optional[str] = None,
        internal_id: Optional[str] = None,
    ):
        self._client = client
        self._model_path = model_path
        self._base_model = base_model
        self._session_id = session_id
        self._internal_id = internal_id
        self._executor = ThreadPoolExecutor(max_workers=4)

    def _get_endpoint(self) -> str:
        """Get the sampling endpoint."""
        if self._session_id and self._internal_id:
            return f"/sessions/{self._session_id}/internal/{self._internal_id}/sample"
        return "/sample"

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
            prompt: Input tokens
            num_samples: Number of independent samples
            sampling_params: Generation parameters
            include_prompt_logprobs: Include logprobs for prompt tokens
            topk_prompt_logprobs: Top-k logprobs per position

        Returns:
            APIFuture resolving to List[SampleResponse]
        """
        params = sampling_params or SamplingParams()

        def _call():
            payload = {
                "token_ids": prompt.token_ids,
                "num_samples": num_samples,
                "include_prompt_logprobs": include_prompt_logprobs,
                **params.to_dict(),
            }
            if topk_prompt_logprobs is not None:
                payload["topk_prompt_logprobs"] = topk_prompt_logprobs
            if self._model_path:
                payload["model_path"] = self._model_path
            if self._base_model:
                payload["base_model"] = self._base_model

            resp = self._client.post(self._get_endpoint(), json=payload)
            if not resp.is_success:
                raise TrainingError(f"sample failed: {resp.text}")

            data = resp.json()
            samples = data.get("samples", [data])  # Handle single vs batch
            return [SampleResponse(**s) for s in samples]

        return APIFuture(self._executor.submit(_call), list)

    async def sample_async(
        self,
        prompt: ModelInput,
        num_samples: int = 1,
        sampling_params: Optional[SamplingParams] = None,
        **kwargs,
    ) -> List[SampleResponse]:
        """Generate completions (async)."""
        return await self.sample(prompt, num_samples, sampling_params, **kwargs).result_async()

    def compute_logprobs(self, prompt: ModelInput) -> APIFuture:
        """Compute log probabilities for prompt tokens.

        Returns:
            APIFuture resolving to List[Optional[float]]
        """
        def _call():
            payload = {"token_ids": prompt.token_ids}
            if self._model_path:
                payload["model_path"] = self._model_path
            if self._base_model:
                payload["base_model"] = self._base_model

            endpoint = self._get_endpoint().replace("/sample", "/compute_logprobs")
            if endpoint == "/sample":
                endpoint = "/compute_logprobs"

            resp = self._client.post(endpoint, json=payload)
            if not resp.is_success:
                raise TrainingError(f"compute_logprobs failed: {resp.text}")

            return resp.json()["logprobs"]

        return APIFuture(self._executor.submit(_call), list)

    async def compute_logprobs_async(self, prompt: ModelInput) -> List[Optional[float]]:
        """Compute logprobs (async)."""
        return await self.compute_logprobs(prompt).result_async()
```

### 2.2 Add Backend Endpoints

**File**: `services/training-service/src/server.py` (additions)

```python
class ComputeLogprobsRequest(BaseModel):
    """Compute logprobs request."""
    token_ids: List[int]


class ComputeLogprobsResponse(BaseModel):
    """Compute logprobs response."""
    logprobs: List[Optional[float]]


class ForwardRequest(BaseModel):
    """Forward-only request."""
    input_ids: List[List[int]]
    attention_mask: List[List[int]]


class ForwardResponse(BaseModel):
    """Forward-only response."""
    logprobs: List[List[float]]
    tokens_processed: int


@app.post("/sessions/{session_id}/forward", response_model=ForwardResponse)
async def forward(session_id: str, request: ForwardRequest) -> ForwardResponse:
    """Forward pass without gradient computation."""
    try:
        input_ids = torch.tensor(request.input_ids)
        attention_mask = torch.tensor(request.attention_mask)

        result = backend.forward(
            session_id=session_id,
            input_ids=input_ids,
            attention_mask=attention_mask,
        )

        return ForwardResponse(
            logprobs=result.logprobs,
            tokens_processed=result.tokens_processed,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@app.post("/sessions/{session_id}/compute_logprobs", response_model=ComputeLogprobsResponse)
async def compute_logprobs(session_id: str, request: ComputeLogprobsRequest) -> ComputeLogprobsResponse:
    """Compute log probabilities for token sequence."""
    try:
        logprobs = backend.compute_logprobs(
            session_id=session_id,
            token_ids=request.token_ids,
        )
        return ComputeLogprobsResponse(logprobs=logprobs)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
```

### 2.3 Add Backend Methods

**File**: `services/training-service/src/backend.py` (additions)

```python
def forward(
    self,
    session_id: str,
    input_ids: torch.Tensor,
    attention_mask: torch.Tensor,
) -> ForwardResult:
    """Forward pass without gradients."""
    session = self._get_session(session_id)
    model = session.model
    model.eval()

    with torch.no_grad():
        outputs = model(
            input_ids=input_ids.to(self.device),
            attention_mask=attention_mask.to(self.device),
        )

        # Compute logprobs
        log_probs = torch.nn.functional.log_softmax(outputs.logits, dim=-1)
        # Get logprobs for actual tokens
        token_logprobs = torch.gather(
            log_probs[:, :-1, :],
            dim=-1,
            index=input_ids[:, 1:].unsqueeze(-1).to(self.device),
        ).squeeze(-1)

    return ForwardResult(
        logprobs=token_logprobs.cpu().tolist(),
        tokens_processed=int(attention_mask.sum().item()),
    )


def compute_logprobs(
    self,
    session_id: str,
    token_ids: List[int],
) -> List[Optional[float]]:
    """Compute per-token logprobs for a sequence."""
    session = self._get_session(session_id)
    model = session.model
    model.eval()

    input_ids = torch.tensor([token_ids], device=self.device)

    with torch.no_grad():
        outputs = model(input_ids=input_ids)
        log_probs = torch.nn.functional.log_softmax(outputs.logits, dim=-1)

        # Get logprob for each token given previous context
        result = [None]  # First token has no logprob
        for i in range(1, len(token_ids)):
            result.append(log_probs[0, i-1, token_ids[i]].item())

    return result
```

### 2.4 Acceptance Criteria - Phase 2

- [ ] `SamplingClient` class with `sample()` and `compute_logprobs()`
- [ ] `include_prompt_logprobs` and `topk_prompt_logprobs` params working
- [ ] `/sessions/{id}/forward` endpoint added
- [ ] `/sessions/{id}/compute_logprobs` endpoint added
- [ ] `TrainingClient.save_weights_for_sampler()` working
- [ ] `TrainingClient.save_weights_and_get_sampling_client()` working

---

## Phase 3: RestClient & Checkpoint Management

**Goal**: Full checkpoint lifecycle management
**Effort**: 2-3 days

### 3.1 Implement RestClient

**File**: `rest_client.py`

```python
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional
import httpx

from .types import APIFuture
from .exceptions import TrainingError


class RestClient:
    """REST client for checkpoint and run management.

    Example:
        >>> rest = client.create_rest_client()
        >>> runs = rest.list_training_runs().result()
        >>> checkpoints = rest.list_checkpoints(run_id).result()
    """

    def __init__(self, client: httpx.Client):
        self._client = client
        self._executor = ThreadPoolExecutor(max_workers=4)

    # --- Training Runs ---

    def list_training_runs(self, limit: int = 20, offset: int = 0) -> APIFuture:
        """List training runs (paginated)."""
        def _call():
            resp = self._client.get("/training_runs", params={"limit": limit, "offset": offset})
            if not resp.is_success:
                raise TrainingError(f"list_training_runs failed: {resp.text}")
            return resp.json()["runs"]

        return APIFuture(self._executor.submit(_call), list)

    def get_training_run(self, run_id: str) -> APIFuture:
        """Get training run by ID."""
        def _call():
            resp = self._client.get(f"/training_runs/{run_id}")
            if not resp.is_success:
                raise TrainingError(f"get_training_run failed: {resp.text}")
            return resp.json()

        return APIFuture(self._executor.submit(_call), dict)

    def get_training_run_by_path(self, path: str) -> APIFuture:
        """Get training run by path."""
        def _call():
            resp = self._client.get("/training_runs/by_path", params={"path": path})
            if not resp.is_success:
                raise TrainingError(f"get_training_run_by_path failed: {resp.text}")
            return resp.json()

        return APIFuture(self._executor.submit(_call), dict)

    # --- Checkpoints ---

    def list_checkpoints(self, run_id: Optional[str] = None, limit: int = 100) -> APIFuture:
        """List checkpoints for a run (or all user checkpoints)."""
        def _call():
            params = {"limit": limit}
            if run_id:
                params["run_id"] = run_id
            resp = self._client.get("/checkpoints", params=params)
            if not resp.is_success:
                raise TrainingError(f"list_checkpoints failed: {resp.text}")
            return resp.json()["checkpoints"]

        return APIFuture(self._executor.submit(_call), list)

    def get_checkpoint_archive_url(self, checkpoint_id: str) -> APIFuture:
        """Get signed download URL for checkpoint."""
        def _call():
            resp = self._client.get(f"/checkpoints/{checkpoint_id}/download_url")
            if not resp.is_success:
                raise TrainingError(f"get_checkpoint_archive_url failed: {resp.text}")
            return resp.json()["url"]

        return APIFuture(self._executor.submit(_call), str)

    def delete_checkpoint(self, checkpoint_id: str) -> APIFuture:
        """Delete a checkpoint."""
        def _call():
            resp = self._client.delete(f"/checkpoints/{checkpoint_id}")
            if not resp.is_success:
                raise TrainingError(f"delete_checkpoint failed: {resp.text}")

        return APIFuture(self._executor.submit(_call))

    def get_weights_info_by_path(self, path: str) -> APIFuture:
        """Get checkpoint metadata."""
        def _call():
            resp = self._client.get("/checkpoints/info", params={"path": path})
            if not resp.is_success:
                raise TrainingError(f"get_weights_info_by_path failed: {resp.text}")
            return resp.json()

        return APIFuture(self._executor.submit(_call), dict)

    # --- Publishing ---

    def publish_checkpoint(self, path: str) -> APIFuture:
        """Make checkpoint publicly accessible."""
        def _call():
            resp = self._client.post("/checkpoints/publish", json={"path": path})
            if not resp.is_success:
                raise TrainingError(f"publish_checkpoint failed: {resp.text}")
            return resp.json()["public_url"]

        return APIFuture(self._executor.submit(_call), str)

    def unpublish_checkpoint(self, path: str) -> APIFuture:
        """Revert checkpoint to private."""
        def _call():
            resp = self._client.post("/checkpoints/unpublish", json={"path": path})
            if not resp.is_success:
                raise TrainingError(f"unpublish_checkpoint failed: {resp.text}")

        return APIFuture(self._executor.submit(_call))

    # --- Sessions ---

    def list_sessions(self, limit: int = 20) -> APIFuture:
        """List sessions."""
        def _call():
            resp = self._client.get("/sessions", params={"limit": limit})
            if not resp.is_success:
                raise TrainingError(f"list_sessions failed: {resp.text}")
            return resp.json()

        return APIFuture(self._executor.submit(_call), list)

    def get_session(self, session_id: str) -> APIFuture:
        """Get session details."""
        def _call():
            resp = self._client.get(f"/sessions/{session_id}")
            if not resp.is_success:
                raise TrainingError(f"get_session failed: {resp.text}")
            return resp.json()

        return APIFuture(self._executor.submit(_call), dict)
```

### 3.2 Add Basilica API Endpoints

**File**: `crates/basilica-api/src/api/routes/training.rs` (additions)

```rust
// GET /training_runs
pub async fn list_training_runs(
    State(state): State<AppState>,
    Query(params): Query<PaginationParams>,
    claims: Claims,
) -> Result<Json<ListTrainingRunsResponse>, ApiError> {
    let runs = state.db.list_training_runs(&claims.user_id, params.limit, params.offset).await?;
    Ok(Json(ListTrainingRunsResponse { runs }))
}

// GET /training_runs/:run_id
pub async fn get_training_run(
    State(state): State<AppState>,
    Path(run_id): Path<String>,
    claims: Claims,
) -> Result<Json<TrainingRun>, ApiError> {
    let run = state.db.get_training_run(&run_id, &claims.user_id).await?;
    Ok(Json(run))
}

// GET /checkpoints
pub async fn list_checkpoints(
    State(state): State<AppState>,
    Query(params): Query<CheckpointListParams>,
    claims: Claims,
) -> Result<Json<ListCheckpointsResponse>, ApiError> {
    let checkpoints = state.db.list_checkpoints(&claims.user_id, params.run_id.as_deref(), params.limit).await?;
    Ok(Json(ListCheckpointsResponse { checkpoints }))
}

// GET /checkpoints/:checkpoint_id/download_url
pub async fn get_checkpoint_download_url(
    State(state): State<AppState>,
    Path(checkpoint_id): Path<String>,
    claims: Claims,
) -> Result<Json<DownloadUrlResponse>, ApiError> {
    let checkpoint = state.db.get_checkpoint(&checkpoint_id, &claims.user_id).await?;
    let url = state.storage.generate_presigned_url(&checkpoint.path, Duration::hours(1)).await?;
    Ok(Json(DownloadUrlResponse { url }))
}

// DELETE /checkpoints/:checkpoint_id
pub async fn delete_checkpoint(
    State(state): State<AppState>,
    Path(checkpoint_id): Path<String>,
    claims: Claims,
) -> Result<StatusCode, ApiError> {
    let checkpoint = state.db.get_checkpoint(&checkpoint_id, &claims.user_id).await?;
    state.storage.delete(&checkpoint.path).await?;
    state.db.delete_checkpoint(&checkpoint_id).await?;
    Ok(StatusCode::NO_CONTENT)
}

// POST /checkpoints/publish
pub async fn publish_checkpoint(
    State(state): State<AppState>,
    Json(req): Json<PublishCheckpointRequest>,
    claims: Claims,
) -> Result<Json<PublishCheckpointResponse>, ApiError> {
    let checkpoint = state.db.get_checkpoint_by_path(&req.path, &claims.user_id).await?;
    let public_url = state.storage.make_public(&checkpoint.path).await?;
    state.db.update_checkpoint_visibility(&checkpoint.id, true).await?;
    Ok(Json(PublishCheckpointResponse { public_url }))
}

// GET /capabilities
pub async fn get_capabilities(
    State(state): State<AppState>,
) -> Result<Json<CapabilitiesResponse>, ApiError> {
    Ok(Json(CapabilitiesResponse {
        models: state.config.supported_models.clone(),
        max_batch_tokens: 4096,
        max_sequence_length: 8192,
    }))
}
```

### 3.3 Database Schema Additions

```sql
-- migrations/20240115_training_runs.sql

CREATE TABLE training_runs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    base_model TEXT NOT NULL,
    lora_rank INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    metadata JSONB DEFAULT '{}',
    FOREIGN KEY (session_id) REFERENCES training_sessions(id)
);

CREATE TABLE checkpoints (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    step INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_public BOOLEAN DEFAULT FALSE,
    metadata JSONB DEFAULT '{}',
    FOREIGN KEY (run_id) REFERENCES training_runs(id)
);

CREATE INDEX idx_training_runs_user ON training_runs(user_id);
CREATE INDEX idx_checkpoints_run ON checkpoints(run_id);
CREATE INDEX idx_checkpoints_user ON checkpoints(user_id);
```

### 3.4 Acceptance Criteria - Phase 3

- [ ] `RestClient` class implemented
- [ ] `/training_runs` endpoints working
- [ ] `/checkpoints` endpoints working
- [ ] Checkpoint download URLs (presigned) working
- [ ] Checkpoint publishing working
- [ ] Database schema migrated

---

## Phase 4: Integration & Testing

**Goal**: End-to-end testing and documentation
**Effort**: 2 days

### 4.1 Update Example

**File**: `examples/training_example.py`

```python
#!/usr/bin/env python3
"""Basilica Training Example"""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "crates", "basilica-sdk-python", "python"))

from basilica.training import ServiceClient, Datum, SamplingParams, ModelInput

# Create client
client = ServiceClient(
    api_key=open("build/api-token.txt").read().strip() if os.path.exists("build/api-token.txt") else None
)

# Check capabilities
caps = client.get_server_capabilities()
print(f"Available models: {caps.models[:3]}...")

# Create training session
with client.create_lora_training_client(
    base_model="facebook/opt-125m",
    rank=8,
    train_mlp=True,
    train_attn=True,
    train_unembed=False,
    gpu_count=0,  # CPU for testing
) as training:

    print(f"Session info: {training.get_info()}")

    # Training loop
    for i in range(5):
        # forward_backward returns APIFuture
        result = training.forward_backward([
            Datum(input_ids=[2, 133, 2119, 6219, 23602])
        ]).result()

        print(f"Step {i}: loss={result.loss:.4f}")

        # optim_step also returns APIFuture
        training.optim_step().result()

    # Save checkpoint
    path = training.save_state("checkpoint-final").result()
    print(f"Saved to: {path}")

    # Get sampling client
    sampling = training.save_weights_and_get_sampling_client("sampler-weights")

    # Generate sample
    tokenizer = training.get_tokenizer()
    prompt = ModelInput.from_string("The quick brown", tokenizer)
    samples = sampling.sample(prompt, SamplingParams(max_tokens=10)).result()
    print(f"Sample: {samples[0].text}")

    # Compute logprobs
    logprobs = sampling.compute_logprobs(prompt).result()
    print(f"Logprobs: {logprobs[:5]}...")

# RestClient example
rest = client.create_rest_client()
sessions = rest.list_sessions().result()
print(f"Active sessions: {len(sessions)}")
```

### 4.2 Add Unit Tests

**File**: `crates/basilica-sdk-python/python/basilica/training/tests/test_clients.py`

```python
import pytest
from unittest.mock import Mock, patch

from basilica.training import (
    ServiceClient,
    TrainingClient,
    SamplingClient,
    RestClient,
    Datum,
    SamplingParams,
    APIFuture,
)


class TestServiceClient:
    def test_create_lora_training_client_builds_target_modules(self):
        with patch('httpx.Client') as mock_client:
            mock_client.return_value.post.return_value.is_success = True
            mock_client.return_value.post.return_value.json.return_value = {"sessionId": "test"}
            mock_client.return_value.get.return_value.is_success = True
            mock_client.return_value.get.return_value.json.return_value = {"phase": "ready"}

            client = ServiceClient(api_key="test")
            training = client.create_lora_training_client(
                "test-model",
                train_mlp=True,
                train_attn=True,
                train_unembed=False,
            )

            # Check that target_modules were built correctly
            call_args = mock_client.return_value.post.call_args_list[0]
            lora_config = call_args.kwargs["json"]["loraConfig"]
            assert "q_proj" in lora_config["targetModules"]
            assert "gate_proj" in lora_config["targetModules"]
            assert "lm_head" not in lora_config["targetModules"]


class TestAPIFuture:
    def test_result_blocks(self):
        from concurrent.futures import Future

        future = Future()
        future.set_result(42)

        api_future = APIFuture(future, int)
        assert api_future.result() == 42

    @pytest.mark.asyncio
    async def test_result_async(self):
        from concurrent.futures import Future

        future = Future()
        future.set_result(42)

        api_future = APIFuture(future, int)
        result = await api_future.result_async()
        assert result == 42


class TestDatum:
    def test_to_dict(self):
        datum = Datum(input_ids=[1, 2, 3], labels=[4, 5, 6])
        d = datum.to_dict()
        assert d["input_ids"] == [1, 2, 3]
        assert d["labels"] == [4, 5, 6]
```

### 4.3 Integration Test Script

**File**: `scripts/test-training-sdk.sh`

```bash
#!/bin/bash
set -e

echo "=== Testing Training SDK ==="

# Start local services
just local-dev-up

# Wait for services
sleep 10

# Run example
cd examples
python training_example.py

# Run SDK tests
cd ../crates/basilica-sdk-python
pytest python/basilica/training/tests/ -v

echo "=== All tests passed ==="
```

### 4.4 Acceptance Criteria - Phase 4

- [ ] Example works end-to-end
- [ ] Unit tests passing
- [ ] Integration tests passing
- [ ] Documentation updated

---

## Summary

| Phase | Goal | Effort | Dependencies |
|-------|------|--------|--------------|
| **Phase 1** | SDK structure alignment | 1-2 days | None |
| **Phase 2** | SamplingClient & logprobs | 2-3 days | Phase 1 |
| **Phase 3** | RestClient & checkpoints | 2-3 days | Phase 1 |
| **Phase 4** | Integration & testing | 2 days | Phases 1-3 |

**Total estimated effort**: 7-10 days

## Files to Create/Modify

### New Files
- `crates/basilica-sdk-python/python/basilica/training/service_client.py`
- `crates/basilica-sdk-python/python/basilica/training/training_client.py`
- `crates/basilica-sdk-python/python/basilica/training/sampling_client.py`
- `crates/basilica-sdk-python/python/basilica/training/rest_client.py`
- `crates/basilica-sdk-python/python/basilica/training/types.py`
- `crates/basilica-sdk-python/python/basilica/training/exceptions.py`
- `crates/basilica-sdk-python/python/basilica/training/tests/test_clients.py`

### Modified Files
- `crates/basilica-sdk-python/python/basilica/training/__init__.py` - Re-exports
- `services/training-service/src/server.py` - New endpoints
- `services/training-service/src/backend.py` - New methods
- `crates/basilica-api/src/api/routes/training.rs` - RestClient endpoints
- `examples/training_example.py` - Updated example

### Database Migrations
- `crates/basilica-api/migrations/YYYYMMDD_training_runs.sql`
