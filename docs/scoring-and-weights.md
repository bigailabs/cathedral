# Scoring and Weight Setting in Basilica

This document explains how the Basilica validator scores miners and sets weights on the Bittensor network.

## Overview

The validator uses a GPU-based scoring system that evaluates miners based on their hardware capabilities and validation success rates. Only H100 and H200 GPUs are eligible for rewards, while other GPU types are excluded from weight distribution.

## GPU Categories

The system recognizes three GPU categories, but only two are eligible for rewards:

- **H100** - NVIDIA H100 GPUs
- **H200** - NVIDIA H200 GPUs
- **OTHER** - All other GPU types

**NOTE**: The "OTHER" category is not rewarded. Only miners with H100 or H200 GPUs are considered for weight allocation.

**NOTE**: Currently we only validate GPUs with CUDA version 12.8 or higher.

### Current Allocation

Based on the emission configuration, the weight pool is distributed as follows:

- **Burn**: 95% of the total weight allocation is sent to a burn address to maintain network economics.
- **H100**: 40% of total **available** weight allocation
- **H200**: 60% of total **available** weight allocation

## Scoring Formula

### For Each Miner

The base score is calculated as:

```text
validation_ratio = successful_validations / total_validations
```

This ratio represents the miner's availability and reliability. High availability makes miners rank higher.

### For Each Miner in a Category

Within each GPU category, the miner's score is weighted by their effective (uptime-adjusted) GPU count:

```text
category_score = validation_ratio × effective_gpu_count
```

Where:
- `validation_ratio` = successful_validations / total_validations across all nodes in this category
- `effective_gpu_count` = SUM(gpu_count_n × uptime_multiplier_n) for all nodes n in this category

For a single node, the contribution to effective GPU count is:
```text
effective_gpu_count_n = gpu_count_n × uptime_multiplier_n
```

Where:
- `gpu_count_n` = raw number of GPUs on node n
- `uptime_multiplier_n` = node uptime factor (0.0 to 1.0, based on continuous uptime - see [Node Uptime Ramp-up](#node-uptime-ramp-up-incentives-ramp-up))

The effective GPU count is aggregated across all machines the miner operates within that category, with each node's GPU count multiplied by its individual uptime multiplier.

### Category Competition

For each category C, the total score is calculated using effective (uptime-adjusted) GPU counts:

```text
total_category_score = SUM(validation_ratio_i × effective_gpu_count_i) for all miners i in C
```

Where `effective_gpu_count_i` is the sum of all uptime-adjusted GPU counts across all nodes that miner i operates in category C:

```text
effective_gpu_count_i = SUM(gpu_count_n × uptime_multiplier_n) for all nodes n of miner i in C
```

Miners compete locally within their category. The more populated a category is (measured by total effective GPU count), the more competition exists for that category's weight pool.

### Weight Distribution Within Category

Each miner's weight within their category is proportional to their contribution:

```text
miner_weight_in_category = (category_score / total_category_score) × category_weight_pool
```

### Final Miner Weight

The final weight for each miner is the sum of their weights across all categories:

```text
final_weight = SUM(miner_weight_in_category) across all categories
```

## Node Uptime Ramp-up (Incentives Ramp-up)

### Overview

The Node Uptime Ramp-up mechanism rewards miner loyalty and stability by gradually increasing reward allocation based on continuous node availability. This system incentivizes miners to maintain consistent uptime and penalizes nodes that go offline or fail validations.

**Key Principles:**
- Rewards scale linearly from 0% to 100% over 14 days of continuous uptime
- Any validation failure resets the uptime counter to zero
- Ramp-up is tracked **per GPU node**, not per miner UID

### How It Works

Each GPU node maintains an independent uptime multiplier that affects how its GPU count contributes to the miner's category score. When a node first comes online or after any failure, it starts with a 0% multiplier (effectively 0 GPUs for scoring). This multiplier increases linearly each day the node remains online and passes validations.

**Timeline:**
```
Day 0 ─────────────────────> Day 14
0%     7.14%    50%           100%
│      │        │             │
New    1 day    7 days        Full rewards
node   online   online        (14+ days online)
```

### Mathematical Formula

The uptime multiplier for each node is calculated as:

```text
uptime_multiplier = min(continuous_uptime_minutes / 20160, 1.0)
```

Where:
- `continuous_uptime_minutes` = minutes since last failure (or node registration)
- `20160` = 14 days in minutes (14 × 24 × 60)
- `1.0` = maximum multiplier (100% rewards)

This multiplier is then applied to the node's GPU count:

```text
effective_gpu_count = gpu_count × uptime_multiplier
```

### Reset Conditions

The uptime counter resets to **zero** when:

1. **Any validation failure occurs** (`success = 0` in verification logs)
2. **Node goes offline** for extended period (no successful validations)
3. **Validation attestation fails** (binary validation failure)

**Important:** Only "full validations" (those with binary attestation: `last_binary_validation IS NOT NULL`) count toward uptime. Partial validations do not affect the uptime calculation.

After a reset, the node must build up its uptime multiplier again from 0%, starting from the timestamp of the next successful validation.

### Uptime Progression Timeline

Here's how rewards scale over time for a single H100 GPU node:

```
Timeline (Days)    Uptime Multiplier    Effective GPU Count    Reward %
─────────────────────────────────────────────────────────────────────────
Day 0  (0 hours)         0.000                 0.000           0.00%
Day 1  (24 hours)        0.071                 0.071           7.14%
Day 2  (48 hours)        0.143                 0.143          14.29%
Day 3  (72 hours)        0.214                 0.214          21.43%
Day 7  (168 hours)       0.500                 0.500          50.00%
Day 10 (240 hours)       0.714                 0.714          71.43%
Day 14 (336 hours)       1.000                 1.000         100.00%
Day 30 (720+ hours)      1.000                 1.000         100.00% (capped)
```

### Example Scenarios

#### Scenario 1: New Miner Joins H100 Category

**Setup:**
- GPU Category: H100
- Category weight pool: 40% of available emissions
- Existing miner: 1 H100 node, 14 days uptime (multiplier = 1.0), 100% validation success
- New miner: 1 H100 node, just joined (multiplier = 0.0), 100% validation success

**Calculations (Day 0):**

```
Existing miner:
  effective_gpu_count = 1 GPU × 1.0 = 1.0
  category_score = 1.0 (validation_ratio) × 1.0 (effective_gpu_count) = 1.0

New miner:
  effective_gpu_count = 1 GPU × 0.0 = 0.0
  category_score = 1.0 (validation_ratio) × 0.0 (effective_gpu_count) = 0.0

total_category_score = 1.0 + 0.0 = 1.0

Existing miner share = 1.0 / 1.0 = 100% of H100 pool
New miner share      = 0.0 / 1.0 = 0% of H100 pool
```

**Result:** The new miner receives 0% of H100 emissions until their uptime builds up.

**After 7 days:**
```
Existing miner:
  effective_gpu_count = 1 GPU × 1.0 = 1.0
  category_score = 1.0 × 1.0 = 1.0

New miner:
  effective_gpu_count = 1 GPU × 0.5 = 0.5
  category_score = 1.0 × 0.5 = 0.5

total_category_score = 1.0 + 0.5 = 1.5

Existing miner share = 1.0 / 1.5 = 66.7% of H100 pool
New miner share      = 0.5 / 1.5 = 33.3% of H100 pool
```

**After 14 days:**
```
Existing miner:
  effective_gpu_count = 1 GPU × 1.0 = 1.0
  category_score = 1.0 × 1.0 = 1.0

New miner:
  effective_gpu_count = 1 GPU × 1.0 = 1.0
  category_score = 1.0 × 1.0 = 1.0

total_category_score = 1.0 + 1.0 = 2.0

Existing miner share = 1.0 / 2.0 = 50% of H100 pool
New miner share      = 1.0 / 2.0 = 50% of H100 pool
```

#### Scenario 2: Impact of Downtime

**Setup:**
- Miner with 1 H100 node
- Node has been online for 7 days (multiplier = 0.5, receiving 50% rewards)
- Node goes offline or fails validation
- Assume 100% validation success rate when online

**Before failure:**
```
effective_gpu_count = 1 × 0.5 = 0.5
category_score = 1.0 (validation_ratio) × 0.5 (effective_gpu_count) = 0.5
Reward share = 0.5 / total_category_score
```

**Immediately after failure:**
```
effective_gpu_count = 1 × 0.0 = 0.0 (RESET!)
category_score = 1.0 × 0.0 = 0.0
Reward share = 0.0 / total_category_score = 0%
```

**Impact:** The 7 days of built-up uptime is lost. The miner must start over from 0% and build back up over another 14 days.

#### Scenario 3: Multiple Nodes with Different Uptimes

**Setup:**
- Miner with 3 H100 nodes (100% validation success):
  - Node A: 1 GPU, 14 days uptime (multiplier = 1.0)
  - Node B: 1 GPU, 7 days uptime (multiplier = 0.5)
  - Node C: 1 GPU, 3 days uptime (multiplier = 0.214)

**Calculation:**
```
Node A: effective_gpu_count = 1 × 1.000 = 1.000
Node B: effective_gpu_count = 1 × 0.500 = 0.500
Node C: effective_gpu_count = 1 × 0.214 = 0.214
────────────────────────────────────────────────
Miner's total effective_gpu_count = 2.714 GPUs

category_score = 1.0 (validation_ratio) × 2.714 (effective_gpu_count) = 2.714

vs. theoretical maximum:
  effective_gpu_count = 3.000 GPUs (all at 100%)
  category_score = 1.0 × 3.000 = 3.000
```

**Current efficiency:** 2.714 / 3.0 = 90.47% of maximum possible rewards

### Visual: Rewards Distribution Comparison

Here's how the H100 category rewards are split between an established miner and a new entrant:

```
Time: Day 0 (New miner just joined)
─────────────────────────────────────────────────────────────
Miner A (1 H100, 14d uptime)  ████████████████████████ 100%
Miner B (1 H100, 0d uptime)   (no rewards)              0%

Time: Day 7 (New miner has 7 days uptime)
─────────────────────────────────────────────────────────────
Miner A (1 H100, 14d uptime)  ████████████████ 66.7%
Miner B (1 H100, 7d uptime)   ████████ 33.3%

Time: Day 14 (Both at maximum)
─────────────────────────────────────────────────────────────
Miner A (1 H100, 14d uptime)  ████████████ 50%
Miner B (1 H100, 14d uptime)  ████████████ 50%
```

### Monitoring Your Uptime Status

Validators expose Prometheus metrics that miners can monitor to track their uptime status:

**Key Metrics:**

1. **`basilica_node_uptime_minutes{miner_uid="X", node_id="Y"}`**
   - Shows continuous uptime in minutes for each node
   - Resets to 0 on any validation failure

2. **`basilica_node_uptime_multiplier{miner_uid="X", node_id="Y"}`**
   - Shows current uptime multiplier (0.0 to 1.0)
   - Directly correlates to reward percentage (0% to 100%)

### Best Practices for Maximizing Rewards

1. **Maintain Continuous Uptime**
   - Set up monitoring and alerting for node health
   - Use systemd or supervisor for automatic service restarts
   - Implement redundant power and network connections

2. **Monitor Validation Success**
   - Check validator logs regularly for failed validations
   - Ensure all validations include binary attestation
   - Fix any issues immediately to prevent uptime resets

3. **Plan Maintenance Windows Carefully**
   - Any downtime resets your uptime to zero
   - If you must take a node offline, understand you'll lose 14 days of ramp-up progress
   - Consider the trade-off: is the maintenance worth restarting the 14-day clock?

4. **Per-Node vs. Per-Miner**
   - Uptime is tracked per GPU node, not per miner UID
   - Adding a new node to an existing miner starts that node at 0% multiplier
   - Existing nodes maintain their independent uptime multipliers
   - If you have multiple nodes, they can be at different uptime levels simultaneously

5. **Recovery Strategy After Failure**
   - After a validation failure or outage, uptime resets to zero
   - The next successful validation starts a new uptime period
   - It takes 14 days of continuous success to return to 100% rewards
   - No shortcuts—you must rebuild trust through proven uptime

## Implementation Details

### Validation Process

1. **Miner Discovery**: Validators discover miners from the Bittensor metagraph
2. **SSH Endpoint Discovery**: Validators query miners via gRPC for GPU node SSH endpoints
3. **Direct SSH Verification**: Validators SSH directly to GPU nodes for hardware verification
4. **Score Calculation**: Based on validation success and GPU specifications

### GPU Profile Updates

The system maintains GPU profiles for each miner that track:

- Primary GPU model
- GPU count distribution across models (raw counts)
- Node uptime multipliers (per-node basis)
- Total validation score (calculated using effective GPU counts)
- Verification count
- Last update timestamp

### Weight Setting Frequency

Weights are set periodically based on the configured `blocks_per_weight_set` parameter. The weight setter:

- Sets weights when 360 blocks have passed
- Only includes miners with active axons on the chain and GPU nodes that passed verification

### Filtering Criteria

Miners must meet several criteria to receive weights:

- Have GPU nodes that passed validation within the cutoff time (default: 3 hours)
- Have active axons on the Bittensor network (non-zero IP and port)
- Own H100 or H200 GPUs (OTHER category GPUs are excluded)

## Multi-Category Support

A single miner can appear in multiple categories if they operate different GPU types:

- Miners with both H100 and H200 GPUs compete in both categories
- Scores are calculated proportionally based on effective GPU count distribution (uptime-adjusted)
- Final weight is the sum of weights earned in each category
