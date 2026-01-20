use anyhow::{anyhow, Result};
use std::sync::Arc;

use crate::config::auction::AuctionConfig;
use crate::pricing::PriceClient;

#[derive(Debug, Clone, PartialEq)]
pub struct ValidatedBid {
    pub miner_uid: u16,
    pub bid_per_hour: f64,
    pub gpu_count: u32,
}

#[derive(Debug, Clone, PartialEq)]
pub struct AuctionWinner {
    pub miner_uid: u16,
    pub bid_per_hour: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct AuctionResult {
    pub category: String,
    pub baseline_price: f64,
    pub winners: Vec<AuctionWinner>,
}

pub struct AuctionEngine {
    price_client: Arc<PriceClient>,
    config: AuctionConfig,
}

impl AuctionEngine {
    pub fn new(price_client: Arc<PriceClient>, config: AuctionConfig) -> Self {
        Self { price_client, config }
    }

    pub async fn clear_auction(
        &self,
        category: &str,
        bids: &[ValidatedBid],
    ) -> Result<AuctionResult> {
        let prices = self.price_client.get_baseline_prices().await?;
        let baseline = prices
            .get(category)
            .copied()
            .ok_or_else(|| anyhow!("No baseline for category: {category}"))?;

        let floor = baseline * self.config.min_bid_floor_fraction;
        let mut valid_bids: Vec<ValidatedBid> = bids
            .iter()
            .cloned()
            .filter(|b| b.bid_per_hour >= floor)
            .collect();

        valid_bids.sort_by(|a, b| {
            a.bid_per_hour
                .partial_cmp(&b.bid_per_hour)
                .unwrap_or(std::cmp::Ordering::Equal)
        });

        let mut winners = Vec::new();
        for bid in valid_bids {
            winners.push(AuctionWinner {
                miner_uid: bid.miner_uid,
                bid_per_hour: bid.bid_per_hour,
            });
        }

        Ok(AuctionResult {
            category: category.to_string(),
            baseline_price: baseline,
            winners,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pricing::{PriceFetcher, PriceClient};
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
        prices.insert("H100".to_string(), 10.0);
        let engine = make_engine_with_floor(prices, 0.5);

        let bids = vec![
            ValidatedBid {
                miner_uid: 1,
                bid_per_hour: 6.0,
                gpu_count: 1,
            },
            ValidatedBid {
                miner_uid: 2,
                bid_per_hour: 4.0,
                gpu_count: 1,
            },
            ValidatedBid {
                miner_uid: 3,
                bid_per_hour: 8.0,
                gpu_count: 1,
            },
        ];

        let result = engine.clear_auction("H100", &bids).await.unwrap();
        assert_eq!(result.winners.len(), 2);
        assert_eq!(result.winners[0].miner_uid, 1);
        assert_eq!(result.winners[1].miner_uid, 3);
    }

    #[tokio::test]
    async fn test_clear_auction_missing_baseline() {
        let engine = make_engine(HashMap::new());
        let bids = vec![ValidatedBid {
            miner_uid: 1,
            bid_per_hour: 5.0,
            gpu_count: 1,
        }];
        assert!(engine.clear_auction("H100", &bids).await.is_err());
    }

}

