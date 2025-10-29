use std::sync::Arc;

use anyhow::Result;
use basilica_common::config::types::MetricsConfig;

pub use api_metrics::{ApiMetricNames, ApiMetrics, API_METRIC_NAMES};
pub use prometheus_metrics::PrometheusMetricsRecorder;

mod api_metrics;
mod prometheus_metrics;

pub struct ApiMetricsSystem {
    config: MetricsConfig,
    prometheus: Arc<PrometheusMetricsRecorder>,
    api: Arc<ApiMetrics>,
}

impl ApiMetricsSystem {
    pub fn new(config: MetricsConfig) -> Result<Self> {
        let prometheus = Arc::new(PrometheusMetricsRecorder::new()?);
        let api = Arc::new(ApiMetrics::new(prometheus.clone()));

        Ok(Self {
            config,
            prometheus,
            api,
        })
    }

    pub fn is_enabled(&self) -> bool {
        self.config.enabled
    }

    pub fn prometheus_recorder(&self) -> Arc<PrometheusMetricsRecorder> {
        self.prometheus.clone()
    }

    pub fn api_metrics(&self) -> Arc<ApiMetrics> {
        self.api.clone()
    }

    pub fn render_prometheus(&self) -> String {
        self.prometheus.render()
    }
}
