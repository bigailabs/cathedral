use crate::domain::types::{UserId, UserMetadata, UserTier};
use crate::error::{BillingError, Result};
use async_trait::async_trait;
use chrono::Utc;
use rust_decimal::Decimal;
use sqlx::{PgPool, Row};
use std::collections::HashMap;
use std::str::FromStr;
use tracing::{debug, info, warn};

#[async_trait]
pub trait UserMetadataRepository: Send + Sync {
    async fn get_user_metadata(&self, user_id: &UserId) -> Result<UserMetadata>;
    async fn update_user_tier(&self, user_id: &UserId, tier: UserTier) -> Result<()>;
    async fn set_custom_discount(&self, user_id: &UserId, percentage: Decimal) -> Result<()>;
}

pub struct SqlUserMetadataRepository {
    pool: PgPool,
}

impl SqlUserMetadataRepository {
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }
}

#[async_trait]
impl UserMetadataRepository for SqlUserMetadataRepository {
    async fn get_user_metadata(&self, user_id: &UserId) -> Result<UserMetadata> {
        debug!(user_id = %user_id.as_str(), "Fetching user metadata");

        let row = sqlx::query(
            r#"
            SELECT user_id, user_tier, discount_percentage, promo_codes,
                   tier_updated_at, custom_attributes
            FROM billing.user_metadata
            WHERE user_id = $1
            "#,
        )
        .bind(user_id.as_str())
        .fetch_optional(&self.pool)
        .await
        .map_err(|e| {
            warn!(
                user_id = %user_id.as_str(),
                error = %e,
                "Failed to fetch user metadata from database"
            );
            BillingError::DatabaseError {
                operation: "get_user_metadata".to_string(),
                source: Box::new(e),
            }
        })?;

        if let Some(row) = row {
            let tier_str: String = row.get("user_tier");
            let user_tier = UserTier::from_str(&tier_str).unwrap_or(UserTier::Standard);

            info!(
                user_id = %user_id.as_str(),
                tier = ?user_tier,
                discount = ?row.get::<Option<Decimal>, _>("discount_percentage"),
                "User metadata retrieved from database"
            );

            Ok(UserMetadata {
                user_id: user_id.clone(),
                user_tier,
                discount_percentage: row.get("discount_percentage"),
                promo_codes: row.get("promo_codes"),
                tier_updated_at: row.get("tier_updated_at"),
                custom_attributes: row
                    .try_get::<serde_json::Value, _>("custom_attributes")
                    .ok()
                    .and_then(|v| serde_json::from_value(v).ok())
                    .unwrap_or_default(),
            })
        } else {
            info!(
                user_id = %user_id.as_str(),
                "User metadata not found, returning default Standard tier"
            );

            Ok(UserMetadata {
                user_id: user_id.clone(),
                user_tier: UserTier::Standard,
                discount_percentage: None,
                promo_codes: vec![],
                tier_updated_at: Utc::now(),
                custom_attributes: HashMap::new(),
            })
        }
    }

    async fn update_user_tier(&self, user_id: &UserId, tier: UserTier) -> Result<()> {
        let discount = tier.default_discount_percentage();

        debug!(
            user_id = %user_id.as_str(),
            tier = ?tier,
            discount = ?discount,
            "Updating user tier"
        );

        sqlx::query(
            r#"
            INSERT INTO billing.user_metadata
                (user_id, user_tier, discount_percentage, tier_updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                user_tier = EXCLUDED.user_tier,
                discount_percentage = EXCLUDED.discount_percentage,
                tier_updated_at = NOW(),
                updated_at = NOW()
            "#,
        )
        .bind(user_id.as_str())
        .bind(tier.to_string())
        .bind(discount)
        .execute(&self.pool)
        .await
        .map_err(|e| {
            warn!(
                user_id = %user_id.as_str(),
                tier = ?tier,
                error = %e,
                "Failed to update user tier"
            );
            BillingError::DatabaseError {
                operation: "update_user_tier".to_string(),
                source: Box::new(e),
            }
        })?;

        info!(
            user_id = %user_id.as_str(),
            tier = ?tier,
            discount = ?discount,
            "User tier updated successfully"
        );

        Ok(())
    }

    async fn set_custom_discount(&self, user_id: &UserId, percentage: Decimal) -> Result<()> {
        debug!(
            user_id = %user_id.as_str(),
            percentage = %percentage,
            "Setting custom discount"
        );

        if percentage < Decimal::ZERO || percentage > Decimal::ONE {
            warn!(
                user_id = %user_id.as_str(),
                percentage = %percentage,
                "Invalid discount percentage, must be between 0.0 and 1.0"
            );
            return Err(BillingError::ValidationError {
                field: "discount_percentage".to_string(),
                message: "Discount percentage must be between 0.0 and 1.0".to_string(),
            });
        }

        sqlx::query(
            r#"
            INSERT INTO billing.user_metadata
                (user_id, user_tier, discount_percentage, tier_updated_at)
            VALUES ($1, 'custom', $2, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                user_tier = 'custom',
                discount_percentage = EXCLUDED.discount_percentage,
                tier_updated_at = NOW(),
                updated_at = NOW()
            "#,
        )
        .bind(user_id.as_str())
        .bind(percentage)
        .execute(&self.pool)
        .await
        .map_err(|e| {
            warn!(
                user_id = %user_id.as_str(),
                percentage = %percentage,
                error = %e,
                "Failed to set custom discount"
            );
            BillingError::DatabaseError {
                operation: "set_custom_discount".to_string(),
                source: Box::new(e),
            }
        })?;

        info!(
            user_id = %user_id.as_str(),
            percentage = %percentage,
            "Custom discount set successfully"
        );

        Ok(())
    }
}
