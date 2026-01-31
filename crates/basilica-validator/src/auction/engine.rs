use anyhow::{anyhow, Result};
use std::sync::Arc;

use crate::config::auction::AuctionConfig;
use crate::pricing::PriceClient;

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
    price_client: Arc<PriceClient>,
    config: AuctionConfig,
}

impl AuctionEngine {
    pub fn new(price_client: Arc<PriceClient>, config: AuctionConfig) -> Self {
        Self {
            price_client,
            config,
        }
    }

    pub async fn clear_auction(
        &self,
        category: &str,
        bids: &[ValidatedBid],
    ) -> Result<AuctionResult> {
        let prices = self.price_client.get_baseline_prices().await?;
        // PriceClient returns dollars, convert to cents
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
    use crate::pricing::client::{PriceClient, PriceFetcher};
    use async_trait::async_trait;
    use std::collections::HashMap;
    use std::sync::Arc;
    use std::time::Duration;

    struct TestFetcher {
        prices: HashMap<String, f64>,
    }

    #[async_trait]
    impl PriceFetcher for TestFetcher {
        async fn fetch(&self, _endpoint: &str) -> Result<HashMap<String, f64>> {
            Ok(self.prices.clone())
        }
    }

    fn make_engine(prices: HashMap<String, f64>) -> AuctionEngine {
        make_engine_with_floor(prices, AuctionConfig::default().min_bid_floor_fraction)
    }

    fn make_engine_with_floor(prices: HashMap<String, f64>, floor: f64) -> AuctionEngine {
        let fetcher: Arc<dyn PriceFetcher> = Arc::new(TestFetcher { prices });
        let client = Arc::new(PriceClient::new_with_fetcher(
            "http://localhost".to_string(),
            Duration::from_secs(60),
            fetcher,
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
