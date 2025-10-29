use crate::domain::types::{DiscountType, PackageId};
use crate::error::{BillingError, Result};
use async_trait::async_trait;
use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use sqlx::{PgPool, Row};
use std::str::FromStr;
use tracing::{debug, info, warn};

#[derive(Clone, Debug)]
pub struct PromoCode {
    pub code: String,
    pub discount_type: DiscountType,
    pub discount_value: Decimal,
    pub max_uses: Option<i32>,
    pub current_uses: i32,
    pub valid_from: DateTime<Utc>,
    pub valid_until: Option<DateTime<Utc>>,
    pub active: bool,
    pub applicable_packages: Vec<PackageId>,
    pub description: String,
}

impl PromoCode {
    pub fn is_valid(&self) -> bool {
        if !self.active {
            return false;
        }

        let now = Utc::now();
        if now < self.valid_from {
            return false;
        }

        if let Some(expiry) = self.valid_until {
            if now > expiry {
                return false;
            }
        }

        if let Some(max) = self.max_uses {
            if self.current_uses >= max {
                return false;
            }
        }

        true
    }
}

#[async_trait]
pub trait PromoCodeRepository: Send + Sync {
    async fn get_promo_code(&self, code: &str) -> Result<Option<PromoCode>>;
    async fn validate_and_get(&self, code: &str) -> Result<PromoCode>;
    async fn increment_usage(&self, code: &str) -> Result<()>;
}

pub struct SqlPromoCodeRepository {
    pool: PgPool,
}

impl SqlPromoCodeRepository {
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }
}

#[async_trait]
impl PromoCodeRepository for SqlPromoCodeRepository {
    async fn get_promo_code(&self, code: &str) -> Result<Option<PromoCode>> {
        debug!(code = %code, "Fetching promo code");

        let row = sqlx::query(
            r#"
            SELECT code, discount_type, discount_value, max_uses, current_uses,
                   valid_from, valid_until, active, applicable_packages, description
            FROM billing.promo_codes
            WHERE code = $1
            "#,
        )
        .bind(code)
        .fetch_optional(&self.pool)
        .await
        .map_err(|e| {
            warn!(
                code = %code,
                error = %e,
                "Failed to fetch promo code from database"
            );
            BillingError::DatabaseError {
                operation: "get_promo_code".to_string(),
                source: Box::new(e),
            }
        })?;

        if let Some(row) = row {
            let discount_type_str: String = row.get("discount_type");
            let discount_type =
                DiscountType::from_str(&discount_type_str).unwrap_or(DiscountType::Percentage);

            let applicable_packages: Vec<String> = row.get("applicable_packages");
            let packages = applicable_packages
                .into_iter()
                .map(PackageId::new)
                .collect();

            let promo = PromoCode {
                code: row.get("code"),
                discount_type,
                discount_value: row.get("discount_value"),
                max_uses: row.get("max_uses"),
                current_uses: row.get("current_uses"),
                valid_from: row.get("valid_from"),
                valid_until: row.get("valid_until"),
                active: row.get("active"),
                applicable_packages: packages,
                description: row.get("description"),
            };

            info!(
                code = %code,
                discount_type = ?promo.discount_type,
                discount_value = %promo.discount_value,
                active = %promo.active,
                current_uses = %promo.current_uses,
                max_uses = ?promo.max_uses,
                "Promo code retrieved from database"
            );

            Ok(Some(promo))
        } else {
            debug!(code = %code, "Promo code not found in database");
            Ok(None)
        }
    }

    async fn validate_and_get(&self, code: &str) -> Result<PromoCode> {
        debug!(code = %code, "Validating promo code");

        let promo = self.get_promo_code(code).await?.ok_or_else(|| {
            warn!(code = %code, "Promo code not found during validation");
            BillingError::ValidationError {
                field: "promo_code".to_string(),
                message: "Promo code not found".to_string(),
            }
        })?;

        if !promo.is_valid() {
            if !promo.active {
                warn!(code = %code, "Promo code is not active");
                return Err(BillingError::ValidationError {
                    field: "promo_code".to_string(),
                    message: "Promo code is not active".to_string(),
                });
            }

            let now = Utc::now();
            if now < promo.valid_from {
                warn!(
                    code = %code,
                    valid_from = %promo.valid_from,
                    "Promo code not yet valid"
                );
                return Err(BillingError::ValidationError {
                    field: "promo_code".to_string(),
                    message: "Promo code not yet valid".to_string(),
                });
            }

            if let Some(expiry) = promo.valid_until {
                if now > expiry {
                    warn!(
                        code = %code,
                        expired_at = %expiry,
                        "Promo code has expired"
                    );
                    return Err(BillingError::ValidationError {
                        field: "promo_code".to_string(),
                        message: "Promo code expired".to_string(),
                    });
                }
            }

            if let Some(max) = promo.max_uses {
                if promo.current_uses >= max {
                    warn!(
                        code = %code,
                        current_uses = %promo.current_uses,
                        max_uses = %max,
                        "Promo code usage limit reached"
                    );
                    return Err(BillingError::ValidationError {
                        field: "promo_code".to_string(),
                        message: "Promo code usage limit reached".to_string(),
                    });
                }
            }
        }

        info!(code = %code, "Promo code validation successful");
        Ok(promo)
    }

    async fn increment_usage(&self, code: &str) -> Result<()> {
        debug!(code = %code, "Incrementing promo code usage with race condition protection");

        let result = sqlx::query(
            r#"
            UPDATE billing.promo_codes
            SET current_uses = current_uses + 1, updated_at = NOW()
            WHERE code = $1
              AND active = true
              AND (max_uses IS NULL OR current_uses < max_uses)
              AND (valid_until IS NULL OR valid_until > NOW())
              AND valid_from <= NOW()
            "#,
        )
        .bind(code)
        .execute(&self.pool)
        .await
        .map_err(|e| {
            warn!(
                code = %code,
                error = %e,
                "Failed to increment promo code usage"
            );
            BillingError::DatabaseError {
                operation: "increment_promo_uses".to_string(),
                source: Box::new(e),
            }
        })?;

        if result.rows_affected() == 0 {
            warn!(
                code = %code,
                "Promo code increment failed - code not found, inactive, expired, or limit reached"
            );
            return Err(BillingError::ValidationError {
                field: "promo_code".to_string(),
                message: "Promo code cannot be used - it may be inactive, expired, or has reached its usage limit".to_string(),
            });
        }

        info!(
            code = %code,
            "Promo code usage incremented successfully with atomic validation"
        );
        Ok(())
    }
}
