use crate::error::{BillingError, Result};
use crate::pricing::types::GpuPrice;
use chrono::Utc;
use sqlx::PgPool;
use tracing::{debug, info};

/// Price cache for storing and retrieving GPU prices from database
pub struct PriceCache {
    pool: PgPool,
}

/// Check if a price is expired (standalone function for easier testing)
fn is_price_expired(price: &GpuPrice, ttl_seconds: u64) -> bool {
    let now = Utc::now();
    let age = now - price.updated_at;
    age.num_seconds() >= ttl_seconds as i64
}

impl PriceCache {
    /// Create a new price cache
    pub fn new(pool: PgPool) -> Self {
        Self { pool }
    }

    /// Create a fake cache for testing (never actually touches database)
    #[cfg(test)]
    pub fn new_fake() -> Self {
        use sqlx::postgres::PgPoolOptions;
        // Create a pool with an invalid connection that will never be used
        let pool = PgPoolOptions::new()
            .max_connections(1)
            .connect_lazy("postgresql://fake:fake@localhost/fake")
            .expect("Failed to create fake pool");
        Self { pool }
    }

    /// Get reference to the database pool (for internal use by PricingService)
    pub(crate) fn pool(&self) -> &PgPool {
        &self.pool
    }

    /// Store prices in cache
    pub async fn store(&self, prices: Vec<GpuPrice>) -> Result<()> {
        info!("Storing {} prices in cache", prices.len());

        for price in prices {
            self.store_single(price).await?;
        }

        Ok(())
    }

    /// Store a single price in cache
    async fn store_single(&self, price: GpuPrice) -> Result<()> {
        let expires_at = price.updated_at + chrono::Duration::seconds(86400); // 24 hours from updated_at

        sqlx::query(
            r#"
            INSERT INTO billing.price_cache (
                gpu_model, vram_gb, market_price_per_hour, discounted_price_per_hour,
                discount_percent, source, provider, location, instance_name,
                updated_at, expires_at, is_spot
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            ON CONFLICT (gpu_model, provider, location, is_spot)
            DO UPDATE SET
                vram_gb = EXCLUDED.vram_gb,
                market_price_per_hour = EXCLUDED.market_price_per_hour,
                discounted_price_per_hour = EXCLUDED.discounted_price_per_hour,
                discount_percent = EXCLUDED.discount_percent,
                source = EXCLUDED.source,
                instance_name = EXCLUDED.instance_name,
                updated_at = EXCLUDED.updated_at,
                expires_at = EXCLUDED.expires_at
            "#,
        )
        .bind(&price.gpu_model)
        .bind(price.vram_gb.map(|v| v as i32))
        .bind(price.market_price_per_hour)
        .bind(price.discounted_price_per_hour)
        .bind(price.discount_percent)
        .bind(&price.source)
        .bind(&price.provider)
        .bind(&price.location)
        .bind(&price.instance_name)
        .bind(price.updated_at)
        .bind(expires_at)
        .bind(price.is_spot)
        .execute(&self.pool)
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "store_price".to_string(),
            source: Box::new(e),
        })?;

        Ok(())
    }

    /// Get cached price for a specific GPU model
    pub async fn get(&self, gpu_model: &str) -> Result<Option<GpuPrice>> {
        debug!("Fetching cached price for GPU model: {}", gpu_model);

        let row = sqlx::query_as::<_, GpuPriceRow>(
            r#"
            SELECT
                gpu_model, vram_gb, market_price_per_hour, discounted_price_per_hour,
                discount_percent, source, provider, location, instance_name,
                updated_at, is_spot
            FROM billing.price_cache
            WHERE gpu_model = $1
                AND is_spot = false
                AND expires_at > NOW()
            ORDER BY updated_at DESC
            LIMIT 1
            "#,
        )
        .bind(gpu_model)
        .fetch_optional(&self.pool)
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "get_price".to_string(),
            source: Box::new(e),
        })?;

        Ok(row.map(|r| r.into()))
    }

    /// Get all cached prices
    pub async fn get_all(&self) -> Result<Vec<GpuPrice>> {
        debug!("Fetching all cached prices");

        let rows = sqlx::query_as::<_, GpuPriceRow>(
            r#"
            SELECT
                gpu_model, vram_gb, market_price_per_hour, discounted_price_per_hour,
                discount_percent, source, provider, location, instance_name,
                updated_at, is_spot
            FROM billing.price_cache
            WHERE expires_at > NOW()
            ORDER BY gpu_model, updated_at DESC
            "#,
        )
        .fetch_all(&self.pool)
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "get_all_prices".to_string(),
            source: Box::new(e),
        })?;

        Ok(rows.into_iter().map(|r| r.into()).collect())
    }

    /// Check if a price is expired
    pub fn is_expired(&self, price: &GpuPrice, ttl_seconds: u64) -> bool {
        is_price_expired(price, ttl_seconds)
    }

    /// Clear expired cache entries
    pub async fn clear_expired(&self, _ttl_seconds: u64) -> Result<u64> {
        debug!("Clearing expired cache entries");

        let result = sqlx::query(
            r#"
            DELETE FROM billing.price_cache
            WHERE expires_at <= NOW()
            "#,
        )
        .execute(&self.pool)
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "clear_expired".to_string(),
            source: Box::new(e),
        })?;

        info!("Cleared {} expired price entries", result.rows_affected());

        Ok(result.rows_affected())
    }
}

/// Database row representation for GpuPrice
#[derive(sqlx::FromRow)]
struct GpuPriceRow {
    gpu_model: String,
    vram_gb: Option<i32>,
    market_price_per_hour: rust_decimal::Decimal,
    discounted_price_per_hour: rust_decimal::Decimal,
    discount_percent: rust_decimal::Decimal,
    source: String,
    provider: String,
    location: Option<String>,
    instance_name: Option<String>,
    updated_at: chrono::DateTime<Utc>,
    is_spot: bool,
}

impl From<GpuPriceRow> for GpuPrice {
    fn from(row: GpuPriceRow) -> Self {
        GpuPrice {
            gpu_model: row.gpu_model,
            vram_gb: row.vram_gb.map(|v| v as u32),
            market_price_per_hour: row.market_price_per_hour,
            discounted_price_per_hour: row.discounted_price_per_hour,
            discount_percent: row.discount_percent,
            source: row.source,
            provider: row.provider,
            location: row.location,
            instance_name: row.instance_name,
            updated_at: row.updated_at,
            is_spot: row.is_spot,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal::Decimal;

    #[test]
    fn test_is_expired() {
        let mut price = GpuPrice {
            gpu_model: "H100".to_string(),
            vram_gb: Some(80),
            market_price_per_hour: Decimal::from(100),
            discounted_price_per_hour: Decimal::from(80),
            discount_percent: Decimal::from(-20),
            source: "test".to_string(),
            provider: "test".to_string(),
            location: None,
            instance_name: None,
            updated_at: Utc::now(),
            is_spot: false,
        };

        // Fresh price should not be expired
        assert!(!is_price_expired(&price, 86400));

        // Old price should be expired
        price.updated_at = Utc::now() - chrono::Duration::seconds(100000);
        assert!(is_price_expired(&price, 86400));
    }

    #[test]
    fn test_is_expired_boundary() {
        // Test exactly at boundary
        let price = GpuPrice {
            gpu_model: "A100".to_string(),
            vram_gb: Some(40),
            market_price_per_hour: Decimal::from(50),
            discounted_price_per_hour: Decimal::from(40),
            discount_percent: Decimal::from(-20),
            source: "test".to_string(),
            provider: "test".to_string(),
            location: None,
            instance_name: None,
            updated_at: Utc::now() - chrono::Duration::seconds(86400),
            is_spot: false,
        };

        // Exactly at TTL boundary should be expired
        assert!(is_price_expired(&price, 86400));
    }

    #[test]
    fn test_is_expired_just_before_expiry() {
        // Test just before expiry
        let price = GpuPrice {
            gpu_model: "H200".to_string(),
            vram_gb: Some(80),
            market_price_per_hour: Decimal::from(120),
            discounted_price_per_hour: Decimal::from(96),
            discount_percent: Decimal::from(-20),
            source: "test".to_string(),
            provider: "test".to_string(),
            location: None,
            instance_name: None,
            updated_at: Utc::now() - chrono::Duration::seconds(86399),
            is_spot: false,
        };

        // Just before expiry should not be expired
        assert!(!is_price_expired(&price, 86400));
    }
}
