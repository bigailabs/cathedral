//! Marketplace-2-Compute Cost Calculator
//!
//! This module provides pure functions for calculating rental costs using the
//! marketplace pricing model. Unlike the legacy package-based system, pricing
//! information comes directly from rental requests.
//!
//! ## Formula
//!
//! ```text
//! cost = gpu_hours × base_price_per_gpu × gpu_count × profit_margin
//!
//! where:
//!   profit_margin = 1 + (markup_percent / 100)
//!
//! Example: 10.5 hours × $2.50/GPU × 2 GPUs × 1.10 (10% markup) = $57.75
//! ```

use rust_decimal::Decimal;
use std::str::FromStr;

use crate::domain::types::{CostBreakdown, CreditBalance};

/// Calculate rental cost using marketplace-2-compute pricing formula.
///
/// This is a pure function with no external dependencies. All pricing information
/// is provided as parameters.
///
/// # Arguments
///
/// * `gpu_hours` - Total GPU hours consumed (from telemetry)
/// * `base_price_per_gpu` - Base price per GPU per hour (before markup)
/// * `gpu_count` - Number of GPUs in the rental
/// * `markup_percent` - Markup percentage to apply (e.g., 10.0 for 10%)
///
/// # Returns
///
/// A `CostBreakdown` containing:
/// - `base_cost`: Cost before markup (gpu_hours × base_price_per_gpu × gpu_count)
/// - `discounts`: Markup amount (for transparency, stored in discounts field)
/// - `total_cost`: Final cost including markup
///
/// # Examples
///
/// ```rust
/// use rust_decimal::Decimal;
/// use rust_decimal_macros::dec;
/// use basilica_billing::domain::cost_calculator::calculate_marketplace_cost;
///
/// // 10 hours × $2.50/GPU × 2 GPUs × 1.10 (10% markup) = $55
/// let breakdown = calculate_marketplace_cost(
///     dec!(10.0),  // gpu_hours
///     dec!(2.50),  // base_price_per_gpu
///     2,           // gpu_count
///     dec!(10.0),  // markup_percent
/// );
///
/// assert_eq!(breakdown.base_cost.as_decimal(), dec!(50.00));
/// assert_eq!(breakdown.total_cost.as_decimal(), dec!(55.00));
/// ```
pub fn calculate_marketplace_cost(
    gpu_hours: Decimal,
    base_price_per_gpu: Decimal,
    gpu_count: u32,
    markup_percent: Decimal,
) -> CostBreakdown {
    // Ensure at least 1 GPU for calculation
    let effective_gpu_count = Decimal::from(gpu_count.max(1));

    // Step 1: Calculate base cost (before markup)
    // base_cost = gpu_hours × base_price_per_gpu × gpu_count
    let base_cost = gpu_hours
        .checked_mul(base_price_per_gpu)
        .and_then(|v| v.checked_mul(effective_gpu_count))
        .unwrap_or_else(|| {
            tracing::error!(
                "Base cost calculation overflow: {} hours × ${} × {} GPUs",
                gpu_hours,
                base_price_per_gpu,
                gpu_count
            );
            Decimal::ZERO
        });

    // Step 2: Calculate profit margin
    // profit_margin = 1 + (markup_percent / 100)
    // Example: 10% → 1.10
    let profit_margin = Decimal::ONE
        + markup_percent
            .checked_div(Decimal::from(100))
            .unwrap_or(Decimal::ZERO);

    // Step 3: Apply profit margin
    // total_cost = base_cost × profit_margin
    let total_cost = base_cost
        .checked_mul(profit_margin)
        .unwrap_or_else(|| {
            tracing::error!(
                "Total cost calculation overflow: ${} × {}",
                base_cost,
                profit_margin
            );
            base_cost
        });

    // Step 4: Calculate markup amount for transparency
    let markup_amount = total_cost.checked_sub(base_cost).unwrap_or(Decimal::ZERO);

    CostBreakdown {
        base_cost: CreditBalance::from_decimal(base_cost),
        usage_cost: CreditBalance::zero(), // Reserved for future use
        volume_discount: CreditBalance::zero(), // No volume discounts in marketplace model
        discounts: CreditBalance::from_decimal(markup_amount), // Repurposed to show markup
        overage_charges: CreditBalance::zero(), // Reserved for future use
        total_cost: CreditBalance::from_decimal(total_cost),
    }
}

/// Legacy package-based cost calculation (DEPRECATED)
///
/// This function is kept for backward compatibility with existing tests
/// and code that hasn't been migrated yet. New code should use
/// `calculate_marketplace_cost` instead.
///
/// # Deprecation Notice
///
/// This will be removed in a future version once all callers have been
/// migrated to the marketplace pricing model.
#[deprecated(
    since = "1.0.0",
    note = "Use calculate_marketplace_cost for marketplace-2-compute pricing"
)]
pub fn calculate_legacy_cost(
    hourly_rate: Decimal,
    gpu_hours: Decimal,
    gpu_count: u32,
) -> CostBreakdown {
    let effective_gpu_count = Decimal::from(gpu_count.max(1));

    // Legacy formula: hourly_rate × gpu_hours × gpu_count
    let raw_gpu_cost = hourly_rate
        .checked_mul(gpu_hours)
        .and_then(|v| v.checked_mul(effective_gpu_count))
        .unwrap_or(Decimal::ZERO);

    // Legacy volume discount: 10% if gpu_count > 1
    let volume_discount = if gpu_count > 1 {
        raw_gpu_cost.checked_mul(Decimal::from_str("0.10").unwrap()).unwrap_or(Decimal::ZERO)
    } else {
        Decimal::ZERO
    };

    let total_cost = raw_gpu_cost
        .checked_sub(volume_discount)
        .unwrap_or(raw_gpu_cost);

    CostBreakdown {
        base_cost: CreditBalance::from_decimal(raw_gpu_cost),
        usage_cost: CreditBalance::zero(),
        volume_discount: CreditBalance::from_decimal(volume_discount),
        discounts: CreditBalance::zero(),
        overage_charges: CreditBalance::zero(),
        total_cost: CreditBalance::from_decimal(total_cost),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal_macros::dec;

    #[test]
    fn test_marketplace_cost_basic() {
        // 1 hour × $3.00/GPU × 1 GPU × 1.10 (10% markup) = $3.30
        let breakdown = calculate_marketplace_cost(
            dec!(1.0),  // gpu_hours
            dec!(3.00), // base_price_per_gpu
            1,          // gpu_count
            dec!(10.0), // markup_percent
        );

        assert_eq!(breakdown.base_cost.as_decimal(), dec!(3.00));
        assert_eq!(breakdown.total_cost.as_decimal(), dec!(3.30));
        assert_eq!(breakdown.discounts.as_decimal(), dec!(0.30)); // markup amount
    }

    #[test]
    fn test_marketplace_cost_multi_gpu() {
        // 10.5 hours × $2.50/GPU × 2 GPUs × 1.10 (10% markup) = $57.75
        let breakdown = calculate_marketplace_cost(
            dec!(10.5), // gpu_hours
            dec!(2.50), // base_price_per_gpu
            2,          // gpu_count
            dec!(10.0), // markup_percent
        );

        assert_eq!(breakdown.base_cost.as_decimal(), dec!(52.50));
        assert_eq!(breakdown.total_cost.as_decimal(), dec!(57.75));
        assert_eq!(breakdown.discounts.as_decimal(), dec!(5.25));
    }

    #[test]
    fn test_marketplace_cost_zero_markup() {
        // 5 hours × $1.00/GPU × 1 GPU × 1.00 (0% markup) = $5.00
        let breakdown = calculate_marketplace_cost(
            dec!(5.0),  // gpu_hours
            dec!(1.00), // base_price_per_gpu
            1,          // gpu_count
            dec!(0.0),  // markup_percent
        );

        assert_eq!(breakdown.base_cost.as_decimal(), dec!(5.00));
        assert_eq!(breakdown.total_cost.as_decimal(), dec!(5.00));
        assert_eq!(breakdown.discounts.as_decimal(), dec!(0.00));
    }

    #[test]
    fn test_marketplace_cost_high_markup() {
        // 1 hour × $10.00/GPU × 1 GPU × 1.50 (50% markup) = $15.00
        let breakdown = calculate_marketplace_cost(
            dec!(1.0),   // gpu_hours
            dec!(10.00), // base_price_per_gpu
            1,           // gpu_count
            dec!(50.0),  // markup_percent
        );

        assert_eq!(breakdown.base_cost.as_decimal(), dec!(10.00));
        assert_eq!(breakdown.total_cost.as_decimal(), dec!(15.00));
        assert_eq!(breakdown.discounts.as_decimal(), dec!(5.00));
    }

    #[test]
    fn test_marketplace_cost_zero_hours() {
        // 0 hours × $2.50/GPU × 1 GPU × 1.10 = $0.00
        let breakdown = calculate_marketplace_cost(
            dec!(0.0),  // gpu_hours
            dec!(2.50), // base_price_per_gpu
            1,          // gpu_count
            dec!(10.0), // markup_percent
        );

        assert_eq!(breakdown.base_cost.as_decimal(), dec!(0.00));
        assert_eq!(breakdown.total_cost.as_decimal(), dec!(0.00));
    }

    #[test]
    fn test_marketplace_cost_zero_gpu_count() {
        // Edge case: 0 GPUs should be treated as 1 GPU
        let breakdown = calculate_marketplace_cost(
            dec!(1.0),  // gpu_hours
            dec!(2.00), // base_price_per_gpu
            0,          // gpu_count (treated as 1)
            dec!(10.0), // markup_percent
        );

        assert_eq!(breakdown.base_cost.as_decimal(), dec!(2.00));
        assert_eq!(breakdown.total_cost.as_decimal(), dec!(2.20));
    }

    #[test]
    fn test_marketplace_cost_fractional_hours() {
        // 0.5 hours × $4.00/GPU × 1 GPU × 1.10 = $2.20
        let breakdown = calculate_marketplace_cost(
            dec!(0.5),  // gpu_hours
            dec!(4.00), // base_price_per_gpu
            1,          // gpu_count
            dec!(10.0), // markup_percent
        );

        assert_eq!(breakdown.base_cost.as_decimal(), dec!(2.00));
        assert_eq!(breakdown.total_cost.as_decimal(), dec!(2.20));
    }

    #[test]
    fn test_marketplace_cost_large_numbers() {
        // 1000 hours × $10.00/GPU × 8 GPUs × 1.10 = $88,000
        let breakdown = calculate_marketplace_cost(
            dec!(1000.0), // gpu_hours
            dec!(10.00),  // base_price_per_gpu
            8,            // gpu_count
            dec!(10.0),   // markup_percent
        );

        assert_eq!(breakdown.base_cost.as_decimal(), dec!(80000.00));
        assert_eq!(breakdown.total_cost.as_decimal(), dec!(88000.00));
        assert_eq!(breakdown.discounts.as_decimal(), dec!(8000.00));
    }

    #[test]
    fn test_legacy_cost_basic() {
        // Legacy: 10 hours × $2.50 × 1 GPU = $25.00
        #[allow(deprecated)]
        let breakdown = calculate_legacy_cost(
            dec!(2.50), // hourly_rate
            dec!(10.0), // gpu_hours
            1,          // gpu_count
        );

        assert_eq!(breakdown.base_cost.as_decimal(), dec!(25.00));
        assert_eq!(breakdown.total_cost.as_decimal(), dec!(25.00));
        assert_eq!(breakdown.volume_discount.as_decimal(), dec!(0.00));
    }

    #[test]
    fn test_legacy_cost_volume_discount() {
        // Legacy: 10 hours × $2.50 × 2 GPUs × 0.9 (10% discount) = $45.00
        #[allow(deprecated)]
        let breakdown = calculate_legacy_cost(
            dec!(2.50), // hourly_rate
            dec!(10.0), // gpu_hours
            2,          // gpu_count
        );

        assert_eq!(breakdown.base_cost.as_decimal(), dec!(50.00));
        assert_eq!(breakdown.volume_discount.as_decimal(), dec!(5.00));
        assert_eq!(breakdown.total_cost.as_decimal(), dec!(45.00));
    }
}
