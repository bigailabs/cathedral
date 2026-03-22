# Technical Spec: CU-Based Incentive Mechanism

## Why This Change

The Basilica subnet (on Bittensor) currently uses a **delivery-based weight model**: miners only earn emissions when their GPU nodes generate rental revenue. Weights are set proportional to `revenue_usd` from billing API delivery records. The existing implementation lives in `crates/basilica-validator/src/bittensor_core/weight_setter.rs` and `weight_allocation.rs`.

**Problem**: This creates misaligned incentives. Miners aren't rewarded for maintaining available GPU capacity — only for active rentals. If no one rents a miner's GPUs, the miner earns nothing even though they're contributing supply to the network. This discourages miners from staying online during low-demand periods.

**Solution**: Introduce **Compute Units (CUs)** — a time-based availability metric. **1 CU = 1 GPU-hour of validated availability.** Miners earn CUs simply for keeping nodes online and passing validation, regardless of rental status. Revenue sharing from actual rentals (Revenue Units / RUs) is layered on top. A dynamic burn rate adjusts how much of the subnet's alpha emissions are distributed vs. burned, based on the incentive pool's USD-denominated demand relative to emission capacity.

**Branch**: `new-incentive-mechanism`

---

## Background: Current System (Being Replaced)

Understanding the existing system is critical for implementing the new one. Here's what exists today:

### Existing Components to Understand

| Component | File | What It Does |
|---|---|---|
| GPU Categories | `crates/basilica-common/src/types.rs` | `GpuCategory` enum: A100, H100, H200, B200, Other(String) |
| Emission Config | `crates/basilica-validator/src/config/emission.rs` | `EmissionConfig`: burn_percentage, burn_uid, gpu_allocations (weight per category), weight_set_interval_blocks (360) |
| Weight Allocation | `crates/basilica-validator/src/bittensor_core/weight_allocation.rs` | `WeightAllocationEngine`: distributes u16::MAX weight across burn + GPU categories, proportional to revenue |
| Weight Setter | `crates/basilica-validator/src/bittensor_core/weight_setter.rs` | Runs every 360 blocks (~72min). Syncs delivery records from billing API, calls WeightAllocationEngine, submits weights to chain |
| GPU Assignments | `crates/basilica-validator/src/persistence/gpu_assignments.rs` | Tracks GPU UUIDs assigned to (miner_id, node_id) pairs |
| Validation Loop | `crates/basilica-validator/src/miner_prover/` | ~5min scoring loop, full validation ~6hrs, lightweight ~10min |
| Price Fetching | `crates/basilica-validator/src/basilica_api/mod.rs` | `HttpTokenPriceFetcher` → `TokenPriceSnapshot` with `alpha_price_usd`. Uses `PriceCache` with TTL. Fetches from Basilica API |
| Database | `crates/basilica-validator/migrations/` | SQLite. Tables: miner_nodes, gpu_uuid_assignments, miner_gpu_profiles, verification_logs, weight_allocation_history, weight_set_epochs, rentals |
| Rental State | `crates/basilica-validator/src/persistence/entities/rental.rs` | Rental model with status (Provisioning, Active, Restarting, Stopping, Stopped, Failed), cost_per_hour, total_cost, termination_reason |
| Config System | `crates/basilica-validator/src/config/` | TOML-based via figment. Uses `Option` types to enable/disable features (per project convention — no `enabled: bool` flags) |

### Existing Weight Setting Flow (current — being replaced)
```
Billing API → WeightSetter.sync_deliveries_for_epoch()
  → MinerDelivery { miner_hotkey, node_id, gpu_category, revenue_usd }
  → Group by GPU category
  → WeightAllocationEngine.calculate_weight_distribution()
    → burn_weight = total × burn_percentage
    → category_pool = remaining × category_allocation_pct
    → miner_weight = (miner_revenue / category_revenue) × category_pool
  → Submit weights to Bittensor chain
```

---

## Resolved Design Decisions

These were discussed and resolved during spec design:

| Decision | Resolution | Rationale |
|---|---|---|
| Database for CU Ledger | **PostgreSQL** (in basilica-backend) | Already running for API and other services. Centralized, HA, data-resilient. Validators access it via REST API endpoints on basilica-backend (same `BasilicaApiClient` pattern as `/v1/weights/miner-delivery` and `/v1/prices/tokens`). Validators never connect to the DB directly. |
| Database for Availability Log | **Local SQLite** (validator DB) | High-frequency writes during validation. No need for centralization. No network dependency. |
| CU definition | **1 CU = 1 GPU-hour** | CU accrual uses actual elapsed uptime (`elapsed_hours * gpu_count`), not fixed increments, because the cron doesn't run at exact intervals |
| CU generation interval | **~1 hour (hardcoded)** | The generator cron runs roughly every hour. Not configurable — no reason to change it. |
| "Total number of Asking" | **Validator config parameter** | Configured as target GPU counts per category, not fetched from any external API |
| Slashing trigger | **Rental terminated due to node loss** | Triggered when the validator gives up health-checking and marks the rental as deleted/failed. NOT triggered by transient blips — the validator's own decision to abandon the node is the signal. |
| Revenue Units (RU) | **Backend-generated from billing data** | RU rows are generated by basilica-api from actual credit deductions in the billing system (`credit_transactions`). Each telemetry-driven charge during an active rental produces billing events; the backend aggregates these into `ru_ledger` rows. `ru_amount` = actual USD charged. Payout: `vested_fraction * ru_amount * revenue_share_pct / 100` — no per-category dilution. Subject to global emission cap. |
| Alpha price source | **Existing BasilicaApiClient** | Uses `get_token_prices(netuid)` → `TokenPriceSnapshot.alpha_price_usd`. Same provider used throughout infra. No additional providers. |
| Feature toggle | **`Option<IncentiveConfig>`** | Per CLAUDE.md convention: use `Option` to enable/disable. When `None`, legacy delivery-based weights continue unchanged. |

---

## Architecture Overview

```
Miner → makes nodes available
  │
  ▼
Validation Loop (~15min)  [EXISTING — crates/basilica-validator/src/miner_prover/]
  ├── measure uptime
  ├── validate hardware (GPU attestation)
  └── assign GPU category + count
        │
        ▼
  Availability Log (local SQLite, append-only)  ◄── Rent State (from rentals table)
  [NEW — writes one row per node per validation tick]
        │
        ▼
  CU Generator (cron ~1hr)  [NEW]
  ├── reads availability log from local SQLite
  ├── computes actual elapsed uptime per node
  ├── detects state changes (SCD2 row transitions)
  ├── submits CU entries via POST /v1/incentive/cus (basilica-backend API)
  ├── accrues CUs proportional to uptime: elapsed_hours * gpu_count
  └── detects slash conditions (node completely lost during rental)
        │
        ▼ (via basilica-backend REST API)
  CU Ledger (PostgreSQL, in basilica-backend)  [NEW — centralized, HA, data-resilient]

  Billing System (basilica-billing, existing)
  ├── charges users incrementally per telemetry tick during active rentals
  ├── each charge recorded in credit_transactions with exact USD amount
  └── generates RU rows in ru_ledger from actual billed revenue
        │
        ▼
  RU Ledger (PostgreSQL, in basilica-backend)  [NEW — alongside cu_ledger]

  Both ledgers ──▼── (via basilica-backend REST API)

  Weight Setter (cron ~360 blocks ≈ 72min)  [MODIFIED — crates/basilica-validator/src/bittensor_core/weight_setter.rs]
  ├── fetches CU ledger for epoch window via GET /v1/incentive/cus
  ├── fetches RU ledger for epoch window via GET /v1/incentive/rus
  ├── calculates incentive pool per GPU category (CU dilution)
  ├── computes per-hotkey USD payouts (CU + RU combined)
  ├── fetches alpha price (same API provider as rest of infra)
  ├── applies emission cap (pro-rata scale-down if demand > capacity)
  ├── derives dynamic burn rate
  └── calls setWeights() on chain

Incentive Pool Inputs (all from validator config):
  ├── Window for payouts (hours)
  ├── Max CU value (USD cap per CU)
  ├── Per-category config: target node count + price (USD/hr)
  ├── Actual GPUs = target_count × 8 (8x variant only)
  └── Total number of Asking (= sum of target_count × 8 across categories)
```

### Data Flow Summary
```
CU path:  Validation Loop → [local SQLite] Availability Log → CU Generator → [REST API] basilica-backend CU Ledger → [REST API] Weight Setter → Bittensor Chain
RU path:  Billing System (telemetry charges) → credit_transactions → basilica-api RU Generator → ru_ledger → [REST API] Weight Setter → Bittensor Chain
```

---

## Module 1: Incentive Configuration

**New file**: `crates/basilica-validator/src/config/incentive.rs`

### Purpose
Holds all configurable parameters for the CU-based incentive pool. Per the project convention in CLAUDE.md, uses `Option` types for enable/disable rather than `enabled: bool` flags.

### Struct Design

```rust
/// Top-level incentive mechanism config.
/// Presence on ValidatorConfig (as Option<IncentiveConfig>) enables the new CU-based mechanism.
/// Absence keeps the legacy delivery-based weights.
pub struct IncentiveConfig {
    /// Per-category GPU configuration: target counts, pricing.
    /// Also determines "Total number of Asking" = SUM(target_count * 8).
    /// NOTE: We only support the 8x variant of each model (8 GPUs per node).
    /// The configured target_count is the number of 8-GPU nodes;
    /// actual GPU count = target_count * 8.
    pub gpu_categories: HashMap<String, GpuCategoryConfig>,

    /// Payout window in hours (e.g. 72.0).
    /// The period over which CU accrual is measured for payout calculations.
    /// NOTE: Snapshotted per CU row at earn time — config changes only affect future CUs.
    pub window_hours: f64,

    /// Max CU value cap in USD per CU.
    /// Absolute ceiling across all categories — prevents any single CU from being worth
    /// more than this regardless of supply/demand or per-category pricing.
    pub max_cu_value_usd: f64,

    /// Revenue share percentage (0.0-100.0).
    /// Percentage of actual rental revenue (from billing credit deductions) that contributes
    /// to miner payout on top of CU rewards. None = RU payout disabled. Some(30.0) = 30% share.
    /// Snapshotted per RU row at generation time — config changes only affect future RU rows.
    pub revenue_share_pct: Option<f64>,

    /// Slash percentage (0.0-100.0).
    /// Percentage of ALL unvested CUs and RUs that are voided on a slash event.
    /// Example: 100.0 = slash all unvested, 50.0 = slash half.
    pub slash_pct: f64,

}

/// Per-GPU-category configuration combining target count and pricing.
/// Each entry represents the 8x variant (8 GPUs per node) — the only form factor we support.
pub struct GpuCategoryConfig {
    /// Target number of 8-GPU nodes the network wants for this category.
    /// Actual GPU count for pool calculations = target_count * 8.
    pub target_count: u32,

    /// Pre-defined price in USD/hour for this GPU category.
    /// Used for pool budget calculations.
    /// NOTE: Snapshotted per CU row at earn time — config changes only affect future CUs.
    pub price_usd: f64,
}
```

### Functional Requirements
- **FR-1**: All pool parameters defined in TOML config, no hardcoded values
- **FR-2**: Config validation rejects: `window_hours <= 0`, `max_cu_value <= 0`, `revenue_share` outside `[0,100]`, `slash_pct` outside `[0,100]`, negative `price_usd`, zero target counts
- **FR-3**: `Option<IncentiveConfig>` on `ValidatorConfig` — presence enables new mechanism, absence keeps legacy delivery-based weights
- **FR-4**: `total_number_of_asking()` helper method returns `SUM(gpu_categories[cat].target_count * 8)` — derived from config, not fetched externally

### Non-Functional Requirements
- **NFR-1**: All values are modular — designed so a future admin interface can set them dynamically without code changes

### Config Example
```toml
[incentive]
window_hours = 72.0
max_cu_value_usd = 0.05
revenue_share_pct = 30.0
slash_pct = 100.0
# No database_url needed — CU ledger lives in basilica-backend's PostgreSQL.
# Validators access it via BasilicaApiClient (same api_endpoint used for all backend calls).

# Each entry is the 8x variant. target_count = number of 8-GPU nodes.
# Actual GPU count for pool math = target_count * 8.
[incentive.gpu_categories]
A100 = { target_count = 4, price_usd = 1.50 }
H100 = { target_count = 3, price_usd = 3.00 }
H200 = { target_count = 2, price_usd = 4.50 }
B200 = { target_count = 1, price_usd = 6.00 }
```

### Files Modified
- `crates/basilica-validator/src/config/mod.rs` — add `pub mod incentive;`
- `crates/basilica-validator/src/config/main_config.rs` — add `pub incentive: Option<IncentiveConfig>` to `ValidatorConfig`

---

## Module 2: Availability Log

**New file**: `crates/basilica-validator/src/persistence/availability_log.rs`
**New SQLite migration**: `crates/basilica-validator/migrations/017_availability_log.sql`

### Purpose
Converges all node availability signals into a single append-only table in the **local validator SQLite database**. The validator currently assesses availability in multiple disconnected places (validation loops, rental health checks, stale node cleanup, node deletion). Instead of each writing to different tables or status fields, they all write to the availability log.

This is the raw data source the CU Generator reads before submitting aggregated CU entries to the centralized CU ledger via the basilica-backend API. The availability log captures "was this node available at this point in time?" — the CU Generator then aggregates these snapshots into time-bounded CU accrual records.

### Database Schema (SQLite — local validator DB)

The availability log is intentionally minimal — it only records **identity + binary availability state**. GPU details (category, count, memory) are available via joins on `miner_nodes` / `gpu_uuid_assignments` / `miner_gpu_profiles` using `node_id` and `hotkey`, so they are not duplicated here.

```sql
CREATE TABLE IF NOT EXISTS availability_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    miner_uid INTEGER NOT NULL,
    hotkey TEXT NOT NULL,
    node_id TEXT NOT NULL,
    is_available INTEGER NOT NULL,       -- 1 = passed validation, 0 = failed
    is_rented INTEGER NOT NULL,          -- 1 = node has active rental
    is_validated INTEGER NOT NULL,       -- 1 = from hardware validation, 0 = from indirect signal
    recorded_at INTEGER NOT NULL,         -- unix epoch seconds
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- TODO: Add indexes after implementation is complete, based on actual query patterns.
-- Candidates to consider:
--   (recorded_at)            — CU Generator range scans
--   (node_id, recorded_at)   — per-node lookups
--   (hotkey, recorded_at)    — per-miner lookups
```

### Functional Requirements
- **FR-1**: Every availability-affecting event inserts one row per node. `is_available` is `1` if the node is available, `0` if unavailable — no score, just binary.
- **FR-2**: `is_rented` derived by checking if node has an active rental in the `rentals` table (or `true` directly for rental health check failures where the rental is known to be active)
- **FR-3**: GPU details (category, count, memory) are NOT stored here — they are resolved at CU generation time by joining on existing tables via `node_id` / `hotkey`
- **FR-4**: Append-only — rows are never updated or deleted (except by retention cleanup)
- **FR-5**: Old rows cleaned up by the existing `CleanupTask` (configurable retention, e.g. 90 days)
- **FR-6**: All writes are fire-and-forget — log warning on failure, never block the calling operation
- **FR-7**: `is_validated` distinguishes entries from actual hardware validation (integration point 1, where GPU attestation occurred) versus indirect signals (integration points 2–5: health check failure, stale cleanup, node deletion, RemoveBid). This enables audit of which availability observations are backed by real validation.

### Integration Points

The availability log is written to from **5 distinct sources**, converging all availability signals into one table:

#### 1. Validation Pass/Fail (primary signal)

The highest-frequency source. Every completed validation (full or lightweight) writes one row per node.

- **File**: `crates/basilica-validator/src/miner_prover/verification.rs`
- **Function**: `store_node_verification_result_with_miner_info()`
- **Where**: After the `success` boolean is determined, before the DB transaction
- **Values**: `is_available = success`, `is_rented` from `active_rental_id` check, `is_validated = true`
- **Frequency**: Every ~5 min per node

#### 2. Rental Health Check Terminal Failure

When the rental health monitor gives up on a node (consecutive failure threshold reached), it records the node as unavailable. This is the signal that feeds slashing (Module 5).

- **File**: `crates/basilica-validator/src/rental/monitoring.rs`
- **Function**: `check_rental_health()`
- **Where**: When consecutive failure threshold is reached, before the rental state transition
- **Values**: `is_available = false`, `is_rented = true`, `is_validated = false`
- **Frequency**: Only on terminal failure (e.g. 3 consecutive), NOT every 30s health check tick
- **Important**: Individual health check failures (1st, 2nd out of 3) are transient and NOT logged

#### 3. Stale Node Cleanup

When the periodic cleanup task marks nodes as offline because they haven't been checked recently.

- **File**: `crates/basilica-validator/src/persistence/gpu_profile_repository.rs`
- **Function**: `cleanup_stale_nodes()`
- **Where**: After nodes are marked offline, batch-write entries for all affected nodes
- **Values**: `is_available = false`, `is_rented = false`, `is_validated = false` (query filters `active_rental_id IS NULL`)
- **Frequency**: Every ~30 min, typically a handful of nodes

#### 4. Consecutive Failure Node Deletion

When nodes are permanently deleted after exceeding the consecutive verification failure threshold.

- **File**: `crates/basilica-validator/src/persistence/miner_nodes.rs`
- **Function**: `cleanup_failed_nodes_after_failures()`
- **Where**: After deletions are committed, batch-write entries for all deleted nodes
- **Values**: `is_available = false`, `is_rented = false`, `is_validated = false`
- **Frequency**: Every ~15 min, typically zero nodes

#### 5. Miner RemoveBid

When a miner explicitly removes their nodes via the RemoveBid RPC.

- **File**: `crates/basilica-validator/src/persistence/miner_nodes.rs`
- **Function**: `remove_registered_nodes()`
- **Where**: After the status UPDATE succeeds, write entries for affected node_ids
- **Values**: `is_available = false`, `is_rented = false`, `is_validated = false`
- **Frequency**: On-demand, rare

### What NOT to Log

| Event | Why |
|---|---|
| `deactivate_missing_bids()` | Bid eligibility change, not availability. Node may still be online and passing validation. |
| `stop_rental()` | User-initiated graceful stop. Node becomes available again — next verification tick captures this naturally. |
| `ensure_not_banned()` | Pre-rental rejection. Administrative decision, not observed unavailability. |
| `ensure_recent_validation()` | Pre-rental staleness check. Node might still be available — just hasn't been validated recently. |
| `deploy_container_or_log_failure()` | Container-level issue, already captured in misbehaviour log. Next verification tick determines true availability. |
| `restart_rental()` failure | Same as deployment failure. Health monitor will detect the resulting unhealthy container and fire Integration Point 2. |

### Non-Functional Requirements
- **NFR-1**: Minimal overhead — single INSERT per event for high-frequency sources (validation, health check), batch INSERT for cleanup operations. Local DB only (no network latency).
- **NFR-2**: Time-indexed for efficient range scans by CU Generator
- **NFR-3**: No external dependency — works even if the basilica-backend API is temporarily unavailable

---

## Module 3: CU Ledger (Per-CU Event Log)

**New file**: `crates/basilica-validator/src/persistence/cu_ledger.rs` (API client)
**New file**: `crates/basilica-validator/src/incentive/vesting.rs`
**Backend migration** (in basilica-backend, NOT the validator): `cu_ledger` table

### Purpose
Per-CU event table in **basilica-backend's PostgreSQL database**. Each row represents a single CU earning event — one row per available node per CU Generator tick (~1 hour). This replaces the SCD2 approach; there are no `valid_from`/`valid_to` temporal columns, no running counters, and no state transitions.

Each CU row is self-contained: it captures the GPU-hours earned (`cu_amount`) at a specific time (`earned_at`), along with the `gpu_category`, `window_hours`, and `price_usd` snapshotted from config at earn time. The CU Generator computes `cu_amount = elapsed_hours * gpu_count` at insert time by resolving GPU info from existing tables (`miner_nodes`, `miner_gpu_profiles`) — `gpu_count` is not stored on the CU row, but `gpu_category` is.

Rows are append-only. The sole mutation is slashing (setting `is_slashed = true`). Linear vesting math is computed in Rust (not SQL) using the `earned_at` timestamp and each row's stored `window_hours`.

This is the central ledger for the new incentive mechanism — it must be highly available, data-resilient, and centralized so it can serve as the single source of truth for CU accrual across the network. **Validators do not connect to this database directly** — they access it via REST API endpoints on basilica-backend, using the same `BasilicaApiClient` + validator signature auth pattern as existing endpoints (`/v1/weights/miner-delivery`, `/v1/prices/tokens`).

### Database Schema (PostgreSQL — in basilica-backend)

```sql
CREATE TABLE IF NOT EXISTS cu_ledger (
    id BIGSERIAL PRIMARY KEY,

    -- Identity (gpu_count resolvable via miner_nodes/miner_gpu_profiles using these keys)
    hotkey TEXT NOT NULL,
    miner_uid INTEGER NOT NULL,
    node_id TEXT NOT NULL,

    -- CU earning details
    cu_amount DOUBLE PRECISION NOT NULL,   -- GPU-hours: elapsed_hours * gpu_count (computed at insert)
    earned_at TIMESTAMPTZ NOT NULL,         -- when this CU was earned

    -- State snapshot at earn time
    is_rented BOOLEAN NOT NULL DEFAULT FALSE,  -- node had active rental when CU was earned
    gpu_category TEXT NOT NULL,                 -- GPU category at earn time (e.g. "H100")
    window_hours DOUBLE PRECISION NOT NULL,    -- vesting window from config at earn time
    price_usd DOUBLE PRECISION NOT NULL,       -- category $/hr from config at earn time

    -- Slashing (only mutable field)
    is_slashed BOOLEAN NOT NULL DEFAULT FALSE,  -- CU voided due to node loss during rental

    -- Crash-safe idempotency: "{node_id}:{tick_ts_seconds}"
    idempotency_key TEXT NOT NULL UNIQUE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- TODO: Add indexes after implementation is complete, based on actual query patterns.
-- Candidates to consider:
--   (hotkey, earned_at) WHERE NOT is_slashed  — weight setter per-hotkey queries
--   (earned_at) WHERE NOT is_slashed           — window range scans
--   (node_id, earned_at)                       — slashing lookups
--   (gpu_category, earned_at) WHERE NOT is_slashed  — per-category dilution queries
```

### Type Definitions

```rust
struct CuLedgerRow {
    id: i64,
    hotkey: String,
    miner_uid: i32,
    node_id: String,
    cu_amount: f64,
    earned_at: DateTime<Utc>,
    is_rented: bool,
    gpu_category: String,
    window_hours: f64,
    price_usd: f64,
}

struct NewCuRow {
    hotkey: String,
    miner_uid: i32,
    node_id: String,
    cu_amount: f64,
    earned_at: DateTime<Utc>,
    is_rented: bool,
    gpu_category: String,          // from miner_gpu_profiles at earn time
    window_hours: f64,             // from incentive_config.window_hours
    price_usd: f64,                // from incentive_config.gpu_categories[cat].price_usd
    idempotency_key: String,
}
```

### Linear Vesting Model

Each CU vests linearly from `earned_at` to `earned_at + window_hours`. During an epoch `[E_start, E_end]`, the vesting engine computes the temporal overlap between each CU's vesting window and the epoch:

```
For a CU row with earned_at = T, cu_amount = A, window = W:

  overlap = max(0, min(T + W, E_end) - max(T, E_start))
  vesting_fraction = overlap / W
  vested_cu = A * vesting_fraction
```

**All vesting math is computed in Rust, not SQL.** The repository fetches raw CU rows via a simple range scan; Rust iterates and applies the overlap formula.

### Vesting Visualization

The overlap formula handles every possible alignment between a CU's vesting window and an epoch. The diagrams below use `window_hours = 72` and an epoch spanning `[50h, 51h]`.

**Case 1: CU fully within vesting window (normal case)**

A CU earned at `T = 10h` vests over `[10h, 82h]`. The epoch `[50h, 51h]` falls entirely inside.

```
      T=10h                                              T+W=82h
      |======================== vesting window ========================|
                                        |==| epoch [50h, 51h]
```

```
overlap = min(82, 51) - max(10, 50) = 51 - 50 = 1h
vesting_fraction = 1 / 72 ≈ 0.0139
vested_cu = A * 0.0139
```

**Case 2: CU earned mid-epoch (partial overlap at start)**

A CU earned at `T = 50.5h` vests over `[50.5h, 122.5h]`. The vesting window starts partway through the epoch.

```
                                           T=50.5h                          T+W=122.5h
                                           |============ vesting window ============|
                                        |==| epoch [50h, 51h]
```

```
overlap = min(122.5, 51) - max(50.5, 50) = 51 - 50.5 = 0.5h
vesting_fraction = 0.5 / 72 ≈ 0.0069
vested_cu = A * 0.0069
```

**Case 3: CU earned after epoch (zero contribution)**

A CU earned at `T = 60h` vests over `[60h, 132h]`. The vesting window starts after the epoch ends.

```
                                                     T=60h                     T+W=132h
                                                     |====== vesting window ======|
                                        |==| epoch [50h, 51h]
```

```
overlap = max(0, min(132, 51) - max(60, 50)) = max(0, 51 - 60) = 0
vesting_fraction = 0
vested_cu = 0
```

**Case 4: CU fully vested before epoch (zero contribution)**

A CU earned at `T = 0h` with a short `window_hours = 24` vests over `[0h, 24h]`. The window closes before the epoch starts.

```
T=0h         T+W=24h
|== vesting ==|
                                        |==| epoch [50h, 51h]
```

```
overlap = max(0, min(24, 51) - max(0, 50)) = max(0, 24 - 50) = 0
vesting_fraction = 0
vested_cu = 0
```

**Case 5: Vesting window entirely contains epoch**

A CU earned at `T = 0h` vests over `[0h, 72h]`. The epoch is a small slice of the full window.

```
T=0h                                                          T+W=72h
|=========================== vesting window ===========================|
                                        |==| epoch [50h, 51h]
```

```
overlap = min(72, 51) - max(0, 50) = 51 - 50 = 1h
vesting_fraction = 1 / 72 ≈ 0.0139
vested_cu = A * 0.0139
```

> Cases 1 and 5 produce the same arithmetic when the epoch fits inside the window. The distinction matters when reasoning about boundary conditions: in Case 1 the CU was earned *after* hour 0, so a shorter window could exclude the epoch entirely (Case 4).

#### Data Flow

1. **API call (validator-side)**: Fetch all non-slashed CU rows whose vesting window overlaps the epoch:
   ```rust
   let cu_rows = cu_ledger_client.get_cus_in_window(epoch_start, epoch_end).await?;
   ```

2. **Rust (vesting engine)**: Iterate rows, compute per-CU vesting, aggregate per-hotkey:
   ```rust
   /// Pure function — no DB dependency. Lives in incentive/vesting.rs.
   fn compute_vested_cus(
       rows: &[CuLedgerRow],
       epoch_start: DateTime<Utc>,
       epoch_end: DateTime<Utc>,
       // window_hours REMOVED — uses row.window_hours
   ) -> HashMap<String, f64> {
       let mut hotkey_vested: HashMap<String, f64> = HashMap::new();

       for row in rows {
           let window_secs = row.window_hours * 3600.0;  // per-row vesting window
           let vest_end = row.earned_at + Duration::seconds(window_secs as i64);
           let overlap_start = row.earned_at.max(epoch_start);
           let overlap_end = vest_end.min(epoch_end);
           let overlap_secs = (overlap_end - overlap_start).num_seconds().max(0) as f64;
           let vesting_fraction = overlap_secs / window_secs;
           let vested_cu = row.cu_amount * vesting_fraction;

           *hotkey_vested.entry(row.hotkey.clone()).or_default() += vested_cu;
       }
       hotkey_vested
   }
   ```

### API Client (validator-side — replaces direct DB access)
```rust
/// Validator-side client for the CU ledger. Wraps BasilicaApiClient to access
/// the cu_ledger table in basilica-backend's PostgreSQL via REST endpoints.
/// Uses the same validator signature auth pattern (X-Validator-Hotkey,
/// X-Validator-Signature, X-Timestamp) as existing endpoints.
pub struct CuLedgerClient {
    api_client: Arc<BasilicaApiClient>,
}

impl CuLedgerClient {
    // CU Generator writes (POST /v1/incentive/cus)
    async fn submit_cus(&self, rows: Vec<NewCuRow>) -> Result<u64>;       // returns number of new rows inserted

    // Slashing (POST /v1/incentive/slash) — backend uses per-row window_hours
    async fn slash_node_cus(&self, node_id: &str, slash_pct: f64) -> Result<u64>; // returns rows slashed

    // Weight setter reads (GET /v1/incentive/cus)
    async fn get_cus_in_window(&self, epoch_start: DateTime<Utc>, epoch_end: DateTime<Utc>) -> Result<Vec<CuLedgerRow>>;
}
```

### Vesting Engine (business logic — pure Rust, no DB)
```rust
// File: crates/basilica-validator/src/incentive/vesting.rs

/// Compute per-hotkey vested CU for the given epoch.
/// Each CU vests linearly from earned_at to earned_at + row.window_hours.
pub fn compute_vested_cus(
    rows: &[CuLedgerRow],
    epoch_start: DateTime<Utc>,
    epoch_end: DateTime<Utc>,
) -> HashMap<String, f64>;

/// Compute total vested CU across all hotkeys (for per-category dilution denominator).
pub fn compute_total_vested_cu(
    rows: &[CuLedgerRow],
    epoch_start: DateTime<Utc>,
    epoch_end: DateTime<Utc>,
) -> f64;
```

### Functional Requirements
- **FR-1**: Each CU earning event is a single INSERT — rows are never modified after creation (except slashing)
- **FR-2**: `cu_amount` is computed at insert time (`elapsed_hours * gpu_count`) and set once — no running counter
- **FR-3**: Slashed CUs (`is_slashed = true`) are excluded from all payout calculations
- **FR-4**: Unavailable nodes simply produce no CU rows — absence implies unavailability
- **FR-5**: `cu_amount` uses actual elapsed time proportional to the CU Generator interval, NOT a fixed increment
- **FR-6**: `idempotency_key` prevents duplicate rows if the CU Generator crashes and re-runs the same tick
- **FR-7**: `gpu_category`, `price_usd`, and `window_hours` are stored on the CU row at earn time (snapshotted from config). `gpu_count` is still resolved from existing tables at generation time and is not stored on the row.

### Backend Implementation Notes

The following SQL details describe what **basilica-backend executes internally** when handling API requests. Validators do not run these queries — they are provided here for the backend team's reference.

**Window query** (used by `GET /v1/incentive/cus`):
```sql
SELECT id, hotkey, miner_uid, node_id,
       cu_amount, earned_at, is_rented,
       gpu_category, window_hours, price_usd
FROM cu_ledger
WHERE NOT is_slashed
  AND earned_at < $2                                         -- earned before epoch ends
  AND earned_at + make_interval(hours => window_hours) > $1  -- per-row vesting window overlaps epoch
ORDER BY hotkey, earned_at;
-- $1 = epoch_start, $2 = epoch_end (no window_hours param)
```

**Batch insert** (used by `POST /v1/incentive/cus`):
```sql
INSERT INTO cu_ledger (hotkey, miner_uid, node_id, cu_amount, earned_at, is_rented, gpu_category, window_hours, price_usd, idempotency_key)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
ON CONFLICT (idempotency_key) DO NOTHING;
```

**Slash query** (used by `POST /v1/incentive/slash`): see Module 5 backend notes.

### Non-Functional Requirements
- **NFR-1**: **Data resilience** — PostgreSQL with WAL, append-only semantics, no destructive operations (backend-side)
- **NFR-2**: **High availability** — centralized PostgreSQL in basilica-backend, accessible by all validators via REST API
- **NFR-3**: **Centralized** — single source of truth for CU data across the network
- **NFR-4**: Retention policy for rows older than configurable threshold (e.g. 180 days) — fully-vested CUs (`earned_at + window_hours < NOW()`) are safe to delete (backend-side cleanup)

---

## Module 3R: RU Ledger (Revenue Unit Event Log)

**Backend table**: `ru_ledger` in basilica-backend's PostgreSQL (alongside `cu_ledger`)

### Purpose

Per-event log of revenue earned by miners during rentals, derived from **actual credit deductions** in the billing system. While CU tracks availability (GPU-hours), RU tracks revenue (USD actually charged to users).

The billing system already charges users incrementally during active rentals — each telemetry heartbeat triggers a credit deduction via `process_telemetry_event()` → `deduct_credits_tx()`. RU rows are generated from these real charges, ensuring that RU only reflects revenue that was actually collected from the user's balance. This is fundamentally different from CU generation: CU is generated by the **validator** (which owns availability data), while RU is generated by the **backend** (which owns billing data).

### RU Generation

RU rows are generated by **basilica-api** (not the validator). The backend has access to the billing `credit_transactions` table, which records every incremental charge with exact USD amounts, `rental_id`, and idempotency keys. The RU generation process:

1. Periodically scans recent rental-related credit deductions from `credit_transactions`
2. For each charge, resolves the miner identity (hotkey, miner_uid, node_id) via the rental record
3. Creates an `ru_ledger` row with `ru_amount` = the actual USD deducted
4. Snapshots `revenue_share_pct` and `window_hours` from the current incentive config

**Key rule**: RU rows are only created for charges where the node was both available and rented. If a rental was terminated due to insufficient credits or node failure, no further RU rows are generated for that rental.

### Key Columns

| Column | Description |
|---|---|
| `hotkey` | Miner's hotkey (identity) |
| `miner_uid` | Miner UID on the subnet |
| `node_id` | Specific node that earned the revenue |
| `ru_amount` | Actual USD charged to the user for this period (from `credit_transactions`) |
| `earned_at` | When the charge occurred |
| `revenue_share_pct` | Snapshotted from config at generation time |
| `window_hours` | Snapshotted from config at generation time |
| `gpu_category` | GPU category of the node |
| `rental_id` | Links to the specific rental that generated this revenue |
| `is_slashed` | Whether this RU has been voided (same semantics as CU slashing) |
| `idempotency_key` | Prevents duplicate RU rows (e.g., `"{node_id}:ru:{charge_event_id}"`) |

### Vesting

Same linear overlap formula as CU (see Module 3 Vesting section). Each RU vests linearly from `earned_at` to `earned_at + window_hours`.

**Payout per row**: `vested_fraction * ru_amount * revenue_share_pct / 100`

This means a miner doesn't receive the full revenue share immediately — it vests over the configured window, creating a collateral-like incentive to remain available.

### Differences from CU Ledger

| Aspect | CU Ledger | RU Ledger |
|---|---|---|
| **What it measures** | GPU-hours of availability | USD of actual revenue |
| **Generated by** | Validator (CU Generator) | Backend (basilica-api, from billing data) |
| **Source data** | Local availability log (SQLite) | Billing `credit_transactions` (PostgreSQL) |
| **Per-category dilution** | Yes (pool budget / supply) | No (direct revenue share) |
| **Payout formula** | `vested_fraction * cu_amount * effective_price` | `vested_fraction * ru_amount * revenue_share_pct / 100` |

### Functional Requirements
- **FR-1**: RU rows are append-only — never modified after creation (except slashing)
- **FR-2**: `ru_amount` reflects actual USD deducted from the user's balance, not an estimate from rental rates
- **FR-3**: Slashed RUs (`is_slashed = true`) are excluded from all payout calculations
- **FR-4**: `idempotency_key` prevents duplicate rows if the RU generation process re-runs
- **FR-5**: `revenue_share_pct` and `window_hours` are snapshotted at generation time — config changes only affect future RU rows

### Non-Functional Requirements
- **NFR-1**: Same data resilience and HA properties as CU Ledger (PostgreSQL, append-only)
- **NFR-2**: Same retention policy as CU Ledger — fully-vested RUs safe to delete after threshold

---

## Module 3.5: Backend CU Ledger API

**Location**: basilica-backend (routes + service logic)

### Purpose
REST API endpoints that basilica-backend must implement to serve the CU ledger data to validators. All endpoints use the existing validator signature auth pattern (`X-Validator-Hotkey`, `X-Validator-Signature`, `X-Timestamp` headers, verified by the backend's auth middleware).

The `cu_ledger` PostgreSQL table and its migration live in basilica-backend (e.g., `basilica-backend/crates/basilica-billing/migrations/`). Detailed backend implementation is outside the scope of this validator spec — this section specifies the API contract.

### Endpoints

#### `POST /v1/incentive/cus` — Submit CU accrual batch

Called by the CU Generator after each tick to submit earned CU rows.

- **Auth**: Validator signature (existing middleware)
- **Request body**:
  ```json
  {
    "cus": [
      {
        "hotkey": "5F...",
        "miner_uid": 42,
        "node_id": "node-abc",
        "cu_amount": 8.0,
        "earned_at": "2025-03-15T10:00:00Z",
        "is_rented": false,
        "gpu_category": "H100",
        "window_hours": 72.0,
        "price_usd": 3.00,
        "idempotency_key": "node-abc:1710500400"
      }
    ]
  }
  ```
- **Response**: `{ "inserted": 5 }` (number of new rows; duplicates silently ignored via idempotency)
- **Backend behavior**: `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING`

#### `POST /v1/incentive/slash` — Slash unvested CUs for a node

Called by the CU Generator when a slash condition is detected (node lost during rental).

- **Auth**: Validator signature (existing middleware)
- **Request body**:
  ```json
  {
    "node_id": "node-abc",
    "slash_pct": 100.0
  }
  ```
- **Response**: `{ "slashed_cu_count": 15, "slashed_ru_count": 8 }`
- **Backend behavior**: Identifies unvested CUs and RUs (non-slashed, per-row `earned_at + window_hours > NOW()`), applies `slash_pct` (newest first in each table), sets `is_slashed = true`

#### `GET /v1/incentive/cus` — Fetch CU rows for weight computation

Called by the Weight Setter at each epoch to fetch CU data for payout calculations.

- **Auth**: Validator signature (existing middleware)
- **Query params**: `epoch_start` (ISO 8601 timestamp), `epoch_end` (ISO 8601 timestamp)
- **Response**: Array of `CuLedgerRow` objects:
  ```json
  [
    {
      "id": 1,
      "hotkey": "5F...",
      "miner_uid": 42,
      "node_id": "node-abc",
      "cu_amount": 8.0,
      "earned_at": "2025-03-15T10:00:00Z",
      "is_rented": false,
      "gpu_category": "H100",
      "window_hours": 72.0,
      "price_usd": 3.00
    }
  ]
  ```
- **Backend behavior**: Runs the window query (non-slashed CUs whose per-row vesting window overlaps the requested range)

---

## Module 3.5R: Backend RU Ledger API

**Location**: basilica-api (routes + service logic, alongside CU Ledger API)

### Purpose
REST API endpoints for the RU ledger. Unlike CU endpoints (where validators POST rows), RU rows are generated internally by the backend from billing data. Validators only need to **read** RU data for weight computation.

### Endpoints

#### `GET /v1/incentive/rus` — Fetch RU rows for weight computation

Called by the Weight Setter at each epoch to fetch RU data for payout calculations.

- **Auth**: Validator signature (existing middleware)
- **Query params**: `epoch_start` (ISO 8601 timestamp), `epoch_end` (ISO 8601 timestamp)
- **Response**: Array of RU ledger row objects (same structure as CU rows but with `ru_amount` and `revenue_share_pct` instead of `cu_amount` and `price_usd`)
- **Backend behavior**: Returns non-slashed RU rows whose per-row vesting window (`earned_at` to `earned_at + window_hours`) overlaps the requested epoch range

#### `POST /v1/incentive/slash` — Extended to cover both CU and RU

The existing slash endpoint (Module 3.5) is extended to slash both CU and RU rows for the target node.

- **Request body**: unchanged — `{ "node_id": "...", "slash_pct": 100.0 }`
- **Response**: `{ "slashed_cu_count": 15, "slashed_ru_count": 8 }` (was `{ "slashed_count": 15 }`)
- **Backend behavior**: Applies `slash_pct` to unvested rows in **both** `cu_ledger` and `ru_ledger`, newest first in each table

### RU Generation (internal — not an external endpoint)

RU rows are generated internally by the backend, not submitted by validators. The generation mechanism reads from the billing system's `credit_transactions` table (which records every incremental rental charge) and creates corresponding `ru_ledger` rows. The exact implementation approach (periodic job, event-driven handler, or on-demand computation) is a backend implementation detail.

---

## Module 4: CU Generator

**New file**: `crates/basilica-validator/src/incentive/cu_generator.rs`

### Purpose
Periodic task (~1 hour) that reads availability log entries from local SQLite since the last run, then submits CU rows to the centralized CU ledger via the basilica-backend API (`POST /v1/incentive/cus`). Uses **actual elapsed uptime** for CU computation since the cron doesn't run at exact 1-hour intervals.

This is the bridge between local validation data and the centralized CU ledger. Unlike the previous SCD2 approach, the generator is a pure append operation — no reads from the ledger before writing, no state change detection, no UPDATE queries.

### Algorithm
```
every ~1 hour (hardcoded 3600s interval, not configurable):
  now = current_time
  elapsed_hours = (now - last_run_ts).as_secs_f64() / 3600.0
  tick_id = now.truncated_to_seconds()

  1. entries = [local SQLite] availability_log WHERE recorded_at > last_run_ts ORDER BY recorded_at
  2. batch = []
     for each distinct (hotkey, node_id) in entries:
     a. latest = most recent availability entry for this node

     b. if latest.is_available:
        (gpu_count, gpu_category) = resolve from miner_nodes/miner_gpu_profiles via node_id
        price_usd = incentive_config.gpu_categories[gpu_category].price_usd
        → accumulate row into batch:
            NewCuRow {
              hotkey, miner_uid, node_id,
              cu_amount: elapsed_hours * gpu_count,
              earned_at: now,
              is_rented: latest.is_rented,
              gpu_category: gpu_category,
              window_hours: incentive_config.window_hours,
              price_usd: price_usd,
              idempotency_key: "{node_id}:{tick_id}",
            }

     c. if NOT latest.is_available:
        → no row added to batch (absence of CU rows = unavailability)

     d. check slash condition (Module 5):
        if rental for this node transitioned to Failed/Stopped with termination_reason
           indicating node loss (validator gave up health-checking):
           → call cu_ledger_client.slash_node_cus(node_id, slash_pct)
             (backend identifies unvested CUs via per-row window_hours and applies slash_pct, newest first)

  3. Submit batch via cu_ledger_client.submit_cus(batch)
     (backend inserts with ON CONFLICT (idempotency_key) DO NOTHING)

  4. last_run_ts = now  (persisted in a control table or validator state)
```

### Functional Requirements
- **FR-1**: Idempotent — `idempotency_key` with UNIQUE constraint and `ON CONFLICT DO NOTHING` prevents duplicate CU rows if generator crashes and re-runs
- **FR-2**: Catch-up — if generator misses a tick, next tick processes all unprocessed availability entries. `elapsed_hours` captures actual time elapsed, producing one larger CU row
- **FR-3**: Rented nodes that are available still earn CUs (renting doesn't stop availability rewards — the miner is providing both availability AND active compute)
- **FR-4**: Generator only spawned when `config.incentive.is_some()`
- **FR-5**: CU amount is proportional to real time: `elapsed_hours * gpu_count`, NOT a fixed increment per tick
- **FR-6**: `gpu_category`, `price_usd`, and `window_hours` are snapshotted on the CU row at earn time. `gpu_count` is resolved from existing tables at generation time and not stored on the row.

### Non-Functional Requirements
- **NFR-1**: Tolerates API connectivity blips — retries with exponential backoff
- **NFR-2**: Logs each tick at INFO level: nodes processed, CUs accrued, slashes detected, elapsed time

### Scheduling
Spawned as a tokio task in `service.rs` alongside existing `scoring_task` and `weight_setter_task`. Only spawned when `config.incentive.is_some()`.

---

## Module 5: Slashing

**New file**: `crates/basilica-validator/src/incentive/slashing.rs`

### Purpose
Detect nodes that have become completely inaccessible during an active rental and slash a configurable percentage of ALL their unvested CUs and RUs. "Unvested" means any CU/RU whose vesting window has not yet fully elapsed — i.e., `earned_at + window_hours > NOW()`. Slashed CUs and RUs are excluded from payout calculations, effectively penalizing the miner's accumulated collateral pool.

### Important: What Slashing Is NOT
- NOT triggered by one-off health check failures (transient network issues)
- NOT triggered by user-initiated rental stops

### Trigger
A node is slashed when:
1. It has an **active rental** (`is_rented = true`)
2. The **validator decides the node is permanently lost** and stops health-checking it
3. This causes the rental to transition to `Failed`/`Stopped` with a `termination_reason` indicating node loss

Slashing is driven entirely by the rental lifecycle — when the validator gives up on a node and marks the rental as deleted/failed, that event triggers the slash. There is no separate consecutive-failure counter; the validator's own decision to abandon health checks is the authoritative signal.

### Detection Logic
Integrated into the CU Generator tick (Module 4, step 2d):

1. **Check rental termination events** (validator-side): Query local rentals that transitioned to `Failed`/`Stopped` with termination reason indicating node loss since the last CU Generator tick
2. **Call slash API** (validator-side): `cu_ledger_client.slash_node_cus(node_id, slash_pct)` — the backend handles identifying unvested CUs via per-row `window_hours` and marking them
3. **Recovery**: Automatic — when a node comes back online, the CU Generator simply starts submitting new CU rows with `is_slashed = false`. No "unslash" of previously slashed CUs. Once slashed, those CUs are void.

### Backend Implementation Notes

The following SQL describes what **basilica-backend executes internally** when handling the `POST /v1/incentive/slash` request. Validators do not run these queries. The slash operation applies to **both** `cu_ledger` and `ru_ledger`.

**Identify unvested CUs**: All non-slashed CU rows for the node still within their per-row vesting window:
```sql
SELECT id, cu_amount, earned_at FROM cu_ledger
WHERE node_id = $1 AND NOT is_slashed
  AND earned_at + make_interval(hours => window_hours) > NOW();
```

**Identify unvested RUs**: Same pattern for RU rows:
```sql
SELECT id, ru_amount, earned_at FROM ru_ledger
WHERE node_id = $1 AND NOT is_slashed
  AND earned_at + make_interval(hours => window_hours) > NOW();
```

**Apply slash_pct**: Mark `slash_pct`% of the unvested rows as slashed in both tables. When `slash_pct = 100`, all unvested CUs and RUs are slashed. For partial slashing (`slash_pct < 100`), slash the most recent rows first (newest unvested rows have the most remaining collateral value):
```sql
-- CU slash
UPDATE cu_ledger SET is_slashed = true
WHERE id IN (
  SELECT id FROM cu_ledger
  WHERE node_id = $1 AND NOT is_slashed
    AND earned_at + make_interval(hours => window_hours) > NOW()
  ORDER BY earned_at DESC
  LIMIT $2  -- number of rows to slash, computed from slash_pct
);

-- RU slash (same logic, separate table)
UPDATE ru_ledger SET is_slashed = true
WHERE id IN (
  SELECT id FROM ru_ledger
  WHERE node_id = $1 AND NOT is_slashed
    AND earned_at + make_interval(hours => window_hours) > NOW()
  ORDER BY earned_at DESC
  LIMIT $3  -- number of rows to slash, computed from slash_pct
);
```

### Functional Requirements
- **FR-1**: Slashing only triggers when a rental is terminated/deleted due to node loss — the validator's decision to stop health-checking is the authoritative signal
- **FR-2**: Slashing affects a configurable percentage (`slash_pct`) of ALL unvested CUs for the node — not limited to the rental period
- **FR-3**: When `slash_pct < 100`, the most recent (newest) CU rows are slashed first, since they have the highest unvested value
- **FR-4**: Recovery is automatic: new CU rows created after recovery have `is_slashed = false`. Previously slashed CUs remain void.
- **FR-5**: Slashed CUs excluded from all payout calculations in the incentive pool
- **FR-6**: Slash state visible in CU Ledger for audit trail (the `is_slashed` column on rows)
- **FR-7**: The same `slash_pct` applies to unvested RUs — the slash API call (Module 3.5R) slashes both `cu_ledger` and `ru_ledger` in a single operation, newest rows first in each table

### Non-Functional Requirements
- **NFR-1**: Zero false positives from transient issues — requires sustained unavailability before slashing

---

## Module 6: Incentive Pool & Weight Setting

**New file**: `crates/basilica-validator/src/incentive/incentive_pool.rs`
**Modified file**: `crates/basilica-validator/src/bittensor_core/weight_setter.rs`

### Purpose
The core payout engine. Computes per-hotkey USD payouts from CU ledger data + delivery revenue, then converts to Bittensor chain weights with a dynamically calculated burn rate. This replaces the delivery-based `WeightAllocationEngine` when the new incentive config is present.

### Formulas

These formulas implement the incentive pool math shown in the architecture diagram.

**Step 1 — Pool Budget (per GPU category, from current config)**
```
// Pool budget represents what the network WANTS to pay going forward.
// Uses CURRENT config values (target capacity + price), not per-row values.
For each gpu_category cat in incentive_config.gpu_categories:
  target_gpus[cat] = gpu_categories[cat].target_count * 8  // 8x = 8 GPUs per node
  pool_budget[cat] = target_gpus[cat] * window_hours * gpu_categories[cat].price_usd
  // Example (H100): 24 GPUs * 72 hrs * $3.00/hr = $5,184
  // Example (A100): 32 GPUs * 72 hrs * $1.50/hr = $3,456

total_pool_budget = SUM(pool_budget[cat]) across all categories
```

**Step 2 — Per-Category Dilution**
```
// Fetch raw CU rows via API (backend uses per-row window_hours for overlap):
cu_rows = cu_ledger_client.get_cus_in_window(epoch_start, epoch_end)

// Group CU supply by gpu_category (from per-row stored value):
For each gpu_category cat:
  cat_cu_in_window[cat] = SUM(row.cu_amount) WHERE row.gpu_category = cat AND NOT is_slashed
  per_cu_budget[cat] = pool_budget[cat] / cat_cu_in_window[cat]

  // Under-provisioned: per_cu_budget > price_usd → capped at stored price
  // Over-provisioned:  per_cu_budget < price_usd → diluted
```
- **Per-category independence**: Oversupply of H100s dilutes only H100 payouts, not A100. Each GPU category is its own dilution pool.
- **Window-scoped denominator**: `cat_cu_in_window` is the raw sum of CU amounts per category in the window (not epoch-scoped vesting fractions). This keeps the denominator at the same scale as `pool_budget`, so dilution kicks in correctly when supply exceeds target. Per-CU vesting overlap is used separately in Step 4 for per-epoch miner payouts.
- This creates a natural market dynamic: if few miners of a category are online, each one earns more per GPU-hour for that category.

**Step 3 — Revenue Reward (RU)**
```
// Fetch RU rows from backend (generated from actual billing credit deductions):
ru_rows = ru_ledger_client.get_rus_in_window(epoch_start, epoch_end)

// Compute vested RU payout per hotkey using same overlap formula as CU:
For each RU row i:
  vested_fraction_i = overlap(row_i.earned_at, row_i.window_hours, epoch_start, epoch_end) / row_i.window_hours
  ru_payout_i = vested_fraction_i * row_i.ru_amount * row_i.revenue_share_pct / 100

// Aggregate per hotkey:
For each hotkey:
  revenue_reward[hotkey] = SUM(ru_payout_i for RU rows belonging to hotkey)

// No per-category dilution for RU — it's a direct share of actual revenue.
// ru_amount already reflects real USD charged to users (from billing system).
```

**Step 4 — Per-Row Effective Price & Miner Payout**
```
// Vesting math computed in Rust — uses per-row window_hours
miner_vested_cu = vesting::compute_vested_cus(cu_rows, epoch_start, epoch_end)
// miner_vested_cu is a HashMap<hotkey, f64> with each hotkey's vested CU for this epoch

// Per-row payout uses a three-way MIN:
For each CU row i:
  cat = row_i.gpu_category
  effective_price_i = MIN(row_i.price_usd, per_cu_budget[cat], max_cu_value_usd)
  row_payout_i = vested_fraction_i * row_i.cu_amount * effective_price_i

// Aggregate per hotkey:
For each hotkey:
  miner_usd_per_epoch[hotkey] = SUM(row_payout_i for rows belonging to hotkey) + revenue_reward[hotkey]

// Three-way MIN explained:
//   row.price_usd:       never pay more than what was promised at earn time
//   per_cu_budget[cat]:   dilution when category supply > target
//   max_cu_value_usd:     absolute global ceiling (safety net)
//
// NOTE: No separate prorate needed — the vesting overlap formula already computes
// the epoch-specific fraction for each CU based on its earned_at and row.window_hours.
```

**Step 5 — Weight Conversion (Emission Cap + Dynamic Burn)**
```
usd_required_epoch = SUM(miner_usd_per_epoch[hotkey]) across all hotkeys

// How much USD can the subnet's alpha emissions cover?
alpha_emission_per_epoch = subnet_emission_rate  // from Bittensor metagraph chain data
alpha_price_usd = TokenPriceSnapshot.alpha_price_usd  // from existing BasilicaApiClient
usd_emission_capacity = alpha_emission_per_epoch * alpha_price_usd

// Emission cap: when CU + RU demand exceeds what emissions can cover,
// scale ALL payouts down proportionally (uniform reduction, no CU vs RU priority)
if usd_required_epoch > usd_emission_capacity:
    scale_factor = usd_emission_capacity / usd_required_epoch  // always < 1.0
    For each hotkey:
        miner_usd_per_epoch[hotkey] *= scale_factor
    usd_required_epoch = usd_emission_capacity
    burn_rate = 0.0  // all emissions distributed, nothing to burn
else:
    // Dynamic burn: incentive pool demands less than emissions can cover, burn the excess
    burn_rate = 1.0 - (usd_required_epoch / usd_emission_capacity)
    burn_rate = clamp(burn_rate, 0.0, 0.99)  // never burn 100%, always leave some for miners

// Per-hotkey weight (normalized to u16 space for Bittensor)
For each hotkey:
  miner_weight[hotkey] = miner_usd_per_epoch[hotkey] / usd_emission_capacity
  weight_u16[hotkey] = round(miner_weight[hotkey] * u16::MAX)
```

**Emission cap properties:**
- The scale-down is **uniform** — every miner's total (CU + RU) is multiplied by the same factor
- Preserves relative ordering: a miner with higher combined payout still gets a higher weight
- No priority between CU vs RU payouts — both scale equally
- CU per-category dilution (Step 2) still applies *before* this cap; the emission cap is a global ceiling applied *after* all USD payouts are computed

### Integration into WeightSetter

The existing `WeightSetter` struct in `weight_setter.rs` gains:
- `incentive_config: Option<IncentiveConfig>` field
- `cu_ledger_client: Option<CuLedgerClient>` field
- `ru_ledger_client: Option<RuLedgerClient>` field

In `attempt_weight_setting()`, add a branch:

```rust
if let Some(ref incentive_config) = self.incentive_config {
    // NEW PATH: CU-based incentive pool
    let pool = IncentivePool::new(
        incentive_config,
        self.cu_ledger_client.as_ref().unwrap(),
        &self.api_client,
    );
    let result = pool.calculate_epoch_payouts(
        epoch.period_start,
        epoch.period_end,
        self.config.netuid,
    ).await?;
    // result.weights: Vec<NormalizedWeight>
    // result.burn_rate: f64
    // result.burn_allocation: BurnAllocation
    self.submit_weights_to_chain_with_retry(result.weights, version_key).await?;
} else {
    // LEGACY PATH: delivery-based weights (unchanged)
    let deliveries = self.sync_deliveries_for_epoch(...).await?;
    let distribution = self.weight_allocation_engine.calculate_weight_distribution(miners_by_category)?;
    self.submit_weights_to_chain_with_retry(distribution.weights, version_key).await?;
}
```

### Alpha Price Source
Uses existing `BasilicaApiClient::get_token_prices(netuid)` → `TokenPriceSnapshot.alpha_price_usd`. This is the same API provider used throughout the infrastructure. No additional price providers are introduced (requirement: avoid multiple providers to prevent price fluctuation issues).

### Functional Requirements
- **FR-1**: Dynamic burn rate derived from the formula above, not statically configured like the current `burn_percentage`
- **FR-2**: Burn rate clamped to `[0.0, 0.99]` — never burn everything
- **FR-3**: When `total_vested_cu = 0`, set all weights to burn (guards against division by zero)
- **FR-4**: Revenue share (RU) component is additive on top of CU-based availability rewards — see Step 3 (RU vesting) and Step 4 (CU + RU combination)
- **FR-5**: Per-epoch payout uses linear vesting — the overlap formula computes epoch-specific fractions per CU, no separate prorate step
- **FR-6**: "Total number of Asking" derived from `SUM(gpu_categories[cat].target_count * 8)` in validator config

### Non-Functional Requirements
- **NFR-1**: Calculation completes within 30 seconds for up to 500 miners
- **NFR-2**: All intermediate values logged at INFO level for auditability (effective CU value, burn rate, per-hotkey payouts)
- **NFR-3**: Result struct includes full breakdown for debugging (not just final weights)

---

## GPU Categories

### Current Categories (no code changes needed)
The existing `GpuCategory` enum in `crates/basilica-common/src/types.rs` supports:
- **A100** — NVIDIA A100 (high-end training & inference)
- **H100** — NVIDIA H100 (flagship AI training & inference)
- **H200** — NVIDIA H200 (high-memory AI training & inference)
- **B200** — NVIDIA B200 (next-gen AI acceleration)
- **Other(String)** — catch-all (never rewarded)

**Important: 8x variants only.** We only support the 8-GPU variant of each model. Every node in the network runs 8 GPUs of the same category. This means:
- `target_count` in config = number of 8-GPU nodes
- Actual GPU count for pool math = `target_count * 8`
- CU accrual uses the node's actual `gpu_count` (always 8 for supported categories)

The TOML config system already supports adding/removing categories without code changes. New GPU categories only require a config update, not a code deployment. The `GpuCategory` enum's `Other(String)` variant handles unknown GPUs gracefully — they're tracked but never receive emissions.

---

## File Organization

### New Files
```
crates/basilica-validator/
  src/
    config/incentive.rs                    # IncentiveConfig, GpuCategoryConfig, validation
    incentive/
      mod.rs                               # Module root, re-exports
      cu_generator.rs                      # CU Generator periodic task (~1hr)
      vesting.rs                           # Linear vesting math (pure Rust, no DB dependency)
      incentive_pool.rs                    # Pool math and weight conversion
      slashing.rs                          # Slash detection (node-loss heuristic)
    persistence/
      availability_log.rs                  # Availability log SQLite operations (local)
      cu_ledger.rs                         # CuLedgerClient — API client wrapping BasilicaApiClient for CU ledger operations
      ru_ledger.rs                         # RuLedgerClient — API client for RU ledger (read-only from validator side)
  migrations/
    017_availability_log.sql               # SQLite migration (local validator DB)
    # NOTE: cu_ledger and ru_ledger table migrations live in basilica-backend
    # (e.g., basilica-backend/crates/basilica-billing/migrations/).
    # The backend also needs:
    #   - Handler code (routes, service logic) for /v1/incentive/* endpoints
    #   - RU generation logic (reads credit_transactions, writes ru_ledger)
    # Detailed backend implementation is outside the scope of this validator spec.
```

### Modified Files
```
crates/basilica-validator/src/
  config/mod.rs                            # pub mod incentive
  config/main_config.rs                    # Add incentive: Option<IncentiveConfig> to ValidatorConfig
  persistence/mod.rs                       # pub mod availability_log, cu_ledger, ru_ledger
  bittensor_core/weight_setter.rs          # Add incentive_config field, branch in attempt_weight_setting(), wire RuLedgerClient
  service.rs                               # Spawn CU Generator task, init CuLedgerClient + RuLedgerClient, wire dependencies
  miner_prover/verification.rs             # Add record_availability_event() call after validation
  lib.rs                                   # pub mod incentive
```

---

## Implementation Phases

### Phase 1: Foundation (config + schema + persistence)
1. Create `config/incentive.rs` — `IncentiveConfig` + `GpuCategoryConfig` + validation logic
2. Wire into `config/mod.rs` and `main_config.rs`
3. Create SQLite migration `017_availability_log.sql` (local validator DB)
4. Coordinate with basilica-backend team to add the `cu_ledger` and `ru_ledger` table migrations and API endpoints (`POST /v1/incentive/cus`, `POST /v1/incentive/slash`, `GET /v1/incentive/cus`, `GET /v1/incentive/rus`)
5. Create `persistence/availability_log.rs` — local SQLite write/read operations
6. Create `persistence/cu_ledger.rs` — API client wrapping `BasilicaApiClient` for CU ledger operations
7. Create `persistence/ru_ledger.rs` — API client for RU ledger (read-only: `get_rus_in_window`)

### Phase 2: Data Capture
8. Create `incentive/mod.rs` — module declaration and re-exports
9. Integrate availability logging into verification workflow (`verification.rs`) — writes to local SQLite after each validation
10. Create `incentive/slashing.rs` — slash detection logic (flip `is_slashed` on CU and RU rows for node-loss during rental)
11. Create `incentive/cu_generator.rs` — reads local SQLite availability log, submits CU rows via basilica-backend API (pure append)
12. Spawn CU Generator in `service.rs` (gated on `config.incentive.is_some()`)
13. Backend: Implement RU generation in basilica-api — reads billing `credit_transactions` for rental charges, generates `ru_ledger` rows

### Phase 3: Payout & Weights
14. Create `incentive/vesting.rs` — linear vesting math for both CU and RU (pure Rust, no DB dependency)
15. Create `incentive/incentive_pool.rs` — full payout math (pool budget → CU dilution → RU revenue share → emission cap → weight conversion)
16. Modify `weight_setter.rs` — add `incentive_config` field, wire `RuLedgerClient`, branch to incentive pool path in `attempt_weight_setting()`
17. Wire dependencies in `service.rs` (CuLedgerClient + RuLedgerClient → WeightSetter)

### Phase 4: Testing & Observability
18. Unit tests: config validation, vesting math (CU + RU), payout calculations with known inputs, emission cap edge cases
19. Integration test: `tests/incentive_e2e.rs` — full cycle from mock availability data through weight calculation (including RU)
20. Add metrics: `cu_earned_total`, `ru_earned_total`, `slashed_cu_total`, `slashed_ru_total`, `burn_rate_gauge`, `scale_factor_gauge`, `per_cu_budget_gauge` (per category)

---

## Verification Plan

1. **Unit tests per module**: Config validation, linear vesting math (CU + RU), pool math, slash detection — all with known inputs and expected outputs
2. **Integration test**: `tests/incentive_e2e.rs` — full cycle from mock availability data → CU generation → RU integration → payout calculation → weight normalization
3. **Vesting correctness**: Test CUs/RUs expiring mid-epoch, earned mid-epoch, steady-state convergence with flat prorate, window boundary handling
4. **Payout math edge cases**: zero miners, single miner, all slashed, alpha price = 0, total_cu = 0 (division by zero), generator catch-up after missed ticks, burn rate clamping, emission cap scale-down when CU+RU demand exceeds capacity
5. **Regression**: Verify legacy delivery-based path continues unchanged when `incentive = None`
6. **API connectivity**: Verify CU Generator handles temporary API outages gracefully (retry with backoff)
7. **CuLedgerClient unit tests**: Mock API responses for unit testing the CuLedgerClient (submit_cus, slash_node_cus, get_cus_in_window)
8. **RuLedgerClient unit tests**: Mock API responses for unit testing the RuLedgerClient (get_rus_in_window)
9. **Emission cap verification**: When all miners are rented at high rates, verify the scale-down formula produces correct weights (sum of all weights ≤ 1.0, relative ordering preserved)

---

## Remaining TBD

1. **Initial parameter values**: Specific starting values for `window_hours`, `max_cu_value_usd`, `revenue_share_pct`, `target_count` per category, `gpu_prices_usd` per category need to be determined during testing/simulation.
2. **RU generation implementation detail**: The exact backend mechanism for generating RU rows from `credit_transactions` (periodic job vs event-driven handler) needs to be determined during basilica-api implementation.
