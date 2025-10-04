use anyhow::Result;
use async_trait::async_trait;

use crate::crd::gpu_rental::{GpuRental, GpuRentalStatus};
use kube::ResourceExt;

#[async_trait]
pub trait BillingClient: Send + Sync {
    async fn approve_extension(&self, rental: &GpuRental, additional_hours: u32) -> Result<bool>;
    async fn emit_usage_event(&self, _rental: &GpuRental, _status: &GpuRentalStatus) -> Result<()> {
        Ok(())
    }
}

#[derive(Default, Clone)]
pub struct MockBillingClient {
    pub approvals: std::sync::Arc<tokio::sync::RwLock<std::collections::HashMap<String, bool>>>,
    pub events: std::sync::Arc<tokio::sync::RwLock<Vec<(String, String)>>>,
}

#[async_trait]
impl BillingClient for MockBillingClient {
    async fn approve_extension(&self, rental: &GpuRental, _additional_hours: u32) -> Result<bool> {
        let approvals = self.approvals.read().await;
        Ok(approvals.get(&rental.name_any()).cloned().unwrap_or(true))
    }
    async fn emit_usage_event(&self, rental: &GpuRental, status: &GpuRentalStatus) -> Result<()> {
        let mut ev = self.events.write().await;
        ev.push((rental.name_any(), status.state.clone().unwrap_or_default()));
        Ok(())
    }
}
