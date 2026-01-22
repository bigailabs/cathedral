use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use async_trait::async_trait;
use reqwest::Client;
use serde::Deserialize;
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::Instant;
use tokio::sync::RwLock;
use tracing::{debug, warn};

use super::cache::PriceCache;

#[async_trait]
pub trait PriceFetcher: Send + Sync {
    async fn fetch(&self, endpoint: &str) -> Result<HashMap<String, f64>>;
}

pub struct HttpPriceFetcher {
    http_client: Client,
}

impl Default for HttpPriceFetcher {
    fn default() -> Self {
        Self {
            http_client: Client::new(),
        }
    }
}

#[async_trait]
impl PriceFetcher for HttpPriceFetcher {
    async fn fetch(&self, endpoint: &str) -> Result<HashMap<String, f64>> {
        let response: BaselinePricesResponse = self
            .http_client
            .get(endpoint)
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        let mut prices = HashMap::new();
        for price in response.prices {
            // TODO: Decide how to handle duplicates (e.g., prefer newest or max provider_count).
            prices.insert(price.gpu_category, price.price_per_hour);
        }

        Ok(prices)
    }
}

#[derive(Debug, Deserialize)]
struct BaselinePricesResponse {
    prices: Vec<BaselinePrice>,
}

#[derive(Debug, Deserialize)]
struct BaselinePrice {
    gpu_category: String,
    price_per_hour: f64,
}

#[derive(Debug)]
struct CircuitBreaker {
    failures: AtomicU32,
    threshold: u32,
    reset_timeout: std::time::Duration,
    last_failure: RwLock<Option<Instant>>,
}

impl CircuitBreaker {
    fn new(threshold: u32, reset_timeout: std::time::Duration) -> Self {
        Self {
            failures: AtomicU32::new(0),
            threshold,
            reset_timeout,
            last_failure: RwLock::new(None),
        }
    }

    async fn is_open(&self) -> bool {
        let failures = self.failures.load(Ordering::SeqCst);
        if failures < self.threshold {
            return false;
        }

        let mut last_failure = self.last_failure.write().await;
        if let Some(ts) = *last_failure {
            if ts.elapsed() > self.reset_timeout {
                self.failures.store(0, Ordering::SeqCst);
                *last_failure = None;
                return false;
            }
        }
        true
    }

    async fn record_success(&self) {
        self.failures.store(0, Ordering::SeqCst);
        *self.last_failure.write().await = None;
    }

    async fn record_failure(&self) {
        let failures = self.failures.fetch_add(1, Ordering::SeqCst) + 1;
        if failures >= self.threshold {
            *self.last_failure.write().await = Some(Instant::now());
        }
    }
}

pub struct PriceClient {
    endpoint: String,
    cache: Arc<RwLock<PriceCache>>,
    cache_ttl: Duration,
    fetcher: Arc<dyn PriceFetcher>,
    circuit_breaker: CircuitBreaker,
}

impl PriceClient {
    pub fn new(endpoint: String, cache_ttl: Duration) -> Self {
        Self::new_with_fetcher(endpoint, cache_ttl, Arc::new(HttpPriceFetcher::default()))
    }

    pub fn new_with_fetcher(
        endpoint: String,
        cache_ttl: Duration,
        fetcher: Arc<dyn PriceFetcher>,
    ) -> Self {
        Self {
            endpoint,
            cache: Arc::new(RwLock::new(PriceCache::default())),
            cache_ttl,
            fetcher,
            circuit_breaker: CircuitBreaker::new(3, Duration::from_secs(30)),
        }
    }

    pub async fn get_baseline_prices(&self) -> Result<HashMap<String, f64>> {
        if let Some(cached) = self.cache.read().await.get_if_valid(self.cache_ttl) {
            debug!("Using cached baseline prices");
            return Ok(cached);
        }

        let stale = self.cache.read().await.get_any();
        if self.circuit_breaker.is_open().await {
            if let Some(stale_prices) = stale {
                warn!("Price circuit open; using stale cache");
                return Ok(stale_prices);
            }
            return Err(anyhow::anyhow!(
                "Price circuit open and no cached baseline prices available"
            ));
        }

        match self.fetcher.fetch(&self.endpoint).await {
            Ok(prices) => {
                self.cache.write().await.update(prices.clone());
                self.circuit_breaker.record_success().await;
                Ok(prices)
            }
            Err(err) => {
                self.circuit_breaker.record_failure().await;
                if let Some(stale_prices) = stale {
                    warn!("Price fetch failed; using stale cache: {}", err);
                    Ok(stale_prices)
                } else {
                    Err(err)
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::time::Instant;

    struct TestFetcher {
        calls: Arc<AtomicUsize>,
        response: Arc<dyn Fn() -> Result<HashMap<String, f64>> + Send + Sync>,
    }

    #[async_trait]
    impl PriceFetcher for TestFetcher {
        async fn fetch(&self, _endpoint: &str) -> Result<HashMap<String, f64>> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            (self.response)()
        }
    }

    fn make_prices() -> HashMap<String, f64> {
        let mut prices = HashMap::new();
        prices.insert("H100".to_string(), 2.0);
        prices
    }

    #[tokio::test]
    async fn test_cache_hit_returns_cached() {
        let calls = Arc::new(AtomicUsize::new(0));
        let fetcher = Arc::new(TestFetcher {
            calls: calls.clone(),
            response: Arc::new(|| Ok(make_prices())),
        });
        let client = PriceClient::new_with_fetcher(
            "http://localhost".to_string(),
            Duration::from_secs(60),
            fetcher,
        );

        client
            .cache
            .write()
            .await
            .update_with_timestamp(make_prices(), Instant::now());

        let prices = client.get_baseline_prices().await.unwrap();
        assert_eq!(prices.get("H100"), Some(&2.0));
        assert_eq!(calls.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn test_cache_miss_fetch_success() {
        let calls = Arc::new(AtomicUsize::new(0));
        let fetcher = Arc::new(TestFetcher {
            calls: calls.clone(),
            response: Arc::new(|| Ok(make_prices())),
        });
        let client = PriceClient::new_with_fetcher(
            "http://localhost".to_string(),
            Duration::from_secs(60),
            fetcher,
        );

        let prices = client.get_baseline_prices().await.unwrap();
        assert_eq!(prices.get("H100"), Some(&2.0));
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn test_expired_cache_fetch_failure_returns_stale() {
        let calls = Arc::new(AtomicUsize::new(0));
        let fetcher = Arc::new(TestFetcher {
            calls: calls.clone(),
            response: Arc::new(|| Err(anyhow::anyhow!("fetch failed"))),
        });
        let client = PriceClient::new_with_fetcher(
            "http://localhost".to_string(),
            Duration::from_secs(1),
            fetcher,
        );

        client
            .cache
            .write()
            .await
            .update_with_timestamp(make_prices(), Instant::now() - Duration::from_secs(10));

        let prices = client.get_baseline_prices().await.unwrap();
        assert_eq!(prices.get("H100"), Some(&2.0));
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn test_cache_miss_fetch_failure_errors() {
        let calls = Arc::new(AtomicUsize::new(0));
        let fetcher = Arc::new(TestFetcher {
            calls: calls.clone(),
            response: Arc::new(|| Err(anyhow::anyhow!("fetch failed"))),
        });
        let client = PriceClient::new_with_fetcher(
            "http://localhost".to_string(),
            Duration::from_secs(1),
            fetcher,
        );

        let result = client.get_baseline_prices().await;
        assert!(result.is_err());
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }
}
