use basilica_autoscaler::{runtime::ControllerRuntime, AutoscalerConfig};
use tracing_subscriber::{fmt, prelude::*, EnvFilter};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Initialize tracing
    tracing_subscriber::registry()
        .with(fmt::layer())
        .with(EnvFilter::from_default_env().add_directive("basilica_autoscaler=info".parse()?))
        .init();

    // Load configuration
    let config = AutoscalerConfig::from_env();

    // Create and run the controller runtime
    let runtime = ControllerRuntime::new(config);
    runtime.run().await.map_err(|e| anyhow::anyhow!("{}", e))
}
