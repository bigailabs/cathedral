use crate::domain::events::EventStore;
use crate::domain::{
    credits::{CreditManager, CreditOperations},
    rentals::{RentalManager, RentalOperations},
    types::{CreditBalance, GpuSpec, PackageId, RentalId, RentalState, ResourceSpec, UserId},
};
use crate::error::BillingError;
use crate::metrics::BillingMetricsSystem;
use crate::pricing::PricingService;
use crate::storage::events::{EventType, UsageEvent};
use crate::storage::rds::RdsConnection;
use crate::storage::{PackageRepository, SqlPackageRepository};
use crate::storage::{PriceCacheRepository, SqlPriceCacheRepository};
use crate::storage::{RentalRepository, SqlCreditRepository, SqlRentalRepository};
use crate::storage::{SqlUserPreferencesRepository, UserPreferencesRepository};
use crate::telemetry::{TelemetryIngester, TelemetryProcessor};

use basilica_protocol::billing::{
    billing_service_server::BillingService, ActiveRental, ApplyCreditsRequest,
    ApplyCreditsResponse, FinalizeRentalRequest, FinalizeRentalResponse, GetActiveRentalsRequest,
    GetActiveRentalsResponse, GetBalanceRequest, GetBalanceResponse, GetBillingPackagesRequest,
    GetBillingPackagesResponse, GetCachedPricesRequest, GetCachedPricesResponse,
    GetPriceHistoryRequest, GetPriceHistoryResponse, IngestResponse, RentalStatus,
    SetUserPackageRequest, SetUserPackageResponse, SyncPricesRequest, SyncPricesResponse,
    TelemetryData, TrackRentalRequest, TrackRentalResponse, UpdateRentalStatusRequest,
    UpdateRentalStatusResponse, UsageDataPoint, UsageReportRequest, UsageReportResponse,
    UsageSummary,
};

use rust_decimal::prelude::*;
use serde_json;
use std::str::FromStr;
use std::sync::Arc;
use tokio_stream::StreamExt;
use tonic::{Request, Response, Status};
use tracing::{error, info, warn};
use uuid;

pub struct BillingServiceImpl {
    credit_manager: Arc<dyn CreditOperations + Send + Sync>,
    rental_manager: Arc<dyn RentalOperations + Send + Sync>,
    #[allow(dead_code)] // Used in server's consumer loop
    telemetry_processor: Arc<TelemetryProcessor>,
    telemetry_ingester: Arc<TelemetryIngester>,
    rental_repository: Arc<dyn RentalRepository + Send + Sync>,
    package_repository: Arc<dyn PackageRepository + Send + Sync>,
    user_preferences_repository: Arc<dyn UserPreferencesRepository + Send + Sync>,
    event_store: Arc<EventStore>,
    metrics: Option<Arc<BillingMetricsSystem>>,
    pricing_service: Option<Arc<PricingService>>,
    pricing_config: Option<crate::pricing::types::DynamicPricingConfig>,
    price_cache: Arc<dyn PriceCacheRepository>,
}

impl BillingServiceImpl {
    pub async fn new(
        rds_connection: Arc<RdsConnection>,
        telemetry_ingester: Arc<TelemetryIngester>,
        telemetry_processor: Arc<TelemetryProcessor>,
        metrics: Option<Arc<BillingMetricsSystem>>,
    ) -> anyhow::Result<Self> {
        // Create default price cache
        let price_cache: Arc<dyn PriceCacheRepository> =
            Arc::new(SqlPriceCacheRepository::new(Arc::clone(&rds_connection)));

        Self::new_with_pricing(
            rds_connection,
            telemetry_ingester,
            telemetry_processor,
            metrics,
            None,
            None,
            price_cache,
        )
        .await
    }

    pub async fn new_with_pricing(
        rds_connection: Arc<RdsConnection>,
        telemetry_ingester: Arc<TelemetryIngester>,
        telemetry_processor: Arc<TelemetryProcessor>,
        metrics: Option<Arc<BillingMetricsSystem>>,
        pricing_service: Option<Arc<PricingService>>,
        pricing_config: Option<crate::pricing::types::DynamicPricingConfig>,
        price_cache: Arc<dyn PriceCacheRepository>,
    ) -> anyhow::Result<Self> {
        let credit_repository = Arc::new(SqlCreditRepository::new(rds_connection.clone()));
        let rental_repository = Arc::new(SqlRentalRepository::new(rds_connection.clone()));

        // Create package repository with shared pricing service if available
        let mut package_repository = SqlPackageRepository::new(rds_connection.pool().clone());
        if let Some(ref pricing_svc) = pricing_service {
            package_repository = package_repository.with_pricing_service(pricing_svc.clone());
        }
        let package_repository = Arc::new(package_repository);

        package_repository
            .initialize()
            .await
            .map_err(|e| anyhow::anyhow!("Failed to initialize package repository: {}", e))?;
        let user_preferences_repository =
            Arc::new(SqlUserPreferencesRepository::new(rds_connection.clone()));

        // Create event repositories using proper pattern
        let event_repository = Arc::new(crate::storage::events::SqlEventRepository::new(
            rds_connection.clone(),
        ));
        let batch_repository = Arc::new(crate::storage::events::SqlBatchRepository::new(
            rds_connection.clone(),
        ));
        let event_store = Arc::new(crate::domain::events::EventStore::new(
            event_repository,
            batch_repository,
            1000,
            90,
        ));

        Ok(Self {
            credit_manager: Arc::new(CreditManager::new(credit_repository.clone())),
            rental_manager: Arc::new(RentalManager::new(rental_repository.clone())),
            telemetry_processor,
            telemetry_ingester,
            rental_repository: rental_repository.clone(),
            package_repository: package_repository.clone(),
            user_preferences_repository: user_preferences_repository.clone(),
            event_store,
            metrics,
            pricing_service,
            pricing_config,
            price_cache,
        })
    }

    fn parse_decimal(s: &str) -> crate::error::Result<Decimal> {
        Decimal::from_str(s).map_err(|e| BillingError::ValidationError {
            field: "amount".to_string(),
            message: format!("Invalid decimal value: {}", e),
        })
    }

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

    fn format_credit_balance(b: CreditBalance) -> String {
        Self::format_decimal(b.as_decimal())
    }

    fn rental_status_to_domain(status: RentalStatus) -> RentalState {
        match status {
            RentalStatus::Pending => RentalState::Pending,
            RentalStatus::Active => RentalState::Active,
            RentalStatus::Stopping => RentalState::Terminating,
            RentalStatus::Stopped => RentalState::Completed,
            RentalStatus::Failed => RentalState::Failed,
            RentalStatus::Unspecified => RentalState::Pending,
        }
    }

    fn domain_status_to_proto(state: RentalState) -> RentalStatus {
        match state {
            RentalState::Pending => RentalStatus::Pending,
            RentalState::Active => RentalStatus::Active,
            RentalState::Suspended => RentalStatus::Stopping,
            RentalState::Terminating => RentalStatus::Stopping,
            RentalState::Completed => RentalStatus::Stopped,
            RentalState::Failed => RentalStatus::Failed,
        }
    }
}

#[tonic::async_trait]
impl BillingService for BillingServiceImpl {
    async fn apply_credits(
        &self,
        request: Request<ApplyCreditsRequest>,
    ) -> std::result::Result<Response<ApplyCreditsResponse>, Status> {
        let timer = self
            .metrics
            .as_ref()
            .map(|m| m.billing_metrics().start_grpc_timer());

        let req = request.into_inner();
        let user_id = UserId::new(req.user_id.clone());

        let result = async {
            let amount = Self::parse_decimal(&req.amount)
                .map_err(|e| Status::invalid_argument(format!("Invalid amount: {}", e)))?;
            let credit_balance = CreditBalance::from_decimal(amount);

            info!("Applying {} credits to user {}", amount, user_id);

            let new_balance = self
                .credit_manager
                .apply_credits(&user_id, credit_balance)
                .await
                .map_err(|e| Status::internal(format!("Failed to apply credits: {}", e)))?;

            if let Some(ref metrics) = self.metrics {
                metrics
                    .billing_metrics()
                    .record_credit_applied(amount.to_f64().unwrap_or(0.0), user_id.as_str())
                    .await;
            }

            let response = ApplyCreditsResponse {
                success: true,
                new_balance: Self::format_credit_balance(new_balance),
                credit_id: req.transaction_id,
                applied_at: Some(prost_types::Timestamp::from(std::time::SystemTime::now())),
            };

            Ok(response)
        }
        .await;

        if let Some(ref metrics) = self.metrics {
            if let Some(timer) = timer {
                let status = if result.is_ok() { "success" } else { "error" };
                metrics
                    .billing_metrics()
                    .record_grpc_request(timer, "apply_credits", status)
                    .await;
            }
        }

        result.map(Response::new)
    }

    async fn get_balance(
        &self,
        request: Request<GetBalanceRequest>,
    ) -> std::result::Result<Response<GetBalanceResponse>, Status> {
        let req = request.into_inner();
        let user_id = UserId::new(req.user_id);

        let account = self
            .credit_manager
            .get_account(&user_id)
            .await
            .map_err(|e| Status::internal(format!("Failed to get account: {}", e)))?;

        let response = GetBalanceResponse {
            available_balance: Self::format_credit_balance(account.balance),
            total_balance: Self::format_credit_balance(account.balance),
            last_updated: Some(prost_types::Timestamp::from(std::time::SystemTime::from(
                account.last_updated,
            ))),
        };

        Ok(Response::new(response))
    }

    async fn track_rental(
        &self,
        request: Request<TrackRentalRequest>,
    ) -> std::result::Result<Response<TrackRentalResponse>, Status> {
        let timer = self
            .metrics
            .as_ref()
            .map(|m| m.billing_metrics().start_grpc_timer());

        let req = request.into_inner();

        let result = async {
            let rental_id = RentalId::from_str(&req.rental_id)
                .map_err(|e| Status::invalid_argument(format!("Invalid rental ID: {}", e)))?;
            let user_id = UserId::new(req.user_id);

            let resource_spec = if let Some(spec) = req.resource_spec {
                ResourceSpec {
                    gpu_specs: spec
                        .gpus
                        .into_iter()
                        .map(|gpu| GpuSpec {
                            model: gpu.model,
                            memory_mb: gpu.memory_mb,
                            count: gpu.count,
                        })
                        .collect(),
                    cpu_cores: spec.cpu_cores,
                    memory_gb: (spec.memory_mb / 1024) as u32,
                    storage_gb: spec.disk_gb as u32,
                    disk_iops: 0,
                    network_bandwidth_mbps: spec.network_bandwidth_mbps,
                }
            } else {
                ResourceSpec {
                    gpu_specs: vec![],
                    cpu_cores: 4,
                    memory_gb: 16,
                    storage_gb: 100,
                    disk_iops: 1000,
                    network_bandwidth_mbps: 1000,
                }
            };

            let package = if !resource_spec.gpu_specs.is_empty() {
                self.package_repository
                    .find_package_for_gpu_model(&resource_spec.gpu_specs[0].model)
                    .await
                    .map_err(|e| Status::internal(format!("Failed to retrieve package: {}", e)))?
            } else {
                self.package_repository
                    .get_package(&PackageId::h100())
                    .await
                    .map_err(|e| Status::internal(format!("Failed to retrieve package: {}", e)))?
            };

            let package_id = package.id.clone();
            let credit_rate = package.hourly_rate;

            if !req.hourly_rate.is_empty() {
                let api_provided_rate = Self::parse_decimal(&req.hourly_rate)
                    .map_err(|e| Status::invalid_argument(format!("Invalid hourly rate: {}", e)))?;
                if (api_provided_rate - credit_rate.as_decimal()).abs() > rust_decimal::Decimal::new(1, 2) {
                    warn!(
                        "API-provided hourly_rate ({}) differs from package rate ({}) for rental {}",
                        api_provided_rate, credit_rate, rental_id
                    );
                }
            }

            info!(
                "Tracking rental {} for user {} at {} credits/hour (package: {})",
                rental_id, user_id, credit_rate, package_id
            );
            let package_id_str = package_id.to_string();
            let resource_spec_value =
                serde_json::to_value(&resource_spec).unwrap_or(serde_json::Value::Null);
            let validator_id = req.validator_id.clone();
            let validator_id_copy = validator_id.clone();

            // Check if rental already exists (idempotency)
            if let Ok(Some(_existing)) = self.rental_repository.get_rental(&rental_id).await {
                info!(
                    "Rental {} already exists, returning success (idempotent)",
                    rental_id
                );

                let response = TrackRentalResponse {
                    success: true,
                    tracking_id: rental_id.to_string(),
                };
                return Ok(response);
            }

            // Calculate max duration first
            // Create rental with the provided ID
            use crate::domain::rentals::Rental;
            use crate::domain::types::{CostBreakdown, UsageMetrics};

            let now = chrono::Utc::now();
            let rental = Rental {
                id: rental_id,
                user_id: user_id.clone(),
                node_id: req.node_id.clone(),
                validator_id: validator_id.clone(),
                package_id,
                state: crate::domain::types::RentalState::Pending,
                resource_spec,
                usage_metrics: UsageMetrics::zero(),
                cost_breakdown: CostBreakdown {
                    base_cost: credit_rate,
                    usage_cost: CreditBalance::zero(),
                    volume_discount: CreditBalance::zero(),
                    discounts: CreditBalance::zero(),
                    overage_charges: CreditBalance::zero(),
                    total_cost: CreditBalance::zero(),
                },
                started_at: now,
                updated_at: now,
                ended_at: None,
                metadata: Default::default(),
                created_at: now,
                last_updated: now,
                actual_start_time: Some(now),
                actual_end_time: None,
                actual_cost: CreditBalance::zero(),
            };

            // Persist to database
            self.rental_repository
                .create_rental(&rental)
                .await
                .map_err(|e| Status::internal(format!("Failed to create rental: {}", e)))?;

            let rental_start_event = UsageEvent {
                event_id: uuid::Uuid::new_v4(),
                rental_id: rental_id.as_uuid(),
                user_id: user_id.to_string(),
                node_id: req.node_id.clone(),
                validator_id: validator_id_copy,
                event_type: EventType::RentalStart,
                event_data: serde_json::json!({
                    "package_id": package_id_str,
                    "hourly_rate": Self::format_credit_balance(credit_rate),
                    "resource_spec": resource_spec_value,
                }),
                timestamp: chrono::Utc::now(),
                processed: false,
                processed_at: None,
                batch_id: None,
            };

            self.event_store
                .append_usage_event(&rental_start_event)
                .await
                .map_err(|e| {
                    Status::internal(format!("Failed to store rental start event: {}", e))
                })?;

            if let Some(ref metrics) = self.metrics {
                metrics
                    .billing_metrics()
                    .record_rental_tracked(&rental_id.to_string(), &package_id_str)
                    .await;
            }

            let response = TrackRentalResponse {
                success: true,
                tracking_id: rental_id.to_string(),
            };

            Ok(response)
        }
        .await;

        if let Some(ref metrics) = self.metrics {
            if let Some(timer) = timer {
                let status = if result.is_ok() { "success" } else { "error" };
                metrics
                    .billing_metrics()
                    .record_grpc_request(timer, "track_rental", status)
                    .await;
            }
        }

        result.map(Response::new)
    }

    async fn update_rental_status(
        &self,
        request: Request<UpdateRentalStatusRequest>,
    ) -> std::result::Result<Response<UpdateRentalStatusResponse>, Status> {
        let req = request.into_inner();
        let rental_id = RentalId::from_str(&req.rental_id)
            .map_err(|e| Status::invalid_argument(format!("Invalid rental ID: {}", e)))?;
        let new_status = Self::rental_status_to_domain(req.status());

        info!("Updating rental {} status to {}", rental_id, new_status);

        let _rental = self
            .rental_manager
            .update_status(&rental_id, new_status)
            .await
            .map_err(|e| match e {
                BillingError::RentalNotFound { .. } => {
                    Status::not_found(format!("Rental not found: {}", e))
                }
                BillingError::InvalidStateTransition { .. } => {
                    Status::failed_precondition(format!("Invalid state transition: {}", e))
                }
                _ => Status::internal(format!("Failed to update rental: {}", e)),
            })?;

        let rental = self
            .rental_manager
            .get_rental(&rental_id)
            .await
            .map_err(|e| Status::internal(format!("Failed to get rental: {}", e)))?;
        self.rental_repository
            .update_rental(&rental)
            .await
            .map_err(|e| Status::internal(format!("Failed to persist status: {}", e)))?;

        let status_change_event = UsageEvent {
            event_id: uuid::Uuid::new_v4(),
            rental_id: rental_id.as_uuid(),
            user_id: rental.user_id.to_string(),
            node_id: rental.node_id.clone(),
            validator_id: rental.validator_id.clone(),
            event_type: EventType::StatusChange,
            event_data: serde_json::json!({
                "old_status": req.status().as_str_name(),
                "new_status": new_status.to_string(),
                "reason": if req.reason.is_empty() { None } else { Some(&req.reason) },
            }),
            timestamp: chrono::Utc::now(),
            processed: false,
            processed_at: None,
            batch_id: None,
        };
        self.event_store
            .append_usage_event(&status_change_event)
            .await
            .map_err(|e| Status::internal(format!("Failed to store status change event: {}", e)))?;

        let response = UpdateRentalStatusResponse {
            success: true,
            current_cost: Self::format_credit_balance(rental.cost_breakdown.total_cost),
            updated_at: Some(prost_types::Timestamp::from(std::time::SystemTime::now())),
        };

        Ok(Response::new(response))
    }

    async fn get_active_rentals(
        &self,
        request: Request<GetActiveRentalsRequest>,
    ) -> std::result::Result<Response<GetActiveRentalsResponse>, Status> {
        let req = request.into_inner();

        let rentals = if let Some(filter) = req.filter {
            match filter {
                basilica_protocol::billing::get_active_rentals_request::Filter::UserId(user_id) => {
                    let uid = UserId::new(user_id);
                    self.rental_repository
                        .get_active_rentals(Some(&uid))
                        .await
                        .map_err(|e| Status::internal(format!("Failed to list rentals: {}", e)))?
                }
                basilica_protocol::billing::get_active_rentals_request::Filter::NodeId(node_id) => {
                    let all_rentals = self
                        .rental_repository
                        .get_active_rentals(None)
                        .await
                        .map_err(|e| Status::internal(format!("Failed to list rentals: {}", e)))?;

                    all_rentals
                        .into_iter()
                        .filter(|r| r.node_id == node_id)
                        .collect()
                }
                basilica_protocol::billing::get_active_rentals_request::Filter::ValidatorId(
                    validator_id,
                ) => {
                    let all_rentals = self
                        .rental_repository
                        .get_active_rentals(None)
                        .await
                        .map_err(|e| Status::internal(format!("Failed to list rentals: {}", e)))?;

                    all_rentals
                        .into_iter()
                        .filter(|r| r.validator_id == validator_id)
                        .collect()
                }
            }
        } else {
            self.rental_repository
                .get_active_rentals(None)
                .await
                .map_err(|e| Status::internal(format!("Failed to list rentals: {}", e)))?
        };

        let active_rentals: Vec<ActiveRental> = rentals
            .into_iter()
            .filter(|r| r.state.is_active())
            .map(|r| {
                // Convert ResourceSpec to proto format
                let resource_spec = Some(basilica_protocol::billing::ResourceSpec {
                    cpu_cores: r.resource_spec.cpu_cores,
                    memory_mb: (r.resource_spec.memory_gb as u64) * 1024,
                    gpus: r
                        .resource_spec
                        .gpu_specs
                        .iter()
                        .map(|gpu| basilica_protocol::billing::GpuSpec {
                            model: gpu.model.clone(),
                            memory_mb: gpu.memory_mb,
                            count: gpu.count,
                        })
                        .collect(),
                    disk_gb: r.resource_spec.storage_gb as u64,
                    network_bandwidth_mbps: r.resource_spec.network_bandwidth_mbps,
                });

                ActiveRental {
                    rental_id: r.id.to_string(),
                    user_id: r.user_id.to_string(),
                    node_id: r.node_id.clone(),
                    validator_id: r.validator_id.clone(),
                    status: Self::domain_status_to_proto(r.state).into(),
                    resource_spec,
                    hourly_rate: Self::format_credit_balance(r.cost_breakdown.base_cost),
                    current_cost: Self::format_credit_balance(r.cost_breakdown.total_cost),
                    start_time: Some(prost_types::Timestamp::from(std::time::SystemTime::from(
                        r.created_at,
                    ))),
                    last_updated: Some(prost_types::Timestamp::from(std::time::SystemTime::from(
                        r.last_updated,
                    ))),
                    metadata: std::collections::HashMap::new(),
                }
            })
            .collect();

        let response = GetActiveRentalsResponse {
            rentals: active_rentals.clone(),
            total_count: active_rentals.len() as u64,
        };

        Ok(Response::new(response))
    }

    async fn finalize_rental(
        &self,
        request: Request<FinalizeRentalRequest>,
    ) -> std::result::Result<Response<FinalizeRentalResponse>, Status> {
        let timer = self
            .metrics
            .as_ref()
            .map(|m| m.billing_metrics().start_grpc_timer());

        let req = request.into_inner();

        let result = async {
            let rental_id = RentalId::from_str(&req.rental_id)
                .map_err(|e| Status::invalid_argument(format!("Invalid rental ID: {}", e)))?;

            let rental = self
                .rental_repository
                .get_rental(&rental_id)
                .await
                .map_err(|e| Status::internal(format!("Failed to get rental: {}", e)))?
                .ok_or_else(|| Status::not_found(format!("Rental {} not found", rental_id)))?;

            info!(
                "finalize_rental called for {} - telemetry already charged incrementally (actual_cost: {})",
                rental_id, rental.actual_cost
            );

            let final_cost_decimal = if req.final_cost.is_empty() {
                rental.actual_cost.as_decimal()
            } else {
                req.final_cost.parse::<rust_decimal::Decimal>()
                    .map_err(|e| Status::invalid_argument(format!("Invalid final_cost: {}", e)))?
            };

            let end_time = req.end_time
                .map(|ts| chrono::DateTime::<chrono::Utc>::from_timestamp(ts.seconds, ts.nanos as u32).unwrap())
                .unwrap_or_else(chrono::Utc::now);

            let rental_end_data = crate::domain::processor::RentalEndData {
                end_time,
                final_cost: final_cost_decimal,
                termination_reason: if req.termination_reason.is_empty() {
                    None
                } else {
                    Some(req.termination_reason.clone())
                },
            };

            // Update rental status to completed immediately
            let mut rental = rental.clone();
            rental.state = crate::domain::types::RentalState::Completed;
            rental.last_updated = end_time;

            self.rental_repository
                .update_rental(&rental)
                .await
                .map_err(|e| {
                    error!("Failed to update rental status to completed: {}", e);
                    Status::internal(format!("Failed to update rental status: {}", e))
                })?;

            info!("Updated rental {} state to completed", rental_id);

            // Publish rental_end event for audit purposes
            let usage_event = crate::storage::UsageEvent {
                event_id: uuid::Uuid::new_v4(),
                rental_id: rental.id.as_uuid(),
                user_id: rental.user_id.as_str().to_string(),
                node_id: rental.node_id.clone(),
                validator_id: rental.validator_id.clone(),
                event_type: crate::storage::EventType::RentalEnd,
                event_data: serde_json::to_value(&rental_end_data)
                    .map_err(|e| Status::internal(format!("Failed to serialize rental end data: {}", e)))?,
                timestamp: chrono::Utc::now(),
                processed: false,
                processed_at: None,
                batch_id: None,
            };

            self.event_store
                .append_usage_event(&usage_event)
                .await
                .map_err(|e| Status::internal(format!("Failed to append rental end event: {}", e)))?;

            info!("Published rental_end event for rental {} for audit purposes", rental_id);

            let duration = rental.last_updated - rental.created_at;

            if let Some(ref metrics) = self.metrics {
                metrics
                    .billing_metrics()
                    .record_rental_finalized(
                        &rental_id.to_string(),
                        rental.actual_cost.as_decimal().to_f64().unwrap_or(0.0),
                    )
                    .await;
            }

            let duration_proto = prost_types::Duration {
                seconds: duration.num_seconds(),
                nanos: (duration.num_nanoseconds().unwrap_or(0) % 1_000_000_000) as i32,
            };

            let response = FinalizeRentalResponse {
                success: true,
                total_cost: Self::format_credit_balance(rental.actual_cost),
                duration: Some(duration_proto),
                charged_amount: "0.00".to_string(),
                refunded_amount: "0.00".to_string(),
            };

            Ok(response)
        }
        .await;

        if let Some(ref metrics) = self.metrics {
            if let Some(timer) = timer {
                let status = if result.is_ok() { "success" } else { "error" };
                metrics
                    .billing_metrics()
                    .record_grpc_request(timer, "finalize_rental", status)
                    .await;
            }
        }

        result.map(Response::new)
    }

    async fn ingest_telemetry(
        &self,
        request: Request<tonic::Streaming<TelemetryData>>,
    ) -> std::result::Result<Response<IngestResponse>, Status> {
        let timer = self
            .metrics
            .as_ref()
            .map(|m| m.billing_metrics().start_grpc_timer());

        let mut stream = request.into_inner();
        let ingester = self.telemetry_ingester.clone();
        let metrics = self.metrics.clone();

        let mut events_received = 0u64;
        let mut events_processed = 0u64;
        let mut events_failed = 0u64;
        let mut last_processed = chrono::Utc::now();

        while let Some(result) = stream.next().await {
            match result {
                Ok(telemetry_data) => {
                    events_received += 1;

                    let rental_id = telemetry_data.rental_id.clone();

                    match ingester.ingest(telemetry_data).await {
                        Ok(_) => {
                            events_processed += 1;
                            last_processed = chrono::Utc::now();

                            if let Some(ref metrics) = metrics {
                                metrics
                                    .billing_metrics()
                                    .record_telemetry_received(&rental_id)
                                    .await;
                            }
                        }
                        Err(e) => {
                            error!("Failed to ingest telemetry: {}", e);
                            events_failed += 1;

                            if let Some(ref metrics) = metrics {
                                metrics
                                    .billing_metrics()
                                    .record_telemetry_dropped("ingestion_failed")
                                    .await;
                            }
                        }
                    }
                }
                Err(e) => {
                    error!("Error receiving telemetry: {}", e);
                    events_failed += 1;

                    if let Some(ref metrics) = metrics {
                        metrics
                            .billing_metrics()
                            .record_telemetry_dropped("stream_error")
                            .await;
                    }
                }
            }
        }

        let response = IngestResponse {
            events_received,
            events_processed,
            events_failed,
            last_processed: Some(prost_types::Timestamp::from(std::time::SystemTime::from(
                last_processed,
            ))),
        };

        if let Some(ref metrics) = self.metrics {
            if let Some(timer) = timer {
                let status = if events_failed == 0 {
                    "success"
                } else if events_processed > 0 {
                    "partial_failure"
                } else {
                    "failure"
                };
                metrics
                    .billing_metrics()
                    .record_grpc_request(timer, "ingest_telemetry", status)
                    .await;
            }
        }

        Ok(Response::new(response))
    }

    async fn get_usage_report(
        &self,
        request: Request<UsageReportRequest>,
    ) -> std::result::Result<Response<UsageReportResponse>, Status> {
        let req = request.into_inner();
        let rental_id = RentalId::from_str(&req.rental_id)
            .map_err(|e| Status::invalid_argument(format!("Invalid rental ID: {}", e)))?;

        let rental = self
            .rental_repository
            .get_rental(&rental_id)
            .await
            .map_err(|e| Status::internal(format!("Failed to get rental: {}", e)))?
            .ok_or_else(|| Status::not_found("Rental not found"))?;

        let duration = rental.last_updated - rental.created_at;
        let _duration_hours =
            duration.num_hours() as f64 + (duration.num_minutes() % 60) as f64 / 60.0;

        let events = self
            .event_store
            .get_rental_events(
                uuid::Uuid::parse_str(&rental_id.to_string())
                    .map_err(|_| Status::internal("Invalid rental ID format"))?,
                None,
                None,
            )
            .await
            .map_err(|e| Status::internal(format!("Failed to get telemetry events: {}", e)))?;

        let mut total_cpu_percent = 0.0;
        let mut total_memory_mb = 0u64;
        let mut total_network_bytes = 0u64;
        let mut total_disk_bytes = 0u64;
        let mut total_gpu_percent = 0.0;
        let mut telemetry_count = 0u64;

        let mut data_points = Vec::new();

        for event in &events {
            if let Some(cpu_percent) = event.event_data.get("cpu_percent").and_then(|v| v.as_f64())
            {
                telemetry_count += 1;

                total_cpu_percent += cpu_percent * 100.0; // Convert from hours to percent

                if let Some(memory_gb) = event.event_data.get("memory_gb").and_then(|v| v.as_f64())
                {
                    total_memory_mb += (memory_gb * 1024.0) as u64;
                }

                if let Some(network_gb) =
                    event.event_data.get("network_gb").and_then(|v| v.as_f64())
                {
                    total_network_bytes += (network_gb * 1_073_741_824.0) as u64;
                }

                if let Some(gpu_hours) = event.event_data.get("gpu_hours").and_then(|v| v.as_f64())
                {
                    total_gpu_percent += gpu_hours * 100.0; // Convert from hours to percent
                }

                // For disk I/O, check if it exists in the data
                if let Some(disk_gb) = event.event_data.get("disk_io_gb").and_then(|v| v.as_f64()) {
                    total_disk_bytes += (disk_gb * 1_073_741_824.0) as u64;
                }

                data_points.push(UsageDataPoint {
                    timestamp: Some(prost_types::Timestamp::from(std::time::SystemTime::from(
                        event.timestamp,
                    ))),
                    usage: Some(basilica_protocol::billing::ResourceUsage {
                        cpu_percent: cpu_percent * 100.0,
                        memory_mb: (event
                            .event_data
                            .get("memory_gb")
                            .and_then(|v| v.as_f64())
                            .unwrap_or(0.0)
                            * 1024.0) as u64,
                        network_rx_bytes: 0,
                        network_tx_bytes: (event
                            .event_data
                            .get("network_gb")
                            .and_then(|v| v.as_f64())
                            .unwrap_or(0.0)
                            * 1_073_741_824.0) as u64,
                        disk_read_bytes: 0,
                        disk_write_bytes: (event
                            .event_data
                            .get("disk_io_gb")
                            .and_then(|v| v.as_f64())
                            .unwrap_or(0.0)
                            * 1_073_741_824.0) as u64,
                        gpu_usage: vec![],
                    }),
                    // Cost calculation would be done per interval
                    cost: "0".to_string(),
                });
            }
        }

        let duration_proto = prost_types::Duration {
            seconds: duration.num_seconds(),
            nanos: (duration.num_nanoseconds().unwrap_or(0) % 1_000_000_000) as i32,
        };

        let summary = UsageSummary {
            avg_cpu_percent: if telemetry_count > 0 {
                total_cpu_percent / telemetry_count as f64
            } else {
                0.0
            },
            avg_memory_mb: if telemetry_count > 0 {
                total_memory_mb / telemetry_count
            } else {
                0
            },
            total_network_bytes,
            total_disk_bytes,
            avg_gpu_utilization: if telemetry_count > 0 {
                total_gpu_percent / telemetry_count as f64
            } else {
                0.0
            },
            duration: Some(duration_proto),
        };

        if data_points.is_empty() {
            data_points.push(UsageDataPoint {
                timestamp: Some(prost_types::Timestamp::from(std::time::SystemTime::from(
                    rental.created_at,
                ))),
                usage: None,
                cost: Self::format_credit_balance(rental.cost_breakdown.base_cost),
            });
        }

        let response = UsageReportResponse {
            rental_id: rental_id.to_string(),
            data_points,
            summary: Some(summary),
            total_cost: Self::format_credit_balance(rental.cost_breakdown.total_cost),
        };

        Ok(Response::new(response))
    }

    async fn get_billing_packages(
        &self,
        request: Request<GetBillingPackagesRequest>,
    ) -> std::result::Result<Response<GetBillingPackagesResponse>, Status> {
        let req = request.into_inner();
        let user_id = UserId::new(req.user_id);

        let packages = self
            .package_repository
            .list_packages()
            .await
            .map_err(|e| Status::internal(format!("Failed to list packages: {}", e)))?;

        let billing_packages = packages.into_iter().map(|p| p.to_proto()).collect();

        // Get the user's current package preference (empty string if none set)
        // Package assignment happens automatically based on GPU model when creating rentals
        let current_package_id = match self
            .user_preferences_repository
            .get_user_package(&user_id)
            .await
        {
            Ok(Some(pref)) => pref.package_id.to_string(),
            Ok(None) | Err(_) => String::new(), // No preference set
        };

        let response = GetBillingPackagesResponse {
            packages: billing_packages,
            current_package_id,
        };

        Ok(Response::new(response))
    }

    async fn set_user_package(
        &self,
        request: Request<SetUserPackageRequest>,
    ) -> std::result::Result<Response<SetUserPackageResponse>, Status> {
        let req = request.into_inner();
        let user_id = UserId::new(req.user_id);
        let new_package_id = PackageId::new(req.package_id.clone());

        info!("Setting package {} for user {}", new_package_id, user_id);

        let _package = self
            .package_repository
            .get_package(&new_package_id)
            .await
            .map_err(|e| Status::internal(format!("Failed to get package: {}", e)))?;

        let effective_from = req.effective_from.as_ref().map(|timestamp| {
            chrono::DateTime::from_timestamp(timestamp.seconds, timestamp.nanos as u32)
                .unwrap_or_else(chrono::Utc::now)
        });

        let previous_package_id = self
            .user_preferences_repository
            .set_user_package(&user_id, &new_package_id, effective_from)
            .await
            .map_err(|e| Status::internal(format!("Failed to update user package: {}", e)))?;

        let response = SetUserPackageResponse {
            success: true,
            previous_package_id: previous_package_id
                .unwrap_or_else(PackageId::standard)
                .to_string(),
            new_package_id: new_package_id.to_string(),
            effective_from: req.effective_from,
        };

        Ok(Response::new(response))
    }

    async fn sync_prices(
        &self,
        request: Request<SyncPricesRequest>,
    ) -> std::result::Result<Response<SyncPricesResponse>, Status> {
        let req = request.into_inner();

        info!("Manual price sync requested (force={})", req.force_sync);

        // Check if pricing service is available
        let pricing_service = self.pricing_service.as_ref().ok_or_else(|| {
            Status::failed_precondition("Dynamic pricing is not enabled on this server")
        })?;

        let sync_started_at = chrono::Utc::now();

        // Perform the sync
        let prices_synced = pricing_service
            .sync_prices()
            .await
            .map_err(|e| Status::internal(format!("Failed to sync prices: {}", e)))?;

        let sync_completed_at = chrono::Utc::now();

        // Calculate next scheduled sync based on update_interval_seconds
        let next_scheduled_sync = if let Some(ref config) = self.pricing_config {
            sync_completed_at + chrono::Duration::seconds(config.update_interval_seconds as i64)
        } else {
            // Default to 24 hours if config not available
            sync_completed_at + chrono::Duration::hours(24)
        };

        let response = SyncPricesResponse {
            success: true,
            prices_synced: prices_synced as u32,
            sync_started_at: Some(prost_types::Timestamp {
                seconds: sync_started_at.timestamp(),
                nanos: sync_started_at.timestamp_subsec_nanos() as i32,
            }),
            sync_completed_at: Some(prost_types::Timestamp {
                seconds: sync_completed_at.timestamp(),
                nanos: sync_completed_at.timestamp_subsec_nanos() as i32,
            }),
            next_scheduled_sync: Some(prost_types::Timestamp {
                seconds: next_scheduled_sync.timestamp(),
                nanos: next_scheduled_sync.timestamp_subsec_nanos() as i32,
            }),
            error_message: String::new(),
        };

        info!("Price sync completed: {} prices synced", prices_synced);
        Ok(Response::new(response))
    }

    async fn get_cached_prices(
        &self,
        request: Request<GetCachedPricesRequest>,
    ) -> std::result::Result<Response<GetCachedPricesResponse>, Status> {
        let req = request.into_inner();

        info!("Get cached prices requested");

        // Get all cached prices
        let cached_prices = self
            .price_cache
            .get_all()
            .await
            .map_err(|e| Status::internal(format!("Failed to get cached prices: {}", e)))?;

        // Filter by GPU models if specified
        let filtered_prices: Vec<_> = if req.gpu_models.is_empty() {
            cached_prices
        } else {
            cached_prices
                .into_iter()
                .filter(|p| req.gpu_models.contains(&p.gpu_model))
                .collect()
        };

        // Filter by providers if specified (use source field for provider filtering)
        let filtered_prices: Vec<_> = if req.providers.is_empty() {
            filtered_prices
        } else {
            filtered_prices
                .into_iter()
                .filter(|p| req.providers.contains(&p.source))
                .collect()
        };

        // Convert to proto format
        let prices = filtered_prices
            .into_iter()
            .map(|p| basilica_protocol::billing::GpuPrice {
                gpu_model: p.gpu_model.clone(),
                vram_gb: p.vram_gb.unwrap_or(0),
                market_price_per_hour: Self::format_decimal(p.market_price_per_hour),
                discounted_price_per_hour: Self::format_decimal(p.discounted_price_per_hour),
                discount_percent: Self::format_decimal(p.discount_percent),
                source: p.source.clone(),
                provider: p.source, // Use source as provider for backward compatibility
                location: String::new(), // No longer stored
                instance_name: String::new(), // No longer stored
                updated_at: Some(prost_types::Timestamp {
                    seconds: p.updated_at.timestamp(),
                    nanos: p.updated_at.timestamp_subsec_nanos() as i32,
                }),
                expires_at: {
                    let expires = p.updated_at + chrono::Duration::seconds(86400);
                    Some(prost_types::Timestamp {
                        seconds: expires.timestamp(),
                        nanos: expires.timestamp_subsec_nanos() as i32,
                    })
                },
                is_spot: p.is_spot,
            })
            .collect();

        let response = GetCachedPricesResponse {
            prices,
            cached_at: Some(prost_types::Timestamp {
                seconds: chrono::Utc::now().timestamp(),
                nanos: chrono::Utc::now().timestamp_subsec_nanos() as i32,
            }),
        };

        info!("Returned {} cached prices", response.prices.len());
        Ok(Response::new(response))
    }

    async fn get_price_history(
        &self,
        request: Request<GetPriceHistoryRequest>,
    ) -> std::result::Result<Response<GetPriceHistoryResponse>, Status> {
        use crate::storage::PriceHistoryFilter;

        let req = request.into_inner();

        if req.gpu_model.is_empty() {
            return Err(Status::invalid_argument("gpu_model is required"));
        }

        info!("Get price history requested for GPU: {}", req.gpu_model);

        // Build filter from request
        let filter = PriceHistoryFilter {
            gpu_model: req.gpu_model.clone(),
            start_time: req
                .start_time
                .and_then(|ts| chrono::DateTime::from_timestamp(ts.seconds, ts.nanos as u32)),
            end_time: req
                .end_time
                .and_then(|ts| chrono::DateTime::from_timestamp(ts.seconds, ts.nanos as u32)),
            providers: req.providers.clone(),
            limit: req.limit,
        };

        // Query repository
        let history_entries = self
            .price_cache
            .get_price_history(filter)
            .await
            .map_err(|e| Status::internal(format!("Failed to fetch price history: {}", e)))?;

        // Convert to protocol format
        let entries = history_entries
            .into_iter()
            .map(|entry| basilica_protocol::billing::PriceHistoryEntry {
                gpu_model: entry.gpu_model,
                price_per_hour: Self::format_decimal(entry.price_per_hour),
                source: entry.source,
                provider: entry.provider,
                recorded_at: Some(prost_types::Timestamp {
                    seconds: entry.recorded_at.timestamp(),
                    nanos: entry.recorded_at.timestamp_subsec_nanos() as i32,
                }),
            })
            .collect::<Vec<_>>();

        let total_count = entries.len() as u64;

        let response = GetPriceHistoryResponse {
            gpu_model: req.gpu_model,
            entries,
            total_count,
        };

        info!("Returned {} price history entries", total_count);
        Ok(Response::new(response))
    }
}
