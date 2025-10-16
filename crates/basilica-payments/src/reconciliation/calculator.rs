use crate::reconciliation::{SkipReason, SweepDecision};

pub struct SweepCalculator {
    minimum_threshold_plancks: u128,
    target_balance_plancks: u128,
    estimated_fee_plancks: u128,
}

impl SweepCalculator {
    pub fn new(
        minimum_threshold_plancks: u128,
        target_balance_plancks: u128,
        estimated_fee_plancks: u128,
    ) -> Self {
        Self {
            minimum_threshold_plancks,
            target_balance_plancks,
            estimated_fee_plancks,
        }
    }

    pub fn calculate(&self, current_balance_plancks: u128) -> SweepDecision {
        if current_balance_plancks < self.minimum_threshold_plancks {
            return SweepDecision::Skip {
                reason: SkipReason::BelowThreshold,
            };
        }

        let required_reserve = self.target_balance_plancks + self.estimated_fee_plancks;

        if current_balance_plancks <= required_reserve {
            return SweepDecision::Skip {
                reason: SkipReason::InsufficientForFees,
            };
        }

        let sweep_amount = current_balance_plancks - required_reserve;

        SweepDecision::Sweep {
            amount_plancks: sweep_amount,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const MIN_THRESHOLD: u128 = 100_000_000; // 0.1 TAO
    const TARGET_BALANCE: u128 = 550_000_000; // 0.55 TAO
    const ESTIMATED_FEE: u128 = 50_000_000; // 0.05 TAO

    #[test]
    fn test_sweep_when_above_threshold() {
        let calculator = SweepCalculator::new(MIN_THRESHOLD, TARGET_BALANCE, ESTIMATED_FEE);
        let balance = 1_000_000_000; // 1 TAO

        match calculator.calculate(balance) {
            SweepDecision::Sweep { amount_plancks } => {
                assert_eq!(amount_plancks, 400_000_000); // 1.0 - 0.55 - 0.05 = 0.4 TAO
            }
            _ => panic!("Expected sweep decision"),
        }
    }

    #[test]
    fn test_skip_below_threshold() {
        let calculator = SweepCalculator::new(MIN_THRESHOLD, TARGET_BALANCE, ESTIMATED_FEE);
        let balance = 50_000_000; // 0.05 TAO (below minimum)

        match calculator.calculate(balance) {
            SweepDecision::Skip {
                reason: SkipReason::BelowThreshold,
            } => {}
            _ => panic!("Expected skip below threshold"),
        }
    }

    #[test]
    fn test_skip_insufficient_for_fees() {
        let calculator = SweepCalculator::new(MIN_THRESHOLD, TARGET_BALANCE, ESTIMATED_FEE);
        let balance = 600_000_000; // 0.6 TAO (above threshold but not enough for fees)

        match calculator.calculate(balance) {
            SweepDecision::Skip {
                reason: SkipReason::InsufficientForFees,
            } => {}
            _ => panic!("Expected skip insufficient for fees"),
        }
    }

    #[test]
    fn test_exact_threshold() {
        let calculator = SweepCalculator::new(MIN_THRESHOLD, TARGET_BALANCE, ESTIMATED_FEE);
        let balance = MIN_THRESHOLD;

        match calculator.calculate(balance) {
            SweepDecision::Skip { .. } => {}
            _ => panic!("Expected skip at exact threshold"),
        }
    }

    #[test]
    fn test_large_balance() {
        let calculator = SweepCalculator::new(MIN_THRESHOLD, TARGET_BALANCE, ESTIMATED_FEE);
        let balance = 10_000_000_000; // 10 TAO

        match calculator.calculate(balance) {
            SweepDecision::Sweep { amount_plancks } => {
                assert_eq!(amount_plancks, 9_400_000_000); // 10.0 - 0.55 - 0.05 = 9.4 TAO
            }
            _ => panic!("Expected sweep for large balance"),
        }
    }

    #[test]
    fn test_edge_case_just_above_reserve() {
        let calculator = SweepCalculator::new(MIN_THRESHOLD, TARGET_BALANCE, ESTIMATED_FEE);
        let balance = 601_000_000; // Just 1 planck above reserve

        match calculator.calculate(balance) {
            SweepDecision::Sweep { amount_plancks } => {
                assert_eq!(amount_plancks, 1_000_000); // Tiny sweep amount
            }
            _ => panic!("Expected sweep even for tiny amount"),
        }
    }
}
