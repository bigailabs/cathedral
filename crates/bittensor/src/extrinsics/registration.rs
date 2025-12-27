//! # Registration Extrinsics
//!
//! Extrinsics for neuron registration on the Bittensor network:
//! - `serve_axon`: Register an axon endpoint
//! - `serve_prometheus`: Register a prometheus endpoint
//! - `burned_register`: Register by burning TAO

use crate::api::api;
use crate::error::BittensorError;
use crate::extrinsics::ExtrinsicResponse;
use subxt::OnlineClient;
use subxt::PolkadotConfig;

/// Parameters for serving an axon
#[derive(Debug, Clone)]
pub struct ServeAxonParams {
    /// Subnet netuid
    pub netuid: u16,
    /// IP version (4 or 6)
    pub version: u32,
    /// IP address as a u128
    pub ip: u128,
    /// Port number
    pub port: u16,
    /// IP type (4 for IPv4, 6 for IPv6)
    pub ip_type: u8,
    /// Protocol (e.g., 0 for gRPC)
    pub protocol: u8,
    /// Placeholder 1
    pub placeholder1: u8,
    /// Placeholder 2
    pub placeholder2: u8,
}

impl ServeAxonParams {
    /// Create params for an IPv4 axon
    ///
    /// # Example
    ///
    /// ```
    /// use bittensor::extrinsics::ServeAxonParams;
    ///
    /// let params = ServeAxonParams::ipv4(1, "192.168.1.1", 8080);
    /// assert!(params.is_ok());
    /// ```
    pub fn ipv4(netuid: u16, ip: &str, port: u16) -> Result<Self, BittensorError> {
        let ip_parts: Vec<u8> = ip
            .split('.')
            .map(|s| s.parse::<u8>())
            .collect::<Result<Vec<_>, _>>()
            .map_err(|_| BittensorError::ConfigError {
                field: "ip".to_string(),
                message: format!("Invalid IPv4 address: {}", ip),
            })?;

        if ip_parts.len() != 4 {
            return Err(BittensorError::ConfigError {
                field: "ip".to_string(),
                message: format!("Invalid IPv4 address: {}", ip),
            });
        }

        let ip_u128 = ((ip_parts[0] as u128) << 24)
            | ((ip_parts[1] as u128) << 16)
            | ((ip_parts[2] as u128) << 8)
            | (ip_parts[3] as u128);

        Ok(Self {
            netuid,
            version: 4,
            ip: ip_u128,
            port,
            ip_type: 4,
            protocol: 0,
            placeholder1: 0,
            placeholder2: 0,
        })
    }

    /// Create params with raw values
    pub fn new(netuid: u16, version: u32, ip: u128, port: u16, ip_type: u8, protocol: u8) -> Self {
        Self {
            netuid,
            version,
            ip,
            port,
            ip_type,
            protocol,
            placeholder1: 0,
            placeholder2: 0,
        }
    }
}

/// Parameters for serving prometheus
#[derive(Debug, Clone)]
pub struct ServePrometheusParams {
    /// Subnet netuid
    pub netuid: u16,
    /// IP version
    pub version: u32,
    /// IP address as u128
    pub ip: u128,
    /// Port number
    pub port: u16,
    /// IP type
    pub ip_type: u8,
}

impl ServePrometheusParams {
    /// Create params for IPv4 prometheus
    pub fn ipv4(netuid: u16, ip: &str, port: u16) -> Result<Self, BittensorError> {
        let ip_parts: Vec<u8> = ip
            .split('.')
            .map(|s| s.parse::<u8>())
            .collect::<Result<Vec<_>, _>>()
            .map_err(|_| BittensorError::ConfigError {
                field: "ip".to_string(),
                message: format!("Invalid IPv4 address: {}", ip),
            })?;

        if ip_parts.len() != 4 {
            return Err(BittensorError::ConfigError {
                field: "ip".to_string(),
                message: format!("Invalid IPv4 address: {}", ip),
            });
        }

        let ip_u128 = ((ip_parts[0] as u128) << 24)
            | ((ip_parts[1] as u128) << 16)
            | ((ip_parts[2] as u128) << 8)
            | (ip_parts[3] as u128);

        Ok(Self {
            netuid,
            version: 4,
            ip: ip_u128,
            port,
            ip_type: 4,
        })
    }
}

/// Serve an axon endpoint on the network
///
/// This registers your neuron's axon endpoint so other neurons can connect to it.
pub async fn serve_axon<S>(
    client: &OnlineClient<PolkadotConfig>,
    signer: &S,
    params: ServeAxonParams,
) -> Result<ExtrinsicResponse<()>, BittensorError>
where
    S: subxt::tx::Signer<PolkadotConfig>,
{
    let call = api::tx().subtensor_module().serve_axon(
        params.netuid,
        params.version,
        params.ip,
        params.port,
        params.ip_type,
        params.protocol,
        params.placeholder1,
        params.placeholder2,
    );

    let tx_hash = client
        .tx()
        .sign_and_submit_default(&call, signer)
        .await
        .map_err(|e| BittensorError::TxSubmissionError {
            message: format!("Failed to submit serve_axon: {}", e),
        })?;

    Ok(ExtrinsicResponse::success()
        .with_message("Axon served successfully")
        .with_extrinsic_hash(&format!("{:?}", tx_hash))
        .with_data(()))
}

/// Serve a prometheus endpoint on the network
pub async fn serve_prometheus<S>(
    client: &OnlineClient<PolkadotConfig>,
    signer: &S,
    params: ServePrometheusParams,
) -> Result<ExtrinsicResponse<()>, BittensorError>
where
    S: subxt::tx::Signer<PolkadotConfig>,
{
    let call = api::tx().subtensor_module().serve_prometheus(
        params.netuid,
        params.version,
        params.ip,
        params.port,
        params.ip_type,
    );

    let tx_hash = client
        .tx()
        .sign_and_submit_default(&call, signer)
        .await
        .map_err(|e| BittensorError::TxSubmissionError {
            message: format!("Failed to submit serve_prometheus: {}", e),
        })?;

    Ok(ExtrinsicResponse::success()
        .with_message("Prometheus served successfully")
        .with_extrinsic_hash(&format!("{:?}", tx_hash))
        .with_data(()))
}

/// Register a neuron by burning TAO
///
/// This is an alternative to the POW-based registration.
pub async fn burned_register<S>(
    client: &OnlineClient<PolkadotConfig>,
    signer: &S,
    netuid: u16,
) -> Result<ExtrinsicResponse<()>, BittensorError>
where
    S: subxt::tx::Signer<PolkadotConfig>,
{
    let call = api::tx()
        .subtensor_module()
        .burned_register(netuid, signer.account_id());

    let tx_hash = client
        .tx()
        .sign_and_submit_default(&call, signer)
        .await
        .map_err(|e| BittensorError::TxSubmissionError {
            message: format!("Failed to submit burned_register: {}", e),
        })?;

    Ok(ExtrinsicResponse::success()
        .with_message("Burned registration successful")
        .with_extrinsic_hash(&format!("{:?}", tx_hash))
        .with_data(()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_serve_axon_ipv4() {
        let params = ServeAxonParams::ipv4(1, "192.168.1.1", 8080).unwrap();
        assert_eq!(params.netuid, 1);
        assert_eq!(params.port, 8080);
        assert_eq!(params.ip_type, 4);
        // 192.168.1.1 = (192 << 24) | (168 << 16) | (1 << 8) | 1
        let expected_ip = (192u128 << 24) | (168u128 << 16) | (1u128 << 8) | 1u128;
        assert_eq!(params.ip, expected_ip);
    }

    #[test]
    fn test_serve_axon_invalid_ip() {
        let result = ServeAxonParams::ipv4(1, "invalid", 8080);
        assert!(result.is_err());
    }

    #[test]
    fn test_serve_axon_too_few_octets() {
        let result = ServeAxonParams::ipv4(1, "192.168.1", 8080);
        assert!(result.is_err());
    }

    #[test]
    fn test_serve_axon_too_many_octets() {
        let result = ServeAxonParams::ipv4(1, "192.168.1.1.1", 8080);
        assert!(result.is_err());
    }

    #[test]
    fn test_serve_axon_new() {
        let params = ServeAxonParams::new(1, 4, 0x7f000001, 8080, 4, 0);
        assert_eq!(params.netuid, 1);
        assert_eq!(params.version, 4);
        assert_eq!(params.ip, 0x7f000001);
        assert_eq!(params.port, 8080);
        assert_eq!(params.ip_type, 4);
        assert_eq!(params.protocol, 0);
        assert_eq!(params.placeholder1, 0);
        assert_eq!(params.placeholder2, 0);
    }

    #[test]
    fn test_serve_axon_debug() {
        let params = ServeAxonParams::ipv4(1, "127.0.0.1", 8080).unwrap();
        let debug = format!("{:?}", params);
        assert!(debug.contains("ServeAxonParams"));
        assert!(debug.contains("netuid: 1"));
    }

    #[test]
    fn test_serve_prometheus_ipv4() {
        let params = ServePrometheusParams::ipv4(1, "10.0.0.1", 9090).unwrap();
        assert_eq!(params.netuid, 1);
        assert_eq!(params.port, 9090);
    }

    #[test]
    fn test_serve_prometheus_invalid_ip() {
        let result = ServePrometheusParams::ipv4(1, "not-an-ip", 9090);
        assert!(result.is_err());
    }

    #[test]
    fn test_serve_prometheus_debug() {
        let params = ServePrometheusParams::ipv4(1, "0.0.0.0", 9090).unwrap();
        let debug = format!("{:?}", params);
        assert!(debug.contains("ServePrometheusParams"));
    }

    #[test]
    fn test_serve_axon_clone() {
        let params = ServeAxonParams::ipv4(1, "192.168.1.1", 8080).unwrap();
        let cloned = params.clone();
        assert_eq!(params.netuid, cloned.netuid);
        assert_eq!(params.ip, cloned.ip);
    }
}
