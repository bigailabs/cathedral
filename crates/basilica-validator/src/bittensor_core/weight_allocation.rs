use std::collections::HashMap;

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
