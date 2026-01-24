use anyhow::Result;
use basilica_protocol::billing::MinerDelivery;
use chrono::{DateTime, Utc};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::sync::Arc;

#[derive(Clone)]
pub struct BillingApiClient {
    api_endpoint: String,
    bittensor_service: Arc<bittensor::Service>,
    http_client: Client,
}

#[derive(Debug, Serialize)]
struct MinerDeliveryQuery {
    since_epoch_seconds: i64,
    until_epoch_seconds: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    miner_hotkeys: Option<String>,
}

#[derive(Debug, Deserialize)]
struct MinerDeliveryResponse {
    deliveries: Vec<MinerDeliveryItem>,
}

#[derive(Debug, Deserialize)]
struct MinerDeliveryItem {
    miner_hotkey: String,
    miner_uid: u32,
    total_hours: f64,
    user_revenue_usd: f64,
    gpu_category: String,
    miner_payment_usd: f64,
    #[serde(default)]
    has_collateral: bool,
    #[serde(default)]
    payout_type: String,
    #[serde(default)]
    cliff_days_remaining: i32,
    #[serde(default)]
    pending_alpha: f64,
    #[serde(default)]
    node_id: String,
}

impl BillingApiClient {
    pub fn new(api_endpoint: String, bittensor_service: Arc<bittensor::Service>) -> Self {
        Self {
            api_endpoint,
            bittensor_service,
            http_client: Client::new(),
        }
    }

    pub async fn get_miner_delivery(
        &self,
        since: DateTime<Utc>,
        until: DateTime<Utc>,
        miner_hotkeys: Vec<String>,
    ) -> Result<Vec<MinerDelivery>> {
        let query = MinerDeliveryQuery {
            since_epoch_seconds: since.timestamp(),
            until_epoch_seconds: until.timestamp(),
            miner_hotkeys: if miner_hotkeys.is_empty() {
                None
            } else {
                Some(miner_hotkeys.join(","))
            },
        };

        let timestamp = Utc::now().timestamp().to_string();
        let message = format!("{}:{}", timestamp, serde_json::to_string(&query)?);
        let signature = self
            .bittensor_service
            .sign_data(message.as_bytes())
            .map_err(|e| anyhow::anyhow!("Failed to sign request: {e}"))?;

        let hotkey = self.bittensor_service.get_account_id().to_string();
        let url = format!(
            "{}/v1/weights/miner-delivery",
            self.api_endpoint.trim_end_matches('/')
        );

        let response = self
            .http_client
            .get(url)
            .header("X-Validator-Signature", signature)
            .header("X-Timestamp", timestamp)
            .header("X-Validator-Hotkey", hotkey)
            .query(&query)
            .send()
            .await?;

        if !response.status().is_success() {
            return Err(anyhow::anyhow!("API returned status {}", response.status()));
        }

        let body: MinerDeliveryResponse = response.json().await?;
        Ok(body
            .deliveries
            .into_iter()
            .map(|delivery| MinerDelivery {
                miner_hotkey: delivery.miner_hotkey,
                miner_uid: delivery.miner_uid,
                total_hours: delivery.total_hours,
                user_revenue_usd: delivery.user_revenue_usd,
                gpu_category: delivery.gpu_category,
                miner_payment_usd: delivery.miner_payment_usd,
                has_collateral: delivery.has_collateral,
                payout_type: delivery.payout_type,
                cliff_days_remaining: delivery.cliff_days_remaining,
                pending_alpha: delivery.pending_alpha,
                node_id: delivery.node_id,
            })
            .collect())
    }
}
