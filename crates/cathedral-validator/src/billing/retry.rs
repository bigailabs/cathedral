use std::time::Duration;

#[derive(Debug, Clone)]
pub struct RetryPolicy {
    attempt: u32,
    max_fast_retries: u32,
    max_extended_retries: u32,
}

impl RetryPolicy {
    pub fn new(max_fast_retries: u32, max_extended_retries: u32) -> Self {
        Self {
            attempt: 0,
            max_fast_retries,
            max_extended_retries,
        }
    }

    pub fn from_config(max_retries: u32) -> Self {
        let max_fast_retries = 3;
        let max_extended_retries = max_retries.max(max_fast_retries);
        Self::new(max_fast_retries, max_extended_retries)
    }

    pub fn next_backoff(&mut self) -> Option<Duration> {
        if self.attempt < self.max_fast_retries {
            let backoff = Duration::from_millis(500 * 2_u64.pow(self.attempt));
            self.attempt += 1;
            Some(backoff)
        } else if self.attempt < self.max_extended_retries {
            let extended_schedule = [10, 30, 60, 120, 300, 600, 1800];
            let extended_index = (self.attempt - self.max_fast_retries) as usize;

            if extended_index < extended_schedule.len() {
                let backoff = Duration::from_secs(extended_schedule[extended_index]);
                self.attempt += 1;
                Some(backoff)
            } else {
                None
            }
        } else {
            None
        }
    }

    pub fn current_attempt(&self) -> u32 {
        self.attempt
    }

    pub fn reset(&mut self) {
        self.attempt = 0;
    }

    pub fn has_exceeded_max(&self) -> bool {
        self.attempt >= self.max_extended_retries
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_fast_retries() {
        let mut policy = RetryPolicy::new(3, 10);

        assert_eq!(policy.next_backoff(), Some(Duration::from_millis(500)));
        assert_eq!(policy.current_attempt(), 1);

        assert_eq!(policy.next_backoff(), Some(Duration::from_millis(1000)));
        assert_eq!(policy.current_attempt(), 2);

        assert_eq!(policy.next_backoff(), Some(Duration::from_millis(2000)));
        assert_eq!(policy.current_attempt(), 3);
    }

    #[test]
    fn test_extended_retries() {
        let mut policy = RetryPolicy::new(3, 10);

        for _ in 0..3 {
            policy.next_backoff();
        }

        assert_eq!(policy.next_backoff(), Some(Duration::from_secs(10)));
        assert_eq!(policy.next_backoff(), Some(Duration::from_secs(30)));
        assert_eq!(policy.next_backoff(), Some(Duration::from_secs(60)));
        assert_eq!(policy.next_backoff(), Some(Duration::from_secs(120)));
        assert_eq!(policy.next_backoff(), Some(Duration::from_secs(300)));
        assert_eq!(policy.next_backoff(), Some(Duration::from_secs(600)));
        assert_eq!(policy.next_backoff(), Some(Duration::from_secs(1800)));
    }

    #[test]
    fn test_max_retries_exceeded() {
        let mut policy = RetryPolicy::new(3, 5);

        for _ in 0..5 {
            assert!(policy.next_backoff().is_some());
        }

        assert!(policy.has_exceeded_max());
        assert_eq!(policy.next_backoff(), None);
    }

    #[test]
    fn test_reset() {
        let mut policy = RetryPolicy::new(3, 10);

        policy.next_backoff();
        policy.next_backoff();
        assert_eq!(policy.current_attempt(), 2);

        policy.reset();
        assert_eq!(policy.current_attempt(), 0);
        assert_eq!(policy.next_backoff(), Some(Duration::from_millis(500)));
    }

    #[test]
    fn test_from_config() {
        let policy = RetryPolicy::from_config(10);
        assert_eq!(policy.max_fast_retries, 3);
        assert_eq!(policy.max_extended_retries, 10);
    }

    #[test]
    fn test_from_config_min_retries() {
        let policy = RetryPolicy::from_config(2);
        assert_eq!(policy.max_fast_retries, 3);
        assert_eq!(policy.max_extended_retries, 3);
    }
}
