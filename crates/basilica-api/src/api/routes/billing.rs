use crate::api::middleware::AuthContext;
use crate::error::{ApiError, Result};
use crate::server::AppState;
use axum::{
    extract::{Path, Query, State},
    response::Json,
    routing::get,
    Router,
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use tracing::{debug, error, info};

#[derive(Debug, Serialize)]
pub struct BalanceResponse {
    pub available: String,
    pub total: String,
    pub last_updated: String,
}

#[derive(Debug, Deserialize)]
pub struct UsageHistoryQuery {
    #[serde(default = "default_limit")]
    pub limit: u32,
    #[serde(default)]
    pub offset: u32,
}

fn default_limit() -> u32 {
    50
}

#[derive(Debug, Serialize)]
pub struct RentalUsageRecord {
    pub rental_id: String,
    pub node_id: String,
    pub status: String,
    pub hourly_rate: String,
    pub current_cost: String,
    pub start_time: DateTime<Utc>,
    pub last_updated: DateTime<Utc>,
}

#[derive(Debug, Serialize)]
pub struct UsageHistoryResponse {
    pub rentals: Vec<RentalUsageRecord>,
    pub total_count: u64,
}

#[derive(Debug, Serialize)]
pub struct UsageDataPoint {
    pub timestamp: DateTime<Utc>,
    pub cpu_percent: f64,
    pub memory_mb: u64,
    pub cost: String,
}

#[derive(Debug, Serialize)]
pub struct UsageSummary {
    pub avg_cpu_percent: f64,
    pub avg_memory_mb: u64,
    pub total_network_bytes: u64,
    pub total_disk_bytes: u64,
    pub avg_gpu_utilization: f64,
    pub duration_secs: u64,
}

#[derive(Debug, Serialize)]
pub struct RentalUsageResponse {
    pub rental_id: String,
    pub data_points: Vec<UsageDataPoint>,
    pub summary: Option<UsageSummary>,
    pub total_cost: String,
}

pub fn routes() -> Router<AppState> {
    Router::new()
        .route("/balance", get(get_balance))
        .route("/usage", get(get_usage_history))
        .route("/usage/:rental_id", get(get_rental_usage))
}

async fn get_balance(
    State(state): State<AppState>,
    axum::Extension(auth): axum::Extension<AuthContext>,
) -> Result<Json<BalanceResponse>> {
    debug!("Getting balance for user: {}", auth.user_id);

    let billing_client = state
        .billing_client
        .as_ref()
        .ok_or_else(|| ApiError::ServiceUnavailable)?;

    let response = billing_client
        .get_balance(&auth.user_id)
        .await
        .map_err(|e| {
            error!("Failed to get balance: {}", e);
            ApiError::Internal {
                message: format!("Failed to get balance: {}", e),
            }
        })?;

    let last_updated = if let Some(timestamp) = response.last_updated {
        DateTime::<Utc>::from_timestamp(timestamp.seconds, timestamp.nanos as u32)
            .unwrap_or_else(Utc::now)
            .to_rfc3339()
    } else {
        Utc::now().to_rfc3339()
    };

    Ok(Json(BalanceResponse {
        available: response.available_balance,
        total: response.total_balance,
        last_updated,
    }))
}

async fn get_usage_history(
    State(state): State<AppState>,
    axum::Extension(auth): axum::Extension<AuthContext>,
    Query(params): Query<UsageHistoryQuery>,
) -> Result<Json<UsageHistoryResponse>> {
    debug!(
        "Getting usage history for user: {}, limit: {}, offset: {}",
        auth.user_id, params.limit, params.offset
    );

    if params.limit > 100 {
        return Err(ApiError::BadRequest {
            message: "Limit cannot exceed 100".into(),
        });
    }

    let billing_client = state
        .billing_client
        .as_ref()
        .ok_or_else(|| ApiError::ServiceUnavailable)?;

    let response = billing_client
        .get_active_rentals_for_user(&auth.user_id, Some(params.limit), Some(params.offset))
        .await
        .map_err(|e| {
            error!("Failed to get usage history: {}", e);
            ApiError::Internal {
                message: format!("Failed to get usage history: {}", e),
            }
        })?;

    let rentals: Vec<RentalUsageRecord> = response
        .rentals
        .into_iter()
        .filter_map(|rental| {
            let start_time = rental
                .start_time
                .and_then(|ts| DateTime::<Utc>::from_timestamp(ts.seconds, ts.nanos as u32))?;

            let last_updated = rental
                .last_updated
                .and_then(|ts| DateTime::<Utc>::from_timestamp(ts.seconds, ts.nanos as u32))?;

            // Calculate hourly rate from marketplace pricing fields (from cloud_type oneof)
            // Note: base_price_per_gpu already includes any markup applied by API layer
            let (node_id, hourly_rate) = match &rental.cloud_type {
                Some(basilica_protocol::billing::active_rental::CloudType::Community(data)) => {
                    let rate = data.base_price_per_gpu * data.gpu_count as f64;
                    (data.node_id.clone(), rate)
                }
                Some(basilica_protocol::billing::active_rental::CloudType::Secure(data)) => {
                    let rate = data.base_price_per_gpu * data.gpu_count as f64;
                    (data.provider_instance_id.clone(), rate)
                }
                None => {
                    tracing::warn!("Rental {} has no cloud_type data", rental.rental_id);
                    return None;
                }
            };

            Some(RentalUsageRecord {
                rental_id: rental.rental_id,
                node_id,
                status: format!("{:?}", rental.status),
                hourly_rate: format!("{:.4}", hourly_rate),
                current_cost: rental.current_cost,
                start_time,
                last_updated,
            })
        })
        .collect();

    Ok(Json(UsageHistoryResponse {
        rentals,
        total_count: response.total_count,
    }))
}

async fn get_rental_usage(
    State(state): State<AppState>,
    axum::Extension(auth): axum::Extension<AuthContext>,
    Path(rental_id): Path<String>,
) -> Result<Json<RentalUsageResponse>> {
    info!(
        "Getting rental usage for rental: {} by user: {}",
        rental_id, auth.user_id
    );

    let billing_client = state
        .billing_client
        .as_ref()
        .ok_or_else(|| ApiError::ServiceUnavailable)?;

    let response = billing_client
        .get_usage_report(
            rental_id.clone(),
            None,
            None,
            basilica_protocol::billing::UsageAggregation::Hour,
        )
        .await
        .map_err(|e| {
            error!("Failed to get rental usage: {}", e);
            ApiError::Internal {
                message: format!("Failed to get rental usage: {}", e),
            }
        })?;

    let data_points: Vec<UsageDataPoint> = response
        .data_points
        .into_iter()
        .filter_map(|dp| {
            let timestamp = dp
                .timestamp
                .and_then(|ts| DateTime::<Utc>::from_timestamp(ts.seconds, ts.nanos as u32))?;

            let usage = dp.usage?;

            Some(UsageDataPoint {
                timestamp,
                cpu_percent: usage.cpu_percent,
                memory_mb: usage.memory_mb,
                cost: dp.cost,
            })
        })
        .collect();

    let summary = response.summary.map(|s| {
        let duration_secs = s.duration.map(|d| d.seconds as u64).unwrap_or(0);

        UsageSummary {
            avg_cpu_percent: s.avg_cpu_percent,
            avg_memory_mb: s.avg_memory_mb,
            total_network_bytes: s.total_network_bytes,
            total_disk_bytes: s.total_disk_bytes,
            avg_gpu_utilization: s.avg_gpu_utilization,
            duration_secs,
        }
    });

    Ok(Json(RentalUsageResponse {
        rental_id,
        data_points,
        summary,
        total_cost: response.total_cost,
    }))
}
