use crate::domain::types::{CostBreakdown, CreditBalance, DiscountType, UsageMetrics, UserId};
use crate::error::Result;
use crate::metrics::BillingMetrics;
use crate::storage::{PromoCodeRepository, RulesRepository, UserMetadataRepository};
use async_trait::async_trait;
use chrono::Timelike;
use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::Arc;

/// Custom billing rule for special conditions
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BillingRule {
    pub id: String,
    pub name: String,
    pub description: String,
    pub condition: RuleCondition,
    pub action: RuleAction,
    pub priority: u32,
    pub active: bool,
}

/// Conditions for rule evaluation
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum RuleCondition {
    Always,
    MinimumUsage {
        gpu_hours: Decimal,
    },
    ResourceThreshold {
        resource: String,
        threshold: Decimal,
    },
    TimeRange {
        start_hour: u32,
        end_hour: u32,
    },
    UserGroup {
        group: String,
    },
    Custom {
        expression: String,
    },
}

impl RuleCondition {
    pub fn evaluate(&self, usage: &UsageMetrics, _metadata: &HashMap<String, String>) -> bool {
        match self {
            RuleCondition::Always => true,
            RuleCondition::MinimumUsage { gpu_hours } => usage.gpu_hours >= *gpu_hours,
            RuleCondition::ResourceThreshold {
                resource,
                threshold,
            } => match resource.as_str() {
                "gpu" => usage.gpu_hours >= *threshold,
                "cpu" => usage.cpu_hours >= *threshold,
                "memory" => usage.memory_gb_hours >= *threshold,
                "storage" => usage.storage_gb_hours >= *threshold,
                "network" => usage.network_gb >= *threshold,
                _ => false,
            },
            RuleCondition::TimeRange {
                start_hour,
                end_hour,
            } => {
                let current_hour = chrono::Utc::now().hour();
                if start_hour <= end_hour {
                    current_hour >= *start_hour && current_hour < *end_hour
                } else {
                    // Handles overnight ranges (e.g., 22:00 - 06:00)
                    current_hour >= *start_hour || current_hour < *end_hour
                }
            }
            RuleCondition::UserGroup { .. } => {
                // Would check user group from metadata
                false
            }
            RuleCondition::Custom { .. } => {
                // Would evaluate custom expression
                false
            }
        }
    }
}

/// Actions to take when rule conditions are met
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum RuleAction {
    ApplyDiscount { percentage: Decimal },
    ApplyCredit { amount: CreditBalance },
    MultiplyRate { factor: Decimal },
    SetFixedRate { rate: CreditBalance },
    AddCharge { amount: CreditBalance },
}

/// Rules evaluation engine
#[async_trait]
pub trait RulesEvaluator: Send + Sync {
    async fn create_rule(&self, rule: BillingRule) -> Result<()>;

    async fn list_rules(&self) -> Result<Vec<BillingRule>>;

    async fn evaluate_rules(
        &self,
        usage: &UsageMetrics,
        metadata: &HashMap<String, String>,
    ) -> Result<Vec<BillingRule>>;
}

#[allow(dead_code)]
pub struct RulesEngine {
    rules_repository: Arc<dyn RulesRepository>,
    user_metadata_repository: Arc<dyn UserMetadataRepository>,
    promo_code_repository: Arc<dyn PromoCodeRepository>,
    metrics: Option<Arc<BillingMetrics>>,
}

impl RulesEngine {
    pub fn new(
        rules_repository: Arc<dyn RulesRepository>,
        user_metadata_repository: Arc<dyn UserMetadataRepository>,
        promo_code_repository: Arc<dyn PromoCodeRepository>,
    ) -> Self {
        Self {
            rules_repository,
            user_metadata_repository,
            promo_code_repository,
            metrics: None,
        }
    }

    pub fn with_metrics(mut self, metrics: Arc<BillingMetrics>) -> Self {
        self.metrics = Some(metrics);
        self
    }

    #[allow(dead_code)]
    async fn apply_automatic_discounts(
        &self,
        user_id: &UserId,
        promo_code: Option<&str>,
        mut cost: CostBreakdown,
    ) -> Result<CostBreakdown> {
        let user_metadata = self
            .user_metadata_repository
            .get_user_metadata(user_id)
            .await?;
        let tier_discount = user_metadata.effective_discount_percentage();

        let mut best_discount_percentage = tier_discount;
        let mut fixed_discount_amount = CreditBalance::zero();
        let mut promo_applied = false;

        if let Some(code) = promo_code {
            match self.promo_code_repository.validate_and_get(code).await {
                Ok(promo) => {
                    // Promo codes now apply to all rentals (no package-based filtering)
                    if let Some(ref metrics) = self.metrics {
                        metrics
                            .record_promo_code_validation(code, true, "valid")
                            .await;
                    }

                    match promo.discount_type {
                        DiscountType::Percentage => {
                            if promo.discount_value > best_discount_percentage {
                                best_discount_percentage = promo.discount_value;
                                promo_applied = true;
                            }
                        }
                        DiscountType::FixedAmount => {
                            fixed_discount_amount =
                                CreditBalance::from_decimal(promo.discount_value);
                            promo_applied = true;
                        }
                    }

                    let _ = self.promo_code_repository.increment_usage(code).await;

                    if promo_applied {
                        if let Some(ref metrics) = self.metrics {
                            let discount_type = match promo.discount_type {
                                DiscountType::Percentage => "percentage",
                                DiscountType::FixedAmount => "fixed_amount",
                            };
                            let amount = if promo.discount_type == DiscountType::Percentage {
                                let subtotal = cost.base_cost.add(cost.usage_cost);
                                subtotal.multiply(promo.discount_value).as_decimal()
                            } else {
                                promo.discount_value
                            };
                            let amount_f64 = amount.to_f64().unwrap_or(0.0);
                            metrics
                                .record_promo_code_applied(code, discount_type, amount_f64)
                                .await;
                        }
                    }
                }
                Err(_) => {
                    if let Some(ref metrics) = self.metrics {
                        metrics
                            .record_promo_code_validation(code, false, "validation_failed")
                            .await;
                    }
                }
            }
        }

        // Choose the single best discount (no stacking)
        let subtotal = cost.base_cost.add(cost.usage_cost);
        let percentage_discount = if best_discount_percentage > Decimal::ZERO {
            subtotal.multiply(best_discount_percentage)
        } else {
            CreditBalance::zero()
        };

        let chosen_discount =
            if fixed_discount_amount.as_decimal() > percentage_discount.as_decimal() {
                fixed_discount_amount
            } else {
                percentage_discount
            };

        if chosen_discount.as_decimal() > Decimal::ZERO {
            cost.discounts = cost.discounts.add(chosen_discount);

            // Record tier discount metrics only if tier discount was chosen
            if !promo_applied
                && best_discount_percentage == tier_discount
                && chosen_discount == percentage_discount
            {
                if let Some(ref metrics) = self.metrics {
                    let amount = percentage_discount.as_decimal().to_f64().unwrap_or(0.0);
                    metrics
                        .record_tier_discount_applied(&user_metadata.user_tier.to_string(), amount)
                        .await;
                }
            }
        }

        cost.total_cost = cost.calculate_total();
        // Clamp total to zero if negative
        if cost.total_cost.as_decimal().is_sign_negative() {
            cost.total_cost = CreditBalance::zero();
        }
        Ok(cost)
    }

    #[allow(dead_code)]
    fn apply_rule_actions(&self, mut cost: CostBreakdown, rules: &[BillingRule]) -> CostBreakdown {
        for rule in rules {
            match &rule.action {
                RuleAction::ApplyDiscount { percentage } => {
                    let discount_amount = cost.base_cost.multiply(*percentage);
                    cost.discounts = cost.discounts.add(discount_amount);
                }
                RuleAction::ApplyCredit { amount } => {
                    cost.discounts = cost.discounts.add(*amount);
                }
                RuleAction::MultiplyRate { factor } => {
                    cost.base_cost = cost.base_cost.multiply(*factor);
                    cost.usage_cost = cost.usage_cost.multiply(*factor);
                }
                RuleAction::SetFixedRate { rate } => {
                    cost.base_cost = *rate;
                    cost.usage_cost = CreditBalance::zero();
                }
                RuleAction::AddCharge { amount } => {
                    cost.overage_charges = cost.overage_charges.add(*amount);
                }
            }
        }

        cost.total_cost = cost.calculate_total();
        cost
    }
}

#[async_trait]
impl RulesEvaluator for RulesEngine {
    async fn create_rule(&self, rule: BillingRule) -> Result<()> {
        self.rules_repository.create_rule(&rule).await
    }

    async fn list_rules(&self) -> Result<Vec<BillingRule>> {
        self.rules_repository.list_rules().await
    }

    async fn evaluate_rules(
        &self,
        usage: &UsageMetrics,
        metadata: &HashMap<String, String>,
    ) -> Result<Vec<BillingRule>> {
        let rules = self.rules_repository.list_active_rules().await?;
        Ok(rules
            .into_iter()
            .filter(|r| r.condition.evaluate(usage, metadata))
            .collect())
    }
}
