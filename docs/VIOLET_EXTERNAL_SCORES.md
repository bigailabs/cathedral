# Violet External Scores and Confidential Sources

Cathedral validators do **not** change for external score integration. External scorers post reports to the Cathedral publisher; the publisher blends/signs the final vector served at `GET /v1/validator/weights/next`.

```txt
External scorer -> Cathedral publisher -> Cathedral-signed vector -> Cathedral validator -> set_weights()
```

## Overview

The blend injects external (non-base) scoring into the real signed weight vector with precise control and fail-closed behavior:

- **Snapshot semantics:** A `complete=true` report is the full truth at its epoch. Omitted hotkeys have implicit score 0 (revoked).
- **Allocation timing:** Payable-hotkey filtering occurs **before** allocation, not after, preserving the configured fraction exactly.
- **Fail-closed:** No external scores, stale snapshot, or incomplete report degrades to base-only. External-only (no base) also fails closed to empty.
- **Source separation:** Dedicated per-source tokens isolate credentials; a confidential source cannot be authorized by the shared token.
- **Confidential sources:** Sources like `cathedral_confidential_tdx` require `complete=true`, forbid `external_primary` mode, and require an explicit `FRACTION` env var (no legacy 50% default).

---

## Publisher Env

### Core Knobs

```bash
# ========== INGEST (allow reports to be POSTed) ==========
CATHEDRAL_EXTERNAL_SCORES_INGEST_ENABLED=1
CATHEDRAL_EXTERNAL_SCORES_TOKEN=<shared bearer token>  # or per-source tokens below
# Optional raw-body HMAC verification
CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET=<shared hmac secret>

# ========== COMPOSITION (include scores in weight calculation) ==========
CATHEDRAL_EXTERNAL_SCORES_ENABLED=1
CATHEDRAL_EXTERNAL_SCORES_SOURCE=violet_audio          # or cathedral_confidential_tdx, etc.
CATHEDRAL_EXTERNAL_SCORES_MODE=blend                    # blend | external_primary
CATHEDRAL_EXTERNAL_SCORES_FRACTION=0.1                  # External share (0..1)
                                                         # Explicit FRACTION required for confidential sources
CATHEDRAL_EXTERNAL_SCORES_WINDOW_SECS=3600             # Report freshness window

# ========== REGISTRATION & PAYABILITY GATES ==========
CATHEDRAL_EXTERNAL_SCORES_REQUIRE_REGISTERED=1         # Default: true
                                                         # Fail-closed if metagraph unavailable
CATHEDRAL_WEIGHTS_PAYABLE_HOTKEYS=filter               # "off" | "mark" | "filter"
                                                         # "filter" removes unpayable hotkeys pre-blend
```

### Caps and Limits

```bash
CATHEDRAL_EXTERNAL_SCORES_MAX_FRACTION=0.5             # Hard ceiling on external share (default 50%)
CATHEDRAL_EXTERNAL_SCORES_MAX_SCORES=4096              # Max scores per report (default 4096)
CATHEDRAL_EXTERNAL_SCORES_MAX_REPORT_AGE_SECS=3600     # Reject if generated_at too old (default 1 hour)
CATHEDRAL_EXTERNAL_SCORES_MAX_REPORT_FUTURE_SECS=120   # Reject if generated_at too far ahead (default 120 s)
```

### Mode and Confirmation

```bash
CATHEDRAL_EXTERNAL_SCORES_MODE=blend                   # Default: blend (capped)
                                                         # "external_primary" = 100% external (requires explicit confirm)
                                                         # "confidential_primary" = 100% confidential TDX (see below)
CATHEDRAL_EXTERNAL_SCORES_PRIMARY_CONFIRM=true         # Required for both external_primary and confidential_primary
                                                         # Vectors degrade to signed burn when this is absent
```

### `confidential_primary` Mode (100% Confidential TDX)

Sets `CATHEDRAL_EXTERNAL_SCORES_MODE=confidential_primary` to route the **entire** signed weight vector through the confidential compute scorer. Base scores are never a positive source in this mode.

```bash
# Required env for confidential_primary
CATHEDRAL_EXTERNAL_SCORES_MODE=confidential_primary
CATHEDRAL_EXTERNAL_SCORES_SOURCE=cathedral_confidential_tdx  # ONLY valid source for this mode
CATHEDRAL_EXTERNAL_SCORES_PRIMARY_CONFIRM=true               # Mandatory; absent = degraded burn vector
CATHEDRAL_EXTERNAL_SCORES_ENABLED=1                          # Composition must be enabled
CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX=<secret>  # Per-source token
```

**Degradation contract:** if any requirement is unmet (ingestion disabled, snapshot unavailable/incomplete/stale, `PRIMARY_CONFIRM` absent, wrong/absent source, no registered scores), the publisher signs a **zero-mass burn vector** (`confidential_mass=0`) carrying `confidential_primary` metadata plus a `degradation_reason` (e.g. `invalid_source`, `primary_confirm_missing`, `confidential_snapshot_unavailable`). The thin validator maps this to the signed burn UID only. Base scores are **never** substituted, even on degradation.

**Policy status:** `effective_external_share` reports `1.0` in `confidential_primary` mode regardless of the legacy base/external weight config.

**Fail-closed source guard:** `confidential_primary` intent is preserved even when `CATHEDRAL_EXTERNAL_SCORES_SOURCE` is not `cathedral_confidential_tdx`. It does **not** fall back to `blend` (which would re-admit base scores). Instead it degrades to a signed burn vector with `degradation_reason=invalid_source`. Only `cathedral_confidential_tdx` can receive 100% of the vector via this mode.

**Unknown mode is rejected:** any nonempty `CATHEDRAL_EXTERNAL_SCORES_MODE` other than `blend`, `external_primary`, or `confidential_primary` raises `VectorError` and aborts the build. It is never silently treated as `blend`. An unset/empty value stays the default `blend`.

**Thin-validator contract (mass=1):** The signed vector must carry `mode=confidential_primary`, `complete=true`, `fresh=true`, and `confirmed=true` in its policy metadata. Rows must explicitly include both `base_component` (always 0.0) and `external_component` (equals weight). Any deviation raises `VectorError` and aborts the tick.

**Validator policy pin (`confidential_primary_v1`):** operators who run confidential-primary can pin the thin validator so it applies ONLY this contract:

```bash
# CLI flag or env; default is unpinned (accepts legacy, v3, and confidential_primary)
cathedral-validator serve --require-policy confidential_primary_v1
export CATHEDRAL_VALIDATOR_REQUIRE_POLICY=confidential_primary_v1
```

When pinned, every vector lacking a valid `confidential_primary` v1 policy block is rejected with `VectorError`, and the legacy and v3 fallback mapping paths are unreachable. Validators that do not set the pin keep the existing behavior (all signed shapes accepted).

### Per-Source Tokens (Optional)

```bash
# Dedicated token for a specific source (e.g., cathedral_confidential_tdx)
CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX=<confidential_secret>
# Fallback token when no dedicated token exists
CATHEDRAL_EXTERNAL_SCORES_TOKEN=<shared_token>
```

### Legacy Weights (Used Only if FRACTION Unset)

```bash
CATHEDRAL_EXTERNAL_SCORES_BASE_WEIGHT=1.0
CATHEDRAL_EXTERNAL_SCORES_WEIGHT=1.0
# Effective share = weight / (base_weight + weight)
# Capped at MAX_FRACTION (default 0.5)
# WARNING: This 50% default is almost never correct; always set FRACTION explicitly.
```

---

## Blend Formula

The final score vector is computed as:

```
output[hotkey] = (1 - fraction) * base_norm[hotkey] + fraction * ext_norm[hotkey]
```

Where:
- `base_norm` and `ext_norm` are L1-normalized (sum to 1.0) independently
- Filtering (registration, payability) happens **before** normalization for each mechanism
- When only one mechanism has positive mass, its result is returned (base-only or fail-closed)
- When neither has positive mass, an empty dict is returned (downstream burn handles this)

### Exact Fraction Guarantee

Because filtering happens pre-allocation (not post), the realized external contribution mass is **exactly** the configured fraction (within floating-point tolerance). Post-filter calls (e.g., in `build_signed_vector`) re-apply the same policy for uniform metadata surface but do not change the result.

### Base-Only / External-Only Behavior

| Scenario | Result |
|----------|--------|
| **Base only** (ext empty) | Return base-norm (100% base) |
| **External only** (base empty) | Fail closed to **empty dict** (NOT external) |
| **Neither** (both empty) | Return **empty dict** |
| **Both** (positive mass in each) | Apply blend formula with configured fraction |

---

## Sources

### Allowed Sources

```python
ALLOWED_ENDPOINT_SOURCES = {
    "violet_audio",              # Legacy public scorer
    "cathedral_sat_fast",        # Cathedral's fast-path SAT scoreboard
    "cathedral_confidential_tdx", # Confidential/attested source
}
```

### Confidential Sources (e.g., cathedral_confidential_tdx)

Confidential/attested sources have stricter requirements:

| Requirement | Rationale |
|---|---|
| `complete=true` **mandatory** | Snapshot is the full truth at its epoch; partial reports are meaningless |
| No `external_primary` mode | Always capped-blend; external never reaches 100% |
| Explicit `FRACTION` env var required | Cannot inherit legacy 50% default; must be intentional |
| Per-source dedicated token | Isolates credentials; shared token cannot authorize confidential reports |
| Empty scores list allowed (with complete=true) | Allows source to revoke all miners explicitly |

#### Confidential global v3 blend

For `cathedral_confidential_tdx`, `FRACTION` must be finite and in `(0, 0.10]`;
invalid explicit values stop vector construction. With the release value
`FRACTION=0.10`, the publisher independently L1-normalizes the payable base and
confidential vectors, then builds the final vector over the union of their
hotkeys:

```text
base_component[hotkey]         = 0.90 * base_norm[hotkey]
confidential_component[hotkey] = 0.10 * confidential_norm[hotkey]
weight[hotkey]                 = base_component[hotkey]
                               + confidential_component[hotkey]
```

Compute-only hotkeys may therefore earn confidential mass, and hotkeys present
in both vectors receive the sum of their base and confidential components. Each
signed row carries `base_component`, `external_component`, and their sum as
`weight`; the v3 validator verifies that the signed rows contain exactly 90%
base mass and 10% confidential mass before mapping hotkeys to UIDs.

The boundary behavior is fail-closed:

| Scenario | Result |
|----------|--------|
| **Base only** | Return the L1-normalized base vector (100% base) |
| **Confidential only** (no base) | Return an **empty vector** |
| **Neither** | Return an **empty vector** |
| **Both** | Return the 90% base + 10% confidential union vector |
| **Thin validator lacks any signed hotkey** | Drop all confidential mass and reconstruct a base-only vector from mapped signed base components |
| **Two signed hotkeys map to one UID** | Reject the vector as a duplicate UID |

The fallback reconstruction is all-or-nothing for confidential mass: it does
not retain a partial confidential vector when the thin validator's current
hotkey map is incomplete.

---

## Report Schema

### POST /v1/external-scores/violet

```json
{
  "source": "violet_audio",
  "mechanism": "violet_audio",
  "netuid": 49,
  "epoch": 12345,
  "complete": true,
  "generated_at": "2026-06-29T12:00:00.000Z",
  "scores": [
    {
      "uid": 48,
      "miner_hotkey": "5Gx...",
      "score": 0.82,
      "quality": 0.88,
      "validity": 1.0,
      "tasks_scored": 12,
      "confidence": 0.91
    }
  ]
}
```

### Field Requirements

| Field | Type | Required | Notes |
|---|---|---|---|
| `source` | string | Yes | Must be in `ALLOWED_ENDPOINT_SOURCES` |
| `mechanism` | string | No | Defaults to `source`; metadata only |
| `epoch` | int | Yes | Must be >= stored epoch for this source; conflicts at same epoch rejected |
| `generated_at` | ISO-8601 | Yes | Checked against `MAX_REPORT_AGE_SECS` and `MAX_REPORT_FUTURE_SECS` |
| `complete` | bool | Depends | **Required for confidential sources**; optional for others (defaults to null/incomplete) |
| `netuid` | int | No | Stored in report, not validated |
| `scores` | list | Yes | Must be non-empty UNLESS `complete=true` (empty revokes all) |

### Score Entry Requirements

| Field | Type | Required | Notes |
|---|---|---|---|
| `miner_hotkey` | string | Yes | Must be a valid hotkey |
| `score` | float | Yes | Finite 0.0..1.0; normalized internally; 0.0 explicit revoke |
| `uid` | int | No | Metadata; not used in blend |
| Other | any | No | Stored in metadata; not used in composition |

---

## Idempotency and Freshness

### Epoch Semantics

- Epochs must be **monotonically increasing** per source
- Identical retry (same epoch + digest) is **idempotent**; returns `"idempotent": true` in response
- Older epoch rejected as `epoch_too_old`
- Conflicting epoch (same number, different digest) rejected as `epoch_conflict`

### Freshness Gates

Reports are rejected if:
- `generated_at` is older than `MAX_REPORT_AGE_SECS` (default 3600s) → `report_too_old`
- `generated_at` is more than `MAX_REPORT_FUTURE_SECS` ahead (default 120s) → `report_in_future`

Blend composition (in `compose_scores` call) also checks freshness against the window:
- If latest snapshot's `generated_at` is older than the composition window, it is not used (fail-closed to base)
- Status API reflects `latest_fresh` boolean for observability

### Snapshot Metadata

The status API (`GET /v1/validator/weights/next` or `status()` call) reports:

```json
{
  "latest_report_id": "...",
  "latest_epoch": 12,
  "latest_generated_at": "2026-06-29T12:00:00.000Z",
  "latest_received_at": "2026-06-29T12:00:05.000Z",
  "latest_score_count": 128,
  "latest_report_sha256": "...",
  "latest_complete": true,           // NEW: is latest report complete?
  "latest_fresh": true,              // NEW: is latest report within cutoff?
  "active_score_count": 128          // NEW: only counted from latest complete+fresh report
}
```

`active_score_count` is **zero** if:
- No report exists for the source
- Latest report is incomplete (complete != true)
- Latest report is stale (generated_at <= cutoff)
- Latest report is empty (complete=true, scores=[])

---

## Real-Money Safety Checklist

**Before enabling on mainnet:**

- [ ] Set `CATHEDRAL_EXTERNAL_SCORES_FRACTION` explicitly (never rely on the legacy 50% default)
- [ ] Verify `FRACTION` <= `MAX_FRACTION` (hard cap, default 50%)
- [ ] Enable `CATHEDRAL_EXTERNAL_SCORES_REQUIRE_REGISTERED=1` (fail-closed registration gate)
- [ ] Enable `CATHEDRAL_WEIGHTS_PAYABLE_HOTKEYS=filter` (consistent payability policy)
- [ ] Secure the token(s): `CATHEDRAL_EXTERNAL_SCORES_TOKEN` and per-source tokens
  - Rotate periodically
  - Use strong randomness (e.g., `openssl rand -hex 32`)
  - Store only in secure credential management (never in code/logs)
- [ ] For confidential sources, verify the dedicated token is not exposed to shared-token holders
- [ ] Monitor status endpoint: `active_score_count`, `latest_fresh`, `latest_complete`
- [ ] Confirm our validator's stake share before relying on the full external share
  - External slice can be shaved toward network median (other validators do not blend)

---

## Example Configurations

### 10% Violet Audio (Public Scorer)

```bash
CATHEDRAL_EXTERNAL_SCORES_INGEST_ENABLED=1
CATHEDRAL_EXTERNAL_SCORES_TOKEN=<shared_token>
CATHEDRAL_EXTERNAL_SCORES_ENABLED=1
CATHEDRAL_EXTERNAL_SCORES_SOURCE=violet_audio
CATHEDRAL_EXTERNAL_SCORES_FRACTION=0.10
CATHEDRAL_EXTERNAL_SCORES_REQUIRE_REGISTERED=1
CATHEDRAL_WEIGHTS_PAYABLE_HOTKEYS=filter
CATHEDRAL_EXTERNAL_SCORES_WINDOW_SECS=3600
```

### 5% Cathedral Confidential TDX

```bash
CATHEDRAL_EXTERNAL_SCORES_INGEST_ENABLED=1
CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX=<confidential_secret>
CATHEDRAL_EXTERNAL_SCORES_TOKEN=<shared_token_for_violet>
CATHEDRAL_EXTERNAL_SCORES_ENABLED=1
CATHEDRAL_EXTERNAL_SCORES_SOURCE=cathedral_confidential_tdx
CATHEDRAL_EXTERNAL_SCORES_FRACTION=0.05        # REQUIRED (no legacy default)
CATHEDRAL_EXTERNAL_SCORES_REQUIRE_REGISTERED=1
CATHEDRAL_WEIGHTS_PAYABLE_HOTKEYS=filter
CATHEDRAL_EXTERNAL_SCORES_WINDOW_SECS=1800     # Tighter window for attested source
```

### Dual Sources (Advance Technique)

To support two sources simultaneously (e.g., Violet + Confidential), you would:

1. Create two separate blend steps (not yet supported in single composition)
   OR
2. Use two separate validator instances, each blending one source, and reconcile upstream

Current implementation supports one source per validator instance.

---

## Observability

### Policy Metadata in Signed Vector

The `build_signed_vector` response includes blend metadata:

```json
{
  "signed_vector": {...},
  "metadata": {
    "blend": {
      "base_mass": 0.9,
      "external_mass": 0.1,
      "base_miner_count": 256,
      "external_miner_count": 32,
      "blended": true,
      "fraction": 0.1,
      "base_payable_filter": {...},
      "external_payable_filter": {...}
    },
    "external_scores": {
      "enabled": true,
      "source": "violet_audio",
      "mode": "blend",
      "require_registered": true,
      "registered_count": 1024,
      "active_score_count": 256
    }
  }
}
```

### Logs

The publisher logs blending decisions at info level:

```
[weights] external_scores blend: fraction=0.1000 base_mass=0.9000 ext_mass=0.1000 (source=violet_audio, ext_miners=32)
[weights] external_scores: registration snapshot unavailable -> NOT blending external scores (fail-closed)
[weights] external_scores compose failed: ExternalScoreError('report_too_old')
```

---

## Testing

Executable proofs (located in `scaffold/publisher/tests/`):

- `test_public_scorer_blend.py::test_fraction_10pct_base_and_100_external` — Exact 90/10 allocation
- `test_public_scorer_blend.py::test_empty_base_plus_external_fails_closed` — External-only fails closed
- `test_public_scorer_blend.py::test_base_only_gets_all_mass` — Base-only passthrough
- `test_public_scorer_blend.py::test_overlapping_hotkey_sum_of_contributions` — Overlap blend
- `test_public_scorer_blend.py::test_newer_zero_revokes_older_positive` — Epoch revocation
- `test_public_scorer_blend.py::test_random_vectors_external_mass_bounded` — Property: fraction bound
- `test_public_scorer_blend.py::test_payable_filter_pre_allocation_preserves_fraction` — Filtering pre-allocation
- `test_public_scorer_blend.py::test_confidential_tdx_requires_complete_true` — Confidential completeness
- `test_public_scorer_blend.py::test_source_token_authorizes_only_that_source` — Per-source auth
- `test_public_scorer_blend.py::test_external_primary_blocked_for_confidential_source` — No external_primary for confidential
- `test_public_scorer_blend.py::test_status_active_score_count_reflects_latest_snapshot` — Status reflects latest

Legacy gate tests in `test_external_scores_gates.py`:

- `test_registration_gate_drops_unregistered` — Registration filter
- `test_external_primary_requires_confirm` — External primary confirmation
- `test_legacy_weights_capped_at_max_fraction` — Cap enforcement
