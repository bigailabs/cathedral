use anyhow::{Context, Result};
use async_trait::async_trait;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::str::FromStr;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::{Mutex, RwLock};
use tracing::{debug, warn};

use crate::billing::api_client::ValidatorSigner;

#[derive(Debug, Clone)]
pub struct TokenPriceSnapshot {
    pub tao_price_usd: Decimal,
    pub alpha_price_usd: Decimal,
    pub alpha_price_tao: Decimal,
    pub tao_reserve: Decimal,
    pub alpha_reserve: Decimal,
    pub fetched_at: String,
}

#[derive(Debug, Clone)]
struct CachedPrices {
    snapshot: TokenPriceSnapshot,
    cached_at: Instant,
}

impl CachedPrices {
    fn new(snapshot: TokenPriceSnapshot) -> Self {
        Self {
            snapshot,
            cached_at: Instant::now(),
        }
    }

    #[cfg(test)]
    fn with_timestamp(snapshot: TokenPriceSnapshot, cached_at: Instant) -> Self {
        Self { snapshot, cached_at }
    }

    fn is_valid(&self, ttl: Duration) -> bool {
        self.cached_at.elapsed() <= ttl
    }
}

#[derive(Debug, Serialize)]
struct TokenPricesQuery {
    netuid: u32,
}

#[derive(Debug, Deserialize)]
struct TokenPricesResponse {
    tao_price_usd: String,
    alpha_price_usd: String,
    alpha_price_tao: String,
    tao_reserve: String,
    alpha_reserve: String,
    fetched_at: String,
}

#[async_trait]
pub trait TokenPriceFetcher: Send + Sync {
    async fn fetch(
        &self,
        api_endpoint: &str,
        netuid: u16,
        signer: &dyn ValidatorSigner,
    ) -> Result<TokenPriceSnapshot>;
}

pub struct HttpTokenPriceFetcher {
    client: reqwest::Client,
}

impl HttpTokenPriceFetcher {
    pub fn new(timeout_secs: u64) -> Result<Self> {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(timeout_secs))
            .build()
            .context("Failed to build HTTP client")?;
        Ok(Self { client })
    }
}

#[async_trait]
impl TokenPriceFetcher for HttpTokenPriceFetcher {
    async fn fetch(
        &self,
        api_endpoint: &str,
        netuid: u16,
        signer: &dyn ValidatorSigner,
    ) -> Result<TokenPriceSnapshot> {
        let query = TokenPricesQuery {
            netuid: netuid as u32,
        };
        let (signature, timestamp, hotkey) = signed_headers(&query, signer)?;

        let url = format!(
            "{}/v1/prices/tokens",
            api_endpoint.trim_end_matches('/')
        );

        let response = self
            .client
            .get(&url)
            .header("X-Validator-Signature", signature)
            .header("X-Timestamp", timestamp)
            .header("X-Validator-Hotkey", hotkey)
            .query(&query)
            .send()
            .await
            .context("Token price request failed")?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            anyhow::bail!("Token price API returned status {status}: {body}");
        }

        let payload: TokenPricesResponse = response
            .json()
            .await
            .context("Failed to parse token price response")?;

        Ok(TokenPriceSnapshot {
            tao_price_usd: Decimal::from_str(&payload.tao_price_usd)
                .context("Invalid tao_price_usd")?,
            alpha_price_usd: Decimal::from_str(&payload.alpha_price_usd)
                .context("Invalid alpha_price_usd")?,
            alpha_price_tao: Decimal::from_str(&payload.alpha_price_tao)
                .context("Invalid alpha_price_tao")?,
            tao_reserve: Decimal::from_str(&payload.tao_reserve).context("Invalid tao_reserve")?,
            alpha_reserve: Decimal::from_str(&payload.alpha_reserve)
                .context("Invalid alpha_reserve")?,
            fetched_at: payload.fetched_at,
        })
    }
}

#[derive(Clone)]
pub struct TokenPriceClient {
    api_endpoint: String,
    cache_ttl: Duration,
    signer: Arc<dyn ValidatorSigner>,
    fetcher: Arc<dyn TokenPriceFetcher>,
    cache: Arc<RwLock<HashMap<u16, CachedPrices>>>,
    fetch_lock: Arc<Mutex<()>>,
}

impl TokenPriceClient {
    pub fn new(
        api_endpoint: String,
        cache_ttl: Duration,
        signer: Arc<dyn ValidatorSigner>,
        timeout_secs: u64,
    ) -> Result<Self> {
        let fetcher = Arc::new(HttpTokenPriceFetcher::new(timeout_secs)?);
        Ok(Self::new_with_fetcher(
            api_endpoint,
            cache_ttl,
            signer,
            fetcher,
        ))
    }

    pub fn new_with_fetcher(
        api_endpoint: String,
        cache_ttl: Duration,
        signer: Arc<dyn ValidatorSigner>,
        fetcher: Arc<dyn TokenPriceFetcher>,
    ) -> Self {
        Self {
            api_endpoint,
            cache_ttl,
            signer,
            fetcher,
            cache: Arc::new(RwLock::new(HashMap::new())),
            fetch_lock: Arc::new(Mutex::new(())),
        }
    }

    pub async fn get_prices(&self, netuid: u16) -> Result<TokenPriceSnapshot> {
        if let Some(snapshot) = self.get_cached_if_valid(netuid).await {
            debug!(netuid = netuid, "Using cached token prices");
            return Ok(snapshot);
        }

        let _guard = self.fetch_lock.lock().await;
        if let Some(snapshot) = self.get_cached_if_valid(netuid).await {
            debug!(netuid = netuid, "Using cached token prices (post-lock)");
            return Ok(snapshot);
        }

        let cached = self.cache.read().await.get(&netuid).cloned();
        match self
            .fetcher
            .fetch(&self.api_endpoint, netuid, self.signer.as_ref())
            .await
        {
            Ok(snapshot) => {
                self.cache
                    .write()
                    .await
                    .insert(netuid, CachedPrices::new(snapshot.clone()));
                Ok(snapshot)
            }
            Err(err) => {
                if let Some(cached) = cached {
                    warn!(
                        netuid = netuid,
                        error = %err,
                        "Token price fetch failed; using cached value"
                    );
                    Ok(cached.snapshot)
                } else {
                    Err(err)
                }
            }
        }
    }

    pub async fn get_alpha_price_usd(&self, netuid: u16) -> Result<Decimal> {
        Ok(self.get_prices(netuid).await?.alpha_price_usd)
    }

    pub async fn get_tao_price_usd(&self, netuid: u16) -> Result<Decimal> {
        Ok(self.get_prices(netuid).await?.tao_price_usd)
    }

    async fn get_cached_if_valid(&self, netuid: u16) -> Option<TokenPriceSnapshot> {
        let cache = self.cache.read().await;
        let cached = cache.get(&netuid)?;
        if cached.is_valid(self.cache_ttl) {
            return Some(cached.snapshot.clone());
        }
        None
    }
}

fn signed_headers<T: Serialize>(
    payload: &T,
    signer: &dyn ValidatorSigner,
) -> Result<(String, String, String)> {
    let timestamp = chrono::Utc::now().timestamp().to_string();
    let payload_json = serde_json::to_string(payload).context("Failed to serialize payload")?;
    let message = format!("{timestamp}:{payload_json}");
    let signature = signer.sign(message.as_bytes())?;
    let hotkey = signer.hotkey();
    Ok((signature, timestamp, hotkey))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    struct TestSigner;

    impl ValidatorSigner for TestSigner {
        fn hotkey(&self) -> String {
            "test_hotkey".to_string()
        }

        fn sign(&self, _message: &[u8]) -> Result<String> {
            Ok("deadbeef".to_string())
        }
    }

    struct TestFetcher {
        calls: Arc<AtomicUsize>,
        response: Arc<dyn Fn() -> Result<TokenPriceSnapshot> + Send + Sync>,
    }

    #[async_trait]
    impl TokenPriceFetcher for TestFetcher {
        async fn fetch(
            &self,
            _api_endpoint: &str,
            _netuid: u16,
            _signer: &dyn ValidatorSigner,
        ) -> Result<TokenPriceSnapshot> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            (self.response)()
        }
    }

    fn snapshot(tao: &str, alpha: &str) -> TokenPriceSnapshot {
        TokenPriceSnapshot {
            tao_price_usd: Decimal::from_str(tao).unwrap(),
            alpha_price_usd: Decimal::from_str(alpha).unwrap(),
            alpha_price_tao: Decimal::from_str("0.1").unwrap(),
            tao_reserve: Decimal::from_str("10").unwrap(),
            alpha_reserve: Decimal::from_str("20").unwrap(),
            fetched_at: "2024-01-01T00:00:00Z".to_string(),
        }
    }

    #[tokio::test]
    async fn test_cache_hit_within_ttl() {
        let calls = Arc::new(AtomicUsize::new(0));
        let fetcher = Arc::new(TestFetcher {
            calls: calls.clone(),
            response: Arc::new(|| Err(anyhow::anyhow!("fetch failed"))),
        });
        let signer: Arc<dyn ValidatorSigner> = Arc::new(TestSigner);
        let client = TokenPriceClient::new_with_fetcher(
            "http://localhost".to_string(),
            Duration::from_secs(60),
            signer,
            fetcher,
        );

        let snap = snapshot("1.0", "2.0");
        client
            .cache
            .write()
            .await
            .insert(1, CachedPrices::with_timestamp(snap.clone(), Instant::now()));

        let result = client.get_prices(1).await.unwrap();
        assert_eq!(result.tao_price_usd, snap.tao_price_usd);
        assert_eq!(calls.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn test_ttl_expired_refresh_success() {
        let calls = Arc::new(AtomicUsize::new(0));
        let fetcher = Arc::new(TestFetcher {
            calls: calls.clone(),
            response: Arc::new(|| Ok(snapshot("3.0", "4.0"))),
        });
        let signer: Arc<dyn ValidatorSigner> = Arc::new(TestSigner);
        let client = TokenPriceClient::new_with_fetcher(
            "http://localhost".to_string(),
            Duration::from_secs(1),
            signer,
            fetcher,
        );

        let stale = snapshot("1.0", "2.0");
        client.cache.write().await.insert(
            1,
            CachedPrices::with_timestamp(stale, Instant::now() - Duration::from_secs(10)),
        );

        let result = client.get_prices(1).await.unwrap();
        assert_eq!(result.tao_price_usd, Decimal::from_str("3.0").unwrap());
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn test_ttl_expired_refresh_failure_uses_cache() {
        let calls = Arc::new(AtomicUsize::new(0));
        let fetcher = Arc::new(TestFetcher {
            calls: calls.clone(),
            response: Arc::new(|| Err(anyhow::anyhow!("fetch failed"))),
        });
        let signer: Arc<dyn ValidatorSigner> = Arc::new(TestSigner);
        let client = TokenPriceClient::new_with_fetcher(
            "http://localhost".to_string(),
            Duration::from_secs(1),
            signer,
            fetcher,
        );

        let stale = snapshot("1.0", "2.0");
        client.cache.write().await.insert(
            1,
            CachedPrices::with_timestamp(stale.clone(), Instant::now() - Duration::from_secs(10)),
        );

        let result = client.get_prices(1).await.unwrap();
        assert_eq!(result.tao_price_usd, stale.tao_price_usd);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }
}
