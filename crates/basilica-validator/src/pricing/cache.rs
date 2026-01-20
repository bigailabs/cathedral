use std::collections::HashMap;
use std::time::{Duration, Instant};

#[derive(Debug, Default)]
pub struct PriceCache {
    prices: Option<HashMap<String, f64>>,
    fetched_at: Option<Instant>,
}

impl PriceCache {
    pub fn get_if_valid(&self, ttl: Duration) -> Option<HashMap<String, f64>> {
        match (self.prices.as_ref(), self.fetched_at) {
            (Some(prices), Some(fetched_at)) if fetched_at.elapsed() <= ttl => Some(prices.clone()),
            _ => None,
        }
    }

    pub fn get_any(&self) -> Option<HashMap<String, f64>> {
        self.prices.clone()
    }

    pub fn update(&mut self, prices: HashMap<String, f64>) {
        self.prices = Some(prices);
        self.fetched_at = Some(Instant::now());
    }

    #[cfg(test)]
    pub fn update_with_timestamp(&mut self, prices: HashMap<String, f64>, fetched_at: Instant) {
        self.prices = Some(prices);
        self.fetched_at = Some(fetched_at);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_cache_validity() {
        let mut cache = PriceCache::default();
        let mut prices = HashMap::new();
        prices.insert("H100".to_string(), 2.0);

        cache.update_with_timestamp(prices.clone(), Instant::now());
        assert!(cache.get_if_valid(Duration::from_secs(10)).is_some());

        cache.update_with_timestamp(prices, Instant::now() - Duration::from_secs(20));
        assert!(cache.get_if_valid(Duration::from_secs(10)).is_none());
    }

    #[test]
    fn test_get_any() {
        let mut cache = PriceCache::default();
        assert!(cache.get_any().is_none());

        let mut prices = HashMap::new();
        prices.insert("A100".to_string(), 1.5);
        cache.update(prices.clone());

        assert_eq!(cache.get_any().unwrap(), prices);
    }
}
