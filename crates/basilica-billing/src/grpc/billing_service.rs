use crate::domain::events::EventStore;
use crate::domain::idempotency::generate_idempotency_key;
use crate::domain::{
    credits::{CreditManager, CreditOperations},
    rentals::{RentalManager, RentalOperations},
    types::{CreditBalance, GpuSpec, RentalId, RentalState, ResourceSpec, UserId},
};
use crate::error::BillingError;
use crate::metrics::BillingMetricsSystem;
use crate::storage::events::{EventType, UsageEvent};
use crate::storage::rds::RdsConnection;
use crate::storage::{RentalRepository, SqlCreditRepository, SqlRentalRepository};
use crate::telemetry::{TelemetryIngester, TelemetryProcessor};

use basilica_protocol::billing::{
    billing_service_server::BillingService, ActiveRental, ApplyCreditsRequest,
    ApplyCreditsResponse, FinalizeRentalRequest, FinalizeRentalResponse, GetActiveRentalsRequest,
    GetActiveRentalsResponse, GetBalanceRequest, GetBalanceResponse, IngestResponse, RentalStatus,
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
use tracing::{error, info};
use uuid;

pub struct BillingServiceImpl {
    credit_manager: Arc<dyn CreditOperations + Send + Sync>,
    rental_manager: Arc<dyn RentalOperations + Send + Sync>,
    #[allow(dead_code)] // Used in server's consumer loop
    telemetry_processor: Arc<TelemetryProcessor>,
    telemetry_ingester: Arc<TelemetryIngester>,
    rental_repository: Arc<dyn RentalRepository + Send + Sync>,
    event_store: Arc<EventStore>,
    metrics: Option<Arc<BillingMetricsSystem>>,
}

impl BillingServiceImpl {
    pub async fn new(
        rds_connection: Arc<RdsConnection>,
        telemetry_ingester: Arc<TelemetryIngester>,
        telemetry_processor: Arc<TelemetryProcessor>,
        metrics: Option<Arc<BillingMetricsSystem>>,
    ) -> anyhow::Result<Self> {
        let audit_repository = Arc::new(crate::storage::SqlAuditRepository::new(
            rds_connection.clone(),
        ));
        let credit_repository = Arc::new(SqlCreditRepository::new(
            rds_connection.clone(),
            audit_repository,
        ));
        let rental_repository = Arc::new(SqlRentalRepository::new(rds_connection.clone()));

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
            event_store,
            metrics,
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

            // Extract cloud type specific data from oneof
            let cloud_type = req.cloud_type.ok_or_else(|| {
                Status::invalid_argument("cloud_type is required (community or secure)")
            })?;

            let resource_spec_value =
                serde_json::to_value(&resource_spec).unwrap_or(serde_json::Value::Null);

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

            use basilica_protocol::billing::track_rental_request::CloudType;
            use crate::domain::rentals::{Rental, SecureCloudRental};

            match cloud_type {
                CloudType::Community(community_data) => {
                    let base_price_per_gpu = rust_decimal::Decimal::from_f64(community_data.base_price_per_gpu)
                        .ok_or_else(|| Status::invalid_argument("Invalid base_price_per_gpu"))?;
                    if community_data.gpu_count == 0 {
                        return Err(Status::invalid_argument("gpu_count must be greater than 0"));
                    }
                    let gpu_count = community_data.gpu_count;

                    info!(
                        "Tracking community rental {} for user {} at ${}/GPU/hour × {} GPUs",
                        rental_id, user_id, base_price_per_gpu, gpu_count
                    );

                    let mut rental = Rental::new_marketplace(
                        user_id.clone(),
                        community_data.node_id.clone(),
                        community_data.validator_id.clone(),
                        resource_spec.clone(),
                        base_price_per_gpu,
                        gpu_count,
                    );
                    rental.id = rental_id;

                    self.rental_repository
                        .create_rental(&rental)
                        .await
                        .map_err(|e| Status::internal(format!("Failed to create community rental: {}", e)))?;

                    let event_data = serde_json::json!({
                        "cloud_type": "community",
                        "node_id": community_data.node_id,
                        "validator_id": community_data.validator_id,
                        "base_price_per_gpu": base_price_per_gpu.to_string(),
                        "gpu_count": gpu_count,
                        "resource_spec": resource_spec_value,
                        "timestamp": chrono::Utc::now().timestamp_millis().to_string(),
                    });

                    let idempotency_key =
                        generate_idempotency_key(rental_id.as_uuid(), &event_data);

                    let rental_start_event = UsageEvent {
                        event_id: uuid::Uuid::new_v4(),
                        rental_id: rental_id.as_uuid(),
                        user_id: user_id.to_string(),
                        node_id: community_data.node_id,
                        validator_id: community_data.validator_id,
                        event_type: EventType::RentalStart,
                        event_data,
                        timestamp: chrono::Utc::now(),
                        processed: false,
                        processed_at: None,
                        batch_id: None,
                        idempotency_key: Some(idempotency_key),
                    };

                    self.event_store
                        .append_usage_event(&rental_start_event)
                        .await
                        .map_err(|e| {
                            Status::internal(format!("Failed to store community rental start event: {}", e))
                        })?;
                }
                CloudType::Secure(secure_data) => {
                    let base_price_per_gpu = rust_decimal::Decimal::from_f64(secure_data.base_price_per_gpu)
                        .ok_or_else(|| Status::invalid_argument("Invalid base_price_per_gpu"))?;
                    if secure_data.gpu_count == 0 {
                        return Err(Status::invalid_argument("gpu_count must be greater than 0"));
                    }
                    let gpu_count = secure_data.gpu_count;

                    info!(
                        "Tracking secure cloud rental {} for user {} (provider: {}) at ${}/GPU/hour × {} GPUs",
                        rental_id, user_id, secure_data.provider, base_price_per_gpu, gpu_count
                    );

                    let mut rental = SecureCloudRental::new_marketplace(
                        user_id.clone(),
                        secure_data.provider.clone(),
                        secure_data.provider_instance_id.clone(),
                        secure_data.offering_id.clone(),
                        resource_spec.clone(),
                        base_price_per_gpu,
                        gpu_count,
                    );
                    rental.id = rental_id;

                    self.rental_repository
                        .create_secure_cloud_rental(&rental)
                        .await
                        .map_err(|e| Status::internal(format!("Failed to create secure cloud rental: {}", e)))?;

                    let event_data = serde_json::json!({
                        "cloud_type": "secure",
                        "provider": secure_data.provider,
                        "provider_instance_id": secure_data.provider_instance_id,
                        "offering_id": secure_data.offering_id,
                        "base_price_per_gpu": base_price_per_gpu.to_string(),
                        "gpu_count": gpu_count,
                        "resource_spec": resource_spec_value,
                        "timestamp": chrono::Utc::now().timestamp_millis().to_string(),
                    });

                    let idempotency_key =
                        generate_idempotency_key(rental_id.as_uuid(), &event_data);

                    let rental_start_event = UsageEvent {
                        event_id: uuid::Uuid::new_v4(),
                        rental_id: rental_id.as_uuid(),
                        user_id: user_id.to_string(),
                        node_id: secure_data.provider_instance_id,
                        validator_id: format!("secure_cloud:{}", secure_data.provider),
                        event_type: EventType::RentalStart,
                        event_data,
                        timestamp: chrono::Utc::now(),
                        processed: false,
                        processed_at: None,
                        batch_id: None,
                        idempotency_key: Some(idempotency_key),
                    };

                    self.event_store
                        .append_usage_event(&rental_start_event)
                        .await
                        .map_err(|e| {
                            Status::internal(format!("Failed to store secure cloud rental start event: {}", e))
                        })?;
                }
            }

            if let Some(ref metrics) = self.metrics {
                metrics
                    .billing_metrics()
                    .record_rental_tracked(&rental_id.to_string())
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

        let event_data = serde_json::json!({
            "old_status": req.status().as_str_name(),
            "new_status": new_status.to_string(),
            "reason": if req.reason.is_empty() { None } else { Some(&req.reason) },
            "timestamp": chrono::Utc::now().timestamp_millis().to_string(),
        });

        let idempotency_key = generate_idempotency_key(rental_id.as_uuid(), &event_data);

        let status_change_event = UsageEvent {
            event_id: uuid::Uuid::new_v4(),
            rental_id: rental_id.as_uuid(),
            user_id: rental.user_id.to_string(),
            node_id: rental.node_id.clone(),
            validator_id: rental.validator_id.clone(),
            event_type: EventType::StatusChange,
            event_data,
            timestamp: chrono::Utc::now(),
            processed: false,
            processed_at: None,
            batch_id: None,
            idempotency_key: Some(idempotency_key),
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
                    status: Self::domain_status_to_proto(r.state).into(),
                    resource_spec,
                    current_cost: Self::format_credit_balance(r.cost_breakdown.total_cost),
                    start_time: Some(prost_types::Timestamp::from(std::time::SystemTime::from(
                        r.created_at,
                    ))),
                    last_updated: Some(prost_types::Timestamp::from(std::time::SystemTime::from(
                        r.last_updated,
                    ))),
                    metadata: std::collections::HashMap::new(),
                    cloud_type: Some(
                        basilica_protocol::billing::active_rental::CloudType::Community(
                            basilica_protocol::billing::CommunityCloudData {
                                node_id: r.node_id.clone(),
                                validator_id: r.validator_id.clone(),
                                base_price_per_gpu: r.base_price_per_gpu.to_f64().unwrap_or(0.0),
                                gpu_count: r.gpu_count,
                            },
                        ),
                    ),
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
            let mut event_data = serde_json::to_value(&rental_end_data)
                .map_err(|e| Status::internal(format!("Failed to serialize rental end data: {}", e)))?;

            if let serde_json::Value::Object(ref mut map) = event_data {
                map.insert("timestamp".to_string(), serde_json::Value::String(chrono::Utc::now().timestamp_millis().to_string()));
            }

            let idempotency_key = generate_idempotency_key(rental.id.as_uuid(), &event_data);

            let usage_event = crate::storage::UsageEvent {
                event_id: uuid::Uuid::new_v4(),
                rental_id: rental.id.as_uuid(),
                user_id: rental.user_id.as_str().to_string(),
                node_id: rental.node_id.clone(),
                validator_id: rental.validator_id.clone(),
                event_type: crate::storage::EventType::RentalEnd,
                event_data,
                timestamp: chrono::Utc::now(),
                processed: false,
                processed_at: None,
                batch_id: None,
                idempotency_key: Some(idempotency_key),
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
}
