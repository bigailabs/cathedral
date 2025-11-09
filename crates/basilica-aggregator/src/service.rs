use crate::config::{AuthConfig, Config};
use crate::db::Database;
use crate::error::{AggregatorError, Result};
use crate::models::{
    Deployment, DeploymentStatus, GpuOffering, Provider as ProviderEnum, ProviderHealth,
};
use crate::providers::datacrunch::{
    DataCrunchProvider, DeployInstanceRequest, Instance, OsImage, SshKey,
};
use crate::providers::hyperstack::{
    DeployVmRequest as HyperstackDeployVmRequest, HyperstackProvider,
};
use crate::providers::Provider;
use basilica_common::types::GpuCategory;
use chrono::{Duration, Utc};
use std::sync::Arc;
use uuid::Uuid;

pub struct AggregatorService {
    db: Arc<Database>,
    datacrunch: Option<DataCrunchProvider>,
    hyperstack: Option<HyperstackProvider>,
    config: Config,
}

impl AggregatorService {
    pub fn new(db: Arc<Database>, config: Config) -> Result<Self> {
        // Initialize DataCrunch provider (optional)
        let datacrunch = if config.providers.datacrunch.is_enabled() {
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

                Some(DataCrunchProvider::new(
                    client_id,
                    client_secret,
                    base_url,
                    config.providers.datacrunch.timeout_seconds,
                )?)
            } else {
                None
            }
        } else {
            None
        };

        // Initialize Hyperstack provider (optional)
        let hyperstack = if config.providers.hyperstack.is_enabled() {
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

                Some(HyperstackProvider::new(
                    api_key,
                    base_url,
                    config.providers.hyperstack.timeout_seconds,
                )?)
            } else {
                None
            }
        } else {
            None
        };

        Ok(Self {
            db,
            datacrunch,
            hyperstack,
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

        // Refresh DataCrunch (if enabled)
        if let Some(ref datacrunch) = self.datacrunch {
            let provider_id = ProviderEnum::DataCrunch;
            if self.should_fetch(provider_id).await? {
                match self.fetch_and_cache(datacrunch).await {
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
                        let _ = self
                            .db
                            .update_provider_status(provider_id, false, Some(e.to_string()))
                            .await;
                    }
                }
            } else {
                tracing::debug!("Skipping {} - cooldown period not elapsed", provider_id);
            }
        }

        // Refresh Hyperstack (if enabled)
        if let Some(ref hyperstack) = self.hyperstack {
            let provider_id = ProviderEnum::Hyperstack;
            if self.should_fetch(provider_id).await? {
                match self.fetch_and_cache(hyperstack).await {
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
                        let _ = self
                            .db
                            .update_provider_status(provider_id, false, Some(e.to_string()))
                            .await;
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

        // Update provider status
        self.db
            .update_provider_status(provider_id, true, None)
            .await?;

        Ok(supported_offerings)
    }

    /// Get health status for all providers
    pub async fn get_provider_health(&self) -> Result<Vec<ProviderHealth>> {
        let mut health_statuses = Vec::new();

        // Get health for DataCrunch (if enabled)
        if self.datacrunch.is_some() {
            let health = self
                .db
                .get_provider_health(ProviderEnum::DataCrunch)
                .await?;
            health_statuses.push(health);
        }

        // Get health for Hyperstack (if enabled)
        if self.hyperstack.is_some() {
            let health = self
                .db
                .get_provider_health(ProviderEnum::Hyperstack)
                .await?;
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
    // DataCrunch Deployment Management
    // ========================================================================

    /// Get DataCrunch provider instance
    fn get_datacrunch_provider(&self) -> Result<&DataCrunchProvider> {
        self.datacrunch.as_ref().ok_or_else(|| {
            AggregatorError::Provider {
                provider: "DataCrunch".to_string(),
                message: "Provider not enabled".to_string(),
            }
            .into()
        })
    }

    /// Get Hyperstack provider instance
    fn get_hyperstack_provider(&self) -> Result<&HyperstackProvider> {
        self.hyperstack.as_ref().ok_or_else(|| {
            AggregatorError::Provider {
                provider: "Hyperstack".to_string(),
                message: "Provider not enabled".to_string(),
            }
            .into()
        })
    }

    /// Get provider as trait object by enum
    fn get_provider_dyn(&self, provider: &ProviderEnum) -> Result<&dyn Provider> {
        match provider {
            ProviderEnum::DataCrunch => self
                .datacrunch
                .as_ref()
                .map(|p| p as &dyn Provider)
                .ok_or_else(|| {
                    AggregatorError::Provider {
                        provider: provider.as_str().to_string(),
                        message: "Provider not enabled".to_string(),
                    }
                    .into()
                }),
            ProviderEnum::Hyperstack => self
                .hyperstack
                .as_ref()
                .map(|p| p as &dyn Provider)
                .ok_or_else(|| {
                    AggregatorError::Provider {
                        provider: provider.as_str().to_string(),
                        message: "Provider not enabled".to_string(),
                    }
                    .into()
                }),
            _ => Err(AggregatorError::Provider {
                provider: provider.as_str().to_string(),
                message: "Provider not supported".to_string(),
            }
            .into()),
        }
    }

    /// List SSH keys from DataCrunch (legacy)
    pub async fn list_ssh_keys(&self) -> Result<Vec<SshKey>> {
        let provider = self.get_datacrunch_provider()?;
        provider.list_ssh_keys_impl().await
    }

    /// Create SSH key in DataCrunch (legacy)
    pub async fn create_ssh_key(&self, name: String, public_key: String) -> Result<SshKey> {
        let provider = self.get_datacrunch_provider()?;
        provider.create_ssh_key_impl(name, public_key).await
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

        let provider_enum = offering.provider.clone();

        // Dispatch to provider-specific deployment logic
        match provider_enum {
            ProviderEnum::DataCrunch => {
                self.deploy_instance_datacrunch(
                    offering,
                    ssh_public_key,
                    ssh_key_name,
                    location_code,
                )
                .await
            }
            ProviderEnum::Hyperstack => {
                self.deploy_instance_hyperstack(
                    offering,
                    ssh_public_key,
                    ssh_key_name,
                    location_code,
                )
                .await
            }
            _ => Err(AggregatorError::Provider {
                provider: provider_enum.as_str().to_string(),
                message: "Deployment not supported for this provider".to_string(),
            }
            .into()),
        }
    }

    /// Deploy instance on DataCrunch
    async fn deploy_instance_datacrunch(
        &self,
        offering: &GpuOffering,
        ssh_public_key: String,
        ssh_key_name: Option<String>,
        location_code: Option<String>,
    ) -> Result<Deployment> {
        let provider = self.get_datacrunch_provider()?;

        // Extract instance type from raw metadata
        let instance_type = offering
            .raw_metadata
            .get("instance_type")
            .and_then(|v| v.as_str())
            .ok_or_else(|| AggregatorError::Provider {
                provider: "datacrunch".to_string(),
                message: "Missing instance_type in offering metadata".to_string(),
            })?
            .to_string();

        // Create or reuse SSH key
        let key_name = ssh_key_name.unwrap_or_else(|| format!("basilica-{}", Uuid::new_v4()));
        let ssh_key = provider
            .create_ssh_key_impl(key_name, ssh_public_key)
            .await?;

        // Generate deployment ID and hostname
        let deployment_id = Uuid::new_v4().to_string();
        let hostname = format!("basilica-{}", deployment_id);

        // Get default image (Ubuntu 22.04 with CUDA)
        let images = provider.list_images().await?;
        let default_image = images
            .iter()
            .find(|img| img.image_type.contains("ubuntu-22") && img.image_type.contains("cuda"))
            .map(|img| img.image_type.clone())
            .unwrap_or_else(|| "ubuntu-22.04-cuda-12.4-docker".to_string());

        // Create deployment request
        let deploy_request = DeployInstanceRequest {
            instance_type: instance_type.clone(),
            image: default_image,
            hostname: hostname.clone(),
            description: format!("Basilica deployment {}", deployment_id),
            ssh_key_ids: vec![ssh_key.id.clone()],
            location_code: location_code.clone().or_else(|| Some("FIN-01".to_string())),
            contract: Some("PAY_AS_YOU_GO".to_string()),
            pricing: Some("FIXED_PRICE".to_string()),
        };

        // Deploy instance
        let provider_instance_id = provider.deploy_instance(deploy_request).await?;

        // Create deployment record
        let now = Utc::now();
        let deployment = Deployment {
            id: deployment_id,
            user_id: "legacy".to_string(),
            provider: ProviderEnum::DataCrunch,
            provider_instance_id: Some(provider_instance_id),
            offering_id: offering.id.clone(),
            instance_type,
            location_code,
            status: DeploymentStatus::Pending,
            hostname,
            ssh_key_id: Some(ssh_key.id),
            ip_address: None,
            connection_info: None,
            raw_response: None,
            error_message: None,
            created_at: now,
            updated_at: now,
        };

        self.db.create_deployment(&deployment).await?;
        Ok(deployment)
    }

    /// Deploy instance on Hyperstack
    async fn deploy_instance_hyperstack(
        &self,
        offering: &GpuOffering,
        ssh_public_key: String,
        ssh_key_name: Option<String>,
        _location_code: Option<String>,
    ) -> Result<Deployment> {
        let provider = self.get_hyperstack_provider()?;

        // Extract flavor name from raw metadata
        let flavor_name = offering
            .raw_metadata
            .get("name")
            .and_then(|v| v.as_str())
            .ok_or_else(|| AggregatorError::Provider {
                provider: "hyperstack".to_string(),
                message: "Missing flavor name in offering metadata".to_string(),
            })?
            .to_string();

        // Generate deployment ID and hostname
        let deployment_id = Uuid::new_v4().to_string();
        let hostname = format!("basilica-{}", &deployment_id[..8]);

        // Create SSH keypair
        let key_name = ssh_key_name.unwrap_or_else(|| format!("basilica-{}", &deployment_id[..8]));
        let environment_name = "default".to_string();
        let keypair = provider
            .create_keypair_impl(key_name.clone(), environment_name.clone(), ssh_public_key)
            .await?;

        // Default image (Ubuntu 22.04 with CUDA)
        let default_image = "Ubuntu Server 22.04 LTS R535 CUDA 12.2".to_string();

        // Create deployment request
        let deploy_request = HyperstackDeployVmRequest {
            name: hostname.clone(),
            environment_name,
            image_name: default_image,
            flavor_name: flavor_name.clone(),
            key_name,
            user_data: None,
            assign_floating_ip: Some(true),
            count: Some(1),
            create_bootable_volume: None,
        };

        // Deploy VM
        let vm = provider.deploy_vm(deploy_request).await?;

        // Create deployment record
        let now = Utc::now();
        let deployment = Deployment {
            id: deployment_id,
            user_id: "legacy".to_string(),
            provider: ProviderEnum::Hyperstack,
            provider_instance_id: Some(vm.id.to_string()),
            offering_id: offering.id.clone(),
            instance_type: flavor_name,
            location_code: None,
            status: DeploymentStatus::Provisioning,
            hostname,
            ssh_key_id: Some(keypair.id.to_string()),
            ip_address: vm.floating_ip.clone(),
            connection_info: Some(serde_json::json!({
                "fixed_ip": vm.fixed_ip,
                "floating_ip": vm.floating_ip,
                "status": vm.status,
            })),
            raw_response: Some(serde_json::to_value(&vm).unwrap_or_default()),
            error_message: None,
            created_at: now,
            updated_at: now,
        };

        self.db.create_deployment(&deployment).await?;
        Ok(deployment)
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
            match deployment.provider {
                ProviderEnum::DataCrunch => {
                    let provider = self.get_datacrunch_provider()?;
                    match provider.get_instance(provider_instance_id).await {
                        Ok(instance) => {
                            let status = map_datacrunch_status_to_deployment(&instance.status);
                            let raw_response = serde_json::to_value(&instance).ok();

                            self.db
                                .update_deployment(
                                    deployment_id,
                                    Some(provider_instance_id.clone()),
                                    status.clone(),
                                    instance.ip.clone(),
                                    None,
                                    raw_response.clone(),
                                    None,
                                )
                                .await?;

                            deployment.status = status;
                            deployment.ip_address = instance.ip;
                            deployment.raw_response = raw_response;
                            deployment.updated_at = Utc::now();
                        }
                        Err(e) => {
                            tracing::error!("Failed to fetch DataCrunch instance status: {}", e);
                        }
                    }
                }
                ProviderEnum::Hyperstack => {
                    let provider = self.get_hyperstack_provider()?;
                    let vm_id: u32 =
                        provider_instance_id
                            .parse()
                            .map_err(|_| AggregatorError::Provider {
                                provider: "hyperstack".to_string(),
                                message: format!("Invalid VM ID: {}", provider_instance_id),
                            })?;

                    match provider.get_vm(vm_id).await {
                        Ok(vm) => {
                            let status = map_hyperstack_status_to_deployment(&vm.status);
                            let raw_response = serde_json::to_value(&vm).ok();
                            let connection_info = Some(serde_json::json!({
                                "fixed_ip": vm.fixed_ip,
                                "floating_ip": vm.floating_ip,
                                "status": vm.status,
                            }));

                            self.db
                                .update_deployment(
                                    deployment_id,
                                    Some(provider_instance_id.clone()),
                                    status.clone(),
                                    vm.floating_ip.clone(),
                                    connection_info.clone(),
                                    raw_response.clone(),
                                    None,
                                )
                                .await?;

                            deployment.status = status;
                            deployment.ip_address = vm.floating_ip;
                            deployment.connection_info = connection_info;
                            deployment.raw_response = raw_response;
                            deployment.updated_at = Utc::now();
                        }
                        Err(e) => {
                            tracing::error!("Failed to fetch Hyperstack VM status: {}", e);
                        }
                    }
                }
                _ => {
                    tracing::warn!(
                        "Unsupported provider for deployment status update: {:?}",
                        deployment.provider
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
            match deployment.provider {
                ProviderEnum::DataCrunch => {
                    let provider = self.get_datacrunch_provider()?;
                    provider.delete_instance(provider_instance_id).await?;
                }
                ProviderEnum::Hyperstack => {
                    let provider = self.get_hyperstack_provider()?;
                    let vm_id: u32 =
                        provider_instance_id
                            .parse()
                            .map_err(|_| AggregatorError::Provider {
                                provider: "hyperstack".to_string(),
                                message: format!("Invalid VM ID: {}", provider_instance_id),
                            })?;
                    provider.delete_vm(vm_id).await?;
                }
                _ => {
                    return Err(AggregatorError::Provider {
                        provider: deployment.provider.as_str().to_string(),
                        message: "Deletion not supported for this provider".to_string(),
                    }
                    .into());
                }
            }
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

    // ========================================================================
    // SSH Key Management
    // ========================================================================

    /// Register a new SSH key for a user
    pub async fn register_ssh_key(
        &self,
        user_id: String,
        name: String,
        public_key: String,
    ) -> Result<crate::models::SshKey> {
        // Check if user already has an SSH key
        if let Some(_existing) = self.db.get_ssh_key_by_user(&user_id).await? {
            return Err(AggregatorError::SshKeyAlreadyExists.into());
        }

        // Create SSH key record
        let ssh_key = crate::models::SshKey {
            id: Uuid::new_v4().to_string(),
            user_id,
            name,
            public_key,
            created_at: Utc::now(),
            updated_at: Utc::now(),
        };

        self.db.create_ssh_key(&ssh_key).await?;

        Ok(ssh_key)
    }

    /// Get user's SSH key
    pub async fn get_ssh_key(&self, user_id: &str) -> Result<Option<crate::models::SshKey>> {
        self.db.get_ssh_key_by_user(user_id).await
    }

    /// Update user's SSH key (replaces existing)
    pub async fn update_ssh_key(
        &self,
        user_id: String,
        name: String,
        public_key: String,
    ) -> Result<crate::models::SshKey> {
        let existing = self
            .db
            .get_ssh_key_by_user(&user_id)
            .await?
            .ok_or_else(|| AggregatorError::SshKeyNotFound)?;

        // Delete from all providers first
        self.delete_ssh_key_from_providers(&existing.id).await?;

        // Update the key
        let updated = crate::models::SshKey {
            id: existing.id,
            user_id: existing.user_id,
            name,
            public_key,
            created_at: existing.created_at,
            updated_at: Utc::now(),
        };

        self.db.update_ssh_key(&updated).await?;

        Ok(updated)
    }

    /// Delete user's SSH key and cleanup from all providers
    pub async fn delete_ssh_key(&self, user_id: &str) -> Result<()> {
        let ssh_key = self
            .db
            .get_ssh_key_by_user(user_id)
            .await?
            .ok_or_else(|| AggregatorError::SshKeyNotFound)?;

        self.delete_ssh_key_from_providers(&ssh_key.id).await?;
        self.db.delete_ssh_key(&ssh_key.id).await?;

        Ok(())
    }

    /// Helper: Delete SSH key from all registered providers
    async fn delete_ssh_key_from_providers(&self, ssh_key_id: &str) -> Result<()> {
        let mappings = self.db.list_provider_ssh_keys_for_key(ssh_key_id).await?;

        for mapping in mappings {
            let provider = self.get_provider_dyn(&mapping.provider)?;

            // Try to delete, but don't fail if provider returns error
            // (key might already be deleted, provider might be down, etc.)
            if let Err(e) = provider.delete_ssh_key(&mapping.provider_key_id).await {
                tracing::warn!(
                    "Failed to delete SSH key {} from provider {}: {}",
                    mapping.provider_key_id,
                    mapping.provider,
                    e
                );
            }
        }

        Ok(())
    }
}

/// Map DataCrunch instance status to deployment status
fn map_datacrunch_status_to_deployment(
    status: &crate::providers::datacrunch::InstanceStatus,
) -> DeploymentStatus {
    use crate::providers::datacrunch::InstanceStatus;

    match status {
        InstanceStatus::Running => DeploymentStatus::Running,
        InstanceStatus::Provisioning
        | InstanceStatus::Ordered
        | InstanceStatus::New
        | InstanceStatus::Validating => DeploymentStatus::Provisioning,
        InstanceStatus::Error | InstanceStatus::NoCapacity | InstanceStatus::NotFound => {
            DeploymentStatus::Error
        }
        InstanceStatus::Deleting | InstanceStatus::Discontinued => DeploymentStatus::Deleted,
        InstanceStatus::Offline | InstanceStatus::Unknown => DeploymentStatus::Pending,
    }
}

/// Map Hyperstack VM status to deployment status
fn map_hyperstack_status_to_deployment(status: &str) -> DeploymentStatus {
    // Hyperstack status strings are UPPERCASE (e.g., "ACTIVE", "BUILDING")
    match status.to_uppercase().as_str() {
        "ACTIVE" => DeploymentStatus::Running,
        "BUILDING" | "MIGRATING" | "REBUILD" | "RESIZE" | "VERIFY_RESIZE" | "REVERT_RESIZE" => {
            DeploymentStatus::Provisioning
        }
        "ERROR" => DeploymentStatus::Error,
        "SHUTOFF" | "SOFT_DELETED" | "SHELVED_OFFLOADED" => DeploymentStatus::Deleted,
        "PAUSED" | "SUSPENDED" | "RESCUED" | "PASSWORD" | "REBOOT" | "HARD_REBOOT" | "UNKNOWN"
        | _ => DeploymentStatus::Pending,
    }
}
