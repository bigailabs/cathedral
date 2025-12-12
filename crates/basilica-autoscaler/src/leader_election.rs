use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use k8s_openapi::api::coordination::v1::Lease;
use kube::api::{Api, PostParams};
use kube::Client;
use tokio::sync::watch;
use tracing::{debug, error, info, warn};

use crate::config::LeaderElectionConfig;
use crate::error::Result;

/// Leader election using Kubernetes Lease objects
pub struct LeaderElector {
    client: Client,
    config: LeaderElectionConfig,
    namespace: String,
    identity: String,
    is_leader: Arc<AtomicBool>,
    shutdown_tx: Option<watch::Sender<bool>>,
    leader_tx: Option<watch::Sender<bool>>,
}

impl LeaderElector {
    pub async fn new(config: LeaderElectionConfig, namespace: String) -> Result<Self> {
        let client = Client::try_default().await?;

        // Generate unique identity for this instance
        let identity = std::env::var("POD_NAME")
            .or_else(|_| std::env::var("HOSTNAME"))
            .unwrap_or_else(|_| format!("autoscaler-{}", uuid::Uuid::new_v4()));

        Ok(Self {
            client,
            config,
            namespace,
            identity,
            is_leader: Arc::new(AtomicBool::new(false)),
            shutdown_tx: None,
            leader_tx: None,
        })
    }

    /// Check if this instance is currently the leader
    pub fn is_leader(&self) -> bool {
        self.is_leader.load(Ordering::SeqCst)
    }

    /// Get a clone of the is_leader flag for sharing
    pub fn is_leader_flag(&self) -> Arc<AtomicBool> {
        Arc::clone(&self.is_leader)
    }

    /// Start the leader election loop
    pub async fn start(&mut self) -> Result<watch::Receiver<bool>> {
        // Guard: prevent multiple start() calls from spawning competing loops
        if self.shutdown_tx.is_some() {
            return Err(crate::error::AutoscalerError::LeaderElection(
                "LeaderElector already started".to_string(),
            ));
        }

        if !self.config.enabled {
            info!("Leader election disabled, assuming leader");
            self.is_leader.store(true, Ordering::SeqCst);
            let (leader_tx, leader_rx) = watch::channel(true);
            self.leader_tx = Some(leader_tx);
            return Ok(leader_rx);
        }

        let (shutdown_tx, shutdown_rx) = watch::channel(false);
        let (leader_tx, leader_rx) = watch::channel(false);
        self.shutdown_tx = Some(shutdown_tx);
        self.leader_tx = Some(leader_tx.clone());

        let client = self.client.clone();
        let config = self.config.clone();
        let namespace = self.namespace.clone();
        let identity = self.identity.clone();
        let is_leader = Arc::clone(&self.is_leader);

        tokio::spawn(async move {
            leader_election_loop(
                client,
                config,
                namespace,
                identity,
                is_leader,
                leader_tx,
                shutdown_rx,
            )
            .await;
        });

        info!(
            identity = %self.identity,
            lease = %self.config.lease_name,
            "Leader election started"
        );

        Ok(leader_rx)
    }

    /// Stop leader election and release leadership
    pub async fn stop(&mut self) {
        if let Some(tx) = self.shutdown_tx.take() {
            let _ = tx.send(true);
        }
        self.is_leader.store(false, Ordering::SeqCst);
        if let Some(tx) = self.leader_tx.take() {
            let _ = tx.send(false);
        }
        info!("Leader election stopped");
    }
}

async fn leader_election_loop(
    client: Client,
    config: LeaderElectionConfig,
    namespace: String,
    identity: String,
    is_leader: Arc<AtomicBool>,
    leader_tx: watch::Sender<bool>,
    mut shutdown_rx: watch::Receiver<bool>,
) {
    let api: Api<Lease> = Api::namespaced(client, &namespace);
    let mut consecutive_failures = 0u32;

    loop {
        tokio::select! {
            res = shutdown_rx.changed() => {
                if res.is_err() {
                    // Sender dropped without calling stop() - exit cleanly
                    info!("Shutdown channel closed; exiting leader election loop");
                    is_leader.store(false, Ordering::SeqCst);
                    let _ = leader_tx.send(false);
                    break;
                }
                if *shutdown_rx.borrow() {
                    info!("Shutdown signal received, releasing leadership");
                    if is_leader.load(Ordering::SeqCst) {
                        let _ = release_lease(&api, &config.lease_name, &identity).await;
                    }
                    is_leader.store(false, Ordering::SeqCst);
                    let _ = leader_tx.send(false);
                    break;
                }
            }
            _ = tokio::time::sleep(config.retry_period) => {
                match try_acquire_or_renew(&api, &config, &identity).await {
                    Ok(acquired) => {
                        consecutive_failures = 0;
                        let was_leader = is_leader.load(Ordering::SeqCst);
                        is_leader.store(acquired, Ordering::SeqCst);

                        if acquired && !was_leader {
                            info!(identity = %identity, "Acquired leadership");
                            let _ = leader_tx.send(true);
                        } else if !acquired && was_leader {
                            warn!(identity = %identity, "Lost leadership");
                            let _ = leader_tx.send(false);
                        }
                    }
                    Err(e) => {
                        consecutive_failures += 1;
                        warn!(
                            error = %e,
                            failures = %consecutive_failures,
                            max_failures = %config.max_consecutive_failures,
                            "Failed to acquire/renew lease"
                        );

                        if consecutive_failures >= config.max_consecutive_failures {
                            error!(
                                failures = %consecutive_failures,
                                "Max consecutive failures reached, exiting leader election loop"
                            );
                            is_leader.store(false, Ordering::SeqCst);
                            let _ = leader_tx.send(false);
                            break;
                        }
                    }
                }
            }
        }
    }
}

async fn try_acquire_or_renew(
    api: &Api<Lease>,
    config: &LeaderElectionConfig,
    identity: &str,
) -> Result<bool> {
    let now = chrono::Utc::now();

    // Try to get existing lease
    match api.get(&config.lease_name).await {
        Ok(lease) => {
            let spec = lease.spec.as_ref();
            let holder = spec.and_then(|s| s.holder_identity.as_ref());
            let renew_time = spec.and_then(|s| s.renew_time.as_ref());
            let lease_duration = spec.and_then(|s| s.lease_duration_seconds).unwrap_or(15) as i64;

            // Check if we hold the lease
            if holder == Some(&identity.to_string()) {
                // Renew the lease
                debug!(identity = %identity, "Renewing lease");
                renew_lease(api, &config.lease_name, identity, config).await?;
                return Ok(true);
            }

            // Check if lease has expired
            if let Some(renew) = renew_time {
                // MicroTime.0 is already DateTime<Utc>
                let elapsed = now.signed_duration_since(renew.0);

                if elapsed.num_seconds() > lease_duration {
                    // Lease expired, try to acquire
                    info!(identity = %identity, "Lease expired, attempting to acquire");
                    acquire_lease(api, &config.lease_name, identity, config).await?;
                    return Ok(true);
                }
            }

            // Lease held by someone else
            debug!(
                identity = %identity,
                holder = ?holder,
                "Lease held by another instance"
            );
            Ok(false)
        }
        Err(kube::Error::Api(ae)) if ae.code == 404 => {
            // Lease doesn't exist, create it
            info!(identity = %identity, "Creating new lease");
            create_lease(api, &config.lease_name, identity, config).await?;
            Ok(true)
        }
        Err(e) => Err(e.into()),
    }
}

async fn create_lease(
    api: &Api<Lease>,
    name: &str,
    identity: &str,
    config: &LeaderElectionConfig,
) -> Result<()> {
    let now = chrono::Utc::now().to_rfc3339();
    let lease = serde_json::json!({
        "apiVersion": "coordination.k8s.io/v1",
        "kind": "Lease",
        "metadata": {
            "name": name
        },
        "spec": {
            "holderIdentity": identity,
            "leaseDurationSeconds": config.lease_duration.as_secs(),
            "acquireTime": now,
            "renewTime": now
        }
    });

    let lease: Lease = serde_json::from_value(lease)?;
    api.create(&PostParams::default(), &lease).await?;
    Ok(())
}

async fn acquire_lease(
    api: &Api<Lease>,
    name: &str,
    identity: &str,
    config: &LeaderElectionConfig,
) -> Result<()> {
    // Get current lease to obtain resourceVersion for optimistic concurrency
    let current = api.get(name).await?;
    let resource_version = current.metadata.resource_version.clone().ok_or_else(|| {
        crate::error::AutoscalerError::LeaderElection("Lease missing resourceVersion".to_string())
    })?;

    // Increment lease transitions counter
    let transitions = current
        .spec
        .as_ref()
        .and_then(|s| s.lease_transitions)
        .unwrap_or(0)
        + 1;

    let now = chrono::Utc::now().to_rfc3339();

    // Build the updated lease object with resourceVersion for replace
    let lease = serde_json::json!({
        "apiVersion": "coordination.k8s.io/v1",
        "kind": "Lease",
        "metadata": {
            "name": name,
            "resourceVersion": resource_version
        },
        "spec": {
            "holderIdentity": identity,
            "leaseDurationSeconds": config.lease_duration.as_secs(),
            "acquireTime": now,
            "renewTime": now,
            "leaseTransitions": transitions
        }
    });

    let lease: Lease = serde_json::from_value(lease)?;
    // Use replace instead of patch to enforce optimistic concurrency via resourceVersion
    api.replace(name, &PostParams::default(), &lease).await?;
    Ok(())
}

async fn renew_lease(
    api: &Api<Lease>,
    name: &str,
    identity: &str,
    config: &LeaderElectionConfig,
) -> Result<()> {
    // Get current lease to obtain resourceVersion for optimistic concurrency
    let current = api.get(name).await?;
    let resource_version = current.metadata.resource_version.clone().ok_or_else(|| {
        crate::error::AutoscalerError::LeaderElection(
            "Lease missing resourceVersion for renew".to_string(),
        )
    })?;

    // Verify we still hold the lease before renewing
    let holder = current
        .spec
        .as_ref()
        .and_then(|s| s.holder_identity.as_ref());
    if holder != Some(&identity.to_string()) {
        return Err(crate::error::AutoscalerError::LeaderElection(
            "Lease held by another instance, cannot renew".to_string(),
        ));
    }

    let now = chrono::Utc::now().to_rfc3339();

    // Build the updated lease object with resourceVersion for replace
    let lease = serde_json::json!({
        "apiVersion": "coordination.k8s.io/v1",
        "kind": "Lease",
        "metadata": {
            "name": name,
            "resourceVersion": resource_version
        },
        "spec": {
            "holderIdentity": identity,
            "leaseDurationSeconds": config.lease_duration.as_secs(),
            "renewTime": now
        }
    });

    let lease: Lease = serde_json::from_value(lease)?;
    // Use replace instead of patch to enforce optimistic concurrency via resourceVersion
    api.replace(name, &PostParams::default(), &lease).await?;
    Ok(())
}

async fn release_lease(api: &Api<Lease>, name: &str, identity: &str) -> Result<()> {
    // Get the lease to verify ownership and obtain resourceVersion for optimistic concurrency
    let current = match api.get(name).await {
        Ok(lease) => lease,
        Err(_) => return Ok(()), // Lease doesn't exist, nothing to release
    };

    let holder = current
        .spec
        .as_ref()
        .and_then(|s| s.holder_identity.as_ref());
    if holder != Some(&identity.to_string()) {
        // We don't hold the lease, nothing to release
        return Ok(());
    }

    let resource_version = match current.metadata.resource_version.clone() {
        Some(rv) => rv,
        None => return Ok(()), // Cannot release without resourceVersion
    };

    // Build the updated lease object with resourceVersion for replace
    let lease = serde_json::json!({
        "apiVersion": "coordination.k8s.io/v1",
        "kind": "Lease",
        "metadata": {
            "name": name,
            "resourceVersion": resource_version
        },
        "spec": {
            "holderIdentity": serde_json::Value::Null
        }
    });

    let lease: Lease = serde_json::from_value(lease)?;
    // Use replace instead of patch to enforce optimistic concurrency via resourceVersion
    match api.replace(name, &PostParams::default(), &lease).await {
        Ok(_) => {
            info!(identity = %identity, "Released lease");
            Ok(())
        }
        Err(kube::Error::Api(ae)) if ae.code == 409 => {
            // Conflict - lease was modified, but we're releasing anyway so this is fine
            debug!("Lease conflict during release, ignoring");
            Ok(())
        }
        Err(e) => Err(e.into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn is_leader_flag_is_atomic() {
        let flag = Arc::new(AtomicBool::new(false));
        let flag_clone = Arc::clone(&flag);

        flag.store(true, Ordering::SeqCst);
        assert!(flag_clone.load(Ordering::SeqCst));
    }
}
