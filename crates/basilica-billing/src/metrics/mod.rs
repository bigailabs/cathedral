use std::sync::Arc;

use anyhow::Result;
use basilica_common::config::types::MetricsConfig;

pub use billing_metrics::{BillingMetrics, BILLING_METRIC_NAMES};
pub use business_metrics::BillingBusinessMetrics;
pub use prometheus_metrics::PrometheusMetricsRecorder;

mod billing_metrics;
mod business_metrics;
mod prometheus_metrics;

pub struct BillingMetricsSystem {
    config: MetricsConfig,
    prometheus: Arc<PrometheusMetricsRecorder>,
    business: Arc<BillingBusinessMetrics>,
    billing: Arc<BillingMetrics>,
}

impl BillingMetricsSystem {
    pub fn new(config: MetricsConfig) -> Result<Self> {
        let prometheus = Arc::new(PrometheusMetricsRecorder::new()?);
        let business = Arc::new(BillingBusinessMetrics::new(prometheus.clone()));
        let billing = Arc::new(BillingMetrics::new(prometheus.clone()));

        Ok(Self {
            config,
            prometheus,
            business,
            billing,
        })
    }

    pub fn is_enabled(&self) -> bool {
        self.config.enabled
    }

    pub fn prometheus_recorder(&self) -> Arc<PrometheusMetricsRecorder> {
        self.prometheus.clone()
    }

    pub fn business_metrics(&self) -> Arc<BillingBusinessMetrics> {
        self.business.clone()
    }

    pub fn billing_metrics(&self) -> Arc<BillingMetrics> {
        self.billing.clone()
    }

    pub async fn start_collection(&self) -> Result<()> {
        if !self.config.enabled {
            return Ok(());
        }

        tracing::info!("Starting billing metrics collection");

        self.business.start_collection(self.config.clone()).await?;
        self.billing.start_collection(self.config.clone()).await?;

        Ok(())
    }

    pub fn render_prometheus(&self) -> String {
        self.prometheus.render()
    }
}
