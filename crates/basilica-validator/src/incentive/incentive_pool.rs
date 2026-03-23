use crate::basilica_api::{
    BasilicaApiError, CuLedgerRowResponse, IncentiveConfigResponse, RuLedgerRowResponse,
};
use crate::bittensor_core::weight_allocation::{
    BurnAllocation, CategoryAllocation, NormalizedWeight, WeightDistribution,
};
use anyhow::Result;
use chrono::{DateTime, Utc};
use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;
use std::cmp::Ordering;
use std::collections::{HashMap, HashSet};

#[derive(Debug, Clone)]
pub struct IncentivePoolResult {
    pub distribution: WeightDistribution,
    pub burn_rate: Decimal,
    pub burn_percentage: f64,
    pub usd_required_epoch: Decimal,
    pub usd_emission_capacity: Decimal,
    pub category_payouts: HashMap<String, Decimal>,
    pub miner_payouts: HashMap<String, Decimal>,
}

pub fn should_fallback_to_legacy(error: &BasilicaApiError) -> bool {
    matches!(error, BasilicaApiError::NotConfigured)
}

pub fn compute_cu_vested_fraction(
    row: &CuLedgerRowResponse,
    epoch_start: DateTime<Utc>,
    epoch_end: DateTime<Utc>,
) -> Decimal {
    compute_vested_fraction(row.earned_at, row.window_hours, epoch_start, epoch_end)
}

pub fn compute_ru_vested_fraction(
    row: &RuLedgerRowResponse,
    epoch_start: DateTime<Utc>,
    epoch_end: DateTime<Utc>,
) -> Decimal {
    compute_vested_fraction(row.earned_at, row.window_hours, epoch_start, epoch_end)
}

#[allow(clippy::too_many_arguments)]
pub fn compute_incentive_pool(
    config: &IncentiveConfigResponse,
    cu_rows: &[CuLedgerRowResponse],
    ru_rows: &[RuLedgerRowResponse],
    epoch_start: DateTime<Utc>,
    epoch_end: DateTime<Utc>,
    alpha_price_usd: Decimal,
    subnet_emission_rate: u64,
    burn_uid: u16,
    hotkey_to_uid: &HashMap<String, u16>,
) -> Result<IncentivePoolResult> {
    let active_cu_rows: Vec<&CuLedgerRowResponse> = cu_rows
        .iter()
        .filter(|row| !row.is_slashed)
        .filter(|row| hotkey_to_uid.contains_key(&row.hotkey))
        .filter(|row| config.gpu_categories.contains_key(&row.gpu_category))
        .collect();
    let active_ru_rows: Vec<&RuLedgerRowResponse> = ru_rows
        .iter()
        .filter(|row| !row.is_slashed)
        .filter(|row| hotkey_to_uid.contains_key(&row.hotkey))
        .collect();

    let mut category_cu_supply: HashMap<String, Decimal> = HashMap::new();
    for row in &active_cu_rows {
        *category_cu_supply
            .entry(row.gpu_category.clone())
            .or_insert(Decimal::ZERO) += row.cu_amount;
    }

    let mut miner_payouts = HashMap::new();
    let mut category_payouts = HashMap::new();
    let mut category_miners: HashMap<String, HashSet<String>> = HashMap::new();

    for row in active_cu_rows {
        let vested_fraction = compute_cu_vested_fraction(row, epoch_start, epoch_end);
        if vested_fraction <= Decimal::ZERO {
            continue;
        }

        let Some(category_config) = config.gpu_categories.get(&row.gpu_category) else {
            continue;
        };
        let category_supply = category_cu_supply
            .get(&row.gpu_category)
            .copied()
            .unwrap_or(Decimal::ZERO);
        if category_supply <= Decimal::ZERO {
            continue;
        }

        let target_gpus = Decimal::from(category_config.target_count) * Decimal::from(8u32);
        let row_capacity_budget = target_gpus * row.window_hours * row.price_usd;
        let per_cu_budget = row_capacity_budget / category_supply;
        let effective_price = min_decimal(
            row.price_usd,
            min_decimal(per_cu_budget, config.max_cu_value_usd),
        );
        let row_payout = vested_fraction * row.cu_amount * effective_price;
        if row_payout <= Decimal::ZERO {
            continue;
        }

        *miner_payouts
            .entry(row.hotkey.clone())
            .or_insert(Decimal::ZERO) += row_payout;
        *category_payouts
            .entry(row.gpu_category.clone())
            .or_insert(Decimal::ZERO) += row_payout;
        category_miners
            .entry(row.gpu_category.clone())
            .or_default()
            .insert(row.hotkey.clone());
    }

    for row in active_ru_rows {
        let vested_fraction = compute_ru_vested_fraction(row, epoch_start, epoch_end);
        if vested_fraction <= Decimal::ZERO {
            continue;
        }

        let row_payout =
            vested_fraction * row.ru_amount * row.revenue_share_pct / Decimal::from(100u32);
        if row_payout <= Decimal::ZERO {
            continue;
        }

        *miner_payouts
            .entry(row.hotkey.clone())
            .or_insert(Decimal::ZERO) += row_payout;
        *category_payouts
            .entry(row.gpu_category.clone())
            .or_insert(Decimal::ZERO) += row_payout;
        category_miners
            .entry(row.gpu_category.clone())
            .or_default()
            .insert(row.hotkey.clone());
    }

    let raw_usd_required_epoch = sum_decimals(miner_payouts.values().copied());
    let usd_emission_capacity = Decimal::from(subnet_emission_rate) * alpha_price_usd;

    if raw_usd_required_epoch <= Decimal::ZERO || usd_emission_capacity <= Decimal::ZERO {
        return Ok(all_burn_result(
            burn_uid,
            usd_emission_capacity,
            category_payouts,
            miner_payouts,
        ));
    }

    let scale_factor = if raw_usd_required_epoch > usd_emission_capacity {
        usd_emission_capacity / raw_usd_required_epoch
    } else {
        Decimal::ONE
    };

    let scaled_miner_payouts = scale_decimal_map(&miner_payouts, scale_factor);
    let scaled_category_payouts = scale_decimal_map(&category_payouts, scale_factor);
    let usd_required_epoch = sum_decimals(scaled_miner_payouts.values().copied());
    if usd_required_epoch <= Decimal::ZERO {
        return Ok(all_burn_result(
            burn_uid,
            usd_emission_capacity,
            scaled_category_payouts,
            scaled_miner_payouts,
        ));
    }

    let mut burn_rate = Decimal::ONE - (usd_required_epoch / usd_emission_capacity);
    burn_rate = clamp_decimal(burn_rate, Decimal::ZERO, Decimal::new(99, 2));
    let burn_share = if usd_required_epoch >= usd_emission_capacity {
        Decimal::ZERO
    } else {
        burn_rate
    };

    let mut shares_by_uid: HashMap<u16, Decimal> = HashMap::new();
    for (hotkey, payout) in &scaled_miner_payouts {
        if *payout <= Decimal::ZERO {
            continue;
        }
        let Some(uid) = hotkey_to_uid.get(hotkey).copied() else {
            continue;
        };
        *shares_by_uid.entry(uid).or_insert(Decimal::ZERO) += *payout / usd_emission_capacity;
    }
    if burn_share > Decimal::ZERO {
        *shares_by_uid.entry(burn_uid).or_insert(Decimal::ZERO) += burn_share;
    } else {
        shares_by_uid.entry(burn_uid).or_insert(Decimal::ZERO);
    }

    let weights = normalize_uid_shares(&shares_by_uid);
    let burn_weight = weights
        .iter()
        .find(|weight| weight.uid == burn_uid)
        .map(|weight| weight.weight)
        .unwrap_or(0);
    let total_weight = u16::MAX as u64;
    let miners_served = weights
        .iter()
        .filter(|weight| weight.uid != burn_uid && weight.weight > 0)
        .count() as u32;

    let category_allocations = build_category_allocations(
        &scaled_category_payouts,
        &category_miners,
        total_weight,
        burn_weight,
        usd_required_epoch,
    );
    let burn_percentage = ((Decimal::from(burn_weight) * Decimal::from(100u32))
        / Decimal::from(total_weight))
    .to_f64()
    .unwrap_or(0.0);

    Ok(IncentivePoolResult {
        distribution: WeightDistribution {
            weights,
            burn_allocation: Some(BurnAllocation {
                uid: burn_uid,
                weight: burn_weight,
                percentage: burn_percentage,
            }),
            category_allocations,
            total_weight,
            miners_served,
        },
        burn_rate,
        burn_percentage,
        usd_required_epoch,
        usd_emission_capacity,
        category_payouts: scaled_category_payouts,
        miner_payouts: scaled_miner_payouts,
    })
}

fn compute_vested_fraction(
    earned_at: DateTime<Utc>,
    window_hours: Decimal,
    epoch_start: DateTime<Utc>,
    epoch_end: DateTime<Utc>,
) -> Decimal {
    let Some(window_ms) = decimal_hours_to_millis(window_hours) else {
        return Decimal::ZERO;
    };
    if window_ms <= 0 {
        return Decimal::ZERO;
    }

    let row_start_ms = earned_at.timestamp_millis() as i128;
    let row_end_ms = row_start_ms + window_ms;
    let overlap_start_ms = row_start_ms.max(epoch_start.timestamp_millis() as i128);
    let overlap_end_ms = row_end_ms.min(epoch_end.timestamp_millis() as i128);
    if overlap_end_ms <= overlap_start_ms {
        return Decimal::ZERO;
    }

    Decimal::from_i128_with_scale(overlap_end_ms - overlap_start_ms, 0)
        / Decimal::from_i128_with_scale(window_ms, 0)
}

fn decimal_hours_to_millis(hours: Decimal) -> Option<i128> {
    (hours * Decimal::from(3_600_000u64)).round_dp(0).to_i128()
}

fn scale_decimal_map(
    values: &HashMap<String, Decimal>,
    scale_factor: Decimal,
) -> HashMap<String, Decimal> {
    values
        .iter()
        .map(|(key, value)| (key.clone(), *value * scale_factor))
        .collect()
}

fn all_burn_result(
    burn_uid: u16,
    usd_emission_capacity: Decimal,
    category_payouts: HashMap<String, Decimal>,
    miner_payouts: HashMap<String, Decimal>,
) -> IncentivePoolResult {
    IncentivePoolResult {
        distribution: WeightDistribution {
            weights: vec![NormalizedWeight {
                uid: burn_uid,
                weight: u16::MAX,
            }],
            burn_allocation: Some(BurnAllocation {
                uid: burn_uid,
                weight: u16::MAX,
                percentage: 100.0,
            }),
            category_allocations: HashMap::new(),
            total_weight: u16::MAX as u64,
            miners_served: 0,
        },
        burn_rate: Decimal::ONE,
        burn_percentage: 100.0,
        usd_required_epoch: Decimal::ZERO,
        usd_emission_capacity,
        category_payouts,
        miner_payouts,
    }
}

fn normalize_uid_shares(shares_by_uid: &HashMap<u16, Decimal>) -> Vec<NormalizedWeight> {
    let total_weight = Decimal::from(u16::MAX);
    let mut candidates: Vec<WeightCandidate> = shares_by_uid
        .iter()
        .filter(|(_, share)| **share > Decimal::ZERO)
        .map(|(uid, share)| {
            let raw_weight = *share * total_weight;
            let floored = raw_weight.trunc().to_u64().unwrap_or(0);
            WeightCandidate {
                uid: *uid,
                base_weight: floored,
                remainder: raw_weight - Decimal::from(floored),
            }
        })
        .collect();

    let base_total: u64 = candidates
        .iter()
        .map(|candidate| candidate.base_weight)
        .sum();
    let mut leftover = (u16::MAX as u64).saturating_sub(base_total);
    candidates.sort_by(weight_candidate_order);
    for candidate in &mut candidates {
        if leftover == 0 {
            break;
        }
        candidate.base_weight += 1;
        leftover -= 1;
    }

    let mut weights: Vec<NormalizedWeight> = candidates
        .into_iter()
        .filter(|candidate| candidate.base_weight > 0)
        .map(|candidate| NormalizedWeight {
            uid: candidate.uid,
            weight: candidate.base_weight.min(u16::MAX as u64) as u16,
        })
        .collect();
    weights.sort_by_key(|weight| weight.uid);
    weights
}

fn weight_candidate_order(left: &WeightCandidate, right: &WeightCandidate) -> Ordering {
    right
        .remainder
        .cmp(&left.remainder)
        .then_with(|| left.uid.cmp(&right.uid))
}

fn build_category_allocations(
    category_payouts: &HashMap<String, Decimal>,
    category_miners: &HashMap<String, HashSet<String>>,
    total_weight: u64,
    burn_weight: u16,
    usd_required_epoch: Decimal,
) -> HashMap<String, CategoryAllocation> {
    let miner_weight_total = total_weight.saturating_sub(burn_weight as u64);
    let mut categories = HashMap::new();

    for (category, payout) in category_payouts {
        if *payout <= Decimal::ZERO || usd_required_epoch <= Decimal::ZERO {
            continue;
        }

        let share = *payout / usd_required_epoch;
        let weight_pool = (share * Decimal::from(miner_weight_total))
            .round_dp(0)
            .to_u64()
            .unwrap_or(0);
        let allocation_percentage = (share * Decimal::from(100u32)).to_f64().unwrap_or(0.0);
        let miner_count = category_miners
            .get(category)
            .map(|miners| miners.len() as u32)
            .unwrap_or(0);

        categories.insert(
            category.clone(),
            CategoryAllocation {
                gpu_model: category.clone(),
                miner_count,
                total_score: payout.to_f64().unwrap_or(0.0),
                weight_pool,
                allocation_percentage,
            },
        );
    }

    categories
}

fn sum_decimals<I>(values: I) -> Decimal
where
    I: IntoIterator<Item = Decimal>,
{
    values
        .into_iter()
        .fold(Decimal::ZERO, |acc, value| acc + value)
}

fn min_decimal(left: Decimal, right: Decimal) -> Decimal {
    if left <= right {
        left
    } else {
        right
    }
}

fn clamp_decimal(value: Decimal, min: Decimal, max: Decimal) -> Decimal {
    if value < min {
        min
    } else if value > max {
        max
    } else {
        value
    }
}

#[derive(Debug, Clone)]
struct WeightCandidate {
    uid: u16,
    base_weight: u64,
    remainder: Decimal,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::basilica_api::{IncentiveGpuCategoryConfig, PostSlashResponse};
    use chrono::TimeZone;
    use std::str::FromStr;
    use uuid::Uuid;

    fn d(value: &str) -> Decimal {
        Decimal::from_str(value).unwrap()
    }

    fn ts(hour: i64) -> DateTime<Utc> {
        Utc.timestamp_opt(hour * 3600, 0).unwrap()
    }

    fn test_config() -> IncentiveConfigResponse {
        let mut gpu_categories = HashMap::new();
        gpu_categories.insert(
            "H100".to_string(),
            IncentiveGpuCategoryConfig {
                target_count: 1,
                price_usd: d("10"),
            },
        );
        gpu_categories.insert(
            "A100".to_string(),
            IncentiveGpuCategoryConfig {
                target_count: 2,
                price_usd: d("8"),
            },
        );

        IncentiveConfigResponse {
            gpu_categories,
            window_hours: d("4"),
            max_cu_value_usd: d("100"),
            revenue_share_pct: Some(d("25")),
            slash_pct: d("100"),
        }
    }

    type CuRowArgs<'a> = (
        &'a str,
        u32,
        &'a str,
        &'a str,
        DateTime<Utc>,
        &'a str,
        &'a str,
        &'a str,
    );

    fn cu_row(
        (
            hotkey,
            miner_uid,
            node_id,
            cu_amount,
            earned_at,
            gpu_category,
            window_hours,
            price_usd,
        ): CuRowArgs<'_>,
    ) -> CuLedgerRowResponse {
        CuLedgerRowResponse {
            id: Uuid::new_v4(),
            hotkey: hotkey.to_string(),
            miner_uid,
            node_id: node_id.to_string(),
            cu_amount: d(cu_amount),
            earned_at,
            is_rented: false,
            gpu_category: gpu_category.to_string(),
            window_hours: d(window_hours),
            price_usd: d(price_usd),
            idempotency_key: format!("{hotkey}-{node_id}"),
            is_slashed: false,
            slash_audit_id: None,
            created_at: earned_at,
        }
    }

    type RuRowArgs<'a> = (
        &'a str,
        u32,
        &'a str,
        &'a str,
        DateTime<Utc>,
        &'a str,
        &'a str,
        &'a str,
    );

    fn ru_row(
        (
            hotkey,
            miner_uid,
            node_id,
            ru_amount,
            earned_at,
            gpu_category,
            window_hours,
            revenue_share_pct,
        ): RuRowArgs<'_>,
    ) -> RuLedgerRowResponse {
        RuLedgerRowResponse {
            id: Uuid::new_v4(),
            hotkey: hotkey.to_string(),
            miner_uid,
            node_id: node_id.to_string(),
            rental_id: format!("rental-{hotkey}"),
            ru_amount: d(ru_amount),
            earned_at,
            gpu_category: gpu_category.to_string(),
            window_hours: d(window_hours),
            revenue_share_pct: d(revenue_share_pct),
            is_slashed: false,
            slash_audit_id: None,
            created_at: earned_at,
        }
    }

    fn hotkey_to_uid() -> HashMap<String, u16> {
        HashMap::from([
            ("miner-1".to_string(), 11),
            ("miner-2".to_string(), 22),
            ("miner-3".to_string(), 33),
        ])
    }

    fn miner_weight(result: &IncentivePoolResult, uid: u16) -> u16 {
        result
            .distribution
            .weights
            .iter()
            .find(|weight| weight.uid == uid)
            .map(|weight| weight.weight)
            .unwrap_or(0)
    }

    #[test]
    fn test_cu_vesting_behavior() {
        let row = cu_row(("miner-1", 11, "node-1", "4", ts(0), "H100", "4", "10"));

        let vested_fraction = compute_cu_vested_fraction(&row, ts(2), ts(4));

        assert_eq!(vested_fraction, d("0.5"));
    }

    #[test]
    fn test_ru_vesting_behavior() {
        let row = ru_row(("miner-1", 11, "node-1", "40", ts(0), "H100", "4", "25"));

        let vested_fraction = compute_ru_vested_fraction(&row, ts(1), ts(3));

        assert_eq!(vested_fraction, d("0.5"));
    }

    #[test]
    fn test_cu_dilution_by_category_target_and_supply() {
        let config = test_config();
        let result = compute_incentive_pool(
            &config,
            &[
                cu_row(("miner-1", 11, "node-1", "4", ts(0), "H100", "4", "10")),
                cu_row(("miner-2", 22, "node-2", "4", ts(0), "H100", "4", "10")),
            ],
            &[],
            ts(0),
            ts(4),
            d("1"),
            100,
            999,
            &hotkey_to_uid(),
        )
        .unwrap();

        assert!(miner_weight(&result, 11) > 0);
        assert_eq!(miner_weight(&result, 11), miner_weight(&result, 22));
    }

    #[test]
    fn test_ru_revenue_share_payout() {
        let config = test_config();
        let result = compute_incentive_pool(
            &config,
            &[],
            &[ru_row((
                "miner-1",
                11,
                "node-1",
                "80",
                ts(0),
                "H100",
                "4",
                "25",
            ))],
            ts(0),
            ts(4),
            d("1"),
            100,
            999,
            &hotkey_to_uid(),
        )
        .unwrap();

        assert!(miner_weight(&result, 11) > 0);
        assert_eq!(miner_weight(&result, 22), 0);
    }

    #[test]
    fn test_combined_cu_and_ru_payout_behavior() {
        let config = test_config();
        let result = compute_incentive_pool(
            &config,
            &[cu_row((
                "miner-1",
                11,
                "node-1",
                "4",
                ts(0),
                "H100",
                "4",
                "10",
            ))],
            &[ru_row((
                "miner-2",
                22,
                "node-2",
                "40",
                ts(0),
                "H100",
                "4",
                "25",
            ))],
            ts(0),
            ts(4),
            d("1"),
            100,
            999,
            &hotkey_to_uid(),
        )
        .unwrap();

        assert!(miner_weight(&result, 11) > 0);
        assert!(miner_weight(&result, 22) > 0);
        assert!(result.distribution.burn_allocation.is_some());
    }

    #[test]
    fn test_emission_cap_scale_down_behavior() {
        let config = test_config();
        let result = compute_incentive_pool(
            &config,
            &[
                cu_row(("miner-1", 11, "node-1", "20", ts(0), "H100", "4", "10")),
                cu_row(("miner-2", 22, "node-2", "20", ts(0), "A100", "4", "8")),
            ],
            &[ru_row((
                "miner-3",
                33,
                "node-3",
                "100",
                ts(0),
                "H100",
                "4",
                "25",
            ))],
            ts(0),
            ts(4),
            d("1"),
            10,
            999,
            &hotkey_to_uid(),
        )
        .unwrap();

        assert_eq!(result.burn_rate, Decimal::ZERO);
        assert_eq!(
            result
                .distribution
                .burn_allocation
                .as_ref()
                .map(|allocation| allocation.weight)
                .unwrap_or(0),
            0
        );
    }

    #[test]
    fn test_dynamic_burn_behavior() {
        let config = test_config();
        let result = compute_incentive_pool(
            &config,
            &[cu_row((
                "miner-1",
                11,
                "node-1",
                "2",
                ts(0),
                "H100",
                "4",
                "10",
            ))],
            &[],
            ts(0),
            ts(4),
            d("10"),
            100,
            999,
            &hotkey_to_uid(),
        )
        .unwrap();

        assert!(result.burn_rate > Decimal::ZERO);
        assert!(result.burn_rate <= d("0.99"));
        assert!(
            result
                .distribution
                .burn_allocation
                .as_ref()
                .map(|allocation| allocation.weight)
                .unwrap_or(0)
                > 0
        );
    }

    #[test]
    fn test_alpha_price_zero_routes_all_weight_to_burn() {
        let config = test_config();
        let result = compute_incentive_pool(
            &config,
            &[cu_row((
                "miner-1",
                11,
                "node-1",
                "4",
                ts(0),
                "H100",
                "4",
                "10",
            ))],
            &[ru_row((
                "miner-1",
                11,
                "node-1",
                "80",
                ts(0),
                "H100",
                "4",
                "25",
            ))],
            ts(0),
            ts(4),
            Decimal::ZERO,
            100,
            999,
            &hotkey_to_uid(),
        )
        .unwrap();

        assert_eq!(result.distribution.weights.len(), 1);
        assert_eq!(result.distribution.weights[0].uid, 999);
        assert_eq!(result.distribution.weights[0].weight, u16::MAX);
    }

    #[test]
    fn test_zero_cu_zero_ru_and_both_zero_cases() {
        let config = test_config();

        let only_ru = compute_incentive_pool(
            &config,
            &[],
            &[ru_row((
                "miner-2",
                22,
                "node-2",
                "40",
                ts(0),
                "H100",
                "4",
                "25",
            ))],
            ts(0),
            ts(4),
            d("1"),
            100,
            999,
            &hotkey_to_uid(),
        )
        .unwrap();
        assert!(miner_weight(&only_ru, 22) > 0);

        let only_cu = compute_incentive_pool(
            &config,
            &[cu_row((
                "miner-1",
                11,
                "node-1",
                "4",
                ts(0),
                "H100",
                "4",
                "10",
            ))],
            &[],
            ts(0),
            ts(4),
            d("1"),
            100,
            999,
            &hotkey_to_uid(),
        )
        .unwrap();
        assert!(miner_weight(&only_cu, 11) > 0);

        let neither = compute_incentive_pool(
            &config,
            &[],
            &[],
            ts(0),
            ts(4),
            d("1"),
            100,
            999,
            &hotkey_to_uid(),
        )
        .unwrap();
        assert_eq!(neither.distribution.weights.len(), 1);
        assert_eq!(neither.distribution.weights[0].uid, 999);
    }

    #[test]
    fn test_rollout_behavior_not_configured_vs_transient_failure() {
        assert!(should_fallback_to_legacy(&BasilicaApiError::NotConfigured));
        assert!(!should_fallback_to_legacy(&BasilicaApiError::Transport(
            "timeout".to_string(),
        )));
        assert!(!should_fallback_to_legacy(&BasilicaApiError::HttpStatus {
            status: reqwest::StatusCode::INTERNAL_SERVER_ERROR,
            body: "boom".to_string(),
        }));
    }

    #[test]
    fn test_weight_computation_is_deterministic_for_same_inputs() {
        let config = test_config();
        let cu_rows = vec![
            cu_row(("miner-1", 11, "node-1", "4", ts(0), "H100", "4", "10")),
            cu_row(("miner-2", 22, "node-2", "8", ts(0), "A100", "4", "8")),
        ];
        let ru_rows = vec![ru_row((
            "miner-3",
            33,
            "node-3",
            "20",
            ts(0),
            "H100",
            "4",
            "25",
        ))];
        let hotkey_to_uid = hotkey_to_uid();

        let left = compute_incentive_pool(
            &config,
            &cu_rows,
            &ru_rows,
            ts(0),
            ts(4),
            d("2"),
            100,
            999,
            &hotkey_to_uid,
        )
        .unwrap();
        let right = compute_incentive_pool(
            &config,
            &cu_rows,
            &ru_rows,
            ts(0),
            ts(4),
            d("2"),
            100,
            999,
            &hotkey_to_uid,
        )
        .unwrap();

        let left_weights: Vec<(u16, u16)> = left
            .distribution
            .weights
            .iter()
            .map(|weight| (weight.uid, weight.weight))
            .collect();
        let right_weights: Vec<(u16, u16)> = right
            .distribution
            .weights
            .iter()
            .map(|weight| (weight.uid, weight.weight))
            .collect();

        assert_eq!(left_weights, right_weights);
        assert_eq!(left.burn_rate, right.burn_rate);
    }

    #[test]
    fn test_post_slash_response_type_stays_constructible() {
        let response = PostSlashResponse {
            slashed_cu_count: 1,
            slashed_ru_count: 2,
        };
        assert_eq!(response.slashed_cu_count + response.slashed_ru_count, 3);
    }
}
