use anyhow::Result;
use basilica_protocol::billing::MinerDelivery;
use chrono::{DateTime, Utc};
use reqwest::Client;
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::sync::Arc;
use std::time::Duration;

pub trait ValidatorSigner: Send + Sync {
    fn hotkey(&self) -> String;
    fn sign(&self, message: &[u8]) -> Result<String>;
}

impl ValidatorSigner for bittensor::Service {
    fn hotkey(&self) -> String {
        self.get_account_id().to_string()
    }

    fn sign(&self, message: &[u8]) -> Result<String> {
        self.sign_data(message)
            .map_err(|e| anyhow::anyhow!("Failed to sign request: {e}"))
    }
}

#[derive(Clone)]
pub struct BillingApiClient {
    api_endpoint: String,
    signer: Arc<dyn ValidatorSigner>,
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

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct PendingRewardsQuery {
    pub miner_hotkey: String,
    pub node_id: String,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct AccumulateRewardsRequestBody {
    pub miner_hotkey: String,
    pub node_id: String,
    pub epoch_earnings_usd: String,
    pub alpha_price_usd: String,
}

#[derive(Debug, Deserialize, Serialize, Clone)]
#[allow(dead_code)]
pub struct AccumulateRewardsResponseBody {
    pub result: i32,
    pub pending_alpha: String,
    pub pending_usd: String,
    pub epochs_accumulated: i32,
    pub immediate_alpha: String,
    pub immediate_usd: String,
}

#[derive(Debug, Deserialize, Serialize, Clone)]
#[allow(dead_code)]
pub struct PendingStatusResponseBody {
    pub exists: bool,
    pub pending_alpha: String,
    pub pending_usd: String,
    pub epochs_accumulated: i32,
    pub threshold_reached: bool,
    pub continuous_uptime_minutes: i32,
    pub ramp_start_time: Option<i64>,
    pub threshold_reached_at: Option<i64>,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct UpdateUptimeRequestBody {
    pub miner_hotkey: String,
    pub node_id: String,
    pub uptime_minutes: i32,
}

#[derive(Debug, Deserialize, Serialize, Clone)]
#[allow(dead_code)]
pub struct UpdateUptimeResponseBody {
    pub threshold_reached: bool,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct ProcessThresholdRequestBody {
    pub miner_hotkey: String,
    pub node_id: String,
}

#[derive(Debug, Deserialize, Serialize, Clone)]
#[allow(dead_code)]
pub struct ProcessThresholdResponseBody {
    pub backpay_alpha: String,
    pub backpay_usd: String,
    pub epochs_paid: i32,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct ProcessValidationFailureRequestBody {
    pub miner_hotkey: String,
    pub node_id: String,
    pub failure_reason: String,
    pub failure_type: Option<String>,
}

#[derive(Debug, Deserialize, Serialize, Clone)]
#[allow(dead_code)]
pub struct ProcessValidationFailureResponseBody {
    pub forfeited_alpha: String,
    pub forfeited_usd: String,
    pub epochs_lost: i32,
}

impl BillingApiClient {
    pub fn new(
        api_endpoint: String,
        bittensor_service: Arc<bittensor::Service>,
        timeout_secs: u64,
    ) -> Result<Self> {
        let signer: Arc<dyn ValidatorSigner> = bittensor_service;
        Self::new_with_signer(api_endpoint, signer, timeout_secs)
    }

    pub fn new_with_signer(
        api_endpoint: String,
        signer: Arc<dyn ValidatorSigner>,
        timeout_secs: u64,
    ) -> Result<Self> {
        let http_client = Client::builder()
            .timeout(Duration::from_secs(timeout_secs))
            .build()
            .map_err(|e| anyhow::anyhow!("Failed to build HTTP client: {e}"))?;

        Ok(Self {
            api_endpoint,
            signer,
            http_client,
        })
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

        let url = format!(
            "{}/v1/weights/miner-delivery",
            self.api_endpoint.trim_end_matches('/')
        );

        let response = self.signed_get(&url, &query).await?;

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

    pub async fn get_pending_rewards_status(
        &self,
        query: PendingRewardsQuery,
    ) -> Result<PendingStatusResponseBody> {
        let url = format!(
            "{}/v1/weights/pending-rewards/status",
            self.api_endpoint.trim_end_matches('/')
        );
        let response = self.signed_get(&url, &query).await?;
        self.read_json_response(response).await
    }

    pub async fn accumulate_miner_rewards(
        &self,
        body: AccumulateRewardsRequestBody,
    ) -> Result<AccumulateRewardsResponseBody> {
        let url = format!(
            "{}/v1/weights/pending-rewards/accumulate",
            self.api_endpoint.trim_end_matches('/')
        );
        let response = self.signed_post(&url, &body).await?;
        self.read_json_response(response).await
    }

    pub async fn process_threshold_reached(
        &self,
        body: ProcessThresholdRequestBody,
    ) -> Result<ProcessThresholdResponseBody> {
        let url = format!(
            "{}/v1/weights/pending-rewards/threshold",
            self.api_endpoint.trim_end_matches('/')
        );
        let response = self.signed_post(&url, &body).await?;
        self.read_json_response(response).await
    }

    pub async fn update_miner_uptime(
        &self,
        body: UpdateUptimeRequestBody,
    ) -> Result<UpdateUptimeResponseBody> {
        let url = format!(
            "{}/v1/weights/pending-rewards/uptime",
            self.api_endpoint.trim_end_matches('/')
        );
        let response = self.signed_post(&url, &body).await?;
        self.read_json_response(response).await
    }

    pub async fn process_validation_failure(
        &self,
        body: ProcessValidationFailureRequestBody,
    ) -> Result<ProcessValidationFailureResponseBody> {
        let url = format!(
            "{}/v1/weights/pending-rewards/failure",
            self.api_endpoint.trim_end_matches('/')
        );
        let response = self.signed_post(&url, &body).await?;
        self.read_json_response(response).await
    }

    async fn signed_get<Q: Serialize>(&self, url: &str, query: &Q) -> Result<reqwest::Response> {
        let (signature, timestamp, hotkey) = self.signed_headers(query)?;
        self.http_client
            .get(url)
            .header("X-Validator-Signature", signature)
            .header("X-Timestamp", timestamp)
            .header("X-Validator-Hotkey", hotkey)
            .query(query)
            .send()
            .await
            .map_err(|e| anyhow::anyhow!("API request failed: {e}"))
    }

    async fn signed_post<T: Serialize>(&self, url: &str, body: &T) -> Result<reqwest::Response> {
        let (signature, timestamp, hotkey) = self.signed_headers(body)?;
        self.http_client
            .post(url)
            .header("X-Validator-Signature", signature)
            .header("X-Timestamp", timestamp)
            .header("X-Validator-Hotkey", hotkey)
            .json(body)
            .send()
            .await
            .map_err(|e| anyhow::anyhow!("API request failed: {e}"))
    }

    fn signed_headers<T: Serialize>(&self, payload: &T) -> Result<(String, String, String)> {
        let timestamp = Utc::now().timestamp().to_string();
        let payload_json = serde_json::to_string(payload)?;
        let message = format!("{}:{}", timestamp, payload_json);
        let signature = self.signer.sign(message.as_bytes())?;
        let hotkey = self.signer.hotkey();
        Ok((signature, timestamp, hotkey))
    }

    async fn read_json_response<T: DeserializeOwned>(
        &self,
        response: reqwest::Response,
    ) -> Result<T> {
        if !response.status().is_success() {
            let status = response.status();
            let body: Option<Value> = response.json().await.ok();
            return Err(anyhow::anyhow!(
                "API returned status {}: {}",
                status,
                body.map(|v| v.to_string()).unwrap_or_default()
            ));
        }
        response
            .json::<T>()
            .await
            .map_err(|e| anyhow::anyhow!("Failed to parse API response: {e}"))
    }
}
