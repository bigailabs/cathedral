use crate::domain::packages::BillingPackage;
use crate::domain::types::{BillingPeriod, CostBreakdown, CreditBalance, PackageId, UsageMetrics};
use crate::error::{BillingError, Result};
use crate::pricing::service::PricingService;
use async_trait::async_trait;
use rust_decimal::Decimal;
use serde_json;
use sqlx::postgres::PgRow;
use sqlx::{PgPool, Row};
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::RwLock;
use tracing::{debug, warn};

#[async_trait]
pub trait PackageRepository: Send + Sync {
    async fn get_package(&self, package_id: &PackageId) -> Result<BillingPackage>;
    async fn list_packages(&self) -> Result<Vec<BillingPackage>>;

    /// Find best matching package for a GPU model
    async fn find_package_for_gpu_model(&self, gpu_model: &str) -> Result<BillingPackage>;

    /// Check if a package supports a specific GPU model
    async fn is_package_compatible_with_gpu(
        &self,
        package_id: &PackageId,
        gpu_model: &str,
    ) -> Result<bool>;

    async fn create_package(&self, package: BillingPackage) -> Result<()>;
    async fn update_package(&self, package: BillingPackage) -> Result<()>;
    async fn delete_package(&self, package_id: &PackageId) -> Result<()>;
    async fn activate_package(&self, package_id: &PackageId) -> Result<()>;
    async fn deactivate_package(&self, package_id: &PackageId) -> Result<()>;
    async fn evaluate_package_cost(
        &self,
        package_id: &PackageId,
        usage: &UsageMetrics,
    ) -> Result<CostBreakdown>;
}

pub struct SqlPackageRepository {
    pool: PgPool,
    cache: Arc<RwLock<HashMap<PackageId, BillingPackage>>>,
    pricing_service: Option<Arc<PricingService>>,
}

const PACKAGE_SELECT_COLUMNS: &str = r#"
    package_id, name, description, hourly_rate, gpu_model,
    billing_period, priority, active, metadata,
    storage_rate_per_gb_hour, network_rate_per_gb, disk_io_rate_per_gb,
    cpu_rate_per_core_hour, memory_rate_per_gb_hour,
    included_storage_gb_hours, included_network_gb, included_disk_io_gb,
    included_cpu_core_hours, included_memory_gb_hours,
    use_dynamic_pricing, last_market_price, price_last_updated_at
"#;

impl SqlPackageRepository {
    pub fn new(pool: PgPool) -> Self {
        Self {
            pool,
            cache: Arc::new(RwLock::new(HashMap::new())),
            pricing_service: None,
        }
    }

    /// Set the pricing service for dynamic pricing integration
    pub fn with_pricing_service(mut self, pricing_service: Arc<PricingService>) -> Self {
        self.pricing_service = Some(pricing_service);
        self
    }

    /// Initialize the repository by loading packages into cache
    pub async fn initialize(&self) -> Result<()> {
        self.refresh_cache().await?;
        Ok(())
    }

    fn row_to_billing_package(row: &PgRow) -> Result<BillingPackage> {
        let billing_period = match row.get::<String, _>("billing_period").as_str() {
            "Hourly" => BillingPeriod::Hourly,
            "Daily" => BillingPeriod::Daily,
            "Weekly" => BillingPeriod::Weekly,
            "Monthly" => BillingPeriod::Monthly,
            _ => BillingPeriod::Hourly,
        };

        Ok(BillingPackage {
            id: PackageId::new(row.get("package_id")),
            name: row.get("name"),
            description: row.get("description"),
            hourly_rate: CreditBalance::from_decimal(row.get("hourly_rate")),
            gpu_model: row.get("gpu_model"),
            billing_period,
            priority: row.get::<i32, _>("priority") as u32,
            active: row.get("active"),
            metadata: row
                .try_get::<Option<serde_json::Value>, _>("metadata")
                .ok()
                .flatten()
                .and_then(|v| serde_json::from_value(v).ok())
                .unwrap_or_default(),

            storage_rate_per_gb_hour: CreditBalance::from_decimal(
                row.try_get("storage_rate_per_gb_hour")
                    .unwrap_or(Decimal::ZERO),
            ),
            network_rate_per_gb: CreditBalance::from_decimal(
                row.try_get("network_rate_per_gb").unwrap_or(Decimal::ZERO),
            ),
            disk_io_rate_per_gb: CreditBalance::from_decimal(
                row.try_get("disk_io_rate_per_gb").unwrap_or(Decimal::ZERO),
            ),
            cpu_rate_per_core_hour: CreditBalance::from_decimal(
                row.try_get("cpu_rate_per_core_hour")
                    .unwrap_or(Decimal::ZERO),
            ),
            memory_rate_per_gb_hour: CreditBalance::from_decimal(
                row.try_get("memory_rate_per_gb_hour")
                    .unwrap_or(Decimal::ZERO),
            ),

            included_storage_gb_hours: row
                .try_get("included_storage_gb_hours")
                .unwrap_or(Decimal::ZERO),
            included_network_gb: row.try_get("included_network_gb").unwrap_or(Decimal::ZERO),
            included_disk_io_gb: row.try_get("included_disk_io_gb").unwrap_or(Decimal::ZERO),
            included_cpu_core_hours: row
                .try_get("included_cpu_core_hours")
                .unwrap_or(Decimal::ZERO),
            included_memory_gb_hours: row
                .try_get("included_memory_gb_hours")
                .unwrap_or(Decimal::ZERO),

            use_dynamic_pricing: row.try_get("use_dynamic_pricing").unwrap_or(false),
            last_market_price: row.try_get("last_market_price").ok(),
            price_last_updated_at: row.try_get("price_last_updated_at").ok(),
        })
    }

    /// Refresh cache from database
    async fn refresh_cache(&self) -> Result<()> {
        let query_str = format!(
            "SELECT {} FROM billing.billing_packages WHERE active = true",
            PACKAGE_SELECT_COLUMNS
        );

        let rows = sqlx::query(&query_str)
            .fetch_all(&self.pool)
            .await
            .map_err(|e| BillingError::DatabaseError {
                operation: "fetch_all_packages".to_string(),
                source: Box::new(e),
            })?;

        let mut cache = self.cache.write().await;
        cache.clear();

        for row in rows {
            let package = Self::row_to_billing_package(&row)?;
            cache.insert(package.id.clone(), package);
        }

        Ok(())
    }

    /// Load package from database
    async fn load_from_database(&self, package_id: &PackageId) -> Result<Option<BillingPackage>> {
        let query_str = format!(
            "SELECT {} FROM billing.billing_packages WHERE package_id = $1",
            PACKAGE_SELECT_COLUMNS
        );

        let row = sqlx::query(&query_str)
            .bind(package_id.as_str())
            .fetch_optional(&self.pool)
            .await
            .map_err(|e| BillingError::DatabaseError {
                operation: "fetch_package".to_string(),
                source: Box::new(e),
            })?;

        row.map(|r| Self::row_to_billing_package(&r)).transpose()
    }

    /// Persist package to database
    async fn persist_to_database(&self, package: &BillingPackage) -> Result<()> {
        sqlx::query(
            r#"
            INSERT INTO billing.billing_packages
                (package_id, name, description, hourly_rate, gpu_model,
                 billing_period, priority, active, metadata,
                 storage_rate_per_gb_hour, network_rate_per_gb, disk_io_rate_per_gb,
                 cpu_rate_per_core_hour, memory_rate_per_gb_hour,
                 included_storage_gb_hours, included_network_gb, included_disk_io_gb,
                 included_cpu_core_hours, included_memory_gb_hours,
                 updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, NOW())
            ON CONFLICT (package_id) DO UPDATE SET
                name = EXCLUDED.name,
                description = EXCLUDED.description,
                hourly_rate = EXCLUDED.hourly_rate,
                gpu_model = EXCLUDED.gpu_model,
                billing_period = EXCLUDED.billing_period,
                priority = EXCLUDED.priority,
                active = EXCLUDED.active,
                metadata = EXCLUDED.metadata,
                storage_rate_per_gb_hour = EXCLUDED.storage_rate_per_gb_hour,
                network_rate_per_gb = EXCLUDED.network_rate_per_gb,
                disk_io_rate_per_gb = EXCLUDED.disk_io_rate_per_gb,
                cpu_rate_per_core_hour = EXCLUDED.cpu_rate_per_core_hour,
                memory_rate_per_gb_hour = EXCLUDED.memory_rate_per_gb_hour,
                included_storage_gb_hours = EXCLUDED.included_storage_gb_hours,
                included_network_gb = EXCLUDED.included_network_gb,
                included_disk_io_gb = EXCLUDED.included_disk_io_gb,
                included_cpu_core_hours = EXCLUDED.included_cpu_core_hours,
                included_memory_gb_hours = EXCLUDED.included_memory_gb_hours,
                updated_at = NOW()
            "#,
        )
        .bind(package.id.as_str())
        .bind(&package.name)
        .bind(&package.description)
        .bind(package.hourly_rate.as_decimal())
        .bind(&package.gpu_model)
        .bind(format!("{:?}", package.billing_period))
        .bind(package.priority as i32)
        .bind(package.active)
        .bind(serde_json::to_value(&package.metadata).unwrap_or(serde_json::json!({})))
        .bind(package.storage_rate_per_gb_hour.as_decimal())
        .bind(package.network_rate_per_gb.as_decimal())
        .bind(package.disk_io_rate_per_gb.as_decimal())
        .bind(package.cpu_rate_per_core_hour.as_decimal())
        .bind(package.memory_rate_per_gb_hour.as_decimal())
        .bind(package.included_storage_gb_hours)
        .bind(package.included_network_gb)
        .bind(package.included_disk_io_gb)
        .bind(package.included_cpu_core_hours)
        .bind(package.included_memory_gb_hours)
        .execute(&self.pool)
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "upsert_package".to_string(),
            source: Box::new(e),
        })?;

        Ok(())
    }
}

#[async_trait]
impl PackageRepository for SqlPackageRepository {
    async fn get_package(&self, package_id: &PackageId) -> Result<BillingPackage> {
        // Check cache first
        let mut package = {
            let cache = self.cache.read().await;
            if let Some(package) = cache.get(package_id) {
                package.clone()
            } else {
                // Load from database
                if let Some(package) = self.load_from_database(package_id).await? {
                    // Update cache
                    let mut cache = self.cache.write().await;
                    cache.insert(package_id.clone(), package.clone());
                    package
                } else {
                    return Err(BillingError::PackageNotFound {
                        id: package_id.to_string(),
                    });
                }
            }
        };

        // Apply dynamic pricing if enabled for this package
        if package.use_dynamic_pricing {
            debug!(
                "Package {} has dynamic pricing enabled, fetching current price for {}",
                package_id, package.gpu_model
            );

            if let Some(pricing_service) = &self.pricing_service {
                // Get dynamic price with fallback to static price
                match pricing_service
                    .get_price_with_fallback(&package.gpu_model, package.hourly_rate.as_decimal())
                    .await
                {
                    Ok(dynamic_price) => {
                        debug!(
                            "Using dynamic price for {}: ${}/hr (static was ${}/hr)",
                            package.gpu_model,
                            dynamic_price,
                            package.hourly_rate.as_decimal()
                        );
                        package.hourly_rate = CreditBalance::from_decimal(dynamic_price);
                    }
                    Err(e) => {
                        warn!(
                            "Failed to get dynamic price for {}: {}. Using static price ${}/hr",
                            package.gpu_model,
                            e,
                            package.hourly_rate.as_decimal()
                        );
                    }
                }
            } else {
                warn!(
                    "Package {} has dynamic pricing enabled but PricingService not configured. Using static price.",
                    package_id
                );
            }
        }

        Ok(package)
    }

    async fn list_packages(&self) -> Result<Vec<BillingPackage>> {
        let query_str = format!(
            "SELECT {} FROM billing.billing_packages WHERE active = true ORDER BY priority, package_id",
            PACKAGE_SELECT_COLUMNS
        );

        let rows = sqlx::query(&query_str)
            .fetch_all(&self.pool)
            .await
            .map_err(|e| BillingError::DatabaseError {
                operation: "list_packages".to_string(),
                source: Box::new(e),
            })?;

        rows.iter().map(Self::row_to_billing_package).collect()
    }

    async fn create_package(&self, package: BillingPackage) -> Result<()> {
        // Check if package already exists
        let existing = sqlx::query(
            r#"
            SELECT package_id FROM billing.billing_packages WHERE package_id = $1
            "#,
        )
        .bind(package.id.as_str())
        .fetch_optional(&self.pool)
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "check_package_exists".to_string(),
            source: Box::new(e),
        })?;

        if existing.is_some() {
            return Err(BillingError::ValidationError {
                field: "package_id".to_string(),
                message: format!("Package {} already exists", package.id),
            });
        }

        self.persist_to_database(&package).await?;

        let mut cache = self.cache.write().await;
        cache.insert(package.id.clone(), package);

        Ok(())
    }

    async fn update_package(&self, package: BillingPackage) -> Result<()> {
        let existing = sqlx::query(
            r#"
            SELECT package_id FROM billing.billing_packages WHERE package_id = $1
            "#,
        )
        .bind(package.id.as_str())
        .fetch_optional(&self.pool)
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "check_package_exists".to_string(),
            source: Box::new(e),
        })?;

        if existing.is_none() {
            return Err(BillingError::PackageNotFound {
                id: package.id.to_string(),
            });
        }

        self.persist_to_database(&package).await?;

        let mut cache = self.cache.write().await;
        cache.insert(package.id.clone(), package);

        Ok(())
    }

    async fn delete_package(&self, package_id: &PackageId) -> Result<()> {
        let result = sqlx::query(
            r#"
            DELETE FROM billing.billing_packages WHERE package_id = $1
            "#,
        )
        .bind(package_id.as_str())
        .execute(&self.pool)
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "delete_package".to_string(),
            source: Box::new(e),
        })?;

        if result.rows_affected() == 0 {
            return Err(BillingError::PackageNotFound {
                id: package_id.to_string(),
            });
        }

        let mut cache = self.cache.write().await;
        cache.remove(package_id);

        Ok(())
    }

    async fn activate_package(&self, package_id: &PackageId) -> Result<()> {
        let result = sqlx::query(
            r#"
            UPDATE billing.billing_packages
            SET active = true, updated_at = NOW()
            WHERE package_id = $1
            "#,
        )
        .bind(package_id.as_str())
        .execute(&self.pool)
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "activate_package".to_string(),
            source: Box::new(e),
        })?;

        if result.rows_affected() == 0 {
            return Err(BillingError::PackageNotFound {
                id: package_id.to_string(),
            });
        }

        if let Some(package) = self.load_from_database(package_id).await? {
            let mut cache = self.cache.write().await;
            cache.insert(package_id.clone(), package);
        }

        Ok(())
    }

    async fn deactivate_package(&self, package_id: &PackageId) -> Result<()> {
        let result = sqlx::query(
            r#"
            UPDATE billing.billing_packages
            SET active = false, updated_at = NOW()
            WHERE package_id = $1
            "#,
        )
        .bind(package_id.as_str())
        .execute(&self.pool)
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "deactivate_package".to_string(),
            source: Box::new(e),
        })?;

        if result.rows_affected() == 0 {
            return Err(BillingError::PackageNotFound {
                id: package_id.to_string(),
            });
        }

        let mut cache = self.cache.write().await;
        cache.remove(package_id);

        Ok(())
    }

    async fn find_package_for_gpu_model(&self, gpu_model: &str) -> Result<BillingPackage> {
        use tracing::{info, warn};

        info!("Looking up package for GPU model: {}", gpu_model);

        let query_str = format!(
            r#"
            SELECT {}
            FROM billing.billing_packages
            WHERE active = true
              AND (
                  LOWER(gpu_model) = LOWER($1)
                  OR LOWER(gpu_model) LIKE '%' || LOWER($1) || '%'
                  OR LOWER($1) LIKE '%' || LOWER(gpu_model) || '%'
              )
            ORDER BY
                CASE WHEN LOWER(gpu_model) = LOWER($1) THEN 0 ELSE 1 END,
                priority DESC
            LIMIT 1
            "#,
            PACKAGE_SELECT_COLUMNS
        );

        let row = sqlx::query(&query_str)
            .bind(gpu_model)
            .fetch_optional(&self.pool)
            .await
            .map_err(|e| BillingError::DatabaseError {
                operation: "find_package_for_gpu_model".to_string(),
                source: Box::new(e),
            })?;

        if let Some(row) = row {
            let package = Self::row_to_billing_package(&row)?;
            info!(
                "Found matching package {} for GPU model {}",
                package.id, gpu_model
            );
            Ok(package)
        } else {
            warn!(
                "No package found for GPU model '{}', falling back to h100 package",
                gpu_model
            );
            self.get_package(&PackageId::h100()).await
        }
    }

    async fn is_package_compatible_with_gpu(
        &self,
        package_id: &PackageId,
        gpu_model: &str,
    ) -> Result<bool> {
        let result = sqlx::query_scalar::<_, bool>(
            r#"
            SELECT EXISTS(
                SELECT 1
                FROM billing.billing_packages
                WHERE package_id = $1
                  AND active = true
                  AND (
                      LOWER(gpu_model) = 'custom'
                      OR LOWER(gpu_model) = LOWER($2)
                      OR LOWER(gpu_model) LIKE '%' || LOWER($2) || '%'
                      OR LOWER($2) LIKE '%' || LOWER(gpu_model) || '%'
                  )
            )
            "#,
        )
        .bind(package_id.as_str())
        .bind(gpu_model)
        .fetch_one(&self.pool)
        .await
        .map_err(|e| BillingError::DatabaseError {
            operation: "is_package_compatible_with_gpu".to_string(),
            source: Box::new(e),
        })?;

        Ok(result)
    }

    async fn evaluate_package_cost(
        &self,
        package_id: &PackageId,
        usage: &UsageMetrics,
    ) -> Result<CostBreakdown> {
        let package = self.get_package(package_id).await?;
        Ok(package.calculate_cost(usage))
    }
}
