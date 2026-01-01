# TrainingSession System Architecture

**Document Version:** 1.0
**Last Updated:** 2025-12-17
**Status:** Production System Documentation

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Architecture Overview](#architecture-overview)
3. [Integration with Existing Infrastructure](#integration-with-existing-infrastructure)
4. [Component Architecture](#component-architecture)
5. [End-to-End Request Flow](#end-to-end-request-flow)
6. [API Layer](#api-layer)
7. [Kubernetes Operator](#kubernetes-operator)
8. [Gateway and Routing](#gateway-and-routing)
9. [Python SDK](#python-sdk)
10. [Custom Resource Definition](#custom-resource-definition)
11. [Security and Multi-Tenancy](#security-and-multi-tenancy)
12. [Operational Runbooks](#operational-runbooks)

---

## Executive Summary

The TrainingSession system provides managed GPU training for LLM fine-tuning with LoRA on Basilica. It follows the **same architectural pattern as UserDeployment**, leveraging the existing Envoy Gateway infrastructure for routing, rate limiting, and multi-tenancy.

### Key Design Principles

| Principle | Implementation |
|-----------|----------------|
| **Follow Existing Patterns** | Same flow as UserDeployment: SDK → API → CRD + HTTPRoute → Operator |
| **Use Existing Infrastructure** | Envoy Gateway for routing, existing NetworkPolicies for security |
| **No New Gateway** | Reuse existing Envoy Gateway - no separate "API Gateway" |
| **Kubernetes Native** | TrainingSession CRD managed by existing Basilica Operator |

### Architecture at a Glance

```
┌─────────────┐     ┌──────────────┐     ┌──────────────────┐     ┌─────────────┐
│ Python SDK  │────►│ Basilica API │────►│ K8s Resources    │────►│   Envoy     │
│ training()  │     │ /sessions    │     │ - TrainingSession│     │   Gateway   │
│             │     │              │     │ - HTTPRoute      │     │             │
└─────────────┘     └──────────────┘     └──────────────────┘     └─────────────┘
```

---

## Architecture Overview

### Comparison with UserDeployment

The TrainingSession system mirrors the UserDeployment architecture:

| Aspect | UserDeployment | TrainingSession |
|--------|----------------|-----------------|
| **SDK Entry Point** | `client.create_deployment()` | `client.create_training_session()` |
| **API Endpoint** | `POST /deployments` | `POST /sessions` |
| **CRD** | `UserDeployment` | `TrainingSession` |
| **Controller** | `UserDeploymentController` | `TrainingSessionController` |
| **Created Resources** | Deployment, Service, NetworkPolicy | Pod, Service, NetworkPolicy |
| **Routing** | HTTPRoute → Envoy Gateway | HTTPRoute → Envoy Gateway |
| **Public URL** | `{name}.deployments.basilica.ai` | `{name}.training.basilica.ai` |

### High-Level Architecture

```
                                    INTERNET
                                       │
                    ┌──────────────────┴──────────────────┐
                    │                                     │
                    ▼                                     ▼
            ┌───────────────┐                    ┌───────────────┐
            │  Python SDK   │                    │   Rust SDK    │
            │   (PyO3)      │                    │   (native)    │
            └───────┬───────┘                    └───────┬───────┘
                    │                                     │
                    └──────────────┬──────────────────────┘
                                   │ HTTP POST /sessions
                                   ▼
                          ┌────────────────┐
                          │  Basilica API  │
                          │   (Port 8080)  │
                          └────────┬───────┘
                                   │
           ┌───────────────────────┼───────────────────────┐
           │                       │                       │
           ▼                       ▼                       ▼
    ┌─────────────┐      ┌─────────────────┐      ┌──────────────┐
    │  Create     │      │  Create         │      │  Create      │
    │  Namespace  │      │  TrainingSession│      │  HTTPRoute   │
    │  u-{user}   │      │  CRD            │      │  (Gateway)   │
    └─────────────┘      └────────┬────────┘      └──────────────┘
                                  │
                                  ▼
                         ┌────────────────┐
                         │   Basilica     │
                         │   Operator     │
                         └────────┬───────┘
                                  │
           ┌──────────────────────┼──────────────────────┐
           │                      │                      │
           ▼                      ▼                      ▼
    ┌─────────────┐      ┌─────────────┐      ┌─────────────────┐
    │     Pod     │      │  Service    │      │  NetworkPolicy  │
    │ training-   │      │ s-{name}    │      │  {name}-netpol  │
    │ {name}      │      │             │      │                 │
    └──────┬──────┘      └──────┬──────┘      └─────────────────┘
           │                    │
           ▼                    ▼
    ┌─────────────┐      ┌─────────────┐
    │  Training   │◄─────│   Envoy     │◄───── Public Traffic
    │  Service    │      │   Gateway   │       (HTTPRoute)
    │  (HF+PEFT)  │      └─────────────┘
    └──────┬──────┘
           │
           │ R2/S3 Storage
           ▼
    ┌─────────────┐
    │ Checkpoints │
    │ & Models    │
    └─────────────┘
```

---

## Integration with Existing Infrastructure

### What We Reuse (No Changes Needed)

| Component | Location | Purpose |
|-----------|----------|---------|
| **Envoy Gateway** | `envoy-gateway-system` namespace | Routes traffic via HTTPRoutes |
| **Gateway** | `basilica-system/basilica-gateway` | Listens on port 8080 |
| **GatewayClass** | `eg` (envoy gateway controller) | Manages EnvoyProxy instances |
| **ReferenceGrant** | Per user namespace | Allows cross-namespace HTTPRoute refs |
| **NetworkPolicies** | Per user namespace | Default-deny + allow from Envoy |
| **Namespace Pattern** | `u-{user}` | Per-user isolation |
| **FUSE Storage** | DaemonSet in `basilica-storage` | Checkpoint persistence |

### What We Add

| Component | Location | Purpose |
|-----------|----------|---------|
| **TrainingSession CRD** | `basilica.ai/v1` | Defines training workload spec |
| **TrainingSessionController** | Basilica Operator | Reconciles CRD to K8s resources |
| **API Routes** | `basilica-api` | `/sessions` endpoints |
| **SDK Methods** | Python/Rust SDK | `create_training_session()` |

---

## Component Architecture

### Detailed Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              CLIENT LAYER                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────────────────┐      ┌─────────────────────────┐          │
│  │     Python SDK              │      │       Rust SDK          │          │
│  │                             │      │                         │          │
│  │  client.create_training_    │      │  client.create_         │          │
│  │    session()                │      │    training_session()   │          │
│  │  session.forward_backward() │      │                         │          │
│  │  session.optim_step()       │      │                         │          │
│  │  session.sample()           │      │                         │          │
│  │  session.save_state()       │      │                         │          │
│  │                             │      │                         │          │
│  └────────────┬────────────────┘      └────────────┬────────────┘          │
│               │                                    │                        │
│               └────────────────┬───────────────────┘                        │
│                                │ HTTP + JSON                                │
│                                ▼                                            │
└─────────────────────────────────────────────────────────────────────────────┘
                                 │
┌─────────────────────────────────────────────────────────────────────────────┐
│                               API LAYER                                      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                │                                            │
│  ┌─────────────────────────────▼─────────────────────────────────────┐      │
│  │                       Basilica API                                │      │
│  │                                                                   │      │
│  │  ┌─────────────┐  ┌──────────────┐  ┌─────────────┐              │      │
│  │  │ Auth        │  │ Sessions     │  │ Gateway     │              │      │
│  │  │ Middleware  │  │ Handlers     │  │ HTTPRoute   │              │      │
│  │  │             │  │              │  │ Manager     │              │      │
│  │  │ JWT/API Key │  │ POST /session│  │             │              │      │
│  │  │ Extraction  │  │ GET /session │  │ Create      │              │      │
│  │  │             │  │ DELETE       │  │ HTTPRoute   │              │      │
│  │  │             │  │ /forward_back│  │             │              │      │
│  │  │             │  │ /optim_step  │  │             │              │      │
│  │  │             │  │ /sample      │  │             │              │      │
│  │  │             │  │ /save /load  │  │             │              │      │
│  │  └─────────────┘  └──────────────┘  └─────────────┘              │      │
│  │                                                                   │      │
│  │  ┌─────────────────────────────────────────────────────────┐     │      │
│  │  │                    K8s Client                           │     │      │
│  │  │                                                         │     │      │
│  │  │  create_namespace()      create_training_session()      │     │      │
│  │  │  create_reference_grant()  create_httproute()           │     │      │
│  │  │  copy_storage_secret()   delete_training_session()      │     │      │
│  │  │                                                         │     │      │
│  │  └─────────────────────────────────────────────────────────┘     │      │
│  │                                                                   │      │
│  └───────────────────────────────────────────────────────────────────┘      │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                 │
                                 │ K8s API
                                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            K3s CLUSTER                                       │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                     basilica-system namespace                        │   │
│  │                                                                      │   │
│  │  ┌─────────────────────────────────────────────────────────────┐    │   │
│  │  │               Basilica Operator                             │    │   │
│  │  │                                                             │    │   │
│  │  │  TrainingSessionController                                  │    │   │
│  │  │    - Watch TrainingSession CRs                              │    │   │
│  │  │    - Create/Update Pods                                     │    │   │
│  │  │    - Create/Update Services                                 │    │   │
│  │  │    - Create/Update NetworkPolicies                          │    │   │
│  │  │    - Update CR Status                                       │    │   │
│  │  │                                                             │    │   │
│  │  └─────────────────────────────────────────────────────────────┘    │   │
│  │                                                                      │   │
│  │  ┌──────────────────┐                                               │   │
│  │  │  Gateway         │ ◄─── Existing Envoy Gateway                   │   │
│  │  │  basilica-       │      (reused, no changes)                     │   │
│  │  │  gateway         │                                               │   │
│  │  └──────────────────┘                                               │   │
│  │                                                                      │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                     u-{user} namespace (per user)                    │   │
│  │                                                                      │   │
│  │  ┌────────────────┐  ┌──────────────┐                               │   │
│  │  │TrainingSession │  │  HTTPRoute   │                               │   │
│  │  │     CRD        │  │ ts-{name}    │                               │   │
│  │  └────────────────┘  └──────────────┘                               │   │
│  │                                                                      │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐   │   │
│  │  │     Pod      │  │   Service    │  │   NetworkPolicy          │   │   │
│  │  │ training-    │  │  s-{name}    │  │   {name}-netpol          │   │   │
│  │  │ {name}       │  │              │  │                          │   │   │
│  │  └──────┬───────┘  └──────────────┘  └──────────────────────────┘   │   │
│  │         │                                                            │   │
│  │         ▼                                                            │   │
│  │  ┌──────────────────────────────────────────────────────────────┐   │   │
│  │  │                     Training Pod                              │   │   │
│  │  │                                                               │   │   │
│  │  │  ┌────────────────────┐                                       │   │   │
│  │  │  │  Training Service  │                                       │   │   │
│  │  │  │  (Python)          │                                       │   │   │
│  │  │  │                    │                                       │   │   │
│  │  │  │  - HuggingFace     │                                       │   │   │
│  │  │  │  - PEFT/LoRA       │                                       │   │   │
│  │  │  │  - FastAPI :8000   │                                       │   │   │
│  │  │  │  - /data mount     │◄──── FUSE storage (checkpoints)       │   │   │
│  │  │  └────────────────────┘                                       │   │   │
│  │  │                                                               │   │   │
│  │  └──────────────────────────────────────────────────────────────┘   │   │
│  │                                                                      │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## End-to-End Request Flow

### Session Creation Sequence

```
┌─────────┐     ┌─────────┐     ┌──────────┐     ┌──────────┐     ┌─────────┐
│  SDK    │     │   API   │     │  K8s API │     │ Operator │     │ Kubelet │
└────┬────┘     └────┬────┘     └────┬─────┘     └────┬─────┘     └────┬────┘
     │               │               │                │                │
     │ POST          │               │                │                │
     │ /sessions     │               │                │                │
     │──────────────>│               │                │                │
     │               │               │                │                │
     │               │ Validate      │                │                │
     │               │ Request       │                │                │
     │               │───────┐       │                │                │
     │               │       │       │                │                │
     │               │<──────┘       │                │                │
     │               │               │                │                │
     │               │ Create        │                │                │
     │               │ Namespace     │                │                │
     │               │ (if needed)   │                │                │
     │               │──────────────>│                │                │
     │               │               │                │                │
     │               │ Create        │                │                │
     │               │ TrainingSession                │                │
     │               │ CRD           │                │                │
     │               │──────────────>│                │                │
     │               │               │                │                │
     │               │ Create        │                │                │
     │               │ HTTPRoute     │                │                │
     │               │──────────────>│                │                │
     │               │               │                │                │
     │ 201 Created   │               │                │                │
     │ state=Pending │               │                │                │
     │ url=...       │               │                │                │
     │<──────────────│               │                │                │
     │               │               │                │                │
     │               │               │ Watch Event    │                │
     │               │               │ TrainingSession│                │
     │               │               │───────────────>│                │
     │               │               │                │                │
     │               │               │ Create         │                │
     │               │               │ Pod            │                │
     │               │               │<───────────────│                │
     │               │               │                │                │
     │               │               │ Create         │                │
     │               │               │ Service        │                │
     │               │               │<───────────────│                │
     │               │               │                │                │
     │               │               │ Create         │                │
     │               │               │ NetworkPolicy  │                │
     │               │               │<───────────────│                │
     │               │               │                │                │
     │               │               │                │ Schedule Pod   │
     │               │               │                │───────────────>│
     │               │               │                │                │
     │               │               │                │ Load Model     │
     │               │               │                │ (HuggingFace)  │
     │               │               │                │<───────────────│
     │               │               │                │                │
     │               │               │                │ Pod Ready      │
     │               │               │<───────────────│────────────────│
     │               │               │                │                │
     │               │               │ Update         │                │
     │               │               │ TrainingSession│                │
     │               │               │ Status=Ready   │                │
     │               │               │<───────────────│                │
     │               │               │                │                │
```

### Training Operation Flow

```
┌─────────┐     ┌─────────────┐     ┌────────────────┐
│  SDK    │     │ Envoy       │     │ Training Pod   │
│         │     │ Gateway     │     │                │
└────┬────┘     └──────┬──────┘     └───────┬────────┘
     │                 │                    │
     │ POST /sessions/{id}/forward_backward │
     │ Host: {id}.training.basilica.ai      │
     │────────────────>│                    │
     │                 │                    │
     │                 │ Route via HTTPRoute│
     │                 │────────────────────>
     │                 │                    │
     │                 │                    │ forward_backward()
     │                 │                    │ - Compute gradients
     │                 │                    │ - Return loss
     │                 │                    │
     │                 │<────────────────────
     │                 │ {loss, logprobs}   │
     │<────────────────│                    │
     │                 │                    │
     │ POST /sessions/{id}/optim_step       │
     │────────────────>│                    │
     │                 │────────────────────>
     │                 │                    │ optim_step()
     │                 │                    │ - Update weights
     │                 │<────────────────────
     │<────────────────│                    │
     │ {step: N}       │                    │
     │                 │                    │
```

---

## API Layer

### API Endpoints

The API adds training-specific routes to `basilica-api`:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/sessions` | POST | Create training session |
| `/sessions` | GET | List user's sessions |
| `/sessions/{id}` | GET | Get session status |
| `/sessions/{id}` | DELETE | Delete session |
| `/sessions/{id}/forward_backward` | POST | Compute gradients |
| `/sessions/{id}/optim_step` | POST | Apply gradients |
| `/sessions/{id}/sample` | POST | Generate text |
| `/sessions/{id}/save` | POST | Save checkpoint |
| `/sessions/{id}/load` | POST | Load checkpoint |

### Request Processing Flow

```rust
// handlers.rs - create_session()
pub async fn create_session(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Json(req): Json<CreateSessionRequest>,
) -> Result<impl IntoResponse> {

    // 1. Validate request
    validate_base_model(&req.base_model)?;
    validate_lora_config(&req.lora_config)?;
    validate_gpu_resources(&req.gpu_resources)?;

    // 2. Generate session ID
    let session_id = generate_session_id(&auth.user_id);

    // 3. Create K8s resources
    let namespace = format!("u-{}", sanitize_user_id(&auth.user_id));
    let public_url = format!("https://{}.training.basilica.ai", session_id);

    // Create namespace if needed (applies RBAC, NetworkPolicies)
    create_namespace_if_needed(&state, &namespace).await?;

    // Create TrainingSession CRD
    create_training_session_crd(&state, CreateTrainingSessionParams {
        namespace: &namespace,
        session_id: &session_id,
        user_id: &auth.user_id,
        request: &req,
    }).await?;

    // Create HTTPRoute for public access
    create_httproute(&state, &namespace, &session_id, &public_url).await?;

    // 4. Persist to database
    let record = db::create_session(&state.db, SessionRecord {
        user_id: auth.user_id,
        session_id: session_id.clone(),
        namespace,
        state: "Pending".to_string(),
        public_url: public_url.clone(),
        // ...
    }).await?;

    Ok((StatusCode::CREATED, Json(CreateSessionResponse {
        session_id,
        url: public_url,
        state: "Pending".to_string(),
    })))
}
```

### HTTPRoute Creation

The API creates an HTTPRoute that routes traffic through the existing Envoy Gateway:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: ts-{session_id}
  namespace: u-{user}
spec:
  parentRefs:
    - name: basilica-gateway
      namespace: basilica-system
  hostnames:
    - "{session_id}.training.basilica.ai"
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: "/"
      backendRefs:
        - name: s-{session_id}
          port: 8000
      timeouts:
        request: "600s"  # Long timeout for training operations
```

---

## Kubernetes Operator

### TrainingSessionController

The controller follows the same pattern as `UserDeploymentController`:

```rust
impl<C: K8sClient> TrainingSessionController<C> {
    pub async fn reconcile(&self, ns: &str, session: &TrainingSession) -> Result<()> {
        let name = session.name_any();
        let spec = &session.spec;

        let current_status = session.status.clone().unwrap_or_default();
        let phase = current_status.phase.clone();

        let new_status = match phase {
            TrainingSessionPhase::Pending => {
                self.handle_pending(session, current_status, ns, name).await?
            }
            TrainingSessionPhase::Scheduling => {
                self.handle_scheduling(session, current_status, ns, name).await?
            }
            TrainingSessionPhase::Initializing => {
                self.handle_initializing(session, current_status, ns, name).await?
            }
            TrainingSessionPhase::LoadingModel => {
                self.handle_loading_model(session, current_status, ns, name).await?
            }
            TrainingSessionPhase::Ready => {
                self.handle_ready(session, current_status, ns, name).await?
            }
            // ... other phases
        };

        self.client.update_training_session_status(ns, name, new_status).await?;
        Ok(())
    }
}
```

### Created Resources

The controller creates:

1. **Pod** - Running the training service container
2. **Service** - ClusterIP service for internal routing
3. **NetworkPolicy** - Allow traffic from Envoy Gateway only

### Pod Specification

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: training-{session_id}
  namespace: u-{user}
  labels:
    app: training-{session_id}
    basilica.ai/type: training-session
    basilica.ai/http-accessible: "true"
spec:
  containers:
    - name: training
      image: basilica/training:latest
      ports:
        - containerPort: 8000
          name: http
      env:
        - name: SESSION_ID
          value: "{session_id}"
        - name: BASE_MODEL
          value: "{base_model}"
        - name: MODEL_CACHE_DIR
          value: "/models"
        - name: CHECKPOINT_DIR
          value: "/data/checkpoints"
      resources:
        limits:
          nvidia.com/gpu: "1"
          memory: "64Gi"
        requests:
          nvidia.com/gpu: "1"
          memory: "32Gi"
      volumeMounts:
        - name: basilica-storage
          mountPath: /data
          mountPropagation: HostToContainer
  volumes:
    - name: basilica-storage
      hostPath:
        path: /var/lib/basilica/fuse/u-{user}
        type: Directory
  nodeSelector:
    basilica.ai/gpu-model: "{gpu_model}"
  tolerations:
    - key: nvidia.com/gpu
      operator: Exists
      effect: NoSchedule
```

---

## Gateway and Routing

### Reusing Existing Infrastructure

TrainingSession uses the **exact same Gateway infrastructure** as UserDeployment:

```
┌─────────────────────────────────────────────────────────────────┐
│                    basilica-system namespace                     │
│                                                                 │
│  ┌─────────────────┐         ┌──────────────────────────────┐  │
│  │   GatewayClass  │         │         Gateway              │  │
│  │   name: eg      │────────>│   name: basilica-gateway     │  │
│  │   (existing)    │         │   listeners:                 │  │
│  │                 │         │     - HTTP port 8080         │  │
│  │                 │         │   (existing)                 │  │
│  └─────────────────┘         └──────────────────────────────┘  │
│                                         │                       │
│                                         │                       │
│                                         ▼                       │
│                              ┌──────────────────────────────┐  │
│                              │       EnvoyProxy             │  │
│                              │   (routes both UserDeployment│  │
│                              │    AND TrainingSession)      │  │
│                              └──────────────────────────────┘  │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                                    │
                                    │ HTTPRoute references
                                    ▼
┌─────────────────────────────────────────────────────────────────┐
│                        u-{user} namespace                       │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  HTTPRoute (UserDeployment)   │   HTTPRoute (Training)  │   │
│  │  name: ud-{name}              │   name: ts-{session_id} │   │
│  │  host: {name}.deployments...  │   host: {id}.training..│   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│  Both route through the SAME Envoy Gateway                      │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### DNS Configuration

Training sessions use a separate subdomain:

| Type | Domain Pattern | Example |
|------|----------------|---------|
| UserDeployment | `{name}.deployments.basilica.ai` | `my-app.deployments.basilica.ai` |
| TrainingSession | `{id}.training.basilica.ai` | `abc123.training.basilica.ai` |

Both point to the same Envoy Gateway load balancer.

---

## Python SDK

### SDK Usage

```python
from basilica import BasilicaClient
from basilica.training import LoraConfig, SamplingParams, Datum

# Initialize client
client = BasilicaClient(api_key="your-api-key")

# Create training session
session = client.create_training_session(
    base_model="meta-llama/Llama-3.1-8B-Instruct",
    lora_config=LoraConfig(rank=32, alpha=64),
    gpu_model="H100",
)

print(f"Session URL: {session.url}")  # https://abc123.training.basilica.ai

# Training loop
for batch in dataloader:
    data = [Datum(input_ids=ids, labels=labels) for ids, labels in batch]
    result = session.forward_backward(data)
    print(f"Loss: {result.loss:.4f}")
    session.optim_step()

# Save checkpoint
session.save_state("checkpoint-final")

# Generate sample
sample = session.sample("Hello, world!", max_tokens=100)
print(sample.text)

# Cleanup
session.close()
```

### SDK Implementation

The SDK calls the API which handles all K8s resource creation:

```python
class BasilicaClient:
    def create_training_session(
        self,
        base_model: str,
        lora_config: LoraConfig = None,
        gpu_model: str = None,
        storage: StorageSpec = None,
        **kwargs,
    ) -> TrainingSession:
        """Create a new training session.

        This creates:
        1. TrainingSession CRD in K8s
        2. HTTPRoute for public access
        3. Returns session object with public URL
        """
        response = self._client.post(
            "/sessions",
            json={
                "baseModel": base_model,
                "loraConfig": lora_config.to_dict() if lora_config else {},
                "gpuResources": {"model": [gpu_model]} if gpu_model else {},
                "checkpointStorage": storage.to_dict() if storage else {},
                **kwargs,
            },
        )
        response.raise_for_status()
        data = response.json()

        return TrainingSession(
            session_id=data["sessionId"],
            url=data["url"],
            client=self._client,
        )
```

---

## Custom Resource Definition

### TrainingSession CRD

```yaml
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: trainingsessions.basilica.ai
spec:
  group: basilica.ai
  names:
    kind: TrainingSession
    plural: trainingsessions
    singular: trainingsession
    shortNames:
      - ts
  scope: Namespaced
  versions:
    - name: v1
      served: true
      storage: true
      subresources:
        status: {}
      schema:
        openAPIV3Schema:
          type: object
          required:
            - spec
          properties:
            spec:
              type: object
              required:
                - userId
                - baseModel
                - checkpointStorage
              properties:
                userId:
                  type: string
                baseModel:
                  type: string
                loraConfig:
                  type: object
                  properties:
                    rank:
                      type: integer
                      default: 32
                    alpha:
                      type: integer
                      default: 64
                    dropout:
                      type: number
                      default: 0.05
                    targetModules:
                      type: array
                      items:
                        type: string
                optimizerConfig:
                  type: object
                  properties:
                    learningRate:
                      type: number
                      default: 0.0001
                    weightDecay:
                      type: number
                      default: 0.01
                    gradClip:
                      type: number
                checkpointStorage:
                  type: object
                  required:
                    - backend
                    - bucket
                  properties:
                    backend:
                      type: string
                      enum: [r2, s3, gcs]
                    bucket:
                      type: string
                    credentialsSecret:
                      type: string
                gpuResources:
                  type: object
                  properties:
                    count:
                      type: integer
                      default: 1
                    model:
                      type: array
                      items:
                        type: string
                    minMemoryGb:
                      type: integer
                image:
                  type: string
                  default: "basilica/training:latest"
                ttlSeconds:
                  type: integer
                  default: 86400
            status:
              type: object
              properties:
                phase:
                  type: string
                  enum:
                    - Pending
                    - Scheduling
                    - Initializing
                    - LoadingModel
                    - Ready
                    - Suspended
                    - Failed
                    - Terminated
                stepsCompleted:
                  type: integer
                  default: 0
                tokensProcessed:
                  type: integer
                  default: 0
                lastCheckpoint:
                  type: string
                podName:
                  type: string
                endpoint:
                  type: string
                publicUrl:
                  type: string
                error:
                  type: string
```

---

## Security and Multi-Tenancy

### Security Model (Same as UserDeployment)

TrainingSession inherits all security from the existing infrastructure:

| Layer | Implementation |
|-------|----------------|
| **Namespace Isolation** | Per-user namespaces (`u-{user}`) |
| **RBAC** | ServiceAccount with minimal permissions |
| **NetworkPolicies** | Default-deny + allow from Envoy Gateway |
| **Pod Security Standards** | Privileged namespace, restricted audit/warn |
| **Storage Isolation** | Per-namespace FUSE mount paths |
| **API Authentication** | JWT/API key via existing middleware |

### NetworkPolicy

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: training-{session_id}-netpol
  namespace: u-{user}
spec:
  podSelector:
    matchLabels:
      app: training-{session_id}
  policyTypes:
    - Ingress
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: envoy-gateway-system
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: basilica-system
```

---

## Operational Runbooks

### Create a Training Session

**Via Python SDK:**
```python
from basilica import BasilicaClient

client = BasilicaClient()
session = client.create_training_session(
    base_model="meta-llama/Llama-3.1-8B-Instruct",
    gpu_model="H100",
)
print(f"Session: {session.session_id}")
print(f"URL: {session.url}")
```

**Via kubectl (debugging):**
```bash
cat <<EOF | kubectl apply -f -
apiVersion: basilica.ai/v1
kind: TrainingSession
metadata:
  name: test-session
  namespace: u-alice
spec:
  userId: "alice"
  baseModel: "meta-llama/Llama-3.1-8B-Instruct"
  loraConfig:
    rank: 32
    alpha: 64
  checkpointStorage:
    backend: "r2"
    bucket: "training-checkpoints"
  gpuResources:
    count: 1
    model: ["H100", "A100"]
EOF

kubectl get ts -n u-alice
```

### Check Session Status

```bash
# Get TrainingSession status
kubectl get ts -n u-{user} -o wide

# Get pod status
kubectl get pods -n u-{user} -l basilica.ai/type=training-session

# Check logs
kubectl logs -n u-{user} training-{session_id}

# Check HTTPRoute
kubectl get httproute -n u-{user}
```

### Delete a Session

```python
session.close()
```

```bash
# Via kubectl
kubectl delete ts -n u-{user} {session_id}
```

---

## Summary

The TrainingSession system integrates seamlessly with Basilica's existing infrastructure:

1. **Same Pattern as UserDeployment** - SDK → API → CRD + HTTPRoute → Operator
2. **Reuses Envoy Gateway** - No separate API gateway, uses existing routing
3. **Same Security Model** - NetworkPolicies, RBAC, namespace isolation
4. **Same Storage Model** - FUSE DaemonSet for checkpoint persistence
5. **Kubernetes Native** - Operator reconciles CRD to standard K8s resources

This approach provides:
- **Consistency** - Developers use familiar patterns
- **Simplicity** - No new infrastructure to maintain
- **Security** - Inherits battle-tested security model
- **Scalability** - Same scaling characteristics as UserDeployment
