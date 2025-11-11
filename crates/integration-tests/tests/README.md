# Kubernetes Integration Tests

This directory contains Rust integration tests that validate Basilica components against a real Kubernetes cluster.

## Test Suites

### 1. Operator K8s Integration Tests (`operator_k8s_integration.rs`)

Tests the Basilica operator's Kubernetes client implementation:
- BasilicaJob CRD creation, status updates, and deletion
- GpuRental CRD lifecycle management
- Pod security context validation
- RBAC permission enforcement
- Concurrent resource creation
- Test fixture loading

### 2. API K8s Integration Tests (`api_k8s_integration.rs`)

Tests the Basilica API's Kubernetes client implementation:
- Jobs API: create, get status, delete, logs
- Rentals API: create, get status, delete, logs, exec, extend, list
- Error handling (NotFound, etc.)
- Resource specification handling
- Environment variable passing
- Concurrent API operations

## Prerequisites

### Required:
- **Kubernetes cluster** accessible via `KUBECONFIG` or in-cluster config
- **Basilica CRDs installed**:
  - `BasilicaJob` (basilica.ai/v1)
  - `GpuRental` (basilica.ai/v1)
  - `BasilicaNodeProfile` (basilica.ai/v1)
  - `BasilicaQueue` (basilica.ai/v1)

### Optional:
- **Operator running** (for reconciliation tests)
- **API server running** (for end-to-end tests)

## Running Tests

### In E2E Environment

The recommended way to run these tests is in the full E2E environment:

```bash
# Start E2E environment (includes K3s cluster)
just e2e-up

# Export kubeconfig
export KUBECONFIG=$(pwd)/build/k3s.yaml

# Run all K8s integration tests
cargo test -p integration-tests --test operator_k8s_integration --test api_k8s_integration

# Run specific test
cargo test -p integration-tests --test operator_k8s_integration test_basilica_job_crd_create_get -- --nocapture

# Run with detailed logs
RUST_LOG=debug cargo test -p integration-tests --test api_k8s_integration -- --nocapture
```

### With Existing Cluster

If you have an existing Kubernetes cluster:

```bash
# Set KUBECONFIG to your cluster
export KUBECONFIG=/path/to/your/kubeconfig

# Apply Basilica CRDs
kubectl apply -f config/crd/

# Run tests
cargo test -p integration-tests --test operator_k8s_integration --test api_k8s_integration
```

### Skip Tests (No Cluster Available)

If you don't have a Kubernetes cluster available:

```bash
# Tests will automatically skip if cluster is unreachable
NO_K8S_TESTS=1 cargo test -p integration-tests --test operator_k8s_integration
```

## Test Behavior

### Automatic Skipping

Tests automatically skip if:
- `NO_K8S_TESTS` environment variable is set
- No Kubernetes config is found
- Cluster is unreachable

Example output when skipped:
```
running 1 test
test test_basilica_job_crd_create_get ... ok

test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out
```

### Namespace Isolation

Each test creates an isolated namespace:
- Format: `test-{test-name}-{uuid}`
- Labeled with `basilica.ai/test=true`
- **Automatically cleaned up** when test completes
- Use `keep_namespace_on_drop()` to preserve for debugging

### Test Fixtures

Tests can load fixtures from `scripts/e2e/fixtures/`:
- `job-simple.json`
- `job-gpu-benchmark.json`
- `rental-cpu-only.json`
- `rental-gpu-single.json`
- `node-profile-valid.yaml`

Example:
```rust
let ctx = K8sTestContext::new("my-test").await?;
let fixture: serde_json::Value = ctx.load_fixture_json("job-simple.json")?;
```

## Writing New Tests

### Example Test Structure

```rust
#[tokio::test]
async fn test_my_kubernetes_feature() -> Result<()> {
    // Skip if cluster unavailable
    if K8sTestContext::should_skip_test().await {
        return Ok(());
    }

    // Create isolated test namespace
    let ctx = K8sTestContext::new("my-feature").await?;

    // Get K8s client
    let client = KubeClient { client: ctx.client.clone() };

    // ... your test logic ...

    // Namespace auto-cleaned on drop
    Ok(())
}
```

### Test Helpers

Available from `K8sTestContext`:
- `load_fixture_json<T>(name)` - Load JSON fixture
- `load_fixture_yaml<T>(name)` - Load YAML fixture
- `wait_for(condition, timeout, poll_interval)` - Wait for condition
- `should_skip_test()` - Check if tests should be skipped

## Debugging Failed Tests

### Keep namespace for inspection:
```rust
let ctx = K8sTestContext::new("my-test")
    .await?
    .keep_namespace_on_drop();
```

### Inspect test resources:
```bash
# List test namespaces
kubectl get namespaces -l basilica.ai/test=true

# Inspect resources in test namespace
kubectl get all -n test-my-test-abc123

# View BasilicaJob CRs
kubectl get basilicajobs -n test-my-test-abc123 -o yaml
```

### Clean up test namespaces manually:
```bash
kubectl delete namespace -l basilica.ai/test=true
```

## CI/CD Integration

These tests are designed to run in CI pipelines:

```yaml
# GitHub Actions example
- name: Run K8s Integration Tests
  env:
    KUBECONFIG: ${{ github.workspace }}/build/k3s.yaml
  run: |
    cargo test -p integration-tests --test operator_k8s_integration
    cargo test -p integration-tests --test api_k8s_integration
```

## Troubleshooting

### Tests hang indefinitely
- Check cluster connectivity: `kubectl cluster-info`
- Verify CRDs installed: `kubectl get crds | grep basilica`
- Check test timeout settings

### Permission errors
- Verify RBAC is configured: `./scripts/e2e/validate-rbac.sh`
- Check ServiceAccount permissions
- Ensure test namespace has correct RoleBindings

### CRD not found errors
- Install CRDs: `kubectl apply -f config/crd/`
- Verify CRDs exist: `kubectl get crds basilicajobs.basilica.ai`

### Tests fail but pass in E2E smoke tests
- Check operator logs for reconciliation errors
- Verify operator is running: `kubectl get pods -n basilica-system`
- Compare test vs smoke test resource specifications

## Coverage

Current test coverage:
- ✅ BasilicaJob CRUD operations
- ✅ GpuRental CRUD operations
- ✅ Status updates and retrieval
- ✅ Pod security contexts
- ✅ RBAC namespace-scoped operations
- ✅ Concurrent resource creation
- ✅ Test fixture loading
- ✅ API error handling
- ✅ List operations

Future coverage (see `docs/e2e-gaps-and-tests.md`):
- ⏳ Error scenarios (quota exhaustion, invalid requests)
- ⏳ Pod exec with real containers
- ⏳ Log streaming with tail/since parameters
- ⏳ Rental extend operations
- ⏳ Network policy enforcement
- ⏳ Storage volume management

## Related Documentation

- [`docs/e2e-gaps-and-tests.md`](../../../docs/e2e-gaps-and-tests.md) - Gap analysis and test roadmap
- [`docs/e2e-readiness-checklist.md`](../../../docs/e2e-readiness-checklist.md) - E2E environment setup
- [`scripts/e2e/README.md`](../../../scripts/e2e/README.md) - E2E scripts and smoke tests
