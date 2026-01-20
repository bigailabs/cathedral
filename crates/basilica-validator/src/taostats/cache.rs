use std::time::{Duration, Instant};

#[derive(Debug, Default)]
pub struct TaoPriceCache {
    price_usd: Option<f64>,
    fetched_at: Option<Instant>,
}

impl TaoPriceCache {
    pub fn get_if_valid(&self, ttl: Duration) -> Option<f64> {
        match (self.price_usd, self.fetched_at) {
            (Some(price), Some(fetched_at)) if fetched_at.elapsed() <= ttl => Some(price),
            _ => None,
        }
    }

    pub fn get_any(&self) -> Option<f64> {
        self.price_usd
    }

    pub fn update(&mut self, price_usd: f64) {
        self.price_usd = Some(price_usd);
        self.fetched_at = Some(Instant::now());
    }

    #[cfg(test)]
    pub fn update_with_timestamp(&mut self, price_usd: f64, fetched_at: Instant) {
        self.price_usd = Some(price_usd);
        self.fetched_at = Some(fetched_at);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_cache_validity() {
        let mut cache = TaoPriceCache::default();
        cache.update_with_timestamp(400.0, Instant::now());
        assert!(cache.get_if_valid(Duration::from_secs(10)).is_some());

        cache.update_with_timestamp(400.0, Instant::now() - Duration::from_secs(20));
        assert!(cache.get_if_valid(Duration::from_secs(10)).is_none());
    }

    #[test]
    fn test_get_any() {
        let mut cache = TaoPriceCache::default();
        assert!(cache.get_any().is_none());

        cache.update(321.0);
        assert_eq!(cache.get_any().unwrap(), 321.0);
    }
}
