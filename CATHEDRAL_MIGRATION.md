# Cathedral Migration: Outstanding TODOs

This document tracks all rename items that could not be completed in the initial
`basilica → cathedral` / `substrate → cathedral` rename pass. Each item is
blocked by backwards compatibility, external infrastructure, or deployed systems.

## Summary

| Category | Count | Blocked By |
|----------|-------|------------|
| CLI binary name | 1 | User scripts and aliases |
| Python SDK module name | 3 | PyPI package, user imports |
| API URLs | 4 | DNS / domain registration |
| Docker images | 4 | Container registry migration |
| K8s CRDs & labels | 3 | Deployed cluster CRDs |
| Environment variables | 5 | User configuration scripts |
| Auth0 configuration | 2 | Auth0 tenant config |
| API error codes | 1 | Client SDK backwards compat |
| Proto package names | 1 | gRPC wire format |

---

## CLI Binary Name

| File | Line | Current Value | Reason | Next Step |
|------|------|---------------|--------|-----------|
| `crates/cathedral-cli/Cargo.toml` | 13 | `name = "basilica"` | Users invoke `basilica login`, `basilica deploy`, etc. Changing the binary name breaks all user scripts and shell aliases. | Ship `cathedral` as an alias first, deprecate `basilica` over 2 releases, then make `cathedral` the primary binary name. |

## Python SDK Module Name

| File | Line | Current Value | Reason | Next Step |
|------|------|---------------|--------|-----------|
| `crates/cathedral-sdk-python/Cargo.toml` | 12 | `name = "basilica"` (lib) | PyO3 native module compiled as `basilica._basilica`. Changing breaks `import basilica`. | Publish `cathedral-sdk` on PyPI alongside `basilica-sdk` with a deprecation wrapper. |
| `crates/cathedral-sdk-python/pyproject.toml` | 63 | `module-name = "basilica._basilica"` | Python import path depends on this. | Update after PyPI migration. |
| `crates/cathedral-sdk-python/pyproject.toml` | 12 | `email = "team@basilica.ai"` | Email domain not yet migrated. | Update once `cathedral.ai` or equivalent domain is live. |

## API URLs (DNS-blocked)

| File | Line | Current Value | Reason | Next Step |
|------|------|---------------|--------|-----------|
| `crates/cathedral-sdk/src/client.rs` | 61 | `DEFAULT_API_URL = "https://api.basilica.ai"` | All clients default to this URL. Changing breaks every existing deployment. | Register `api.cathedral.ai`, point it to same backend, update default. |
| `crates/cathedral-validator/src/config/main_config.rs` | 572 | `"https://api.basilica.ai"` | Validator default API endpoint. | Update after DNS cutover. |
| `crates/cathedral-cli/src/config/mod.rs` | 47 | `"https://api.basilica.ai"` | CLI default API endpoint. | Update after DNS cutover. |
| `crates/cathedral-common/build.rs` | 28 | `AUTH0_AUDIENCE = "https://api.basilica.ai/"` | Auth0 token audience must match server config. | Update after Auth0 tenant is reconfigured. |

## Docker Images (Registry-blocked)

| File | Line | Current Value | Reason | Next Step |
|------|------|---------------|--------|-----------|
| `crates/cathedral-cli/src/cli/handlers/deploy/templates/tau.rs` | 21 | `ghcr.io/one-covenant/basilica-tau:latest` | Image published to this registry path. | Push images under new org/name, update reference. |
| `crates/cathedral-cli/src/cli/handlers/deploy/templates/openclaw.rs` | 25 | `ghcr.io/one-covenant/basilica-openclaw:latest` | Image published to this registry path. | Push images under new org/name, update reference. |
| `examples/24_clawdbot.py` | 26 | `ghcr.io/one-covenant/basilica-clawdbot:latest` | Example references published image. | Update after registry migration. |
| `examples/27_clawdbot_kimi_k2_5.py` | 45 | `ghcr.io/one-covenant/basilica-clawdbot:kimi-k2.5` | Example references published image. | Update after registry migration. |

Also affected: `examples/28_openclaw.py`, `examples/30_tau.py`, `justfile`, `.github/workflows/ci.yml`, `.github/workflows/release.yml`, and all Docker Compose files reference `ghcr.io/one-covenant/basilica/*` images.

## Kubernetes CRDs & Labels

| File | Line | Current Value | Reason | Next Step |
|------|------|---------------|--------|-----------|
| `crates/cathedral-validator/src/k8s_profile_publisher.rs` | 54 | `basilica.ai/v1`, `BasilicaNodeProfile` | CRD is deployed in production K8s clusters. Changing the API group breaks node scheduling. | Create new CRD `cathedral.ai/v1`, migrate existing resources, then remove old CRD. |
| `crates/cathedral-validator/src/node_profile.rs` | 116+ | `basilica.ai/node-role`, `basilica.ai/gpu-model`, etc. | K8s node labels used for scheduling and filtering. | Update labels in coordinated rollout with cluster operators. |
| `crates/cathedral-validator/src/rental_adapter.rs` | 58 | `basilica.ai/v1` | Rental CRD API version. | Update with CRD migration. |

Labels affected: `basilica.ai/node-role`, `basilica.ai/validated`, `basilica.ai/provider`, `basilica.ai/region`, `basilica.ai/node-group`, `basilica.ai/gpu-model`, `basilica.ai/gpu-count`, `basilica.ai/gpu-mem`, `basilica.ai/compute-tier`, `basilica.ai/docker-active`, `basilica.ai/docker-version`, `basilica.ai/dind`, `basilica.ai/docker-error`.

Taints: `basilica.ai/workloads-only`, `basilica.ai/rental-exclusive`.

## Environment Variables

| File | Line | Current Value | Reason | Next Step |
|------|------|---------------|--------|-----------|
| `crates/cathedral-sdk/src/auth/simple_manager.rs` | 40 | `BASILICA_API_TOKEN` | Users set this in their shell profiles and CI pipelines. | Support both `CATHEDRAL_API_TOKEN` and `BASILICA_API_TOKEN` (with the latter as fallback), then deprecate. |
| `crates/cathedral-common/src/auth_constants.rs` | 18+ | `BASILICA_AUTH0_DOMAIN`, `BASILICA_AUTH0_CLIENT_ID`, `BASILICA_AUTH0_AUDIENCE`, `BASILICA_AUTH0_ISSUER` | Build-time and runtime Auth0 config env vars. | Add `CATHEDRAL_AUTH0_*` as primary, keep `BASILICA_AUTH0_*` as fallback. |
| `crates/cathedral-common/build.rs` | 13-16 | `BASILICA_AUTH0_*` | Cargo build env var rerun triggers. | Update alongside auth_constants.rs. |
| `crates/cathedral-validator/src/miner_prover/verification.rs` | 841+ | `BASILICA_ENABLE_K3S_JOIN`, `BASILICA_K3S_URL`, `BASILICA_K3S_TOKEN`, `BASILICA_NAMESPACE`, `BASILICA_TAINT_EXCLUSIVE` | Validator runtime configuration for K3s cluster joining. | Add `CATHEDRAL_*` equivalents with `BASILICA_*` fallback. |
| `crates/cathedral-sdk/src/auth/types.rs` | 200 | Error message references `BASILICA_API_TOKEN` | User-facing error message guides users to set env var. | Update message to reference both env var names. |

## API Error Codes

| File | Line | Current Value | Reason | Next Step |
|------|------|---------------|--------|-----------|
| `crates/cathedral-sdk/src/error.rs` | 77-91 | `BASILICA_API_HTTP_CLIENT_ERROR`, `BASILICA_API_AUTH_MISSING`, `BASILICA_API_RATE_LIMIT`, etc. | Error codes returned to API clients. Changing them breaks client error handling. | Add `CATHEDRAL_API_*` codes in a new API version, keep `BASILICA_API_*` codes in current version. |

## Proto Package Names (Wire Format)

| File | Line | Current Value | Reason | Next Step |
|------|------|---------------|--------|-----------|
| `crates/cathedral-protocol/proto/billing.proto` | 3 | `package basilica.billing.v1` | gRPC wire format. Changing the package name breaks all existing gRPC clients and servers. | Create `cathedral.billing.v2` alongside existing packages, migrate clients, then deprecate. |
| `crates/cathedral-protocol/proto/rental.proto` | 3 | `package basilica.rental.v1` | Same as above. | Same migration path. |
| `crates/cathedral-protocol/proto/payments.proto` | 3 | `package basilica.payments.v1` | Same as above. | Same migration path. |
| `crates/cathedral-protocol/proto/incentive.proto` | 3 | `package basilica.incentive.v1` | Same as above. | Same migration path. |
| `crates/cathedral-protocol/proto/miner_payouts.proto` | 3 | `package basilica.payouts.v1` | Same as above. | Same migration path. |

Also note: some proto files use `basilca` (missing 'i') — this is a pre-existing typo from upstream. Files: `common.proto`, `miner_discovery.proto`, `validator_api.proto`, `gpu_pow.proto`.

## External URL References in Documentation

These URLs reference `basilica.ai` domain and cannot be updated until DNS infrastructure exists:

- `docs/GETTING-STARTED.md`: `api.basilica.ai/health`, `api.basilica.ai` examples
- `docs/miner.md`: `docs.basilica.ai/miners`
- `docs/quickstart.md`: `www.basilica.ai`
- `docs/README.md`: `www.basilica.ai`
- `.claude/skills/cathedral-account-ops/SKILL.md`: install script URL
- `llms.txt`, `llms-full.txt`: `basilica.ai/agents/` URLs
- `crates/cathedral-sdk-python/README.md`: `api.basilica.ai`, `docs.basilica.ai`

## GitHub CI/CD References

Docker image names in CI workflows still reference `ghcr.io/one-covenant/basilica/*`:
- `.github/workflows/ci.yml`
- `.github/workflows/release.yml`
- `.github/workflows/release-cli.yml`
- `.github/workflows/release-python-sdk.yml`
- `justfile` (docker build targets)

GitHub release tag pattern `basilica-cli-v*` in:
- `crates/cathedral-cli/src/github_releases.rs` (self-update mechanism)

---

## Migration Priority

1. **High**: DNS + API URL migration (unblocks URL updates across entire codebase)
2. **High**: Docker registry migration (unblocks image name updates)
3. **Medium**: Environment variables (add dual-name support)
4. **Medium**: CLI binary name (ship alias first)
5. **Medium**: Python SDK package name (publish new PyPI package)
6. **Low**: K8s CRDs (requires coordinated cluster rollout)
7. **Low**: Proto package names (requires gRPC versioning)
8. **Low**: API error codes (requires API versioning)
