// task.rs - Background task wrapper for VIP polling

use crate::vip::csv::VipDataSource;
use crate::vip::poller::VipPoller;
use std::sync::Arc;
use std::time::Duration;
use tokio::task::JoinHandle;
use tokio::time::{interval, MissedTickBehavior};

/// Background task that periodically polls the VIP data source
pub struct VipPollerTask<D: VipDataSource + 'static> {
    poller: Arc<VipPoller<D>>,
    interval: Duration,
}

impl<D: VipDataSource + 'static> VipPollerTask<D> {
    pub fn new(poller: Arc<VipPoller<D>>, poll_interval_secs: u64) -> Self {
        Self {
            poller,
            interval: Duration::from_secs(poll_interval_secs),
        }
    }

    /// Start the background polling task
    pub fn start(self) -> JoinHandle<()> {
        tokio::spawn(async move {
            let mut timer = interval(self.interval);
            timer.set_missed_tick_behavior(MissedTickBehavior::Skip);

            tracing::info!(
                interval_secs = self.interval.as_secs(),
                "VIP poller task started"
            );

            loop {
                timer.tick().await;

                let poll_start = std::time::Instant::now();
                let result = self.poller.poll_once().await;
                let poll_duration = poll_start.elapsed();

                match result {
                    Ok(stats) => {
                        if stats.created > 0 || stats.removed > 0 || stats.updated > 0 {
                            tracing::info!(
                                poll_duration_secs = poll_duration.as_secs_f64(),
                                created = stats.created,
                                updated = stats.updated,
                                removed = stats.removed,
                                "VIP poll cycle completed with changes"
                            );
                        } else {
                            tracing::debug!(
                                poll_duration_secs = poll_duration.as_secs_f64(),
                                active = stats.active_rows,
                                total = stats.total_rows,
                                "VIP poll cycle completed (no changes)"
                            );
                        }
                    }
                    Err(e) => {
                        tracing::error!(
                            poll_duration_secs = poll_duration.as_secs_f64(),
                            error = %e,
                            "VIP poll cycle failed - keeping last known good state"
                        );
                    }
                }
            }
        })
    }
}
