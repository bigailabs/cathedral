use crate::domain::types::{BillingPeriod, CostBreakdown, CreditBalance, PackageId, UsageMetrics};
use crate::error::Result;
use basilica_protocol::billing::{
    BillingPackage as ProtoBillingPackage, IncludedResources as ProtoIncludedResources,
    PackageRates as ProtoPackageRates,
};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BillingPackage {
    pub id: PackageId,
    pub name: String,
    pub description: String,
    pub hourly_rate: CreditBalance,
    pub gpu_model: String,
    pub billing_period: BillingPeriod,
    pub priority: u32,
    pub active: bool,
    pub metadata: HashMap<String, String>,
}

impl BillingPackage {
    pub fn new(
        id: PackageId,
        name: String,
        description: String,
        hourly_rate: CreditBalance,
        gpu_model: String,
    ) -> Self {
        Self {
            id,
            name,
            description,
            hourly_rate,
            gpu_model,
            billing_period: BillingPeriod::Hourly,
            priority: 100,
            active: true,
            metadata: HashMap::new(),
        }
    }

    /// Calculate cost for given usage
    pub fn calculate_cost(&self, usage: &UsageMetrics) -> CostBreakdown {
        let total_hours = usage.gpu_hours.max(Decimal::ONE);
        let total_cost = self.hourly_rate.multiply(total_hours);

        CostBreakdown {
            base_cost: total_cost,
            usage_cost: CreditBalance::zero(),
            discounts: CreditBalance::zero(),
            overage_charges: CreditBalance::zero(),
            total_cost,
        }
    }

    /// Convert to protobuf format for gRPC
    pub fn to_proto(&self) -> ProtoBillingPackage {
        ProtoBillingPackage {
            package_id: self.id.to_string(),
            name: self.name.clone(),
            description: self.description.clone(),
            rates: Some(ProtoPackageRates {
                cpu_rate_per_hour: "0".to_string(),
                memory_rate_per_gb_hour: "0".to_string(),
                gpu_rates: HashMap::from([(self.gpu_model.clone(), self.hourly_rate.to_string())]),
                network_rate_per_gb: "0".to_string(),
                disk_iops_rate: "0".to_string(),
                base_rate_per_hour: self.hourly_rate.to_string(),
            }),
            included_resources: Some(ProtoIncludedResources {
                cpu_core_hours: 0,
                memory_gb_hours: 0,
                gpu_hours: HashMap::new(),
                network_gb: 0,
                disk_iops: 0,
            }),
            overage_rates: None,
            priority: self.priority,
            is_active: self.active,
        }
    }
}

// Pricing business rules - currently empty as all pricing comes from database
// Package assignment happens automatically via GPU model detection in
// PackageId::from_gpu_model() and find_package_for_gpu_model() repository method

use async_trait::async_trait;

/// Package service for business logic operations
#[async_trait]
pub trait PackageService: Send + Sync {
    /// Evaluate the cost for a package given usage metrics
    async fn evaluate_cost(
        &self,
        package_id: &PackageId,
        usage: &UsageMetrics,
    ) -> Result<CostBreakdown>;

    async fn recommend_package(&self, gpu_model: &str) -> Result<BillingPackage>;

    async fn validate_package_requirements(
        &self,
        package: &BillingPackage,
        gpu_model: &str,
    ) -> Result<bool>;
}

use crate::storage::PackageRepository;
use std::sync::Arc;

pub struct RepositoryPackageService {
    repository: Arc<dyn PackageRepository + Send + Sync>,
}

impl RepositoryPackageService {
    pub fn new(repository: Arc<dyn PackageRepository + Send + Sync>) -> Self {
        Self { repository }
    }
}

#[async_trait]
impl PackageService for RepositoryPackageService {
    async fn evaluate_cost(
        &self,
        package_id: &PackageId,
        usage: &UsageMetrics,
    ) -> Result<CostBreakdown> {
        let package = self.repository.get_package(package_id).await?;
        Ok(package.calculate_cost(usage))
    }

    async fn recommend_package(&self, gpu_model: &str) -> Result<BillingPackage> {
        self.repository.find_package_for_gpu_model(gpu_model).await
    }

    async fn validate_package_requirements(
        &self,
        package: &BillingPackage,
        gpu_model: &str,
    ) -> Result<bool> {
        self.repository
            .is_package_compatible_with_gpu(&package.id, gpu_model)
            .await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_package_creation() {
        let package = BillingPackage::new(
            PackageId::h100(),
            "Test Package".to_string(),
            "Test Description".to_string(),
            CreditBalance::from_f64(10.0).unwrap(),
            "H100".to_string(),
        );

        assert_eq!(package.id, PackageId::h100());
        assert_eq!(package.hourly_rate, CreditBalance::from_f64(10.0).unwrap());
        assert!(package.active);
    }

    #[test]
    fn test_cost_calculation() {
        let package = BillingPackage::new(
            PackageId::h100(),
            "H100 GPU".to_string(),
            "NVIDIA H100 GPU instances".to_string(),
            CreditBalance::from_f64(3.5).unwrap(),
            "H100".to_string(),
        );
        let usage = UsageMetrics {
            gpu_hours: Decimal::from(10),
            cpu_hours: Decimal::ZERO,
            memory_gb_hours: Decimal::ZERO,
            storage_gb_hours: Decimal::ZERO,
            network_gb: Decimal::ZERO,
            disk_io_gb: Decimal::ZERO,
        };

        let cost = package.calculate_cost(&usage);
        assert_eq!(cost.total_cost, CreditBalance::from_f64(35.0).unwrap());
    }
}
