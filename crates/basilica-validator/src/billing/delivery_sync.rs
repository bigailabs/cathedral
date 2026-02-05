use anyhow::Result;
use chrono::{Duration, Utc};
use std::sync::Arc;
use tracing::{info, warn};

use crate::basilica_api::BasilicaApiClient;
use crate::persistence::MinerDeliveryRepository;

pub struct DeliverySyncTask {
    api_client: Arc<BasilicaApiClient>,
    delivery_repo: Arc<MinerDeliveryRepository>,
    sync_interval_secs: u64,
    lookback_hours: u64,
}

impl DeliverySyncTask {
    pub fn new(
        api_client: Arc<BasilicaApiClient>,
        delivery_repo: Arc<MinerDeliveryRepository>,
        sync_interval_secs: u64,
        lookback_hours: u64,
    ) -> Self {
        Self {
            api_client,
            delivery_repo,
            sync_interval_secs,
            lookback_hours,
        }
    }

    pub async fn run(&self) {
        let mut interval =
            tokio::time::interval(std::time::Duration::from_secs(self.sync_interval_secs));

        loop {
            interval.tick().await;
            if let Err(e) = self.sync_once().await {
                warn!("Failed to sync miner delivery data: {}", e);
            }
        }
    }

    async fn sync_once(&self) -> Result<()> {
        let until = Utc::now();
        let since = until - Duration::hours(self.lookback_hours as i64);

        let deliveries = self
            .api_client
            .get_miner_delivery(since, until, Vec::new())
            .await?;

        self.delivery_repo
            .store_deliveries(since, until, &deliveries)
            .await
            .map_err(|e| anyhow::anyhow!("Failed to store deliveries: {e}"))?;

        info!(
            count = deliveries.len(),
            "Synced miner delivery data from API"
        );
        Ok(())
    }
}

// TODO: Add backoff and per-endpoint failure metrics.
