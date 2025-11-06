use crate::config::Config;
use crate::db::Database;
use crate::error::{AggregatorError, Result};
use crate::models::{GpuOffering, Provider as ProviderEnum, ProviderHealth};
use crate::providers::datacrunch::DataCrunchProvider;
use crate::providers::Provider;
use chrono::{Duration, Utc};
use std::sync::Arc;

pub struct AggregatorService {
    db: Arc<Database>,
    providers: Vec<Box<dyn Provider>>,
    config: Config,
}

impl AggregatorService {
    pub fn new(db: Arc<Database>, config: Config) -> Result<Self> {
        let mut providers: Vec<Box<dyn Provider>> = Vec::new();

        // Initialize enabled providers
        if config.providers.datacrunch.enabled {
            let client_id = config
                .providers
                .datacrunch
                .client_id
                .clone()
                .ok_or_else(|| AggregatorError::Config("DataCrunch client_id missing".into()))?;

            let client_secret = config
                .providers
                .datacrunch
                .client_secret
                .clone()
                .ok_or_else(|| {
                    AggregatorError::Config("DataCrunch client_secret missing".into())
                })?;

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

            providers.push(Box::new(provider));
        }

        if providers.is_empty() {
            return Err(AggregatorError::NoProvidersAvailable);
        }

        Ok(Self {
            db,
            providers,
            config,
        })
    }

    /// Get GPU offerings from database cache
    /// Note: Background task keeps cache fresh, so this just reads from DB
    pub async fn get_offerings(&self) -> Result<Vec<GpuOffering>> {
        // Simply return all cached offerings from database
        let all_offerings = self.db.get_offerings(None).await?;
        tracing::debug!("Retrieved {} offerings from cache", all_offerings.len());
        Ok(all_offerings)
    }

    /// Refresh offerings from all providers (called by background task)
    /// Returns total number of offerings fetched
    pub async fn refresh_all_providers(&self) -> Result<usize> {
        let mut total_count = 0;

        for provider in &self.providers {
            let provider_id = provider.provider_id();

            // Check if we should fetch (respects cooldown)
            if self.should_fetch(provider_id).await? {
                match self.fetch_and_cache(provider.as_ref()).await {
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
                        // Update provider status with error
                        let _ = self
                            .db
                            .update_provider_status(provider_id, false, Some(e.to_string()))
                            .await;
                    }
                }
            } else {
                tracing::debug!(
                    "Skipping {} - cooldown period not elapsed",
                    provider_id
                );
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
    async fn fetch_and_cache(&self, provider: &dyn Provider) -> Result<Vec<GpuOffering>> {
        let provider_id = provider.provider_id();

        let offerings = provider.fetch_offerings().await?;

        // Store in database
        self.db.upsert_offerings(&offerings).await?;

        // Update provider status
        self.db
            .update_provider_status(provider_id, true, None)
            .await?;

        Ok(offerings)
    }

    /// Get health status for all providers
    pub async fn get_provider_health(&self) -> Result<Vec<ProviderHealth>> {
        let mut health_statuses = Vec::new();

        for provider in &self.providers {
            let health = self
                .db
                .get_provider_health(provider.provider_id())
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
}
