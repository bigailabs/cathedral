use anyhow::Result;
use chrono::{DateTime, Duration, Utc};
use serde::Deserialize;
use std::sync::Arc;
use tokio::sync::{Mutex, RwLock};
use tracing::{debug, warn};

#[derive(Debug, Clone)]
pub struct PriceSnapshot {
    pub alpha_usd: f64,
    pub fetched_at: DateTime<Utc>,
    pub is_stale: bool,
}

#[derive(Debug, Clone)]
struct CachedPrice {
    alpha_usd: f64,
    fetched_at: DateTime<Utc>,
}

#[derive(Debug, Deserialize)]
struct AlphaPriceResponse {
    price: f64,
}

#[derive(Clone)]
pub struct PriceOracle {
    base_url: String,
    endpoint_path: String,
    cache_ttl: Duration,
    stale_after: Duration,
    client: reqwest::Client,
    cache: Arc<RwLock<Option<CachedPrice>>>,
    fetch_lock: Arc<Mutex<()>>,
}

impl PriceOracle {
    pub fn new(
        base_url: String,
        endpoint_path: String,
        cache_ttl: Duration,
        stale_after: Duration,
    ) -> Self {
        Self {
            base_url,
            endpoint_path,
            cache_ttl,
            stale_after,
            client: reqwest::Client::new(),
            cache: Arc::new(RwLock::new(None)),
            fetch_lock: Arc::new(Mutex::new(())),
        }
    }

    pub async fn get_alpha_usd_price(&self) -> Result<PriceSnapshot> {
        if let Some(snapshot) = self.get_cached_if_valid().await {
            self.log_if_stale(&snapshot);
            return Ok(snapshot);
        }

        let _guard = self.fetch_lock.lock().await;
        if let Some(snapshot) = self.get_cached_if_valid().await {
            self.log_if_stale(&snapshot);
            return Ok(snapshot);
        }

        let cached = self.cache.read().await.clone();
        match self.fetch_price().await {
            Ok((price, fetched_at)) => {
                self.cache.write().await.replace(CachedPrice {
                    alpha_usd: price,
                    fetched_at,
                });
                let snapshot = self.snapshot(price, fetched_at);
                self.log_if_stale(&snapshot);
                Ok(snapshot)
            }
            Err(err) => {
                if let Some(cached) = cached {
                    warn!("Alpha price fetch failed; using stale cache: {}", err);
                    let snapshot = self.snapshot(cached.alpha_usd, cached.fetched_at);
                    self.log_if_stale(&snapshot);
                    Ok(snapshot)
                } else {
                    Err(err)
                }
            }
        }
    }

    pub async fn is_stale(&self) -> bool {
        let cached = self.cache.read().await;
        cached
            .as_ref()
            .map(|price| self.snapshot(price.alpha_usd, price.fetched_at).is_stale)
            .unwrap_or(true)
    }

    async fn get_cached_if_valid(&self) -> Option<PriceSnapshot> {
        let cached = self.cache.read().await;
        let cached = cached.as_ref()?;
        let age = Utc::now() - cached.fetched_at;
        if age <= self.cache_ttl {
            debug!("Using cached Alpha price");
            return Some(self.snapshot(cached.alpha_usd, cached.fetched_at));
        }
        None
    }

    fn snapshot(&self, price: f64, fetched_at: DateTime<Utc>) -> PriceSnapshot {
        let is_stale = (Utc::now() - fetched_at) > self.stale_after;
        PriceSnapshot {
            alpha_usd: price,
            fetched_at,
            is_stale,
        }
    }

    fn log_if_stale(&self, snapshot: &PriceSnapshot) {
        if snapshot.is_stale {
            let age_seconds = (Utc::now() - snapshot.fetched_at).num_seconds().max(0);
            warn!(
                "Alpha price snapshot is stale (age={}s, stale_after={}s)",
                age_seconds,
                self.stale_after.num_seconds().max(0)
            );
        }
    }

    async fn fetch_price(&self) -> Result<(f64, DateTime<Utc>)> {
        let url = if self.endpoint_path.starts_with("http") {
            self.endpoint_path.clone()
        } else {
            format!(
                "{}/{}",
                self.base_url.trim_end_matches('/'),
                self.endpoint_path.trim_start_matches('/')
            )
        };
        let response = self.client.get(url).send().await?.error_for_status()?;
        let payload: AlphaPriceResponse = response.json().await?;
        Ok((payload.price, Utc::now()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use wiremock::matchers::{method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    #[tokio::test]
    async fn test_cache_hit_returns_cached() {
        let oracle = PriceOracle::new(
            "https://api.taostats.io".to_string(),
            "/alpha/price".to_string(),
            Duration::minutes(15),
            Duration::hours(1),
        );
        oracle.cache.write().await.replace(CachedPrice {
            alpha_usd: 1.25,
            fetched_at: Utc::now(),
        });
        let snapshot = oracle.get_alpha_usd_price().await.unwrap();
        assert_eq!(snapshot.alpha_usd, 1.25);
        assert!(!snapshot.is_stale);
    }

    #[tokio::test]
    async fn test_cache_stale_marked() {
        let oracle = PriceOracle::new(
            "https://api.taostats.io".to_string(),
            "/alpha/price".to_string(),
            Duration::hours(3),
            Duration::hours(1),
        );
        oracle.cache.write().await.replace(CachedPrice {
            alpha_usd: 1.25,
            fetched_at: Utc::now() - Duration::hours(2),
        });
        let snapshot = oracle.get_alpha_usd_price().await.unwrap();
        assert!(snapshot.is_stale);
    }

    #[tokio::test]
    async fn test_fetch_fallback_uses_cached() {
        let mock_server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/alpha/price"))
            .respond_with(ResponseTemplate::new(500))
            .mount(&mock_server)
            .await;
        let oracle = PriceOracle::new(
            mock_server.uri(),
            "/alpha/price".to_string(),
            Duration::minutes(15),
            Duration::hours(1),
        );
        oracle.cache.write().await.replace(CachedPrice {
            alpha_usd: 0.9,
            fetched_at: Utc::now() - Duration::hours(2),
        });
        // Force fetch by expiring cache TTL.
        let snapshot = oracle.get_alpha_usd_price().await.unwrap();
        assert_eq!(snapshot.alpha_usd, 0.9);
        assert!(snapshot.is_stale);
    }

    #[tokio::test]
    async fn test_fetch_updates_price() {
        let mock_server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/alpha/price"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({"price": 1.42})))
            .mount(&mock_server)
            .await;
        let oracle = PriceOracle::new(
            mock_server.uri(),
            "/alpha/price".to_string(),
            Duration::minutes(15),
            Duration::hours(1),
        );
        let snapshot = oracle.get_alpha_usd_price().await.unwrap();
        assert_eq!(snapshot.alpha_usd, 1.42);
        assert!(!snapshot.is_stale);
    }
}
