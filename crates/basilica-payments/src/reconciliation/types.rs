use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReconciliationSweep {
    pub id: i64,
    pub account_hex: String,
    pub hotwallet_address_ss58: String,
    pub coldwallet_address_ss58: String,
    pub balance_before_plancks: String,
    pub sweep_amount_plancks: String,
    pub estimated_fee_plancks: String,
    pub balance_after_plancks: Option<String>,
    pub status: SweepStatus,
    pub dry_run: bool,
    pub tx_hash: Option<String>,
    pub block_number: Option<i64>,
    pub error_message: Option<String>,
    pub initiated_at: DateTime<Utc>,
    pub completed_at: Option<DateTime<Utc>>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SweepStatus {
    Pending,
    Submitted,
    Confirmed,
    Failed,
}

impl SweepStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            SweepStatus::Pending => "pending",
            SweepStatus::Submitted => "submitted",
            SweepStatus::Confirmed => "confirmed",
            SweepStatus::Failed => "failed",
        }
    }
}

impl std::fmt::Display for SweepStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.as_str())
    }
}

#[derive(Debug, Clone)]
pub enum SweepDecision {
    Sweep { amount_plancks: u128 },
    Skip { reason: SkipReason },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SkipReason {
    BelowThreshold,
    InsufficientForFees,
    RecentSweep,
}

impl std::fmt::Display for SkipReason {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SkipReason::BelowThreshold => write!(f, "balance below minimum threshold"),
            SkipReason::InsufficientForFees => write!(f, "insufficient balance for fees"),
            SkipReason::RecentSweep => write!(f, "recent sweep within cooldown period"),
        }
    }
}

#[derive(Debug, Clone)]
pub struct SweepSummary {
    pub total_checked: usize,
    pub swept_count: usize,
    pub failed_count: usize,
    pub skipped_count: usize,
    pub total_amount_plancks: u128,
}

impl SweepSummary {
    pub fn new() -> Self {
        Self {
            total_checked: 0,
            swept_count: 0,
            failed_count: 0,
            skipped_count: 0,
            total_amount_plancks: 0,
        }
    }
}

impl Default for SweepSummary {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_sweep_status_string() {
        assert_eq!(SweepStatus::Pending.as_str(), "pending");
        assert_eq!(SweepStatus::Submitted.as_str(), "submitted");
        assert_eq!(SweepStatus::Confirmed.as_str(), "confirmed");
        assert_eq!(SweepStatus::Failed.as_str(), "failed");
    }

    #[test]
    fn test_skip_reason_display() {
        assert_eq!(
            format!("{}", SkipReason::BelowThreshold),
            "balance below minimum threshold"
        );
        assert_eq!(
            format!("{}", SkipReason::InsufficientForFees),
            "insufficient balance for fees"
        );
        assert_eq!(
            format!("{}", SkipReason::RecentSweep),
            "recent sweep within cooldown period"
        );
    }

    #[test]
    fn test_sweep_summary_default() {
        let summary = SweepSummary::default();
        assert_eq!(summary.total_checked, 0);
        assert_eq!(summary.swept_count, 0);
        assert_eq!(summary.failed_count, 0);
        assert_eq!(summary.skipped_count, 0);
        assert_eq!(summary.total_amount_plancks, 0);
    }
}
