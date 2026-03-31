use anyhow::{anyhow, Result};
use std::collections::HashMap;
use tracing::{debug, info, warn};

use crate::config::emission::EmissionConfig;
use basilica_common::identity::MinerUid;

pub struct WeightAllocationEngine {
    emission_config: EmissionConfig,
}

impl WeightAllocationEngine {
    pub fn new(emission_config: EmissionConfig) -> Self {
        info!(
            "WeightAllocationEngine initialized with burn_uid: {}, burn_percentage: {:.2}%",
            emission_config.burn_uid, emission_config.burn_percentage
        );
        Self { emission_config }
    }

    /// Calculate weight distribution with burn and GPU allocation
    pub fn calculate_weight_distribution(
        &self,
        miners_by_category: HashMap<String, Vec<(MinerUid, f64)>>,
    ) -> Result<WeightDistribution> {
        // Total weight available (using u16::MAX as the maximum)
        let total_weight = u16::MAX as u64;

        // Calculate burn allocation first
        let burn_allocation = self.calculate_burn_allocation(total_weight)?;
        let burn_weight = burn_allocation
            .as_ref()
            .map(|b| b.weight as u64)
            .unwrap_or(0);

        // Remaining weight after burn
        let remaining_weight = total_weight - burn_weight;

        // Filter miners by minimum score threshold
        let adjusted_miners = self.filter_miners_by_score(miners_by_category)?;

        // Calculate category weight pools for ALL configured categories
        let all_category_pools = self.calculate_all_category_pools(remaining_weight)?;

        // Track which categories have miners
        let mut active_categories = std::collections::HashSet::new();
        for category in adjusted_miners.keys() {
            active_categories.insert(category.clone());
        }

        // Calculate additional burn for empty categories
        let mut empty_category_burn = 0u64;
        for (category, pool) in &all_category_pools {
            if !active_categories.contains(category) {
                empty_category_burn += pool;
                info!(
                    category = %category,
                    weight = pool,
                    "Burning weight for empty GPU category"
                );
            }
        }

        // Distribute weights within each category
        let mut all_weights: Vec<NormalizedWeight> = Vec::new();
        let mut category_allocations = HashMap::new();
        let mut aggregated_count = 0;

        for (category, miners) in adjusted_miners {
            let category_weight_pool = all_category_pools.get(&category).copied().unwrap_or(0);

            if category_weight_pool == 0 || miners.is_empty() {
                continue;
            }

            let category_weights =
                self.distribute_category_weight(&miners, category_weight_pool)?;

            // Calculate category statistics
            let total_score: f64 = miners.iter().map(|(_, score)| score).sum();
            let allocation_percentage =
                (category_weight_pool as f64 / remaining_weight as f64) * 100.0;

            category_allocations.insert(
                category.clone(),
                CategoryAllocation {
                    gpu_model: category.clone(),
                    miner_count: miners.len() as u32,
                    total_score,
                    weight_pool: category_weight_pool,
                    allocation_percentage,
                },
            );

            // Aggregate weights for miners that appear in multiple categories
            for weight in category_weights {
                if let Some(existing) = all_weights.iter_mut().find(|w| w.uid == weight.uid) {
                    existing.weight =
                        (existing.weight as u64 + weight.weight as u64).min(u16::MAX as u64) as u16;
                    aggregated_count += 1;
                } else {
                    all_weights.push(weight);
                }
            }
        }

        let total_burn_weight = burn_weight + empty_category_burn;
        if total_burn_weight > 0 {
            let burn_weight_entry = NormalizedWeight {
                uid: self.emission_config.burn_uid,
                weight: total_burn_weight.min(u16::MAX as u64) as u16,
            };

            debug!(
                "Allocating burn weight: uid={}, weight={}",
                burn_weight_entry.uid, burn_weight_entry.weight
            );

            if let Some(existing) = all_weights
                .iter_mut()
                .find(|w| w.uid == burn_weight_entry.uid)
            {
                existing.weight = (existing.weight as u64 + burn_weight_entry.weight as u64)
                    .min(u16::MAX as u64) as u16;
                aggregated_count += 1;
            } else {
                all_weights.push(burn_weight_entry);
            }
        }

        // Debug: Show all weights before validation
        info!(
            "Final weights before validation ({} entries):",
            all_weights.len()
        );
        for (i, w) in all_weights.iter().enumerate() {
            info!("  Weight {}: UID={}, weight={}", i, w.uid, w.weight);
        }

        // Validate final allocation
        self.validate_allocation(&all_weights)?;

        let miners_served = all_weights.len() as u32 - if total_burn_weight > 0 { 1 } else { 0 };

        info!(
            total_weight = total_weight,
            burn_weight = burn_weight,
            empty_category_burn = empty_category_burn,
            total_burn = total_burn_weight,
            categories = category_allocations.len(),
            miners_served = miners_served,
            aggregated_uids = aggregated_count,
            "Calculated weight distribution"
        );

        Ok(WeightDistribution {
            weights: all_weights,
            burn_allocation: if total_burn_weight > 0 {
                Some(BurnAllocation {
                    uid: self.emission_config.burn_uid,
                    weight: total_burn_weight.min(u16::MAX as u64) as u16,
                    percentage: (total_burn_weight as f64 / total_weight as f64) * 100.0,
                })
            } else {
                None
            },
            category_allocations,
            total_weight,
            miners_served,
        })
    }

    /// Calculate burn allocation
    fn calculate_burn_allocation(&self, total_weight: u64) -> Result<Option<BurnAllocation>> {
        if self.emission_config.burn_percentage <= 0.0 {
            return Ok(None);
        }

        let burn_weight =
            (total_weight as f64 * self.emission_config.burn_percentage / 100.0) as u16;

        if burn_weight == 0 {
            return Ok(None);
        }

        Ok(Some(BurnAllocation {
            uid: self.emission_config.burn_uid,
            weight: burn_weight,
            percentage: self.emission_config.burn_percentage,
        }))
    }

    /// Filter miners by minimum score threshold
    fn filter_miners_by_score(
        &self,
        miners_by_category: HashMap<String, Vec<(MinerUid, f64)>>,
    ) -> Result<HashMap<String, Vec<(MinerUid, f64)>>> {
        let mut filtered = HashMap::new();

        // Use configured minimum miners per category from emission config
        let min_miners_per_category = self.emission_config.min_miners_per_category as usize;

        for (category, miners) in miners_by_category {
            // Remove score threshold filtering - include all miners regardless of score
            let valid_miners: Vec<(MinerUid, f64)> = miners;

            // Only include categories with minimum number of miners
            if valid_miners.len() >= min_miners_per_category {
                filtered.insert(category, valid_miners);
            } else {
                debug!(
                    category = %category,
                    miners = valid_miners.len(),
                    required = min_miners_per_category,
                    "Category excluded due to insufficient miners"
                );
            }
        }

        Ok(filtered)
    }

    /// Calculate weight pools for ALL configured categories (including empty ones)
    fn calculate_all_category_pools(
        &self,
        total_remaining_weight: u64,
    ) -> Result<HashMap<String, u64>> {
        let mut category_pools = HashMap::new();

        // Get all configured GPU categories from emission config
        for (category, allocation) in &self.emission_config.gpu_allocations {
            let weight_pool = (total_remaining_weight as f64 * allocation.weight / 100.0) as u64;
            category_pools.insert(category.clone(), weight_pool);
        }

        Ok(category_pools)
    }

    /// Distribute weight within a category proportionally by score
    fn distribute_category_weight(
        &self,
        category_miners: &[(MinerUid, f64)],
        category_weight_pool: u64,
    ) -> Result<Vec<NormalizedWeight>> {
        if category_miners.is_empty() {
            return Ok(Vec::new());
        }

        let total_score: f64 = category_miners.iter().map(|(_, score)| score).sum();

        if total_score <= 0.0 {
            warn!("Total score is zero for category, distributing equally");
            return self.distribute_equally(category_miners, category_weight_pool);
        }

        let mut weights = Vec::new();
        let mut allocated_weight = 0u64;

        for (i, (miner_uid, score)) in category_miners.iter().enumerate() {
            let weight = if i == category_miners.len() - 1 {
                // Last miner gets remaining weight to avoid rounding errors
                category_weight_pool - allocated_weight
            } else {
                (category_weight_pool as f64 * score / total_score) as u64
            };

            // Ensure weight fits in u16
            let weight = weight.min(u16::MAX as u64) as u16;

            if weight > 0 {
                weights.push(NormalizedWeight {
                    uid: miner_uid.as_u16(),
                    weight,
                });
                allocated_weight += weight as u64;
            }
        }

        Ok(weights)
    }

    /// Distribute weight equally among miners (fallback method)
    fn distribute_equally(
        &self,
        category_miners: &[(MinerUid, f64)],
        category_weight_pool: u64,
    ) -> Result<Vec<NormalizedWeight>> {
        if category_miners.is_empty() {
            return Ok(Vec::new());
        }

        let weight_per_miner = (category_weight_pool / category_miners.len() as u64) as u16;
        let mut weights = Vec::new();

        for (miner_uid, _) in category_miners {
            if weight_per_miner > 0 {
                weights.push(NormalizedWeight {
                    uid: miner_uid.as_u16(),
                    weight: weight_per_miner,
                });
            }
        }

        Ok(weights)
    }

    /// Validate allocation results
    fn validate_allocation(&self, weights: &[NormalizedWeight]) -> Result<()> {
        let total_allocated: u64 = weights.iter().map(|w| w.weight as u64).sum();
        let max_weight = u16::MAX as u64;

        if total_allocated > max_weight {
            return Err(anyhow!(
                "Total allocated weight {} exceeds maximum {}",
                total_allocated,
                max_weight
            ));
        }

        // Check for duplicate UIDs
        let mut seen_uids = std::collections::HashSet::new();
        for weight in weights {
            if !seen_uids.insert(weight.uid) {
                return Err(anyhow!("Duplicate UID {} in weight allocation", weight.uid));
            }
        }

        Ok(())
    }
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct WeightDistribution {
    pub weights: Vec<NormalizedWeight>,
    pub burn_allocation: Option<BurnAllocation>,
    pub category_allocations: HashMap<String, CategoryAllocation>,
    pub total_weight: u64,
    pub miners_served: u32,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct BurnAllocation {
    pub uid: u16,
    pub weight: u16,
    pub percentage: f64,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct CategoryAllocation {
    pub gpu_model: String,
    pub miner_count: u32,
    pub total_score: f64,
    pub weight_pool: u64,
    pub allocation_percentage: f64,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct NormalizedWeight {
    pub uid: u16,
    pub weight: u16,
}
