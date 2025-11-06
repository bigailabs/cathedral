use crate::service::AggregatorService;
use std::sync::Arc;
use tokio::time::{interval, Duration};

/// Background task that periodically refreshes GPU offerings from providers
pub struct BackgroundRefreshTask {
    service: Arc<AggregatorService>,
    refresh_interval: Duration,
}

impl BackgroundRefreshTask {
    pub fn new(service: Arc<AggregatorService>, refresh_interval_seconds: u64) -> Self {
        Self {
            service,
            refresh_interval: Duration::from_secs(refresh_interval_seconds),
        }
    }

    /// Start the background refresh task
    /// Returns a JoinHandle that can be used for graceful shutdown
    pub fn start(self) -> tokio::task::JoinHandle<()> {
        tokio::spawn(async move {
            self.run().await;
        })
    }

    /// Main loop that periodically fetches from all providers
    async fn run(&self) {
        let mut interval_timer = interval(self.refresh_interval);
        interval_timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

        tracing::info!(
            "Background refresh task started with interval: {:?}",
            self.refresh_interval
        );

        // Initial fetch immediately on startup
        self.refresh_all_providers().await;

        loop {
            // Wait for next tick
            interval_timer.tick().await;

            // Refresh all providers
            self.refresh_all_providers().await;
        }
    }

    /// Refresh offerings from all providers
    async fn refresh_all_providers(&self) {
        tracing::debug!("Starting background refresh cycle");

        match self.service.refresh_all_providers().await {
            Ok(total_count) => {
                tracing::info!(
                    "Background refresh completed successfully: {} offerings",
                    total_count
                );
            }
            Err(e) => {
                tracing::error!("Background refresh failed: {}", e);
            }
        }
    }
}
