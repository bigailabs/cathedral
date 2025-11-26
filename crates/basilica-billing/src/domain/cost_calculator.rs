//! Marketplace-2-Compute Cost Calculator
//!
//! This module provides pure functions for calculating rental costs using the
//! marketplace pricing model. Unlike the legacy package-based system, pricing
//! information comes directly from rental requests.
//!
//! ## Formula
//!
//! ```text
//! cost = gpu_hours × price_per_gpu × gpu_count
//!
//! Note: price_per_gpu should already include any markup applied by the API layer.
//!
//! Example: 10.5 hours × $2.75/GPU × 2 GPUs = $57.75
//! ```

use rust_decimal::Decimal;

use crate::domain::types::{CostBreakdown, CreditBalance};

/// Calculate rental cost using marketplace-2-compute pricing formula.
///
/// This is a pure function with no external dependencies. All pricing information
/// is provided as parameters.
///
/// # Arguments
///
/// * `gpu_hours` - Total GPU hours consumed (from telemetry)
/// * `price_per_gpu` - Final price per GPU per hour (should already include any markup)
/// * `gpu_count` - Number of GPUs in the rental
///
/// # Returns
///
/// A `CostBreakdown` containing:
/// - `base_cost`: Same as total_cost (no markup applied here)
/// - `total_cost`: Final cost (gpu_hours × price_per_gpu × gpu_count)
///
/// # Examples
///
/// ```rust
/// use rust_decimal::Decimal;
/// use rust_decimal_macros::dec;
/// use basilica_billing::domain::cost_calculator::calculate_marketplace_cost;
///
/// // 10 hours × $2.75/GPU × 2 GPUs = $55
/// let breakdown = calculate_marketplace_cost(
///     dec!(10.0),  // gpu_hours
///     dec!(2.75),  // price_per_gpu (already includes markup)
///     2,           // gpu_count
/// );
///
/// assert_eq!(breakdown.base_cost.as_decimal(), dec!(55.00));
/// assert_eq!(breakdown.total_cost.as_decimal(), dec!(55.00));
/// ```
pub fn calculate_marketplace_cost(
    gpu_hours: Decimal,
    price_per_gpu: Decimal,
    gpu_count: u32,
) -> CostBreakdown {
    // Ensure at least 1 GPU for calculation
    let effective_gpu_count = Decimal::from(gpu_count.max(1));

    // Calculate total cost
    // total_cost = gpu_hours × price_per_gpu × gpu_count
    let total_cost = gpu_hours
        .checked_mul(price_per_gpu)
        .and_then(|v| v.checked_mul(effective_gpu_count))
        .unwrap_or_else(|| {
            tracing::error!(
                "Cost calculation overflow: {} hours × ${} × {} GPUs",
                gpu_hours,
                price_per_gpu,
                gpu_count
            );
            Decimal::ZERO
        });

    CostBreakdown {
        base_cost: CreditBalance::from_decimal(total_cost),
        usage_cost: CreditBalance::zero(), // Reserved for future use
        volume_discount: CreditBalance::zero(), // No volume discounts in marketplace model
        discounts: CreditBalance::zero(),  // No discounts in this model
        overage_charges: CreditBalance::zero(), // Reserved for future use
        total_cost: CreditBalance::from_decimal(total_cost),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal_macros::dec;

    #[test]
    fn test_marketplace_cost_basic() {
        // 1 hour × $3.30/GPU × 1 GPU = $3.30
        let breakdown = calculate_marketplace_cost(
            dec!(1.0),  // gpu_hours
            dec!(3.30), // price_per_gpu (already includes markup)
            1,          // gpu_count
        );

        assert_eq!(breakdown.base_cost.as_decimal(), dec!(3.30));
        assert_eq!(breakdown.total_cost.as_decimal(), dec!(3.30));
        assert_eq!(breakdown.discounts.as_decimal(), dec!(0.00));
    }

    #[test]
    fn test_marketplace_cost_multi_gpu() {
        // 10.5 hours × $2.75/GPU × 2 GPUs = $57.75
        let breakdown = calculate_marketplace_cost(
            dec!(10.5), // gpu_hours
            dec!(2.75), // price_per_gpu (already includes markup)
            2,          // gpu_count
        );

        assert_eq!(breakdown.base_cost.as_decimal(), dec!(57.75));
        assert_eq!(breakdown.total_cost.as_decimal(), dec!(57.75));
        assert_eq!(breakdown.discounts.as_decimal(), dec!(0.00));
    }

    #[test]
    fn test_marketplace_cost_zero_hours() {
        // 0 hours × $2.75/GPU × 1 GPU = $0.00
        let breakdown = calculate_marketplace_cost(
            dec!(0.0),  // gpu_hours
            dec!(2.75), // price_per_gpu
            1,          // gpu_count
        );

        assert_eq!(breakdown.base_cost.as_decimal(), dec!(0.00));
        assert_eq!(breakdown.total_cost.as_decimal(), dec!(0.00));
    }

    #[test]
    fn test_marketplace_cost_zero_gpu_count() {
        // Edge case: 0 GPUs should be treated as 1 GPU
        let breakdown = calculate_marketplace_cost(
            dec!(1.0),  // gpu_hours
            dec!(2.20), // price_per_gpu
            0,          // gpu_count (treated as 1)
        );

        assert_eq!(breakdown.base_cost.as_decimal(), dec!(2.20));
        assert_eq!(breakdown.total_cost.as_decimal(), dec!(2.20));
    }

    #[test]
    fn test_marketplace_cost_fractional_hours() {
        // 0.5 hours × $4.40/GPU × 1 GPU = $2.20
        let breakdown = calculate_marketplace_cost(
            dec!(0.5),  // gpu_hours
            dec!(4.40), // price_per_gpu
            1,          // gpu_count
        );

        assert_eq!(breakdown.base_cost.as_decimal(), dec!(2.20));
        assert_eq!(breakdown.total_cost.as_decimal(), dec!(2.20));
    }

    #[test]
    fn test_marketplace_cost_large_numbers() {
        // 1000 hours × $11.00/GPU × 8 GPUs = $88,000
        let breakdown = calculate_marketplace_cost(
            dec!(1000.0), // gpu_hours
            dec!(11.00),  // price_per_gpu (already includes markup)
            8,            // gpu_count
        );

        assert_eq!(breakdown.base_cost.as_decimal(), dec!(88000.00));
        assert_eq!(breakdown.total_cost.as_decimal(), dec!(88000.00));
        assert_eq!(breakdown.discounts.as_decimal(), dec!(0.00));
    }
}
