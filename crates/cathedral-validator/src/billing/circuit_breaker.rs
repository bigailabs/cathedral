use std::collections::VecDeque;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::RwLock;
use tracing::{debug, info, warn};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CircuitState {
    Closed,
    Open,
    HalfOpen,
}

impl CircuitState {
    pub fn as_str(&self) -> &'static str {
        match self {
            CircuitState::Closed => "closed",
            CircuitState::Open => "open",
            CircuitState::HalfOpen => "half_open",
        }
    }
}

#[derive(Clone)]
pub struct CircuitBreaker {
    state: Arc<RwLock<CircuitBreakerState>>,
    failure_threshold: u32,
    recovery_timeout: Duration,
    window_duration: Duration,
}

struct CircuitBreakerState {
    current_state: CircuitState,
    recent_failures: VecDeque<Instant>,
    last_state_change: Instant,
    consecutive_successes: u32,
}

impl CircuitBreaker {
    pub fn new(
        failure_threshold: u32,
        recovery_timeout: Duration,
        window_duration: Duration,
    ) -> Self {
        Self {
            state: Arc::new(RwLock::new(CircuitBreakerState {
                current_state: CircuitState::Closed,
                recent_failures: VecDeque::new(),
                last_state_change: Instant::now(),
                consecutive_successes: 0,
            })),
            failure_threshold,
            recovery_timeout,
            window_duration,
        }
    }

    pub fn from_config(
        failure_threshold: u32,
        recovery_timeout_secs: u64,
        window_duration_secs: u64,
    ) -> Self {
        Self::new(
            failure_threshold,
            Duration::from_secs(recovery_timeout_secs),
            Duration::from_secs(window_duration_secs),
        )
    }

    pub async fn can_proceed(&self) -> bool {
        let mut state = self.state.write().await;

        match state.current_state {
            CircuitState::Closed => true,
            CircuitState::Open => {
                if state.last_state_change.elapsed() >= self.recovery_timeout {
                    info!("Circuit breaker transitioning to HalfOpen after recovery timeout");
                    state.current_state = CircuitState::HalfOpen;
                    state.last_state_change = Instant::now();
                    state.consecutive_successes = 0;
                    true
                } else {
                    false
                }
            }
            CircuitState::HalfOpen => true,
        }
    }

    pub async fn record_success(&self) {
        let mut state = self.state.write().await;

        self.cleanup_old_failures(&mut state);

        match state.current_state {
            CircuitState::Closed => {}
            CircuitState::HalfOpen => {
                state.consecutive_successes += 1;

                if state.consecutive_successes >= 3 {
                    info!(
                        "Circuit breaker closing after {} consecutive successes",
                        state.consecutive_successes
                    );
                    state.current_state = CircuitState::Closed;
                    state.last_state_change = Instant::now();
                    state.consecutive_successes = 0;
                    state.recent_failures.clear();
                }
            }
            CircuitState::Open => {}
        }
    }

    pub async fn record_failure(&self) {
        let mut state = self.state.write().await;
        let now = Instant::now();

        state.recent_failures.push_back(now);

        self.cleanup_old_failures(&mut state);

        let failure_count = state.recent_failures.len() as u32;

        match state.current_state {
            CircuitState::Closed => {
                if failure_count >= self.failure_threshold {
                    warn!(
                        "Circuit breaker opening: {} failures in {:?} window (threshold: {})",
                        failure_count, self.window_duration, self.failure_threshold
                    );
                    state.current_state = CircuitState::Open;
                    state.last_state_change = now;
                    state.consecutive_successes = 0;
                }
            }
            CircuitState::HalfOpen => {
                warn!("Circuit breaker reopening after failure in HalfOpen state");
                state.current_state = CircuitState::Open;
                state.last_state_change = now;
                state.consecutive_successes = 0;
            }
            CircuitState::Open => {}
        }
    }

    pub async fn get_state(&self) -> CircuitState {
        let state = self.state.read().await;
        state.current_state
    }

    pub async fn get_failure_count(&self) -> u32 {
        let state = self.state.read().await;
        state.recent_failures.len() as u32
    }

    pub async fn reset(&self) {
        let mut state = self.state.write().await;
        debug!("Circuit breaker reset to Closed state");
        state.current_state = CircuitState::Closed;
        state.last_state_change = Instant::now();
        state.recent_failures.clear();
        state.consecutive_successes = 0;
    }

    fn cleanup_old_failures(&self, state: &mut CircuitBreakerState) {
        let cutoff = Instant::now() - self.window_duration;

        while let Some(&oldest) = state.recent_failures.front() {
            if oldest < cutoff {
                state.recent_failures.pop_front();
            } else {
                break;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::time::sleep;

    #[tokio::test]
    async fn test_circuit_starts_closed() {
        let breaker = CircuitBreaker::new(5, Duration::from_secs(10), Duration::from_secs(60));
        assert_eq!(breaker.get_state().await, CircuitState::Closed);
        assert!(breaker.can_proceed().await);
    }

    #[tokio::test]
    async fn test_circuit_opens_after_threshold() {
        let breaker = CircuitBreaker::new(3, Duration::from_secs(10), Duration::from_secs(60));

        assert_eq!(breaker.get_state().await, CircuitState::Closed);

        breaker.record_failure().await;
        assert_eq!(breaker.get_state().await, CircuitState::Closed);

        breaker.record_failure().await;
        assert_eq!(breaker.get_state().await, CircuitState::Closed);

        breaker.record_failure().await;
        assert_eq!(breaker.get_state().await, CircuitState::Open);
        assert!(!breaker.can_proceed().await);
    }

    #[tokio::test]
    async fn test_circuit_transitions_to_half_open() {
        let breaker = CircuitBreaker::new(2, Duration::from_millis(100), Duration::from_secs(60));

        breaker.record_failure().await;
        breaker.record_failure().await;
        assert_eq!(breaker.get_state().await, CircuitState::Open);

        sleep(Duration::from_millis(150)).await;

        assert!(breaker.can_proceed().await);
        assert_eq!(breaker.get_state().await, CircuitState::HalfOpen);
    }

    #[tokio::test]
    async fn test_circuit_closes_after_successes_in_half_open() {
        let breaker = CircuitBreaker::new(2, Duration::from_millis(100), Duration::from_secs(60));

        breaker.record_failure().await;
        breaker.record_failure().await;
        assert_eq!(breaker.get_state().await, CircuitState::Open);

        sleep(Duration::from_millis(150)).await;
        assert!(breaker.can_proceed().await);

        breaker.record_success().await;
        assert_eq!(breaker.get_state().await, CircuitState::HalfOpen);

        breaker.record_success().await;
        assert_eq!(breaker.get_state().await, CircuitState::HalfOpen);

        breaker.record_success().await;
        assert_eq!(breaker.get_state().await, CircuitState::Closed);
    }

    #[tokio::test]
    async fn test_circuit_reopens_on_failure_in_half_open() {
        let breaker = CircuitBreaker::new(2, Duration::from_millis(100), Duration::from_secs(60));

        breaker.record_failure().await;
        breaker.record_failure().await;
        assert_eq!(breaker.get_state().await, CircuitState::Open);

        sleep(Duration::from_millis(150)).await;
        assert!(breaker.can_proceed().await);
        assert_eq!(breaker.get_state().await, CircuitState::HalfOpen);

        breaker.record_failure().await;
        assert_eq!(breaker.get_state().await, CircuitState::Open);
    }

    #[tokio::test]
    async fn test_sliding_window_cleanup() {
        let breaker = CircuitBreaker::new(3, Duration::from_secs(10), Duration::from_millis(100));

        breaker.record_failure().await;
        breaker.record_failure().await;

        sleep(Duration::from_millis(150)).await;

        breaker.record_failure().await;

        assert_eq!(breaker.get_failure_count().await, 1);
        assert_eq!(breaker.get_state().await, CircuitState::Closed);
    }

    #[tokio::test]
    async fn test_reset() {
        let breaker = CircuitBreaker::new(2, Duration::from_secs(10), Duration::from_secs(60));

        breaker.record_failure().await;
        breaker.record_failure().await;
        assert_eq!(breaker.get_state().await, CircuitState::Open);

        breaker.reset().await;
        assert_eq!(breaker.get_state().await, CircuitState::Closed);
        assert_eq!(breaker.get_failure_count().await, 0);
        assert!(breaker.can_proceed().await);
    }

    #[tokio::test]
    async fn test_from_config() {
        let breaker = CircuitBreaker::from_config(5, 30, 60);
        assert_eq!(breaker.failure_threshold, 5);
        assert_eq!(breaker.recovery_timeout, Duration::from_secs(30));
        assert_eq!(breaker.window_duration, Duration::from_secs(60));
    }
}
