use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::{Mutex, Notify};

/// A token bucket rate limiter with no burst allowance.
///
/// This rate limiter enforces a strict requests-per-second limit.
/// Tokens are replenished one at a time at a fixed interval.
pub struct RateLimiter {
    inner: Arc<Mutex<RateLimiterInner>>,
    notify: Arc<Notify>,
    /// Interval between token replenishments
    refill_interval: Duration,
}

struct RateLimiterInner {
    /// Current number of available tokens
    tokens: u32,
    /// Maximum tokens (equal to RPS, no burst)
    max_tokens: u32,
    /// Last time a token was added
    last_refill: Instant,
}

impl RateLimiter {
    /// Create a new rate limiter with the given requests-per-second limit.
    pub fn new(rps: u32) -> Self {
        let refill_interval = Duration::from_secs_f64(1.0 / rps as f64);

        Self {
            inner: Arc::new(Mutex::new(RateLimiterInner {
                tokens: rps, // Start full
                max_tokens: rps,
                last_refill: Instant::now(),
            })),
            notify: Arc::new(Notify::new()),
            refill_interval,
        }
    }

    /// Try to acquire a token, returning immediately if one is available.
    async fn try_acquire(&self) -> bool {
        let mut inner = self.inner.lock().await;

        // Refill tokens based on elapsed time
        self.refill_tokens(&mut inner);

        if inner.tokens > 0 {
            inner.tokens -= 1;
            true
        } else {
            false
        }
    }

    /// Refill tokens based on time elapsed since last refill.
    fn refill_tokens(&self, inner: &mut RateLimiterInner) {
        let now = Instant::now();
        let elapsed = now.duration_since(inner.last_refill);

        // Calculate how many tokens to add based on elapsed time
        let tokens_to_add = (elapsed.as_secs_f64() / self.refill_interval.as_secs_f64()) as u32;

        if tokens_to_add > 0 {
            inner.tokens = (inner.tokens + tokens_to_add).min(inner.max_tokens);
            // Move last_refill forward by the time accounted for
            inner.last_refill += self.refill_interval * tokens_to_add;
        }
    }

    /// Acquire a token, waiting up to the specified timeout.
    ///
    /// Returns `Ok(())` if a token was acquired, `Err(())` if the timeout elapsed.
    pub async fn acquire(&self, timeout: Option<Duration>) -> Result<(), ()> {
        let deadline = timeout.map(|t| Instant::now() + t);

        loop {
            if self.try_acquire().await {
                return Ok(());
            }

            // Calculate how long until the next token is available
            let wait_duration = {
                let inner = self.inner.lock().await;
                let elapsed = Instant::now().duration_since(inner.last_refill);
                self.refill_interval.saturating_sub(elapsed)
            };

            // Check if we'd exceed the deadline
            if let Some(deadline) = deadline {
                let now = Instant::now();
                if now >= deadline {
                    return Err(());
                }

                let remaining = deadline - now;
                if wait_duration > remaining {
                    // Wait until deadline, then fail
                    tokio::time::sleep(remaining).await;
                    // One more try before giving up
                    if self.try_acquire().await {
                        return Ok(());
                    }
                    return Err(());
                }
            }

            // Wait for a token to become available
            tokio::select! {
                _ = tokio::time::sleep(wait_duration) => {
                    // Token might be available now, loop back to try_acquire
                }
                _ = self.notify.notified() => {
                    // Someone refunded a token, try to acquire it
                }
            }
        }
    }

    /// Refund a token (e.g., after a 429 response).
    ///
    /// This adds one token back to the bucket, up to the maximum.
    pub async fn refund(&self) {
        let mut inner = self.inner.lock().await;
        inner.tokens = (inner.tokens + 1).min(inner.max_tokens);
        drop(inner);

        // Notify any waiters that a token is available
        self.notify.notify_one();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_immediate_acquire_when_available() {
        let limiter = RateLimiter::new(5);

        // Should be able to acquire 5 tokens immediately
        for _ in 0..5 {
            assert!(limiter
                .acquire(Some(Duration::from_millis(10)))
                .await
                .is_ok());
        }

        // 6th should fail with short timeout
        assert!(limiter
            .acquire(Some(Duration::from_millis(10)))
            .await
            .is_err());
    }

    #[tokio::test]
    async fn test_refund_makes_token_available() {
        let limiter = RateLimiter::new(1);

        // Acquire the only token
        assert!(limiter
            .acquire(Some(Duration::from_millis(10)))
            .await
            .is_ok());

        // Should fail - no tokens
        assert!(limiter
            .acquire(Some(Duration::from_millis(10)))
            .await
            .is_err());

        // Refund
        limiter.refund().await;

        // Should succeed now
        assert!(limiter
            .acquire(Some(Duration::from_millis(10)))
            .await
            .is_ok());
    }

    #[tokio::test]
    async fn test_tokens_refill_over_time() {
        let limiter = RateLimiter::new(10); // 10 rps = 1 token per 100ms

        // Drain all tokens
        for _ in 0..10 {
            assert!(limiter
                .acquire(Some(Duration::from_millis(1)))
                .await
                .is_ok());
        }

        // Wait for 1 token to refill (100ms + some buffer)
        tokio::time::sleep(Duration::from_millis(150)).await;

        // Should have 1 token available
        assert!(limiter
            .acquire(Some(Duration::from_millis(1)))
            .await
            .is_ok());
    }

    #[tokio::test]
    async fn test_indefinite_wait() {
        let limiter = RateLimiter::new(10); // 10 rps

        // Drain all tokens
        for _ in 0..10 {
            assert!(limiter
                .acquire(Some(Duration::from_millis(1)))
                .await
                .is_ok());
        }

        // Wait indefinitely - should get a token after ~100ms
        let start = Instant::now();
        assert!(limiter.acquire(None).await.is_ok());
        let elapsed = start.elapsed();

        // Should have taken around 100ms (1 token per 100ms at 10 rps)
        assert!(elapsed >= Duration::from_millis(80));
        assert!(elapsed < Duration::from_millis(200));
    }
}
