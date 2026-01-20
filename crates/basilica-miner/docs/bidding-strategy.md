# Miner Bidding Strategy

## Overview

Miners submit bids to validators specifying the price per GPU-hour they're willing to accept. The lowest bidder wins the rental and gets paid their bid rate. This document outlines how miners can determine and submit bids.

## Current State

The miner has infrastructure for bid submission:
- `create_signed_bid()` creates a cryptographically signed bid
- `forward_bid_to_validator()` sends the bid to the validator's gRPC endpoint
- Bids include: `gpu_category`, `bid_per_hour`, `gpu_count`, `nonce`, `signature`

**Missing**: Automated bidding logic that decides WHEN to bid and at WHAT price.

## Bid Lifecycle

```
┌─────────────────────────────────────────────────────────────────────┐
│                        MINER BIDDING FLOW                          │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────────┐  │
│  │ Config/Algo  │───▶│ Bid Decision │───▶│ create_signed_bid()  │  │
│  │ determines   │    │ Engine       │    │                      │  │
│  │ base price   │    │              │    │ - gpu_category       │  │
│  └──────────────┘    └──────────────┘    │ - bid_per_hour       │  │
│                                          │ - gpu_count          │  │
│                                          │ - nonce              │  │
│                                          │ - signature          │  │
│                                          └──────────┬───────────┘  │
│                                                     │              │
│                                                     ▼              │
│                                          ┌──────────────────────┐  │
│                                          │ forward_bid_to_      │  │
│                                          │ validator()          │  │
│                                          └──────────┬───────────┘  │
│                                                     │              │
│                                                     ▼              │
│                                          ┌──────────────────────┐  │
│                                          │ Validator stores bid │  │
│                                          │ for epoch            │  │
│                                          └──────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

## Bidding Strategy Options

### Option 1: Static Config (Simple)

Miner sets fixed prices per GPU category in config file.

```toml
[bidding]
# Fixed prices per GPU-hour by category
prices = { H100 = 2.50, A100 = 1.20, RTX4090 = 0.80 }

# How often to resubmit bids (seconds)
bid_interval_secs = 300
```

**Pros:**
- Simple to understand and configure
- Predictable revenue
- No risk of algorithmic errors

**Cons:**
- Can't adapt to market conditions
- May leave money on table or never win bids

**Best for:** Miners who know their costs and want predictable operation.

---

### Option 2: Cost-Plus Pricing (Rational)

Base prices on actual costs plus desired margin.

```toml
[bidding.cost_basis]
# Electricity cost per kWh
electricity_cost_kwh = 0.10

# GPU power consumption (watts)
gpu_power_watts = { H100 = 700, A100 = 400, RTX4090 = 450 }

# Hardware depreciation per GPU-hour (amortized over expected lifetime)
depreciation_per_hour = { H100 = 0.50, A100 = 0.30, RTX4090 = 0.15 }

# Overhead (bandwidth, cooling, maintenance) per GPU-hour
overhead_per_hour = 0.10

# Target margin percentage
target_margin_pct = 20
```

**Calculation:**
```rust
fn calculate_bid(category: &str, config: &CostBasisConfig) -> f64 {
    let power_watts = config.gpu_power_watts.get(category).unwrap_or(&500);
    let power_kwh = *power_watts as f64 / 1000.0;
    
    let electricity_cost = power_kwh * config.electricity_cost_kwh;
    let depreciation = config.depreciation_per_hour.get(category).unwrap_or(&0.20);
    let overhead = config.overhead_per_hour;
    
    let total_cost = electricity_cost + depreciation + overhead;
    let margin_multiplier = 1.0 + (config.target_margin_pct / 100.0);
    
    total_cost * margin_multiplier
}
```

**Pros:**
- Based on real economics
- Ensures profitability (won't bid below cost)
- Transparent reasoning

**Cons:**
- Requires knowing actual costs
- Static - doesn't react to market

**Best for:** Miners who want to ensure profitability.

---

### Option 3: Utilization-Based (Dynamic)

Adjust bids based on current node utilization.

```toml
[bidding.utilization]
# Base price when fully idle
idle_price = { H100 = 1.50, A100 = 0.80 }

# Price when fully utilized (no new capacity)
busy_price = { H100 = 4.00, A100 = 2.50 }

# Utilization thresholds
low_utilization_threshold = 0.3   # Below this, bid aggressively low
high_utilization_threshold = 0.8  # Above this, bid high (don't need work)
```

**Algorithm:**
```rust
fn calculate_utilization_bid(
    category: &str,
    current_utilization: f64,  // 0.0 to 1.0
    config: &UtilizationConfig,
) -> f64 {
    let idle_price = config.idle_price.get(category).unwrap_or(&1.0);
    let busy_price = config.busy_price.get(category).unwrap_or(&3.0);
    
    // Linear interpolation based on utilization
    let price_range = busy_price - idle_price;
    let price = idle_price + (price_range * current_utilization);
    
    // Apply urgency adjustments
    if current_utilization < config.low_utilization_threshold {
        // Idle - bid lower to get work
        price * 0.8
    } else if current_utilization > config.high_utilization_threshold {
        // Busy - bid higher, don't need more work
        price * 1.2
    } else {
        price
    }
}
```

**Pros:**
- Automatically seeks work when idle
- Protects capacity when busy
- Maximizes revenue over time

**Cons:**
- More complex
- Needs accurate utilization tracking
- Can be gamed by competitors watching bids

**Best for:** Large miners with multiple nodes who want to optimize utilization.

---

### Option 4: Time-of-Day Pricing

Adjust for electricity cost variations throughout the day.

```toml
[bidding.time_of_day]
# Base prices
base_price = { H100 = 2.00, A100 = 1.00 }

# Time-based multipliers (0-23 hours, UTC)
# Off-peak nights (cheap electricity)
hour_multipliers = [
    0.7, 0.7, 0.7, 0.7, 0.7, 0.7,    # 00:00-05:59 (off-peak)
    0.9, 0.9, 1.0, 1.0, 1.0, 1.0,    # 06:00-11:59 (ramping)
    1.2, 1.2, 1.2, 1.2, 1.2, 1.2,    # 12:00-17:59 (peak)
    1.1, 1.0, 0.9, 0.8, 0.8, 0.7,    # 18:00-23:59 (declining)
]
```

**Pros:**
- Aligns with actual electricity costs
- Can attract off-peak workloads with lower prices
- More profitable during peak hours

**Cons:**
- Timezone complexity
- May not match actual utility billing

**Best for:** Miners in regions with time-of-use electricity pricing.

---

### Option 5: Hybrid Strategy (Recommended)

Combine multiple factors into a unified bidding strategy.

```toml
[bidding]
strategy = "hybrid"

[bidding.hybrid]
# Floor price (never bid below this)
floor_price = { H100 = 1.00, A100 = 0.50 }

# Target price (ideal profit margin)
target_price = { H100 = 2.50, A100 = 1.20 }

# Ceiling price (maximum bid, prevents overpricing)
ceiling_price = { H100 = 5.00, A100 = 3.00 }

# Weights for each factor (must sum to 1.0)
utilization_weight = 0.4
time_of_day_weight = 0.2
cost_basis_weight = 0.4

# Utilization config
utilization_scaling = 0.5  # How much utilization affects price (0-1)

# Time-of-day config
timezone = "America/New_York"
peak_hours = [9, 10, 11, 12, 13, 14, 15, 16, 17]  # 9am-5pm
peak_multiplier = 1.15
offpeak_multiplier = 0.90
```

**Algorithm:**
```rust
fn calculate_hybrid_bid(
    category: &str,
    utilization: f64,
    hour_utc: u32,
    config: &HybridConfig,
) -> f64 {
    let target = *config.target_price.get(category).unwrap_or(&2.0);
    let floor = *config.floor_price.get(category).unwrap_or(&0.5);
    let ceiling = *config.ceiling_price.get(category).unwrap_or(&5.0);
    
    // Factor 1: Utilization adjustment
    // Low utilization → bid lower; High utilization → bid higher
    let util_adjustment = 1.0 + (config.utilization_scaling * (utilization - 0.5));
    
    // Factor 2: Time-of-day adjustment
    let tod_adjustment = if config.peak_hours.contains(&hour_utc) {
        config.peak_multiplier
    } else {
        config.offpeak_multiplier
    };
    
    // Factor 3: Cost basis (assume this is baked into target_price)
    let cost_adjustment = 1.0;
    
    // Weighted combination
    let combined_adjustment = 
        (util_adjustment * config.utilization_weight) +
        (tod_adjustment * config.time_of_day_weight) +
        (cost_adjustment * config.cost_basis_weight);
    
    // Apply to target price and clamp
    let raw_price = target * combined_adjustment;
    raw_price.clamp(floor, ceiling)
}
```

---

## Implementation Roadmap

### Phase 1: Static Config (MVP)

Add bidding configuration to `MinerConfig`:

```rust
// config.rs
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BiddingConfig {
    /// Enable automatic bidding
    pub enabled: bool,
    
    /// Bidding strategy: "static", "cost_plus", "utilization", "hybrid"
    pub strategy: String,
    
    /// Static prices by GPU category ($/GPU-hour)
    pub static_prices: HashMap<String, f64>,
    
    /// How often to submit bids (seconds)
    pub bid_interval_secs: u64,
    
    /// Validator endpoint to submit bids to
    pub validator_endpoint: Option<String>,
}
```

### Phase 2: Automated Bid Submission

Create a background task that periodically submits bids:

```rust
// bidding/auto_bidder.rs
pub struct AutoBidder {
    config: BiddingConfig,
    node_manager: Arc<NodeManager>,
    validator_comms: Arc<ValidatorCommsServer>,
}

impl AutoBidder {
    pub async fn run(&self) {
        let mut interval = tokio::time::interval(
            Duration::from_secs(self.config.bid_interval_secs)
        );
        
        loop {
            interval.tick().await;
            
            if !self.config.enabled {
                continue;
            }
            
            // Get available capacity by category
            let capacity = self.node_manager.get_available_capacity().await;
            
            for (category, gpu_count) in capacity {
                let price = self.calculate_bid(&category).await;
                
                match self.submit_bid(&category, price, gpu_count).await {
                    Ok(_) => info!("Submitted bid: {} @ ${}/hr x{}", category, price, gpu_count),
                    Err(e) => warn!("Failed to submit bid: {}", e),
                }
            }
        }
    }
}
```

### Phase 3: Utilization Tracking

Integrate with node metrics to track utilization:

```rust
// node_manager.rs
impl NodeManager {
    pub async fn get_utilization_by_category(&self) -> HashMap<String, f64> {
        // Count total vs rented GPUs per category
        let nodes = self.list_nodes().await.unwrap_or_default();
        let mut total: HashMap<String, u32> = HashMap::new();
        let mut rented: HashMap<String, u32> = HashMap::new();
        
        for node in nodes {
            let category = node.gpu_category();
            *total.entry(category.clone()).or_default() += node.gpu_count;
            if node.is_rented() {
                *rented.entry(category).or_default() += node.gpu_count;
            }
        }
        
        total.into_iter()
            .map(|(cat, t)| (cat.clone(), rented.get(&cat).copied().unwrap_or(0) as f64 / t as f64))
            .collect()
    }
}
```

### Phase 4: Market Intelligence (Future)

Optional: Watch market to inform pricing.

```rust
// bidding/market_watcher.rs (future)
pub struct MarketWatcher {
    /// Historical winning bid prices by category
    winning_bids: RwLock<HashMap<String, VecDeque<f64>>>,
}

impl MarketWatcher {
    /// Get suggested competitive price for a category
    pub fn get_competitive_price(&self, category: &str) -> Option<f64> {
        let bids = self.winning_bids.read().await;
        let history = bids.get(category)?;
        
        // Return slightly below median winning price
        let mut sorted: Vec<f64> = history.iter().copied().collect();
        sorted.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let median = sorted[sorted.len() / 2];
        Some(median * 0.95) // 5% below median
    }
}
```

---

## Economic Considerations

### The Bidding Tradeoff

```
                    WIN PROBABILITY
                          ▲
                          │
            High ─────────┼─────────● Low bid
                          │        /
                          │       /
                          │      /
                          │     /
                          │    /
                          │   /
            Low ──────────┼──● High bid
                          │
                          └───────────────────▶
                              REWARD PER WIN
```

- **Low bids**: High probability of winning, but lower reward per rental
- **High bids**: Low probability of winning, but higher reward when you do win

**Optimal strategy**: Find the bid that maximizes expected revenue:
```
Expected Revenue = P(win) × Revenue(bid)
```

### Race to Bottom Risk

If all miners use aggressive utilization-based bidding, prices can spiral down. Mitigations:

1. **Floor price**: Never bid below cost
2. **Collateral**: Miners with stake have skin in the game
3. **Reputation**: Long-term miners optimize for sustainable pricing
4. **Category caps**: Emission caps prevent single miner domination

### New Miner Strategy

New miners face a cold-start problem: no track record → validators may prefer established miners.

**Suggested approach:**
1. Start with slightly below-market bids to win initial work
2. Build reputation through reliable delivery
3. Gradually increase prices as reputation grows

---

## Configuration Examples

### Conservative Miner (Profitability Focus)

```toml
[bidding]
enabled = true
strategy = "cost_plus"
bid_interval_secs = 600

[bidding.cost_plus]
electricity_cost_kwh = 0.12
gpu_power_watts = { H100 = 700 }
depreciation_per_hour = { H100 = 0.60 }
overhead_per_hour = 0.15
target_margin_pct = 25
```

### Aggressive Miner (Utilization Focus)

```toml
[bidding]
enabled = true
strategy = "utilization"
bid_interval_secs = 300

[bidding.utilization]
idle_price = { H100 = 1.20 }
busy_price = { H100 = 3.50 }
low_utilization_threshold = 0.2
high_utilization_threshold = 0.7
```

### Balanced Miner (Hybrid)

```toml
[bidding]
enabled = true
strategy = "hybrid"
bid_interval_secs = 300

[bidding.hybrid]
floor_price = { H100 = 1.00 }
target_price = { H100 = 2.20 }
ceiling_price = { H100 = 4.00 }
utilization_weight = 0.5
time_of_day_weight = 0.2
cost_basis_weight = 0.3
utilization_scaling = 0.4
peak_multiplier = 1.10
offpeak_multiplier = 0.92
```

---

## Monitoring & Alerts

Track bidding effectiveness:

```rust
// Metrics to expose
struct BiddingMetrics {
    bids_submitted: Counter,
    bids_accepted: Counter,
    bids_rejected: Counter,
    current_bid_price: Gauge,        // by category
    current_utilization: Gauge,      // by category
    rentals_won: Counter,
    revenue_earned: Counter,
}
```

**Alerts to configure:**
- Utilization below threshold for extended period → bids may be too high
- Win rate dropping → competitors undercutting
- Bids being rejected → check validator connectivity

---

## Summary

| Strategy | Complexity | Adaptability | Risk | Best For |
|----------|------------|--------------|------|----------|
| Static | Low | None | Over/under pricing | Simple operations |
| Cost-Plus | Medium | None | Miss market opportunities | Profit-focused |
| Utilization | Medium | High | Race to bottom | Large miners |
| Time-of-Day | Low | Medium | Timezone complexity | ToU electricity |
| Hybrid | High | High | Configuration complexity | Sophisticated miners |

**Recommended starting point**: Static config with floor prices based on cost, then graduate to hybrid as you understand market dynamics.

