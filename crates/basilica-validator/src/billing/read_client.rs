use std::time::Duration;

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use tonic::transport::{Channel, ClientTlsConfig, Endpoint};
use tracing::info;

use basilica_protocol::billing::billing_service_client::BillingServiceClient;
use basilica_protocol::billing::{GetMinerDeliveryRequest, MinerDelivery};

use crate::config::BillingConfig;

pub struct BillingReadClient {
    config: BillingConfig,
    channel: tokio::sync::Mutex<Option<Channel>>,
}

impl BillingReadClient {
    pub fn new(config: BillingConfig) -> Self {
        if !config.enabled {
            info!("Billing read client disabled by configuration");
            return Self {
                config,
                channel: tokio::sync::Mutex::new(None),
            };
        }

        Self {
            config,
            channel: tokio::sync::Mutex::new(None),
        }
    }

    pub async fn get_miner_delivery(
        &self,
        since: DateTime<Utc>,
        until: DateTime<Utc>,
        miner_hotkeys: Vec<String>,
    ) -> Result<Vec<MinerDelivery>> {
        if !self.config.enabled {
            return Ok(Vec::new());
        }

        let channel = self.get_or_connect().await?;
        let mut client = BillingServiceClient::new(channel);

        let request = GetMinerDeliveryRequest {
            since_epoch_seconds: since.timestamp(),
            until_epoch_seconds: until.timestamp(),
            miner_hotkeys,
        };

        let response = client
            .get_miner_delivery(request)
            .await
            .context("Failed to request miner delivery")?
            .into_inner();

        Ok(response.deliveries)
    }

    async fn connect(config: &BillingConfig) -> Result<Channel> {
        info!(
            "Initializing billing read client for endpoint: {} (TLS: {})",
            config.billing_endpoint, config.use_tls
        );

        let mut endpoint = Endpoint::from_shared(config.billing_endpoint.clone())
            .with_context(|| format!("Invalid billing endpoint: {}", config.billing_endpoint))?
            .connect_timeout(Duration::from_secs(config.timeout_secs))
            .timeout(Duration::from_secs(config.timeout_secs));

        if config.use_tls {
            let host = config
                .billing_endpoint
                .trim_start_matches("https://")
                .trim_start_matches("http://")
                .split([':', '/'].as_ref())
                .next()
                .ok_or_else(|| {
                    anyhow::anyhow!("Invalid TLS endpoint: {}", config.billing_endpoint)
                })?;

            info!(
                "Configuring TLS with system root certificates for host: {}",
                host
            );

            let tls_config = ClientTlsConfig::new().domain_name(host);
            endpoint = endpoint
                .tls_config(tls_config)
                .with_context(|| "Failed to configure TLS for billing endpoint")?;
        }

        endpoint.connect().await.with_context(|| {
            format!(
                "Failed to connect to billing service at {}",
                config.billing_endpoint
            )
        })
    }

    async fn get_or_connect(&self) -> Result<Channel> {
        let mut guard = self.channel.lock().await;
        if let Some(channel) = guard.as_ref() {
            return Ok(channel.clone());
        }

        let channel = Self::connect(&self.config).await?;
        *guard = Some(channel.clone());
        Ok(channel)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use basilica_protocol::billing::billing_service_server::BillingService;
    use basilica_protocol::billing::{
        ApplyCreditsRequest, ApplyCreditsResponse, FinalizeRentalRequest, FinalizeRentalResponse,
        GetActiveRentalsRequest, GetActiveRentalsResponse, GetBalanceRequest, GetBalanceResponse,
        GetMinerDeliveryRequest, GetMinerDeliveryResponse, GetMinerRevenueSummaryRequest,
        GetMinerRevenueSummaryResponse, GetRentalStatusRequest, GetRentalStatusResponse,
        GetUnpaidMinerRevenueSummaryRequest, GetUnpaidMinerRevenueSummaryResponse, IngestResponse,
        MarkMinerRevenuePaidRequest, MarkMinerRevenuePaidResponse,
        RefreshMinerRevenueSummaryRequest, RefreshMinerRevenueSummaryResponse, TelemetryData,
        TrackRentalRequest, TrackRentalResponse, UpdateRentalStatusRequest,
        UpdateRentalStatusResponse, UsageReportRequest, UsageReportResponse,
    };
    use std::net::SocketAddr;
    use tokio::net::TcpListener;
    use tokio_stream::wrappers::TcpListenerStream;
    use tonic::transport::Server;
    use tonic::{Request, Response, Status};

    #[derive(Clone)]
    struct TestBillingService {
        deliveries: Vec<MinerDelivery>,
    }

    #[tonic::async_trait]
    impl BillingService for TestBillingService {
        async fn apply_credits(
            &self,
            _request: Request<ApplyCreditsRequest>,
        ) -> Result<Response<ApplyCreditsResponse>, Status> {
            Err(Status::unimplemented("apply_credits"))
        }

        async fn get_balance(
            &self,
            _request: Request<GetBalanceRequest>,
        ) -> Result<Response<GetBalanceResponse>, Status> {
            Err(Status::unimplemented("get_balance"))
        }

        async fn track_rental(
            &self,
            _request: Request<TrackRentalRequest>,
        ) -> Result<Response<TrackRentalResponse>, Status> {
            Err(Status::unimplemented("track_rental"))
        }

        async fn update_rental_status(
            &self,
            _request: Request<UpdateRentalStatusRequest>,
        ) -> Result<Response<UpdateRentalStatusResponse>, Status> {
            Err(Status::unimplemented("update_rental_status"))
        }

        async fn get_active_rentals(
            &self,
            _request: Request<GetActiveRentalsRequest>,
        ) -> Result<Response<GetActiveRentalsResponse>, Status> {
            Err(Status::unimplemented("get_active_rentals"))
        }

        async fn finalize_rental(
            &self,
            _request: Request<FinalizeRentalRequest>,
        ) -> Result<Response<FinalizeRentalResponse>, Status> {
            Err(Status::unimplemented("finalize_rental"))
        }

        async fn ingest_telemetry(
            &self,
            _request: Request<tonic::Streaming<TelemetryData>>,
        ) -> Result<Response<IngestResponse>, Status> {
            Err(Status::unimplemented("ingest_telemetry"))
        }

        async fn get_usage_report(
            &self,
            _request: Request<UsageReportRequest>,
        ) -> Result<Response<UsageReportResponse>, Status> {
            Err(Status::unimplemented("get_usage_report"))
        }

        async fn refresh_miner_revenue_summary(
            &self,
            _request: Request<RefreshMinerRevenueSummaryRequest>,
        ) -> Result<Response<RefreshMinerRevenueSummaryResponse>, Status> {
            Err(Status::unimplemented("refresh_miner_revenue_summary"))
        }

        async fn get_miner_revenue_summary(
            &self,
            _request: Request<GetMinerRevenueSummaryRequest>,
        ) -> Result<Response<GetMinerRevenueSummaryResponse>, Status> {
            Err(Status::unimplemented("get_miner_revenue_summary"))
        }

        async fn get_miner_delivery(
            &self,
            _request: Request<GetMinerDeliveryRequest>,
        ) -> Result<Response<GetMinerDeliveryResponse>, Status> {
            Ok(Response::new(GetMinerDeliveryResponse {
                deliveries: self.deliveries.clone(),
            }))
        }

        async fn get_unpaid_miner_revenue_summary(
            &self,
            _request: Request<GetUnpaidMinerRevenueSummaryRequest>,
        ) -> Result<Response<GetUnpaidMinerRevenueSummaryResponse>, Status> {
            Err(Status::unimplemented("get_unpaid_miner_revenue_summary"))
        }

        async fn mark_miner_revenue_paid(
            &self,
            _request: Request<MarkMinerRevenuePaidRequest>,
        ) -> Result<Response<MarkMinerRevenuePaidResponse>, Status> {
            Err(Status::unimplemented("mark_miner_revenue_paid"))
        }

        async fn get_rental_status(
            &self,
            _request: Request<GetRentalStatusRequest>,
        ) -> Result<Response<GetRentalStatusResponse>, Status> {
            Err(Status::unimplemented("get_rental_status"))
        }
    }

    async fn start_test_server(deliveries: Vec<MinerDelivery>) -> SocketAddr {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let service = TestBillingService { deliveries };

        tokio::spawn(async move {
            Server::builder()
                .add_service(
                    basilica_protocol::billing::billing_service_server::BillingServiceServer::new(
                        service,
                    ),
                )
                .serve_with_incoming(TcpListenerStream::new(listener))
                .await
                .unwrap();
        });

        addr
    }

    #[tokio::test]
    async fn test_get_miner_delivery_success() {
        let deliveries = vec![MinerDelivery {
            miner_hotkey: "hotkey".to_string(),
            miner_uid: 7,
            total_hours: 12.5,
            user_revenue_usd: 42.0,
            gpu_category: "H100".to_string(),
            miner_payment_usd: 21.0,
        }];
        let addr = start_test_server(deliveries.clone()).await;

        let config = BillingConfig {
            enabled: true,
            billing_endpoint: format!("http://{}", addr),
            ..BillingConfig::default()
        };
        let client = BillingReadClient::new(config);

        let since = Utc::now() - chrono::Duration::hours(1);
        let until = Utc::now();
        let result = client
            .get_miner_delivery(since, until, Vec::new())
            .await
            .unwrap();

        assert_eq!(result.len(), 1);
        assert_eq!(result[0].miner_uid, deliveries[0].miner_uid);

        // Second call should reuse the channel
        let result_again = client
            .get_miner_delivery(since, until, Vec::new())
            .await
            .unwrap();
        assert_eq!(result_again.len(), 1);
    }

    #[tokio::test]
    async fn test_get_miner_delivery_disabled() {
        let config = BillingConfig {
            enabled: false,
            ..BillingConfig::default()
        };
        let client = BillingReadClient::new(config);
        let result = client
            .get_miner_delivery(Utc::now(), Utc::now(), Vec::new())
            .await
            .unwrap();
        assert!(result.is_empty());
    }
}
