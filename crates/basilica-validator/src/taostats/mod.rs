use std::sync::Arc;
use std::time::Duration;

use anyhow::Result;
use async_trait::async_trait;
use serde::Deserialize;
use tokio::sync::RwLock;
use tracing::{debug, warn};

use crate::taostats::cache::TaoPriceCache;

pub mod cache;

#[derive(Debug, Deserialize)]
struct TaoPriceResponse {
    price: f64,
}

#[async_trait]
pub trait TaoPriceFetcher: Send + Sync {
    async fn fetch(&self, base_url: &str) -> Result<f64>;
}

pub struct HttpTaoPriceFetcher {
    client: reqwest::Client,
}

impl Default for HttpTaoPriceFetcher {
    fn default() -> Self {
        Self::new()
    }
}

impl HttpTaoPriceFetcher {
    pub fn new() -> Self {
        Self {
            client: reqwest::Client::new(),
        }
    }
}

#[async_trait]
impl TaoPriceFetcher for HttpTaoPriceFetcher {
    async fn fetch(&self, base_url: &str) -> Result<f64> {
        let url = format!("{}/price", base_url.trim_end_matches('/'));
        let response = self.client.get(url).send().await?.error_for_status()?;
        let payload: TaoPriceResponse = response.json().await?;
        // TODO: Add alpha price and subnet emissions endpoints as they stabilize.
        Ok(payload.price)
    }
}

pub struct TaoStatsClient {
    base_url: String,
    cache: Arc<RwLock<TaoPriceCache>>,
    cache_ttl: Duration,
    fetcher: Arc<dyn TaoPriceFetcher>,
}

impl TaoStatsClient {
    pub fn new(base_url: String, cache_ttl: Duration) -> Self {
        Self::new_with_fetcher(base_url, cache_ttl, Arc::new(HttpTaoPriceFetcher::new()))
    }

    pub fn new_with_fetcher(
        base_url: String,
        cache_ttl: Duration,
        fetcher: Arc<dyn TaoPriceFetcher>,
    ) -> Self {
        Self {
            base_url,
            cache: Arc::new(RwLock::new(TaoPriceCache::default())),
            cache_ttl,
            fetcher,
        }
    }

    pub async fn get_tao_price(&self) -> Result<f64> {
        if let Some(cached) = self.cache.read().await.get_if_valid(self.cache_ttl) {
            debug!("Using cached TAO price");
            return Ok(cached);
        }

        let stale = self.cache.read().await.get_any();
        match self.fetcher.fetch(&self.base_url).await {
            Ok(price) => {
                self.cache.write().await.update(price);
                Ok(price)
            }
            Err(err) => {
                if let Some(stale_price) = stale {
                    warn!("TAO price fetch failed; using stale cache: {}", err);
                    Ok(stale_price)
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

    struct TestFetcher {
        price: f64,
        calls: Arc<AtomicUsize>,
        should_fail: bool,
    }

    #[async_trait]
    impl TaoPriceFetcher for TestFetcher {
        async fn fetch(&self, _base_url: &str) -> Result<f64> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            if self.should_fail {
                anyhow::bail!("fetch failed");
            }
            Ok(self.price)
        }
    }

    #[tokio::test]
    async fn test_get_tao_price_uses_cache() {
        let calls = Arc::new(AtomicUsize::new(0));
        let fetcher = Arc::new(TestFetcher {
            price: 123.0,
            calls: calls.clone(),
            should_fail: true,
        });
        let client = TaoStatsClient::new_with_fetcher(
            "https://api.taostats.io".to_string(),
            Duration::from_secs(60),
            fetcher,
        );

        client.cache.write().await.update(456.0);
        let price = client.get_tao_price().await.unwrap();
        assert_eq!(price, 456.0);
        assert_eq!(calls.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn test_get_tao_price_falls_back_to_stale() {
        let calls = Arc::new(AtomicUsize::new(0));
        let fetcher = Arc::new(TestFetcher {
            price: 123.0,
            calls: calls.clone(),
            should_fail: true,
        });
        let client = TaoStatsClient::new_with_fetcher(
            "https://api.taostats.io".to_string(),
            Duration::from_secs(1),
            fetcher,
        );

        client
            .cache
            .write()
            .await
            .update_with_timestamp(222.0, std::time::Instant::now() - Duration::from_secs(10));

        let price = client.get_tao_price().await.unwrap();
        assert_eq!(price, 222.0);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn test_get_tao_price_fetches_when_missing() {
        let calls = Arc::new(AtomicUsize::new(0));
        let fetcher = Arc::new(TestFetcher {
            price: 789.0,
            calls: calls.clone(),
            should_fail: false,
        });
        let client = TaoStatsClient::new_with_fetcher(
            "https://api.taostats.io".to_string(),
            Duration::from_secs(60),
            fetcher,
        );

        let price = client.get_tao_price().await.unwrap();
        assert_eq!(price, 789.0);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }
}
