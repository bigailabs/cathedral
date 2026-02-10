//! Discovery endpoint for miners to find validator gRPC services

use axum::extract::State;
use axum::Json;

use crate::api::ApiState;

/// Discovery endpoint - returns the validator's bid gRPC port and version.
/// Miners call this after selecting a validator from the metagraph to learn
/// the gRPC endpoint for registration.
pub async fn discovery(State(state): State<ApiState>) -> Json<serde_json::Value> {
    let port = parse_port_from_listen_address(&state.validator_config.bid_grpc.listen_address);

    Json(serde_json::json!({
        "bid_grpc_port": port,
        "version": env!("CARGO_PKG_VERSION")
    }))
}

/// Extract port number from a listen address like "0.0.0.0:50052"
fn parse_port_from_listen_address(addr: &str) -> u16 {
    addr.rsplit(':')
        .next()
        .and_then(|p| p.parse().ok())
        .unwrap_or(50052)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_port() {
        assert_eq!(parse_port_from_listen_address("0.0.0.0:50052"), 50052);
        assert_eq!(parse_port_from_listen_address("127.0.0.1:8080"), 8080);
        assert_eq!(parse_port_from_listen_address("invalid"), 50052);
    }
}
