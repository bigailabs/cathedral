use crate::pricing::{PriceCache, PricingService};
use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::Json,
    routing::{get, post},
    Router,
};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::sync::Arc;
use tracing::{info, warn};

/// State for pricing HTTP endpoints
#[derive(Clone)]
pub struct PricingState {
    pub pricing_service: Option<Arc<PricingService>>,
    pub price_cache: Arc<PriceCache>,
}

/// Create pricing routes
pub fn pricing_routes(state: PricingState) -> Router {
    Router::new()
        .route("/admin/prices", get(list_prices))
        .route("/admin/prices/:gpu_model", get(get_price))
        .route("/admin/prices/sync", post(trigger_sync))
        .route("/admin/prices/history", get(get_history))
        .with_state(state)
}

/// Query parameters for listing prices
#[derive(Debug, Deserialize)]
struct ListPricesQuery {
    #[serde(default)]
    gpu_model: Option<String>,
    #[serde(default)]
    provider: Option<String>,
    #[serde(default)]
    include_expired: bool,
}

/// Response for a GPU price
#[derive(Debug, Serialize)]
struct PriceResponse {
    gpu_model: String,
    vram_gb: Option<u32>,
    market_price_per_hour: String,
    discounted_price_per_hour: String,
    discount_percent: String,
    source: String,
    provider: String,
    location: Option<String>,
    instance_name: Option<String>,
    updated_at: String,
    is_spot: bool,
}

/// Response for price sync
#[derive(Debug, Serialize)]
struct SyncResponse {
    success: bool,
    prices_synced: usize,
    sync_started_at: String,
    sync_completed_at: String,
    next_scheduled_sync: String,
    providers_used: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

/// Query parameters for price history
#[derive(Debug, Deserialize)]
struct HistoryQuery {
    gpu_model: String,
    #[serde(default)]
    provider: Option<String>,
    #[serde(default)]
    start_time: Option<String>,
    #[serde(default)]
    end_time: Option<String>,
    #[serde(default = "default_limit")]
    limit: u32,
}

fn default_limit() -> u32 {
    100
}

/// Response for price history entry
#[derive(Debug, Serialize)]
struct HistoryEntry {
    gpu_model: String,
    price_per_hour: String,
    source: String,
    provider: String,
    recorded_at: String,
}

/// GET /admin/prices - List cached prices
async fn list_prices(
    State(state): State<PricingState>,
    Query(params): Query<ListPricesQuery>,
) -> Result<Json<Vec<PriceResponse>>, (StatusCode, String)> {
    info!(
        "HTTP: List prices - gpu_model={:?}, provider={:?}",
        params.gpu_model, params.provider
    );

    // Get all cached prices
    let cached_prices = state
        .price_cache
        .get_all()
        .await
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, format!("Failed to get cached prices: {}", e)))?;

    // Filter by GPU model if specified
    let filtered_prices: Vec<_> = match params.gpu_model {
        Some(ref model) => cached_prices
            .into_iter()
            .filter(|p| p.gpu_model == *model)
            .collect(),
        None => cached_prices,
    };

    // Filter by provider if specified
    let filtered_prices: Vec<_> = match params.provider {
        Some(ref provider) => filtered_prices
            .into_iter()
            .filter(|p| p.provider == *provider)
            .collect(),
        None => filtered_prices,
    };

    // Convert to response format
    let prices: Vec<PriceResponse> = filtered_prices
        .into_iter()
        .map(|p| PriceResponse {
            gpu_model: p.gpu_model,
            vram_gb: p.vram_gb,
            market_price_per_hour: format_decimal(p.market_price_per_hour),
            discounted_price_per_hour: format_decimal(p.discounted_price_per_hour),
            discount_percent: format_decimal(p.discount_percent),
            source: p.source,
            provider: p.provider,
            location: p.location,
            instance_name: p.instance_name,
            updated_at: p.updated_at.to_rfc3339(),
            is_spot: p.is_spot,
        })
        .collect();

    info!("HTTP: Returning {} prices", prices.len());
    Ok(Json(prices))
}

/// GET /admin/prices/:gpu_model - Get specific GPU price
async fn get_price(
    State(state): State<PricingState>,
    Path(gpu_model): Path<String>,
) -> Result<Json<Value>, (StatusCode, String)> {
    info!("HTTP: Get price for GPU: {}", gpu_model);

    // Check if pricing service is available
    let pricing_service = state
        .pricing_service
        .as_ref()
        .ok_or_else(|| {
            (
                StatusCode::SERVICE_UNAVAILABLE,
                "Dynamic pricing is not enabled".to_string(),
            )
        })?;

    // Get price from service
    let price = pricing_service
        .get_price(&gpu_model)
        .await
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, format!("Failed to get price: {}", e)))?;

    match price {
        Some(price_val) => {
            let response = serde_json::json!({
                "gpu_model": gpu_model,
                "price_per_hour": format_decimal(price_val),
                "timestamp": chrono::Utc::now().to_rfc3339(),
            });
            Ok(Json(response))
        }
        None => Err((
            StatusCode::NOT_FOUND,
            format!("No price found for GPU model: {}", gpu_model),
        )),
    }
}

/// POST /admin/prices/sync - Trigger manual price sync
async fn trigger_sync(
    State(state): State<PricingState>,
) -> Result<Json<SyncResponse>, (StatusCode, String)> {
    info!("HTTP: Manual price sync triggered");

    // Check if pricing service is available
    let pricing_service = state
        .pricing_service
        .as_ref()
        .ok_or_else(|| {
            (
                StatusCode::SERVICE_UNAVAILABLE,
                "Dynamic pricing is not enabled".to_string(),
            )
        })?;

    let sync_started_at = chrono::Utc::now();

    // Perform the sync
    let result = pricing_service.sync_prices().await;

    let sync_completed_at = chrono::Utc::now();

    match result {
        Ok(prices_synced) => {
            // Calculate next scheduled sync
            let next_scheduled_sync = crate::server::BillingServer::calculate_next_sync_time(Some(2));

            let response = SyncResponse {
                success: true,
                prices_synced,
                sync_started_at: sync_started_at.to_rfc3339(),
                sync_completed_at: sync_completed_at.to_rfc3339(),
                next_scheduled_sync: next_scheduled_sync.to_rfc3339(),
                providers_used: vec!["VastAI".to_string(), "RunPod".to_string()],
                error: None,
            };

            info!("HTTP: Price sync completed: {} prices synced", prices_synced);
            Ok(Json(response))
        }
        Err(e) => {
            warn!("HTTP: Price sync failed: {}", e);
            let response = SyncResponse {
                success: false,
                prices_synced: 0,
                sync_started_at: sync_started_at.to_rfc3339(),
                sync_completed_at: sync_completed_at.to_rfc3339(),
                next_scheduled_sync: chrono::Utc::now().to_rfc3339(),
                providers_used: vec![],
                error: Some(e.to_string()),
            };
            Ok(Json(response))
        }
    }
}

/// GET /admin/prices/history - Get price history
async fn get_history(
    State(state): State<PricingState>,
    Query(params): Query<HistoryQuery>,
) -> Result<Json<Vec<HistoryEntry>>, (StatusCode, String)> {
    info!("HTTP: Get price history for GPU: {}", params.gpu_model);

    // Build query
    let mut query = String::from(
        "SELECT gpu_model, price_per_hour, source, provider, recorded_at
         FROM billing.price_history
         WHERE gpu_model = $1",
    );

    let mut bind_count = 2;

    // Add time filters if provided
    let start_time = params.start_time.as_ref().and_then(|s| {
        chrono::DateTime::parse_from_rfc3339(s)
            .ok()
            .map(|dt| dt.with_timezone(&chrono::Utc))
    });

    let end_time = params.end_time.as_ref().and_then(|s| {
        chrono::DateTime::parse_from_rfc3339(s)
            .ok()
            .map(|dt| dt.with_timezone(&chrono::Utc))
    });

    if start_time.is_some() {
        query.push_str(&format!(" AND recorded_at >= ${}", bind_count));
        bind_count += 1;
    }

    if end_time.is_some() {
        query.push_str(&format!(" AND recorded_at <= ${}", bind_count));
        bind_count += 1;
    }

    // Add provider filter if provided
    if params.provider.is_some() {
        query.push_str(&format!(" AND provider = ${}", bind_count));
    }

    query.push_str(&format!(" ORDER BY recorded_at DESC LIMIT {}", params.limit));

    // Execute query
    let pool = state.price_cache.pool();
    let mut query_builder =
        sqlx::query_as::<_, (String, Decimal, String, String, chrono::DateTime<chrono::Utc>)>(&query);

    query_builder = query_builder.bind(&params.gpu_model);

    if let Some(st) = start_time {
        query_builder = query_builder.bind(st);
    }

    if let Some(et) = end_time {
        query_builder = query_builder.bind(et);
    }

    if let Some(ref provider) = params.provider {
        query_builder = query_builder.bind(provider);
    }

    let rows = query_builder
        .fetch_all(pool)
        .await
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, format!("Failed to fetch price history: {}", e)))?;

    let entries: Vec<HistoryEntry> = rows
        .into_iter()
        .map(|(gpu_model, price, source, provider, recorded_at)| HistoryEntry {
            gpu_model,
            price_per_hour: format_decimal(price),
            source,
            provider,
            recorded_at: recorded_at.to_rfc3339(),
        })
        .collect();

    info!("HTTP: Returning {} price history entries", entries.len());
    Ok(Json(entries))
}

/// Format decimal for JSON response
fn format_decimal(d: Decimal) -> String {
    let normalized = d.normalize();
    if normalized.fract().is_zero() {
        normalized.trunc().to_string()
    } else {
        let s = normalized.to_string();
        if s.contains('.') {
            s.trim_end_matches('0').trim_end_matches('.').to_string()
        } else {
            s
        }
    }
}
