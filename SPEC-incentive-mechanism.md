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
| Database for CU Ledger | **PostgreSQL** (existing server) | Already running for API and other services. Centralized, HA, data-resilient. |
| Database for Availability Log | **Local SQLite** (validator DB) | High-frequency writes during validation. No need for centralization. No network dependency. |
| CU definition | **1 CU = 1 GPU-hour** | CU accrual uses actual elapsed uptime (`elapsed_hours * gpu_count`), not fixed increments, because the cron doesn't run at exact intervals |
| CU generation interval | **~1 hour (hardcoded)** | The generator cron runs roughly every hour. Not configurable — no reason to change it. |
| "Total number of Asking" | **Validator config parameter** | Configured as target GPU counts per category, not fetched from any external API |
| Slashing trigger | **Rental terminated due to node loss** | Triggered when the validator gives up health-checking and marks the rental as deleted/failed. NOT triggered by transient blips — the validator's own decision to abandon the node is the signal. |
| Revenue Units (RU) | **TBD** | How to fetch revenue data from the API is still being designed. Will apply linear payout similar to CUs. |
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
  ├── writes CU entries to centralized PostgreSQL
  ├── accrues CUs proportional to uptime: elapsed_hours * gpu_count
  └── detects slash conditions (node completely lost during rental)
        │
        ▼
  CU Ledger (PostgreSQL, SCD2 table)  [NEW — centralized, HA, data-resilient]
        │
        ▼
  Weight Setter (cron ~360 blocks ≈ 72min)  [MODIFIED — crates/basilica-validator/src/bittensor_core/weight_setter.rs]
  ├── reads CU ledger for epoch window
  ├── reads delivery data for revenue share (RU — TBD)
  ├── calculates incentive pool per GPU category
  ├── computes per-hotkey USD payouts
  ├── fetches alpha price (same API provider as rest of infra)
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
Validation Loop → [local SQLite] Availability Log → CU Generator → [PostgreSQL] CU Ledger → Weight Setter → Bittensor Chain
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
    pub window_hours: f64,

    /// Max CU value cap in USD per CU.
    /// Prevents any single CU from being worth more than this regardless of supply/demand.
    pub max_cu_value_usd: f64,

    /// Revenue share percentage (0.0-100.0).
    /// Percentage of rental revenue that contributes to miner payout on top of CU rewards.
    /// TBD integration — leave as Option for now.
    pub revenue_share_pct: Option<f64>,

    /// PostgreSQL connection string for the centralized CU ledger.
    pub database_url: String,
}

/// Per-GPU-category configuration combining target count and pricing.
/// Each entry represents the 8x variant (8 GPUs per node) — the only form factor we support.
pub struct GpuCategoryConfig {
    /// Target number of 8-GPU nodes the network wants for this category.
    /// Actual GPU count for pool calculations = target_count * 8.
    pub target_count: u32,

    /// Pre-defined price in USD/hour for this GPU category.
    /// Used for pool budget calculations.
    pub price_usd: f64,
}
```

### Functional Requirements
- **FR-1**: All pool parameters defined in TOML config, no hardcoded values
- **FR-2**: Config validation rejects: `window_hours <= 0`, `max_cu_value <= 0`, `revenue_share` outside `[0,100]`, negative `price_usd`, zero target counts, empty `database_url`
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
database_url = "postgresql://user:pass@host:5432/basilica"

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
Extends the existing validation flow to record per-node availability events in the **local validator SQLite database**. This is the raw data source the CU Generator reads before writing aggregated CU entries to the centralized PostgreSQL ledger.

The availability log captures "was this node available at this point in time?" — the CU Generator then aggregates these snapshots into time-bounded CU accrual records.

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
    recorded_at INTEGER NOT NULL,         -- unix epoch seconds
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_avail_log_recorded ON availability_log(recorded_at);
CREATE INDEX IF NOT EXISTS idx_avail_log_node_time ON availability_log(node_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_avail_log_hotkey_time ON availability_log(hotkey, recorded_at);
```

### Functional Requirements
- **FR-1**: Every completed validation (pass or fail) inserts one row per node. `is_available` is `1` if validation passed, `0` if it failed — no score, just binary.
- **FR-2**: `is_rented` derived by checking if node has an active rental in the `rentals` table
- **FR-3**: GPU details (category, count, memory) are NOT stored here — they are resolved at CU generation time by joining on existing tables via `node_id` / `hotkey`
- **FR-4**: Append-only — rows are never updated or deleted (except by retention cleanup)
- **FR-5**: Old rows cleaned up by the existing `CleanupTask` (configurable retention, e.g. 90 days)

### Integration Point
In `VerificationEngine::execute_verification_workflow()` (file: `crates/basilica-validator/src/miner_prover/verification.rs`), after the existing `create_verification_log()` call, add `record_availability_event()` that writes to local SQLite.

### Non-Functional Requirements
- **NFR-1**: Minimal overhead — single INSERT per node per validation tick, local DB (no network latency)
- **NFR-2**: Time-indexed for efficient range scans by CU Generator
- **NFR-3**: No external dependency — works even if PostgreSQL is temporarily unavailable

---

## Module 3: CU Ledger (SCD2)

**New file**: `crates/basilica-validator/src/persistence/cu_ledger.rs`
**New PostgreSQL migration**: `018_cu_ledger.sql`

### Purpose
Slowly Changing Dimension Type 2 (SCD2) table in the centralized PostgreSQL database. Each row represents a time-bounded state for a (hotkey, node_id) pair. When a node's state changes (e.g., goes offline, gets rented, gets slashed), the current row is closed (`valid_to` set) and a new row is opened.

This is the central ledger for the new incentive mechanism — it must be highly available, data-resilient, and centralized so it can serve as the single source of truth for CU accrual across the network.

**SCD2 reference**: [Dimensional Modeling - Slowly Changing Dimensions](https://learn.microsoft.com/en-us/fabric/data-warehouse/dimensional-modeling-dimension-tables)

### Database Schema (PostgreSQL — centralized)

**Keys**: Date/Time, Model, GPU Count, Hotkey, Node ID, Rented (bool), Available (bool), Slashing (bool)

```sql
CREATE TABLE IF NOT EXISTS cu_ledger (
    id BIGSERIAL PRIMARY KEY,

    -- Dimension keys
    hotkey TEXT NOT NULL,
    miner_uid INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    gpu_category TEXT NOT NULL,          -- Model (A100, H100, H200, B200)
    gpu_count INTEGER NOT NULL,

    -- State flags
    is_available BOOLEAN NOT NULL,       -- node passed validation
    is_rented BOOLEAN NOT NULL,          -- node has active rental
    is_slashed BOOLEAN NOT NULL DEFAULT FALSE,  -- node lost during rental

    -- SCD2 temporal columns
    valid_from TIMESTAMPTZ NOT NULL,     -- when this state began
    valid_to TIMESTAMPTZ,                -- when this state ended (NULL = current/active record)

    -- Accrued compute units during this state period
    cu_accrued DOUBLE PRECISION NOT NULL DEFAULT 0.0,

    -- Audit timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Fast current-state lookups (only open rows where valid_to IS NULL)
CREATE INDEX idx_cu_ledger_current
    ON cu_ledger(hotkey) WHERE valid_to IS NULL;

-- Node current-state lookups
CREATE INDEX idx_cu_ledger_node_current
    ON cu_ledger(node_id) WHERE valid_to IS NULL;

-- Time-window range queries for weight setter epoch calculations
CREATE INDEX idx_cu_ledger_window
    ON cu_ledger(valid_from, valid_to);

-- Hotkey + window for per-miner aggregation queries
CREATE INDEX idx_cu_ledger_hotkey_window
    ON cu_ledger(hotkey, valid_from, valid_to);
```

### SCD2 State Transition Rules
1. **State unchanged, node available**: Increment `cu_accrued += elapsed_hours * gpu_count` (proportional to actual elapsed time since last tick)
2. **State changed** (any of: available, rented, slashed, gpu_category, gpu_count): Close current row (`valid_to = NOW()`), open new row (`valid_from = NOW()`, `cu_accrued = 0`)
3. **Node disappears** (no availability entries in latest tick): Close row, no new row opened
4. **Slashed**: When rental terminated due to complete node loss → close current row, open new row with `is_slashed = true`
5. **Recovery from slash**: When node comes back online → close slashed row, open new row with `is_slashed = false`

### Repository Methods
```rust
pub struct CuLedgerRepository {
    pool: PgPool,  // sqlx PostgreSQL connection pool
}

impl CuLedgerRepository {
    // Current state queries
    async fn get_current_records(&self, hotkey: &str) -> Result<Vec<CuLedgerRow>>;
    async fn get_current_record_for_node(&self, node_id: &str) -> Result<Option<CuLedgerRow>>;

    // Time-window queries (for weight setter epoch calculations)
    async fn get_records_in_window(&self, from: DateTime<Utc>, to: DateTime<Utc>) -> Result<Vec<CuLedgerRow>>;
    async fn get_total_cu_in_window(&self, from: DateTime<Utc>, to: DateTime<Utc>) -> Result<f64>;
    async fn get_miner_cu_in_window(&self, hotkey: &str, from: DateTime<Utc>, to: DateTime<Utc>) -> Result<f64>;
    async fn get_all_miner_cus_in_window(&self, from: DateTime<Utc>, to: DateTime<Utc>) -> Result<HashMap<String, f64>>;

    // SCD2 mutation operations
    async fn close_record(&self, id: i64, valid_to: DateTime<Utc>) -> Result<()>;
    async fn open_record(&self, row: NewCuLedgerRow) -> Result<i64>;
    async fn accrue_cu(&self, id: i64, cu_delta: f64) -> Result<()>;
}
```

### Functional Requirements
- **FR-1**: SCD2 semantics — rows are never modified after `valid_to` is set (immutable history)
- **FR-2**: `cu_accrued` only incremented when `is_available = true` AND `is_slashed = false`
- **FR-3**: Window queries correctly handle rows that span the window boundary — compute proportional CU for partial overlaps
- **FR-4**: All payout queries exclude rows where `is_slashed = true`
- **FR-5**: `cu_accrued` uses actual elapsed time: `elapsed_hours = (now - last_accrual_time).as_hours()`, NOT a fixed 1.0 per tick

### Non-Functional Requirements
- **NFR-1**: **Data resilience** — PostgreSQL with WAL, append-only semantics, no destructive operations
- **NFR-2**: **High availability** — centralized PostgreSQL (existing infrastructure), accessible by all validators
- **NFR-3**: **Centralized** — single source of truth for CU data across the network
- **NFR-4**: Partial indexes on `valid_to IS NULL` for fast current-state lookups
- **NFR-5**: Retention policy for rows older than configurable threshold (e.g. 180 days)

---

## Module 4: CU Generator

**New file**: `crates/basilica-validator/src/incentive/cu_generator.rs`

### Purpose
Periodic task (~1 hour) that reads availability log entries from local SQLite since the last run, then writes/updates CU entries in the centralized PostgreSQL ledger. Uses **actual elapsed uptime** for CU accrual since the cron doesn't run at exact 1-hour intervals.

This is the bridge between local validation data and the centralized CU ledger.

### Algorithm
```
every ~1 hour (hardcoded 3600s interval, not configurable):
  now = current_time
  elapsed_hours = (now - last_run_ts).as_secs_f64() / 3600.0

  1. entries = [local SQLite] availability_log WHERE recorded_at > last_run_ts ORDER BY recorded_at
  2. for each distinct (hotkey, node_id) in entries:
     a. latest = most recent availability entry for this node
     b. current_ledger = [PostgreSQL] cu_ledger WHERE node_id = node_id AND valid_to IS NULL

     c. if no current_ledger row exists:
        → open new row in cu_ledger with state from latest entry, cu_accrued = 0

     d. check slash condition:
        if rental for this node transitioned to Failed/Stopped with termination_reason
           indicating node loss (validator gave up health-checking):
           → close current cu_ledger row
           → open new row with is_slashed = true

     e. else if state changed (available, rented, category, count differ from current ledger row):
        → close current cu_ledger row (cu_accrued stays as-is for the closed period)
        → open new row with new state, cu_accrued = 0

     f. else if is_available AND NOT is_slashed:
        → accrue CU on current row: cu_accrued += elapsed_hours * gpu_count

     g. else (unavailable or slashed):
        → no CU accrual (row remains open but cu_accrued unchanged)

  3. last_run_ts = now  (persisted in a control table or validator state)
```

### Functional Requirements
- **FR-1**: Idempotent — tracks `last_run_ts`, re-running over same availability data does not double-count CUs
- **FR-2**: Catch-up — if generator misses a tick, next tick processes all unprocessed availability entries and computes CU proportional to actual elapsed time
- **FR-3**: Rented nodes that are available still accrue CUs (renting doesn't stop availability rewards — the miner is providing both availability AND active compute)
- **FR-4**: Generator only spawned when `config.incentive.is_some()`
- **FR-5**: CU accrual is proportional to real time: `elapsed_hours * gpu_count`, NOT a fixed increment per tick

### Non-Functional Requirements
- **NFR-1**: Tolerates PostgreSQL connectivity blips — retries with exponential backoff
- **NFR-2**: Logs each tick at INFO level: nodes processed, CUs accrued, slashes detected, elapsed time

### Scheduling
Spawned as a tokio task in `service.rs` alongside existing `scoring_task` and `weight_setter_task`. Only spawned when `config.incentive.is_some()`.

---

## Module 5: Slashing

**New file**: `crates/basilica-validator/src/incentive/slashing.rs`

### Purpose
Detect nodes that have become completely inaccessible during an active rental and mark their CU periods as slashed. Slashed CUs are excluded from payout calculations, effectively stopping emissions for that node.

### Important: What Slashing Is NOT
- NOT triggered by one-off health check failures (transient network issues)
- NOT triggered by user-initiated rental stops
- NOT retroactive — does not void CUs already accrued before the slash event

### Trigger
A node is slashed when:
1. It has an **active rental** (`is_rented = true`)
2. The **validator decides the node is permanently lost** and stops health-checking it
3. This causes the rental to transition to `Failed`/`Stopped` with a `termination_reason` indicating node loss

Slashing is driven entirely by the rental lifecycle — when the validator gives up on a node and marks the rental as deleted/failed, that event triggers the slash. There is no separate consecutive-failure counter; the validator's own decision to abandon health checks is the authoritative signal.

### Detection Logic
Integrated into the CU Generator tick (Module 4, step 2d):

1. **Check rental termination events**: Query rentals that transitioned to `Failed`/`Stopped` with termination reason indicating node loss since the last CU Generator tick
2. **Mark as slashed**: Close current CU Ledger row, open new row with `is_slashed = true`
3. **Recovery**: If node comes back online (availability log shows `is_available = true`), close slashed row and open new normal row with `is_slashed = false`

### Functional Requirements
- **FR-1**: Slashing only triggers when a rental is terminated/deleted due to node loss — the validator's decision to stop health-checking is the authoritative signal
- **FR-2**: Slashing is forward-only — does NOT retroactively void prior CUs
- **FR-3**: Slashing ends when node recovers or rental terminates normally
- **FR-4**: Slashed CUs excluded from all payout calculations in the incentive pool
- **FR-5**: Slash state visible in CU Ledger for audit trail (the `is_slashed` column on open/closed rows)

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

**Step 1 — Pool Capacity & Budget (per GPU category)**
```
For each gpu_category:
  total_gpus[cat] = gpu_categories[cat].target_count * 8  // 8x = 8 GPUs per node
  pool_capacity[cat] = total_gpus[cat] * window_hours
  // Example (H100): 3 nodes * 8 GPUs * 72 hrs = 1,728 CU capacity

  pool_budget[cat] = pool_capacity[cat] * max_cu_value_usd
  // Example (H100): 1,728 CU * $0.05 = $86.40 budget

total_pool_budget = SUM(pool_budget[cat]) across all categories
```

**Step 2 — Effective CU Value**
```
total_cu_running = SUM(non-slashed cu_accrued in window, across all miners and categories)

effective_cu_value = MIN(max_cu_value_usd, total_pool_budget / total_cu_running)
```
- **Under-provisioned** (fewer GPUs online than target): each CU is worth more, but **capped at max_cu_value_usd**
- **Over-provisioned** (more GPUs online than target): each CU is worth less (diluted across more supply)
- This creates a natural market dynamic: if few miners are online, each one earns more per GPU-hour

**Step 3 — Revenue Reward (TBD)**
```
// Revenue Unit (RU) integration is TBD — will apply linear payout similar to CUs.
// The exact mechanism for fetching revenue data from the API is still being designed.
// Placeholder formula:
//   revenue_reward[hotkey] = revenue_share_pct/100 * delivery_revenue_usd[hotkey]
```

**Step 4 — Miner Payout**
```
miner_cu[hotkey] = SUM(non-slashed cu_accrued for this hotkey in the payout window)
miner_usd_owed[hotkey] = effective_cu_value * miner_cu[hotkey] + revenue_reward[hotkey]

// Prorate to epoch: what fraction of the window does this epoch cover?
payment_increment = epoch_duration_hours / window_hours
miner_usd_per_epoch[hotkey] = miner_usd_owed[hotkey] * payment_increment
```

**Step 5 — Weight Conversion (Dynamic Burn)**
```
usd_required_epoch = SUM(miner_usd_per_epoch[hotkey]) across all hotkeys

// How much USD can the subnet's alpha emissions cover?
alpha_emission_per_epoch = subnet_emission_rate  // from Bittensor metagraph chain data
alpha_price_usd = TokenPriceSnapshot.alpha_price_usd  // from existing BasilicaApiClient
usd_emission_capacity = alpha_emission_per_epoch * alpha_price_usd

// Dynamic burn: if the incentive pool demands less than emissions can cover, burn the excess
burn_rate = 1.0 - (usd_required_epoch / usd_emission_capacity)
burn_rate = clamp(burn_rate, 0.0, 0.99)  // never burn 100%, always leave some for miners

// Per-hotkey weight (normalized to u16 space for Bittensor)
For each hotkey:
  miner_weight[hotkey] = miner_usd_per_epoch[hotkey] / usd_emission_capacity
  weight_u16[hotkey] = round(miner_weight[hotkey] * u16::MAX)
```

### Integration into WeightSetter

The existing `WeightSetter` struct in `weight_setter.rs` gains:
- `incentive_config: Option<IncentiveConfig>` field
- `cu_ledger: Option<CuLedgerRepository>` field

In `attempt_weight_setting()`, add a branch:

```rust
if let Some(ref incentive_config) = self.incentive_config {
    // NEW PATH: CU-based incentive pool
    let pool = IncentivePool::new(
        incentive_config,
        self.cu_ledger.as_ref().unwrap(),
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
- **FR-3**: When `total_cu_running = 0`, set all weights to burn (guards against division by zero)
- **FR-4**: Revenue share component is additive on top of CU-based availability rewards (TBD integration details)
- **FR-5**: Per-epoch payout is prorated: `payment_increment = epoch_duration / window_size`
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
      incentive_pool.rs                    # Pool math and weight conversion
      slashing.rs                          # Slash detection (node-loss heuristic)
    persistence/
      availability_log.rs                  # Availability log SQLite operations (local)
      cu_ledger.rs                         # CU Ledger SCD2 PostgreSQL operations (centralized)
  migrations/
    017_availability_log.sql               # SQLite migration (local validator DB)
    018_cu_ledger.sql                      # PostgreSQL migration (centralized)
```

### Modified Files
```
crates/basilica-validator/src/
  config/mod.rs                            # pub mod incentive
  config/main_config.rs                    # Add incentive: Option<IncentiveConfig> to ValidatorConfig
  persistence/mod.rs                       # pub mod availability_log, cu_ledger
  bittensor_core/weight_setter.rs          # Add incentive_config field, branch in attempt_weight_setting()
  service.rs                               # Spawn CU Generator task, init PgPool, wire dependencies
  miner_prover/verification.rs             # Add record_availability_event() call after validation
  lib.rs                                   # pub mod incentive
```

---

## Implementation Phases

### Phase 1: Foundation (config + schema + persistence)
1. Create `config/incentive.rs` — `IncentiveConfig` + `GpuCategoryConfig` + validation logic
2. Wire into `config/mod.rs` and `main_config.rs`
3. Create SQLite migration `017_availability_log.sql` (local validator DB)
4. Create PostgreSQL migration `018_cu_ledger.sql` (centralized)
5. Create `persistence/availability_log.rs` — local SQLite write/read operations
6. Create `persistence/cu_ledger.rs` — SCD2 PostgreSQL operations (open/close/accrue/query)

### Phase 2: Data Capture
7. Create `incentive/mod.rs` — module declaration and re-exports
8. Integrate availability logging into verification workflow (`verification.rs`) — writes to local SQLite after each validation
9. Create `incentive/slashing.rs` — slash detection logic (node-loss during rental)
10. Create `incentive/cu_generator.rs` — reads local SQLite availability log, writes CU entries to centralized PostgreSQL
11. Spawn CU Generator in `service.rs` (gated on `config.incentive.is_some()`)

### Phase 3: Payout & Weights
12. Create `incentive/incentive_pool.rs` — full payout math (pool budget → effective CU value → per-miner USD → weight conversion)
13. Modify `weight_setter.rs` — add `incentive_config` field, branch to incentive pool path in `attempt_weight_setting()`
14. Wire dependencies in `service.rs` (PgPool, CuLedgerRepository → WeightSetter)

### Phase 4: Testing & Observability
15. Unit tests: config validation, SCD2 operations, payout math with known inputs
16. Integration test: `tests/incentive_e2e.rs` — full cycle from mock availability data through weight calculation
17. Add metrics: `cu_accrued_total`, `slashed_cu_total`, `burn_rate_gauge`, `effective_cu_value_gauge`

---

## Verification Plan

1. **Unit tests per module**: Config validation, SCD2 open/close/accrue, pool math, slash detection — all with known inputs and expected outputs
2. **Integration test**: `tests/incentive_e2e.rs` — full cycle from mock availability data → CU generation → payout calculation → weight normalization
3. **SCD2 correctness**: Test all state transitions (available→unavailable, unrented→rented, normal→slashed→recovered), row closing, proportional CU accrual, window boundary handling
4. **Payout math edge cases**: zero miners, single miner, all slashed, alpha price = 0, total_cu = 0 (division by zero), generator catch-up after missed ticks, burn rate clamping
5. **Regression**: Verify legacy delivery-based path continues unchanged when `incentive = None`
6. **Postgres connectivity**: Verify CU Generator handles temporary Postgres outages gracefully (retry with backoff)

---

## Remaining TBD

1. **Revenue Units (RU) from API**: The mechanism for fetching rental revenue data from the centralized API is still being designed. It will apply linear payout similar to the CU mechanism. This needs a follow-up design session.
2. **Initial parameter values**: Specific starting values for `window_hours`, `max_cu_value_usd`, `revenue_share_pct`, `target_count` per category, `gpu_prices_usd` per category need to be determined during testing/simulation.
