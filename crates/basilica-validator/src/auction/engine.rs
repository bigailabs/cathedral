use anyhow::{anyhow, Result};
use std::sync::Arc;

use crate::basilica_api::BasilicaApiClient;
use crate::config::auction::AuctionConfig;

#[derive(Debug, Clone, PartialEq)]
pub struct ValidatedBid {
    pub miner_uid: u16,
    /// Bid price in cents per GPU per hour (e.g., 250 = $2.50/hour)
    pub bid_per_hour_cents: u32,
    pub gpu_count: u32,
}

#[derive(Debug, Clone, PartialEq)]
pub struct AuctionWinner {
    pub miner_uid: u16,
    /// Bid price in cents per GPU per hour
    pub bid_per_hour_cents: u32,
}

#[derive(Debug, Clone, PartialEq)]
pub struct AuctionResult {
    pub category: String,
    /// Baseline price in cents per GPU per hour
    pub baseline_price_cents: u32,
    pub winners: Vec<AuctionWinner>,
}

pub struct AuctionEngine {
    api_client: Arc<BasilicaApiClient>,
    config: AuctionConfig,
}

impl AuctionEngine {
    pub fn new(api_client: Arc<BasilicaApiClient>, config: AuctionConfig) -> Self {
        Self { api_client, config }
    }

    pub async fn clear_auction(
        &self,
        category: &str,
        bids: &[ValidatedBid],
    ) -> Result<AuctionResult> {
        let prices = self.api_client.get_baseline_prices().await?;
        // Baseline prices are in dollars, convert to cents
        let baseline_dollars = prices
            .get(category)
            .copied()
            .ok_or_else(|| anyhow!("No baseline for category: {category}"))?;
        let baseline_cents = (baseline_dollars * 100.0).round() as u32;

        // Calculate floor in cents
        let floor_cents =
            ((baseline_dollars * self.config.min_bid_floor_fraction) * 100.0).round() as u32;
        let mut valid_bids: Vec<ValidatedBid> = bids
            .iter()
            .filter(|&b| b.bid_per_hour_cents >= floor_cents)
            .cloned()
            .collect();

        valid_bids.sort_by_key(|b| b.bid_per_hour_cents);

        let mut winners = Vec::new();
        for bid in valid_bids {
            winners.push(AuctionWinner {
                miner_uid: bid.miner_uid,
                bid_per_hour_cents: bid.bid_per_hour_cents,
            });
        }

        Ok(AuctionResult {
            category: category.to_string(),
            baseline_price_cents: baseline_cents,
            winners,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::basilica_api::{
        BaselinePriceFetcher, BasilicaApiClient, TokenPriceFetcher, TokenPriceSnapshot,
        ValidatorSigner,
    };
    use async_trait::async_trait;
    use reqwest::Client;
    use std::collections::HashMap;
    use std::sync::Arc;
    use std::time::Duration;

    struct TestBaselineFetcher {
        prices: HashMap<String, f64>,
    }

    #[async_trait]
    impl BaselinePriceFetcher for TestBaselineFetcher {
        async fn fetch(&self, _client: &BasilicaApiClient) -> Result<HashMap<String, f64>> {
            Ok(self.prices.clone())
        }
    }

    struct TestTokenFetcher;

    #[async_trait]
    impl TokenPriceFetcher for TestTokenFetcher {
        async fn fetch(
            &self,
            _client: &BasilicaApiClient,
            _netuid: u16,
        ) -> Result<TokenPriceSnapshot> {
            Ok(TokenPriceSnapshot {
                tao_price_usd: rust_decimal::Decimal::ONE,
                alpha_price_usd: rust_decimal::Decimal::ONE,
                alpha_price_tao: rust_decimal::Decimal::ONE,
                tao_reserve: rust_decimal::Decimal::ONE,
                alpha_reserve: rust_decimal::Decimal::ONE,
                fetched_at: "2024-01-01T00:00:00Z".to_string(),
            })
        }
    }

    struct TestSigner;

    impl ValidatorSigner for TestSigner {
        fn hotkey(&self) -> String {
            "test_hotkey".to_string()
        }

        fn sign(&self, _message: &[u8]) -> Result<String> {
            Ok("deadbeef".to_string())
        }
    }

    fn make_engine(prices: HashMap<String, f64>) -> AuctionEngine {
        make_engine_with_floor(prices, AuctionConfig::default().min_bid_floor_fraction)
    }

    fn make_engine_with_floor(prices: HashMap<String, f64>, floor: f64) -> AuctionEngine {
        let fetcher: Arc<dyn BaselinePriceFetcher> = Arc::new(TestBaselineFetcher { prices });
        let token_fetcher: Arc<dyn TokenPriceFetcher> = Arc::new(TestTokenFetcher);
        let signer: Arc<dyn ValidatorSigner> = Arc::new(TestSigner);
        let client = Arc::new(BasilicaApiClient::new_with_fetchers(
            "http://localhost".to_string(),
            signer,
            Client::new(),
            Duration::from_secs(60),
            Duration::from_secs(60),
            fetcher,
            token_fetcher,
        ));
        let config = AuctionConfig {
            price_api_endpoint: "http://localhost".to_string(),
            min_bid_floor_fraction: floor,
            ..AuctionConfig::default()
        };
        AuctionEngine::new(client, config)
    }

    #[tokio::test]
    async fn test_clear_auction_filters_and_sorts() {
        let mut prices = HashMap::new();
        prices.insert("H100".to_string(), 10.0); // $10.00 baseline
        let engine = make_engine_with_floor(prices, 0.5); // Floor is $5.00 = 500 cents

        let bids = vec![
            ValidatedBid {
                miner_uid: 1,
                bid_per_hour_cents: 600, // $6.00 - passes floor
                gpu_count: 1,
            },
            ValidatedBid {
                miner_uid: 2,
                bid_per_hour_cents: 400, // $4.00 - below floor, filtered out
                gpu_count: 1,
            },
            ValidatedBid {
                miner_uid: 3,
                bid_per_hour_cents: 800, // $8.00 - passes floor
                gpu_count: 1,
            },
        ];

        let result = engine.clear_auction("H100", &bids).await.unwrap();
        assert_eq!(result.winners.len(), 2);
        assert_eq!(result.winners[0].miner_uid, 1); // Sorted by price, $6.00 first
        assert_eq!(result.winners[1].miner_uid, 3); // $8.00 second
        assert_eq!(result.baseline_price_cents, 1000); // $10.00 in cents
    }

    #[tokio::test]
    async fn test_clear_auction_missing_baseline() {
        let engine = make_engine(HashMap::new());
        let bids = vec![ValidatedBid {
            miner_uid: 1,
            bid_per_hour_cents: 500, // $5.00 in cents
            gpu_count: 1,
        }];
        assert!(engine.clear_auction("H100", &bids).await.is_err());
    }
}
