use crate::error::{BillingError, Result};
use crate::pricing::types::GpuPrice;
use crate::storage::rds::RdsConnection;
use async_trait::async_trait;
use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use std::sync::Arc;
use tracing::{debug, info};

/// Filter options for querying price history
#[derive(Debug, Clone)]
pub struct PriceHistoryFilter {
    pub gpu_model: String,
    pub start_time: Option<DateTime<Utc>>,
    pub end_time: Option<DateTime<Utc>>,
    pub providers: Vec<String>,
    pub limit: u32,
}

/// Entry in price history
#[derive(Debug, Clone)]
pub struct PriceHistoryEntry {
    pub gpu_model: String,
    pub price_per_hour: Decimal,
    pub source: String,
    pub provider: String,
    pub recorded_at: DateTime<Utc>,
}

/// Repository trait for price cache operations
#[async_trait]
pub trait PriceCacheRepository: Send + Sync {
    /// Store prices in cache
    async fn store(&self, prices: Vec<GpuPrice>, ttl_seconds: u64) -> Result<()>;

    /// Get cached price for a specific GPU model
    async fn get(&self, gpu_model: &str) -> Result<Option<GpuPrice>>;

    /// Get all cached prices
    async fn get_all(&self) -> Result<Vec<GpuPrice>>;

    /// Clear expired cache entries
    async fn clear_expired(&self, ttl_seconds: u64) -> Result<u64>;

    /// Record price history for tracking price changes over time
    async fn record_price_history(&self, prices: &[GpuPrice]) -> Result<()>;

    /// Get price history with optional filters
    async fn get_price_history(&self, filter: PriceHistoryFilter)
        -> Result<Vec<PriceHistoryEntry>>;

    /// Check if a price is expired
    fn is_expired(&self, price: &GpuPrice, ttl_seconds: u64) -> bool {
        is_price_expired(price, ttl_seconds)
    }
}

/// SQL implementation of price cache repository
pub struct SqlPriceCacheRepository {
    connection: Arc<RdsConnection>,
}

impl SqlPriceCacheRepository {
    /// Create a new SQL price cache repository
    pub fn new(connection: Arc<RdsConnection>) -> Self {
        Self { connection }
    }

    /// Store a single price in cache
    async fn store_single(&self, price: GpuPrice, ttl_seconds: u64) -> Result<()> {
        let expires_at = price.updated_at + chrono::Duration::seconds(ttl_seconds as i64);

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
        .execute(self.connection.pool())
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "store_price".to_string(),
            source: Box::new(e),
        })?;

        Ok(())
    }
}

#[async_trait]
impl PriceCacheRepository for SqlPriceCacheRepository {
    async fn store(&self, prices: Vec<GpuPrice>, ttl_seconds: u64) -> Result<()> {
        info!("Storing {} prices in cache", prices.len());

        for price in prices {
            self.store_single(price, ttl_seconds).await?;
        }

        Ok(())
    }

    async fn get(&self, gpu_model: &str) -> Result<Option<GpuPrice>> {
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
        .fetch_optional(self.connection.pool())
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "get_price".to_string(),
            source: Box::new(e),
        })?;

        Ok(row.map(|r| r.into()))
    }

    async fn get_all(&self) -> Result<Vec<GpuPrice>> {
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
        .fetch_all(self.connection.pool())
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "get_all_prices".to_string(),
            source: Box::new(e),
        })?;

        Ok(rows.into_iter().map(|r| r.into()).collect())
    }

    async fn clear_expired(&self, _ttl_seconds: u64) -> Result<u64> {
        debug!("Clearing expired cache entries");

        let result = sqlx::query(
            r#"
            DELETE FROM billing.price_cache
            WHERE expires_at <= NOW()
            "#,
        )
        .execute(self.connection.pool())
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "clear_expired".to_string(),
            source: Box::new(e),
        })?;

        info!("Cleared {} expired price entries", result.rows_affected());

        Ok(result.rows_affected())
    }

    async fn record_price_history(&self, prices: &[GpuPrice]) -> Result<()> {
        debug!("Recording {} prices to history", prices.len());

        for price in prices {
            sqlx::query(
                r#"
                INSERT INTO billing.price_history (
                    gpu_model, price_per_hour, source, provider, recorded_at
                )
                VALUES ($1, $2, $3, $4, NOW())
                "#,
            )
            .bind(&price.gpu_model)
            .bind(price.discounted_price_per_hour)
            .bind(&price.source)
            .bind(&price.provider)
            .execute(self.connection.pool())
            .await
            .map_err(|e| BillingError::DatabaseError {
                operation: "record_price_history".to_string(),
                source: Box::new(e),
            })?;
        }

        info!("Recorded {} price history entries", prices.len());
        Ok(())
    }

    async fn get_price_history(
        &self,
        filter: PriceHistoryFilter,
    ) -> Result<Vec<PriceHistoryEntry>> {
        debug!("Fetching price history for GPU: {}", filter.gpu_model);

        let limit = if filter.limit == 0 { 100 } else { filter.limit };

        let mut query = String::from(
            "SELECT gpu_model, price_per_hour, source, provider, recorded_at
             FROM billing.price_history
             WHERE gpu_model = $1",
        );

        let mut param_count = 2;

        // Add time filters if provided
        if filter.start_time.is_some() {
            query.push_str(&format!(" AND recorded_at >= ${}", param_count));
            param_count += 1;
        }
        if filter.end_time.is_some() {
            query.push_str(&format!(" AND recorded_at <= ${}", param_count));
            param_count += 1;
        }

        // Add provider filter if provided
        if !filter.providers.is_empty() {
            query.push_str(&format!(" AND provider = ANY(${})", param_count));
        }

        query.push_str(&format!(" ORDER BY recorded_at DESC LIMIT {}", limit));

        // Build and execute query
        let mut query_builder =
            sqlx::query_as::<_, (String, Decimal, String, String, DateTime<Utc>)>(&query);

        query_builder = query_builder.bind(&filter.gpu_model);

        if let Some(start_time) = filter.start_time {
            query_builder = query_builder.bind(start_time);
        }

        if let Some(end_time) = filter.end_time {
            query_builder = query_builder.bind(end_time);
        }

        if !filter.providers.is_empty() {
            query_builder = query_builder.bind(&filter.providers);
        }

        let rows = query_builder
            .fetch_all(self.connection.pool())
            .await
            .map_err(|e| BillingError::DatabaseError {
                operation: "get_price_history".to_string(),
                source: Box::new(e),
            })?;

        Ok(rows
            .into_iter()
            .map(
                |(gpu_model, price, source, provider, recorded_at)| PriceHistoryEntry {
                    gpu_model,
                    price_per_hour: price,
                    source,
                    provider,
                    recorded_at,
                },
            )
            .collect())
    }
}

/// Check if a price is expired (standalone function for easier testing)
fn is_price_expired(price: &GpuPrice, ttl_seconds: u64) -> bool {
    let now = Utc::now();
    let age = now - price.updated_at;
    age.num_seconds() >= ttl_seconds as i64
}

/// Database row representation for GpuPrice
#[derive(sqlx::FromRow)]
struct GpuPriceRow {
    gpu_model: String,
    vram_gb: Option<i32>,
    market_price_per_hour: Decimal,
    discounted_price_per_hour: Decimal,
    discount_percent: Decimal,
    source: String,
    provider: String,
    location: Option<String>,
    instance_name: Option<String>,
    updated_at: DateTime<Utc>,
    is_spot: bool,
}

impl From<GpuPriceRow> for GpuPrice {
    fn from(row: GpuPriceRow) -> Self {
        GpuPrice {
            gpu_model: row.gpu_model,
            vram_gb: row.vram_gb.map(|v| v as u32),
            num_gpus: 1, // Default to 1 GPU for cached prices
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
            num_gpus: 1,
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
            num_gpus: 1,
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
            num_gpus: 1,
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

    #[test]
    fn test_is_expired_custom_ttl() {
        // Test with custom TTL of 3600 seconds (1 hour)
        let custom_ttl = 3600;

        // Fresh price should not be expired
        let fresh_price = GpuPrice {
            gpu_model: "H100".to_string(),
            vram_gb: Some(80),
            num_gpus: 1,
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
        assert!(!is_price_expired(&fresh_price, custom_ttl));

        // Price older than custom TTL should be expired
        let old_price = GpuPrice {
            gpu_model: "A100".to_string(),
            vram_gb: Some(40),
            num_gpus: 1,
            market_price_per_hour: Decimal::from(50),
            discounted_price_per_hour: Decimal::from(40),
            discount_percent: Decimal::from(-20),
            source: "test".to_string(),
            provider: "test".to_string(),
            location: None,
            instance_name: None,
            updated_at: Utc::now() - chrono::Duration::seconds(3601),
            is_spot: false,
        };
        assert!(is_price_expired(&old_price, custom_ttl));

        // Price just at the TTL boundary should be expired
        let boundary_price = GpuPrice {
            gpu_model: "H200".to_string(),
            vram_gb: Some(80),
            num_gpus: 1,
            market_price_per_hour: Decimal::from(120),
            discounted_price_per_hour: Decimal::from(96),
            discount_percent: Decimal::from(-20),
            source: "test".to_string(),
            provider: "test".to_string(),
            location: None,
            instance_name: None,
            updated_at: Utc::now() - chrono::Duration::seconds(custom_ttl as i64),
            is_spot: false,
        };
        assert!(is_price_expired(&boundary_price, custom_ttl));

        // Price just before TTL should not be expired
        let just_before_price = GpuPrice {
            gpu_model: "A6000".to_string(),
            vram_gb: Some(48),
            num_gpus: 1,
            market_price_per_hour: Decimal::from(40),
            discounted_price_per_hour: Decimal::from(32),
            discount_percent: Decimal::from(-20),
            source: "test".to_string(),
            provider: "test".to_string(),
            location: None,
            instance_name: None,
            updated_at: Utc::now() - chrono::Duration::seconds((custom_ttl - 1) as i64),
            is_spot: false,
        };
        assert!(!is_price_expired(&just_before_price, custom_ttl));
    }
}
