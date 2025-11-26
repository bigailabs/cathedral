use axum::{extract::State, http::StatusCode, routing::get, Json, Router};
use basilica_operator::admission::{admission_router, AdmissionState};
use serde_json::json;
use tracing::{info, warn};

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .json()
        .with_target(true)
        .with_level(true)
        .with_thread_ids(true)
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();

    // Install metrics recorder and expose /metrics
    let handle = metrics_exporter_prometheus::PrometheusBuilder::new()
        .install_recorder()
        .expect("install prometheus recorder");

    let metrics_handle = handle.clone();
    let app = Router::new()
        .route("/metrics", get(metrics_handler))
        .route("/health", get(health_handler))
        .route("/ready", get(ready_handler))
        .with_state(metrics_handle);

    // Bind address from env or default
    let bind = std::env::var("OPERATOR_METRICS_ADDR").unwrap_or_else(|_| "0.0.0.0:9400".into());
    let listener = match tokio::net::TcpListener::bind(&bind).await {
        Ok(l) => l,
        Err(e) => {
            tracing::error!("Failed to bind metrics server to {}: {}", bind, e);
            return;
        }
    };

    info!("Metrics server listening on {}", bind);
    let _metrics_handle = tokio::spawn(async move {
        if let Err(e) = axum::serve(listener, app).await {
            tracing::error!("Metrics server failed: {}", e);
        }
    });

    // Start admission webhook server if enabled
    let webhook_enabled = std::env::var("OPERATOR_WEBHOOK_ENABLED")
        .map(|v| v == "true" || v == "1")
        .unwrap_or(false);

    if webhook_enabled {
        let fuse_base_path = std::env::var("OPERATOR_FUSE_BASE_PATH")
            .unwrap_or_else(|_| "/var/lib/basilica/fuse".into());
        let webhook_bind =
            std::env::var("OPERATOR_WEBHOOK_ADDR").unwrap_or_else(|_| "0.0.0.0:9443".into());

        let admission_state = AdmissionState { fuse_base_path };
        let webhook_app = admission_router(admission_state);

        match tokio::net::TcpListener::bind(&webhook_bind).await {
            Ok(listener) => {
                info!("Admission webhook server listening on {}", webhook_bind);
                tokio::spawn(async move {
                    if let Err(e) = axum::serve(listener, webhook_app).await {
                        tracing::error!("Admission webhook server failed: {}", e);
                    }
                });
            }
            Err(e) => {
                warn!(
                    "Failed to bind admission webhook server to {}: {} - webhook disabled",
                    webhook_bind, e
                );
            }
        }
    }

    info!("basilica-operator starting controllers");
    if let Err(e) = basilica_operator::runtime::run().await {
        eprintln!("operator runtime terminated with error: {e}");
    }
}

async fn metrics_handler(
    State(handle): State<metrics_exporter_prometheus::PrometheusHandle>,
) -> axum::response::Response<axum::body::Body> {
    let body = handle.render();
    axum::response::Response::builder()
        .header(
            axum::http::header::CONTENT_TYPE,
            "text/plain; version=0.0.4",
        )
        .body(axum::body::Body::from(body))
        .unwrap()
}

async fn health_handler() -> Json<serde_json::Value> {
    Json(json!({
        "status": "healthy",
        "version": env!("CARGO_PKG_VERSION"),
        "timestamp": chrono::Utc::now().to_rfc3339(),
    }))
}

async fn ready_handler() -> (StatusCode, Json<serde_json::Value>) {
    match kube::Client::try_default().await {
        Ok(_) => (
            StatusCode::OK,
            Json(json!({
                "status": "ready",
                "k8s_api": "connected",
            })),
        ),
        Err(e) => (
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({
                "status": "not ready",
                "k8s_api": "disconnected",
                "error": e.to_string(),
            })),
        ),
    }
}
