# E2E Test Fixtures

This directory contains sample data files for E2E testing of the Basilica K3s platform.

## Rental Fixtures (JSON)

Use with `POST /v2/rentals` API endpoint:

- **rental-cpu-only.json** - Minimal CPU-only rental with busybox
- **rental-gpu-single.json** - Single GPU rental (RTX 3090) with CUDA runtime
- **rental-exclusive.json** - Exclusive node rental (CPU-only, high resources)
- **rental-with-volumes.json** - Rental with persistent volume (10GB)

### Usage Example

```bash
curl -H "Authorization: Bearer $BASILICA_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d @scripts/e2e/fixtures/rental-cpu-only.json \
  http://localhost:8000/v2/rentals
```

## Job Fixtures (JSON)

Use with `POST /api/v1/jobs` API endpoint:

- **job-simple.json** - Simple echo job (completes immediately)
- **job-multi-step.json** - Multi-step job with sleep delays
- **job-gpu-benchmark.json** - GPU job running nvidia-smi (requires A100)

### Usage Example

```bash
curl -H "Authorization: Bearer $BASILICA_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d @scripts/e2e/fixtures/job-simple.json \
  http://localhost:8000/api/v1/jobs
```

## NodeProfile Fixtures (YAML)

Use with `kubectl apply -f`:

- **node-profile-valid.yaml** - Valid node profile (health=Valid)
- **node-profile-invalid.yaml** - Invalid node profile (health=Invalid, triggers removal)

### Usage Example

```bash
# Create a valid node profile
kubectl apply -f scripts/e2e/fixtures/node-profile-valid.yaml

# Verify it was created
kubectl get basilicanodeprofiles.basilica.ai -n basilica-system test-node-valid

# Create an invalid profile (triggers node removal controller)
kubectl apply -f scripts/e2e/fixtures/node-profile-invalid.yaml
```

## Notes

- All rental and job fixtures use publicly available images (busybox, nvidia/cuda)
- GPU fixtures require nodes with appropriate GPU models and drivers
- Exclusive rentals require nodes with the `basilica.ai/rental-exclusive` taint toleration
- Persistent volume fixtures require a StorageClass with dynamic provisioning

## Customization

To create custom fixtures:

1. Copy an existing fixture file
2. Modify the `resources`, `image`, or `command` fields
3. Save with a descriptive name
4. Use in your tests

For full schema documentation, see:
- Rentals: `docs/implementation-plan-k3s-rentals.md` (RentalSpec)
- Jobs: `docs/implementation-plan-k3s-rentals.md` (ComputeSpec)
- NodeProfiles: `crates/basilica-operator/src/crd/basilica_node_profile.rs`
