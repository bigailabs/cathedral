use axum::{routing::get, Router};
use tracing::info;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();

    // Install metrics recorder and expose /metrics
    let handle = metrics_exporter_prometheus::PrometheusBuilder::new()
        .install_recorder()
        .expect("install prometheus recorder");

    let metrics_handle = handle.clone();
    let app = Router::new().route(
        "/metrics",
        get(move || {
            let h = metrics_handle.clone();
            async move {
                let body = h.render();
                Ok::<_, std::convert::Infallible>(
                    axum::response::Response::builder()
                        .header(
                            axum::http::header::CONTENT_TYPE,
                            "text/plain; version=0.0.4",
                        )
                        .body(axum::body::Body::from(body))
                        .unwrap(),
                )
            }
        }),
    );

    // Bind address from env or default
    let bind = std::env::var("OPERATOR_METRICS_ADDR").unwrap_or_else(|_| "0.0.0.0:9400".into());
    let listener = tokio::net::TcpListener::bind(&bind)
        .await
        .expect("bind metrics addr");
    tokio::spawn(async move {
        axum::serve(listener, app).await.expect("serve metrics");
    });

    info!("basilica-operator starting controllers");
    if let Err(e) = basilica_operator::runtime::run().await {
        eprintln!("operator runtime terminated with error: {e}");
    }
}
