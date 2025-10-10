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

    pub storage_rate_per_gb_hour: CreditBalance,
    pub network_rate_per_gb: CreditBalance,
    pub disk_io_rate_per_gb: CreditBalance,
    pub cpu_rate_per_core_hour: CreditBalance,
    pub memory_rate_per_gb_hour: CreditBalance,

    pub included_storage_gb_hours: Decimal,
    pub included_network_gb: Decimal,
    pub included_disk_io_gb: Decimal,
    pub included_cpu_core_hours: Decimal,
    pub included_memory_gb_hours: Decimal,
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

            storage_rate_per_gb_hour: CreditBalance::zero(),
            network_rate_per_gb: CreditBalance::zero(),
            disk_io_rate_per_gb: CreditBalance::zero(),
            cpu_rate_per_core_hour: CreditBalance::zero(),
            memory_rate_per_gb_hour: CreditBalance::zero(),

            included_storage_gb_hours: Decimal::ZERO,
            included_network_gb: Decimal::ZERO,
            included_disk_io_gb: Decimal::ZERO,
            included_cpu_core_hours: Decimal::ZERO,
            included_memory_gb_hours: Decimal::ZERO,
        }
    }

    /// Calculate cost for given usage
    pub fn calculate_cost(&self, usage: &UsageMetrics) -> CostBreakdown {
        let gpu_hours = usage.gpu_hours.max(Decimal::ONE);
        let base_cost = self.hourly_rate.multiply(gpu_hours);

        let storage_overage = usage
            .storage_gb_hours
            .saturating_sub(self.included_storage_gb_hours)
            .max(Decimal::ZERO);
        let network_overage = usage
            .network_gb
            .saturating_sub(self.included_network_gb)
            .max(Decimal::ZERO);
        let disk_io_overage = usage
            .disk_io_gb
            .saturating_sub(self.included_disk_io_gb)
            .max(Decimal::ZERO);
        let cpu_overage = usage
            .cpu_hours
            .saturating_sub(self.included_cpu_core_hours)
            .max(Decimal::ZERO);
        let memory_overage = usage
            .memory_gb_hours
            .saturating_sub(self.included_memory_gb_hours)
            .max(Decimal::ZERO);

        let storage_cost = self.storage_rate_per_gb_hour.multiply(storage_overage);
        let network_cost = self.network_rate_per_gb.multiply(network_overage);
        let disk_io_cost = self.disk_io_rate_per_gb.multiply(disk_io_overage);
        let cpu_cost = self.cpu_rate_per_core_hour.multiply(cpu_overage);
        let memory_cost = self.memory_rate_per_gb_hour.multiply(memory_overage);

        let usage_cost = storage_cost
            .add(network_cost)
            .add(disk_io_cost)
            .add(cpu_cost)
            .add(memory_cost);

        let subtotal = base_cost.add(usage_cost);

        CostBreakdown {
            base_cost,
            usage_cost,
            discounts: CreditBalance::zero(),
            overage_charges: CreditBalance::zero(),
            total_cost: subtotal,
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

    #[test]
    fn test_extras_calculation_with_overages() {
        let mut package = BillingPackage::new(
            PackageId::h100(),
            "H100 GPU".to_string(),
            "NVIDIA H100 GPU instances".to_string(),
            CreditBalance::from_f64(3.5).unwrap(),
            "H100".to_string(),
        );

        package.storage_rate_per_gb_hour = CreditBalance::from_f64(0.10).unwrap();
        package.network_rate_per_gb = CreditBalance::from_f64(0.05).unwrap();
        package.disk_io_rate_per_gb = CreditBalance::from_f64(0.03).unwrap();
        package.cpu_rate_per_core_hour = CreditBalance::from_f64(0.02).unwrap();
        package.memory_rate_per_gb_hour = CreditBalance::from_f64(0.01).unwrap();

        package.included_storage_gb_hours = Decimal::from(100);
        package.included_network_gb = Decimal::from(50);
        package.included_disk_io_gb = Decimal::from(50);
        package.included_cpu_core_hours = Decimal::from(80);
        package.included_memory_gb_hours = Decimal::from(320);

        let usage = UsageMetrics {
            gpu_hours: Decimal::from(10),
            cpu_hours: Decimal::from(100),
            memory_gb_hours: Decimal::from(400),
            storage_gb_hours: Decimal::from(150),
            network_gb: Decimal::from(75),
            disk_io_gb: Decimal::from(60),
        };

        let cost = package.calculate_cost(&usage);

        let expected_base = CreditBalance::from_f64(35.0).unwrap();
        let expected_storage = CreditBalance::from_f64(5.0).unwrap();
        let expected_network = CreditBalance::from_f64(1.25).unwrap();
        let expected_disk_io = CreditBalance::from_f64(0.30).unwrap();
        let expected_cpu = CreditBalance::from_f64(0.40).unwrap();
        let expected_memory = CreditBalance::from_f64(0.80).unwrap();

        assert_eq!(cost.base_cost, expected_base);

        let expected_usage = expected_storage
            .add(expected_network)
            .add(expected_disk_io)
            .add(expected_cpu)
            .add(expected_memory);

        assert_eq!(cost.usage_cost, expected_usage);
        assert_eq!(cost.total_cost, expected_base.add(expected_usage));
    }

    #[test]
    fn test_extras_calculation_within_included_allowances() {
        let mut package = BillingPackage::new(
            PackageId::h100(),
            "H100 GPU".to_string(),
            "NVIDIA H100 GPU instances".to_string(),
            CreditBalance::from_f64(3.5).unwrap(),
            "H100".to_string(),
        );

        package.storage_rate_per_gb_hour = CreditBalance::from_f64(0.10).unwrap();
        package.network_rate_per_gb = CreditBalance::from_f64(0.05).unwrap();
        package.disk_io_rate_per_gb = CreditBalance::from_f64(0.03).unwrap();
        package.cpu_rate_per_core_hour = CreditBalance::from_f64(0.02).unwrap();
        package.memory_rate_per_gb_hour = CreditBalance::from_f64(0.01).unwrap();

        package.included_storage_gb_hours = Decimal::from(200);
        package.included_network_gb = Decimal::from(100);
        package.included_disk_io_gb = Decimal::from(100);
        package.included_cpu_core_hours = Decimal::from(100);
        package.included_memory_gb_hours = Decimal::from(500);

        let usage = UsageMetrics {
            gpu_hours: Decimal::from(10),
            cpu_hours: Decimal::from(50),
            memory_gb_hours: Decimal::from(300),
            storage_gb_hours: Decimal::from(100),
            network_gb: Decimal::from(50),
            disk_io_gb: Decimal::from(50),
        };

        let cost = package.calculate_cost(&usage);

        let expected_base = CreditBalance::from_f64(35.0).unwrap();
        assert_eq!(cost.base_cost, expected_base);
        assert_eq!(cost.usage_cost, CreditBalance::zero());
        assert_eq!(cost.total_cost, expected_base);
    }

    #[test]
    fn test_extras_calculation_zero_rates() {
        let package = BillingPackage::new(
            PackageId::h100(),
            "H100 GPU".to_string(),
            "NVIDIA H100 GPU instances".to_string(),
            CreditBalance::from_f64(3.5).unwrap(),
            "H100".to_string(),
        );

        let usage = UsageMetrics {
            gpu_hours: Decimal::from(10),
            cpu_hours: Decimal::from(100),
            memory_gb_hours: Decimal::from(400),
            storage_gb_hours: Decimal::from(150),
            network_gb: Decimal::from(75),
            disk_io_gb: Decimal::from(60),
        };

        let cost = package.calculate_cost(&usage);

        let expected_base = CreditBalance::from_f64(35.0).unwrap();
        assert_eq!(cost.base_cost, expected_base);
        assert_eq!(cost.usage_cost, CreditBalance::zero());
        assert_eq!(cost.total_cost, expected_base);
    }

    #[test]
    fn test_extras_calculation_negative_prevention() {
        let mut package = BillingPackage::new(
            PackageId::h100(),
            "H100 GPU".to_string(),
            "NVIDIA H100 GPU instances".to_string(),
            CreditBalance::from_f64(3.5).unwrap(),
            "H100".to_string(),
        );

        package.storage_rate_per_gb_hour = CreditBalance::from_f64(0.10).unwrap();
        package.included_storage_gb_hours = Decimal::from(200);

        let usage = UsageMetrics {
            gpu_hours: Decimal::from(10),
            cpu_hours: Decimal::ZERO,
            memory_gb_hours: Decimal::ZERO,
            storage_gb_hours: Decimal::from(50),
            network_gb: Decimal::ZERO,
            disk_io_gb: Decimal::ZERO,
        };

        let cost = package.calculate_cost(&usage);

        assert_eq!(cost.usage_cost, CreditBalance::zero());
    }

    #[test]
    fn test_extras_calculation_partial_overages() {
        let mut package = BillingPackage::new(
            PackageId::h100(),
            "H100 GPU".to_string(),
            "NVIDIA H100 GPU instances".to_string(),
            CreditBalance::from_f64(3.5).unwrap(),
            "H100".to_string(),
        );

        package.storage_rate_per_gb_hour = CreditBalance::from_f64(0.10).unwrap();
        package.network_rate_per_gb = CreditBalance::from_f64(0.05).unwrap();
        package.included_storage_gb_hours = Decimal::from(100);
        package.included_network_gb = Decimal::from(50);

        let usage = UsageMetrics {
            gpu_hours: Decimal::from(10),
            cpu_hours: Decimal::ZERO,
            memory_gb_hours: Decimal::ZERO,
            storage_gb_hours: Decimal::from(110),
            network_gb: Decimal::from(40),
            disk_io_gb: Decimal::ZERO,
        };

        let cost = package.calculate_cost(&usage);

        let expected_base = CreditBalance::from_f64(35.0).unwrap();
        let expected_storage = CreditBalance::from_f64(1.0).unwrap();

        assert_eq!(cost.base_cost, expected_base);
        assert_eq!(cost.usage_cost, expected_storage);
        assert_eq!(cost.total_cost, expected_base.add(expected_storage));
    }

    #[test]
    fn test_extras_calculation_minimum_gpu_hours() {
        use rust_decimal::prelude::FromStr;

        let mut package = BillingPackage::new(
            PackageId::h100(),
            "H100 GPU".to_string(),
            "NVIDIA H100 GPU instances".to_string(),
            CreditBalance::from_f64(3.5).unwrap(),
            "H100".to_string(),
        );

        package.storage_rate_per_gb_hour = CreditBalance::from_f64(0.10).unwrap();
        package.included_storage_gb_hours = Decimal::from(100);

        let usage = UsageMetrics {
            gpu_hours: Decimal::from_str("0.5").unwrap(),
            cpu_hours: Decimal::ZERO,
            memory_gb_hours: Decimal::ZERO,
            storage_gb_hours: Decimal::from(110),
            network_gb: Decimal::ZERO,
            disk_io_gb: Decimal::ZERO,
        };

        let cost = package.calculate_cost(&usage);

        let expected_base = CreditBalance::from_f64(3.5).unwrap();
        assert_eq!(cost.base_cost, expected_base);
    }

    #[test]
    fn test_extras_calculation_realistic_scenario() {
        let mut package = BillingPackage::new(
            PackageId::h100(),
            "H100 GPU".to_string(),
            "NVIDIA H100 GPU instances with extras".to_string(),
            CreditBalance::from_f64(3.5).unwrap(),
            "H100".to_string(),
        );

        package.storage_rate_per_gb_hour = CreditBalance::from_f64(0.10).unwrap();
        package.network_rate_per_gb = CreditBalance::from_f64(0.05).unwrap();
        package.disk_io_rate_per_gb = CreditBalance::from_f64(0.03).unwrap();
        package.cpu_rate_per_core_hour = CreditBalance::from_f64(0.02).unwrap();
        package.memory_rate_per_gb_hour = CreditBalance::from_f64(0.01).unwrap();

        package.included_storage_gb_hours = Decimal::from(1000);
        package.included_network_gb = Decimal::from(500);
        package.included_disk_io_gb = Decimal::from(500);
        package.included_cpu_core_hours = Decimal::from(800);
        package.included_memory_gb_hours = Decimal::from(3200);

        let usage = UsageMetrics {
            gpu_hours: Decimal::from(100),
            cpu_hours: Decimal::from(850),
            memory_gb_hours: Decimal::from(3500),
            storage_gb_hours: Decimal::from(1200),
            network_gb: Decimal::from(600),
            disk_io_gb: Decimal::from(550),
        };

        let cost = package.calculate_cost(&usage);

        let expected_base = CreditBalance::from_f64(350.0).unwrap();
        let expected_storage = CreditBalance::from_f64(20.0).unwrap();
        let expected_network = CreditBalance::from_f64(5.0).unwrap();
        let expected_disk_io = CreditBalance::from_f64(1.5).unwrap();
        let expected_cpu = CreditBalance::from_f64(1.0).unwrap();
        let expected_memory = CreditBalance::from_f64(3.0).unwrap();

        assert_eq!(cost.base_cost, expected_base);

        let expected_usage = expected_storage
            .add(expected_network)
            .add(expected_disk_io)
            .add(expected_cpu)
            .add(expected_memory);

        assert_eq!(cost.usage_cost, expected_usage);
        assert_eq!(cost.total_cost, expected_base.add(expected_usage));
    }
}
