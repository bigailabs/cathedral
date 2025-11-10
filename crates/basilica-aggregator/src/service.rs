use crate::config::{AuthConfig, Config};
use crate::db::Database;
use crate::error::{AggregatorError, Result};
use crate::models::{
    Deployment, DeploymentStatus, GpuOffering, Provider as ProviderEnum, ProviderHealth,
};
use crate::providers::datacrunch::{DataCrunchProvider, Instance, OsImage};
use crate::providers::hyperstack::HyperstackProvider;
use crate::providers::{DeployRequest, Provider, ProviderClient};
use basilica_common::types::GpuCategory;
use chrono::{Duration, Utc};
use std::sync::Arc;
use uuid::Uuid;

pub struct AggregatorService {
    db: Arc<Database>,
    providers: Vec<ProviderClient>,
    config: Config,
}

impl AggregatorService {
    pub fn new(db: Arc<Database>, config: Config) -> Result<Self> {
        let mut providers = Vec::new();

        // Initialize DataCrunch provider (optional)
        if config.providers.datacrunch.is_enabled() {
            if let Some(auth) = config.providers.datacrunch.get_auth() {
                let (client_id, client_secret) = match auth {
                    AuthConfig::OAuth {
                        client_id,
                        client_secret,
                    } => (client_id, client_secret),
                    AuthConfig::ApiKey { .. } => {
                        return Err(AggregatorError::Config(
                            "DataCrunch requires OAuth authentication".into(),
                        ))
                    }
                };

                let base_url = config
                    .providers
                    .datacrunch
                    .api_base_url
                    .clone()
                    .ok_or_else(|| AggregatorError::Config("DataCrunch base URL missing".into()))?;

                let provider = DataCrunchProvider::new(
                    client_id,
                    client_secret,
                    base_url,
                    config.providers.datacrunch.timeout_seconds,
                )?;

                providers.push(ProviderClient::DataCrunch(provider));
                tracing::info!("DataCrunch provider initialized");
            }
        }

        // Initialize Hyperstack provider (optional)
        if config.providers.hyperstack.is_enabled() {
            if let Some(auth) = config.providers.hyperstack.get_auth() {
                let api_key = match auth {
                    AuthConfig::ApiKey { api_key } => api_key,
                    AuthConfig::OAuth { .. } => {
                        return Err(AggregatorError::Config(
                            "Hyperstack requires ApiKey authentication".into(),
                        ))
                    }
                };

                let base_url = config
                    .providers
                    .hyperstack
                    .api_base_url
                    .clone()
                    .ok_or_else(|| AggregatorError::Config("Hyperstack base URL missing".into()))?;

                let provider = HyperstackProvider::new(
                    api_key,
                    base_url,
                    config.providers.hyperstack.timeout_seconds,
                )?;

                providers.push(ProviderClient::Hyperstack(provider));
                tracing::info!("Hyperstack provider initialized");
            }
        }

        if providers.is_empty() {
            tracing::warn!("No GPU providers enabled - secure cloud will not function");
        } else {
            tracing::info!("Initialized {} GPU provider(s)", providers.len());
        }

        Ok(Self {
            db,
            providers,
            config,
        })
    }

    /// Get GPU offerings from database cache
    /// Note: Background task keeps cache fresh, so this just reads from DB
    /// Cache only contains supported GPU types (A100, H100, B200)
    pub async fn get_offerings(&self) -> Result<Vec<GpuOffering>> {
        let offerings = self.db.get_offerings(None).await?;

        tracing::debug!("Retrieved {} offerings from cache", offerings.len());
        Ok(offerings)
    }

    /// Refresh offerings from all providers (called by background task)
    /// Returns total number of offerings fetched
    pub async fn refresh_all_providers(&self) -> Result<usize> {
        let mut total_count = 0;

        // Iterate over all enabled providers
        for provider in &self.providers {
            let provider_id = provider.provider_id();

            if self.should_fetch(provider_id).await? {
                match self.fetch_and_cache(provider).await {
                    Ok(offerings) => {
                        tracing::info!(
                            "Refreshed {} offerings from {}",
                            offerings.len(),
                            provider_id
                        );
                        total_count += offerings.len();
                    }
                    Err(e) => {
                        tracing::error!("Failed to refresh from {}: {}", provider_id, e);
                        // Provider status is not persisted - health checks done on-demand
                    }
                }
            } else {
                tracing::debug!("Skipping {} - cooldown period not elapsed", provider_id);
            }
        }

        Ok(total_count)
    }

    /// Check if we should fetch fresh data
    async fn should_fetch(&self, provider: ProviderEnum) -> Result<bool> {
        let last_fetch = self.db.get_last_fetch_time(provider).await?;

        if let Some(last_fetch) = last_fetch {
            let cooldown = match provider {
                ProviderEnum::DataCrunch => self.config.providers.datacrunch.cooldown_seconds,
                ProviderEnum::Hyperstack => self.config.providers.hyperstack.cooldown_seconds,
                ProviderEnum::Lambda => self.config.providers.lambda.cooldown_seconds,
                ProviderEnum::HydraHost => self.config.providers.hydrahost.cooldown_seconds,
            };

            let cooldown_duration = Duration::seconds(cooldown as i64);
            let elapsed = Utc::now() - last_fetch;

            Ok(elapsed >= cooldown_duration)
        } else {
            // Never fetched before
            Ok(true)
        }
    }

    /// Fetch from provider and cache results
    /// Only caches supported GPU types (A100, H100, B200) - filters out Other
    async fn fetch_and_cache(&self, provider: &dyn Provider) -> Result<Vec<GpuOffering>> {
        let provider_id = provider.provider_id();

        let all_offerings = provider.fetch_offerings().await?;
        let total_count = all_offerings.len();

        // Filter to only supported GPU types before caching
        let supported_offerings: Vec<GpuOffering> = all_offerings
            .into_iter()
            .filter(|o| !matches!(o.gpu_type, GpuCategory::Other(_)))
            .collect();

        tracing::debug!(
            "Filtered {} to {} supported offerings for {}",
            total_count,
            supported_offerings.len(),
            provider_id
        );

        // Store only supported offerings in database
        self.db.upsert_offerings(&supported_offerings).await?;

        // Provider status is not persisted - health checks done on-demand

        Ok(supported_offerings)
    }

    /// Get health status for all providers
    pub async fn get_provider_health(&self) -> Result<Vec<ProviderHealth>> {
        let mut health_statuses = Vec::new();

        // Get health for all enabled providers
        for provider in &self.providers {
            let provider_id = provider.provider_id();
            let health = self.db.get_provider_health(provider_id).await?;
            health_statuses.push(health);
        }

        Ok(health_statuses)
    }

    /// Check if data is stale based on TTL
    pub fn is_stale(&self, offerings: &[GpuOffering]) -> bool {
        if offerings.is_empty() {
            return true;
        }

        let ttl = Duration::seconds(self.config.cache.ttl_seconds as i64);
        let oldest = offerings
            .iter()
            .map(|o| o.fetched_at)
            .min()
            .unwrap_or_else(Utc::now);

        let elapsed = Utc::now() - oldest;
        elapsed >= ttl
    }

    // ========================================================================
    // Provider Access
    // ========================================================================

    /// Get provider by enum
    fn get_provider(&self, provider_enum: ProviderEnum) -> Result<&ProviderClient> {
        self.providers
            .iter()
            .find(|p| p.provider_id() == provider_enum)
            .ok_or_else(|| AggregatorError::Provider {
                provider: provider_enum.as_str().to_string(),
                message: "Provider not enabled".to_string(),
            })
    }

    /// Get DataCrunch provider (for legacy APIs that need provider-specific methods)
    fn get_datacrunch_provider(&self) -> Result<&DataCrunchProvider> {
        self.providers
            .iter()
            .find_map(|p| match p {
                ProviderClient::DataCrunch(dc) => Some(dc),
                _ => None,
            })
            .ok_or_else(|| AggregatorError::Provider {
                provider: "DataCrunch".to_string(),
                message: "DataCrunch provider not enabled".to_string(),
            })
    }

    /// List available OS images from DataCrunch
    pub async fn list_images(&self) -> Result<Vec<OsImage>> {
        let provider = self.get_datacrunch_provider()?;
        provider.list_images().await
    }

    /// Deploy a new GPU instance (supports DataCrunch and Hyperstack)
    pub async fn deploy_instance(
        &self,
        offering_id: String,
        ssh_public_key: String,
        ssh_key_name: Option<String>,
        location_code: Option<String>,
    ) -> Result<Deployment> {
        // Get the offering to determine provider and instance type
        let offerings = self.db.get_offerings(None).await?;
        let offering = offerings
            .iter()
            .find(|o| o.id == offering_id)
            .ok_or_else(|| {
                AggregatorError::NotFound(format!("Offering not found: {}", offering_id))
            })?;

        let provider_enum = offering.provider;

        // Extract instance type from raw metadata
        let instance_type = offering
            .raw_metadata
            .get("instance_type")
            .and_then(|v| v.as_str())
            .or_else(|| offering.raw_metadata.get("name").and_then(|v| v.as_str()))
            .ok_or_else(|| AggregatorError::Provider {
                provider: provider_enum.as_str().to_string(),
                message: "Missing instance_type/name in offering metadata".to_string(),
            })?
            .to_string();

        // Generate deployment ID and hostname
        let deployment_id = Uuid::new_v4().to_string();
        let hostname = format!("basilica-{}", &deployment_id[..8]);
        let key_name = ssh_key_name.unwrap_or_else(|| format!("basilica-{}", &deployment_id[..8]));

        // Get provider and deploy using unified interface
        let provider = self.get_provider(provider_enum)?;

        let deploy_request = DeployRequest {
            instance_type: instance_type.clone(),
            hostname: hostname.clone(),
            ssh_key_name: key_name.clone(),
            ssh_public_key,
            location_code: location_code.clone(),
            image_name: None,       // Use provider defaults
            environment_name: None, // Use provider defaults
        };

        let provider_deployment = provider.deploy(deploy_request).await?;

        // Create deployment record in database
        let now = Utc::now();
        let deployment = Deployment {
            id: deployment_id,
            user_id: "legacy".to_string(),
            provider: provider_enum,
            provider_instance_id: Some(provider_deployment.id.clone()),
            offering_id: offering.id.clone(),
            instance_type,
            location_code,
            status: self
                .map_provider_status_to_deployment(&provider_deployment.status, provider_enum),
            hostname,
            ssh_key_id: Some(provider_deployment.ssh_key_id),
            ip_address: provider_deployment.ip_address,
            connection_info: provider_deployment.raw_data.clone(),
            raw_response: provider_deployment.raw_data,
            error_message: None,
            created_at: now,
            updated_at: now,
        };

        self.db.create_deployment(&deployment).await?;
        Ok(deployment)
    }

    /// Map provider-specific status strings to deployment status
    fn map_provider_status_to_deployment(
        &self,
        status: &str,
        provider: ProviderEnum,
    ) -> DeploymentStatus {
        match provider {
            ProviderEnum::DataCrunch => {
                // Status from DataCrunch Instance will be in Debug format (e.g., "Running", "Provisioning")
                match status {
                    s if s.contains("Running") => DeploymentStatus::Running,
                    s if s.contains("Provisioning")
                        || s.contains("Ordered")
                        || s.contains("New")
                        || s.contains("Validating") =>
                    {
                        DeploymentStatus::Provisioning
                    }
                    s if s.contains("Error")
                        || s.contains("NoCapacity")
                        || s.contains("NotFound") =>
                    {
                        DeploymentStatus::Error
                    }
                    s if s.contains("Deleting") || s.contains("Discontinued") => {
                        DeploymentStatus::Deleted
                    }
                    _ => DeploymentStatus::Pending,
                }
            }
            ProviderEnum::Hyperstack => {
                // Hyperstack status strings are UPPERCASE (e.g., "ACTIVE", "BUILDING")
                match status.to_uppercase().as_str() {
                    "ACTIVE" => DeploymentStatus::Running,
                    "BUILDING" | "MIGRATING" | "REBUILD" | "RESIZE" | "VERIFY_RESIZE"
                    | "REVERT_RESIZE" => DeploymentStatus::Provisioning,
                    "ERROR" => DeploymentStatus::Error,
                    "SHUTOFF" | "SOFT_DELETED" | "SHELVED_OFFLOADED" => DeploymentStatus::Deleted,
                    _ => DeploymentStatus::Pending,
                }
            }
            _ => DeploymentStatus::Pending,
        }
    }

    /// Get deployment status and update database
    pub async fn get_deployment(&self, deployment_id: &str) -> Result<Deployment> {
        // Get deployment from database
        let mut deployment = self
            .db
            .get_deployment(deployment_id)
            .await?
            .ok_or_else(|| {
                AggregatorError::NotFound(format!("Deployment not found: {}", deployment_id))
            })?;

        // If we have a provider instance ID, fetch latest status
        if let Some(provider_instance_id) = &deployment.provider_instance_id {
            let provider = self.get_provider(deployment.provider)?;

            match provider.get_deployment(provider_instance_id).await {
                Ok(provider_deployment) => {
                    let status = self.map_provider_status_to_deployment(
                        &provider_deployment.status,
                        deployment.provider,
                    );

                    self.db
                        .update_deployment(
                            deployment_id,
                            Some(provider_instance_id.clone()),
                            status.clone(),
                            provider_deployment.ip_address.clone(),
                            provider_deployment.raw_data.clone(),
                            provider_deployment.raw_data.clone(),
                            None,
                        )
                        .await?;

                    deployment.status = status;
                    deployment.ip_address = provider_deployment.ip_address;
                    deployment.connection_info = provider_deployment.raw_data.clone();
                    deployment.raw_response = provider_deployment.raw_data;
                    deployment.updated_at = Utc::now();
                }
                Err(e) => {
                    tracing::error!(
                        "Failed to fetch {} instance status: {}",
                        deployment.provider,
                        e
                    );
                }
            }
        }

        Ok(deployment)
    }

    /// Get raw instance details from DataCrunch
    pub async fn get_instance_details(&self, deployment_id: &str) -> Result<Instance> {
        let provider = self.get_datacrunch_provider()?;

        // Get deployment from database
        let deployment = self
            .db
            .get_deployment(deployment_id)
            .await?
            .ok_or_else(|| {
                AggregatorError::NotFound(format!("Deployment not found: {}", deployment_id))
            })?;

        let provider_instance_id =
            deployment
                .provider_instance_id
                .ok_or_else(|| AggregatorError::Provider {
                    provider: "datacrunch".to_string(),
                    message: "Instance not yet provisioned".to_string(),
                })?;

        provider.get_instance(&provider_instance_id).await
    }

    /// Delete a deployment and terminate the instance
    pub async fn delete_deployment(&self, deployment_id: &str) -> Result<()> {
        // Get deployment from database
        let deployment = self
            .db
            .get_deployment(deployment_id)
            .await?
            .ok_or_else(|| {
                AggregatorError::NotFound(format!("Deployment not found: {}", deployment_id))
            })?;

        // Delete instance if it exists
        if let Some(provider_instance_id) = &deployment.provider_instance_id {
            let provider = self.get_provider(deployment.provider)?;
            provider.delete_deployment(provider_instance_id).await?;
        }

        // Update deployment status to deleted
        self.db
            .update_deployment(
                deployment_id,
                deployment.provider_instance_id,
                DeploymentStatus::Deleted,
                deployment.ip_address,
                deployment.connection_info,
                deployment.raw_response,
                None,
            )
            .await?;

        Ok(())
    }

    /// List deployments with optional filters
    pub async fn list_deployments(
        &self,
        provider: Option<ProviderEnum>,
        status: Option<DeploymentStatus>,
    ) -> Result<Vec<Deployment>> {
        self.db.list_deployments(provider, status).await
    }
}
