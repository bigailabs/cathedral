use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use futures::StreamExt;
use kube::api::Api;
use kube::{Client, ResourceExt};
use kube_runtime::controller::{Action, Controller};
use kube_runtime::watcher;
use tokio_util::sync::CancellationToken;
use tracing::{error, info, warn};

use crate::api::{SecureCloudApi, SecureCloudClient};
use crate::config::AutoscalerConfig;
use crate::controllers::{
    AutoscalerK8sClient, KubeClient, NodePoolController, ScalingPolicyController,
};
use crate::crd::{NodePool, ScalingPolicy};
use crate::error::{AutoscalerError, Result};
use crate::health::HealthState;
use crate::leader_election::LeaderElector;
use crate::metrics::AutoscalerMetrics;
use crate::provisioner::SshProvisioner;

/// Controller runtime that manages all controllers and supporting infrastructure
pub struct ControllerRuntime {
    config: AutoscalerConfig,
    shutdown_token: CancellationToken,
}

impl ControllerRuntime {
    pub fn new(config: AutoscalerConfig) -> Self {
        Self {
            config,
            shutdown_token: CancellationToken::new(),
        }
    }

    /// Run the controller runtime
    pub async fn run(self) -> Result<()> {
        info!("Starting basilica-autoscaler controller runtime");

        // Initialize Kubernetes client
        let kube_client = KubeClient::try_default().await?;
        let k8s = Arc::new(kube_client);

        // Initialize Secure Cloud API client
        let api_client = SecureCloudClient::from_env()?;
        let api = Arc::new(api_client);

        // Initialize metrics
        let metrics = Arc::new(AutoscalerMetrics::new());

        // Initialize health state
        let health_state = HealthState::new();

        // Initialize SSH provisioner
        let provisioner = Arc::new(SshProvisioner::from_config(&self.config.ssh));

        // Get namespace from environment or default
        let namespace =
            std::env::var("AUTOSCALER_NAMESPACE").unwrap_or_else(|_| "basilica-system".to_string());

        // Initialize leader election
        let mut leader_elector =
            LeaderElector::new(self.config.leader_election.clone(), namespace.clone()).await?;

        let leader_rx = leader_elector.start().await?;
        let is_leader = leader_elector.is_leader_flag();

        // Spawn health server and track its handle
        let health_config = self.config.health.clone();
        let health_state_clone = health_state.clone();
        let health_server_handle = tokio::spawn(async move {
            crate::health::start_health_server(
                &health_config.host,
                health_config.port,
                health_state_clone,
            )
            .await
        });

        // Spawn metrics server and track its handle
        let metrics_config = self.config.metrics.clone();
        let metrics_clone = Arc::clone(&metrics);
        let metrics_server_handle = tokio::spawn(async move {
            crate::metrics::start_metrics_server(
                &metrics_config.host,
                metrics_config.port,
                metrics_clone,
            )
            .await
        });

        // Create raw kube client for controller APIs
        let raw_client = Client::try_default().await?;

        // Set up controller context
        let reconcile_config = self.config.reconcile.clone();

        // Create NodePool controller
        let node_pool_ctrl = NodePoolController::new(
            Arc::clone(&k8s),
            Arc::clone(&api),
            Arc::clone(&provisioner),
            Arc::clone(&metrics),
            self.config.network_validation.clone(),
        );

        // Create ScalingPolicy controller
        let scaling_policy_ctrl =
            ScalingPolicyController::new(Arc::clone(&k8s), Arc::clone(&api), Arc::clone(&metrics));

        // Set up APIs
        let np_api: Api<NodePool> = Api::namespaced(raw_client.clone(), &namespace);
        let sp_api: Api<ScalingPolicy> = Api::namespaced(raw_client.clone(), &namespace);

        // Context for controllers
        #[derive(Clone)]
        struct ControllerContext<K, A, P>
        where
            K: AutoscalerK8sClient + Clone,
            A: SecureCloudApi + Clone,
            P: crate::provisioner::NodeProvisioner + Clone,
        {
            ctrl: NodePoolController<K, A, P>,
            success_interval: Duration,
            error_interval: Duration,
            is_leader: Arc<AtomicBool>,
        }

        #[derive(Clone)]
        struct ScalingContext<K, A>
        where
            K: AutoscalerK8sClient + Clone,
            A: SecureCloudApi + Clone,
        {
            ctrl: ScalingPolicyController<K, A>,
            success_interval: Duration,
            error_interval: Duration,
            is_leader: Arc<AtomicBool>,
        }

        let np_ctx = Arc::new(ControllerContext {
            ctrl: node_pool_ctrl,
            success_interval: reconcile_config.success_interval,
            error_interval: reconcile_config.error_interval,
            is_leader: Arc::clone(&is_leader),
        });

        let sp_ctx = Arc::new(ScalingContext {
            ctrl: scaling_policy_ctrl,
            success_interval: reconcile_config.success_interval,
            error_interval: reconcile_config.error_interval,
            is_leader: Arc::clone(&is_leader),
        });

        // Reconcile error wrapper
        #[derive(Debug)]
        struct ReconcileError(AutoscalerError);
        impl std::fmt::Display for ReconcileError {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                write!(f, "{}", self.0)
            }
        }
        impl std::error::Error for ReconcileError {}

        // NodePool reconcile function
        async fn reconcile_node_pool<K, A, P>(
            obj: Arc<NodePool>,
            ctx: Arc<ControllerContext<K, A, P>>,
        ) -> std::result::Result<Action, ReconcileError>
        where
            K: AutoscalerK8sClient + Clone + 'static,
            A: SecureCloudApi + Clone + 'static,
            P: crate::provisioner::NodeProvisioner + Clone + 'static,
        {
            // Skip if not leader
            if !ctx.is_leader.load(Ordering::SeqCst) {
                return Ok(Action::requeue(ctx.success_interval));
            }

            let ns = obj.namespace().unwrap_or_else(|| "default".into());
            if let Err(e) = ctx.ctrl.reconcile(&ns, &obj).await {
                return Err(ReconcileError(e));
            }
            Ok(Action::requeue(ctx.success_interval))
        }

        fn error_policy_node_pool<K, A, P>(
            _obj: Arc<NodePool>,
            err: &ReconcileError,
            ctx: Arc<ControllerContext<K, A, P>>,
        ) -> Action
        where
            K: AutoscalerK8sClient + Clone + 'static,
            A: SecureCloudApi + Clone + 'static,
            P: crate::provisioner::NodeProvisioner + Clone + 'static,
        {
            warn!(error = %err, "NodePool reconcile error");
            Action::requeue(ctx.error_interval)
        }

        // ScalingPolicy reconcile function
        async fn reconcile_scaling_policy<K, A>(
            obj: Arc<ScalingPolicy>,
            ctx: Arc<ScalingContext<K, A>>,
        ) -> std::result::Result<Action, ReconcileError>
        where
            K: AutoscalerK8sClient + Clone + 'static,
            A: SecureCloudApi + Clone + 'static,
        {
            // Skip if not leader
            if !ctx.is_leader.load(Ordering::SeqCst) {
                return Ok(Action::requeue(ctx.success_interval));
            }

            let ns = obj.namespace().unwrap_or_else(|| "default".into());
            if let Err(e) = ctx.ctrl.reconcile(&ns, &obj).await {
                return Err(ReconcileError(e));
            }
            Ok(Action::requeue(ctx.success_interval))
        }

        fn error_policy_scaling_policy<K, A>(
            _obj: Arc<ScalingPolicy>,
            err: &ReconcileError,
            ctx: Arc<ScalingContext<K, A>>,
        ) -> Action
        where
            K: AutoscalerK8sClient + Clone + 'static,
            A: SecureCloudApi + Clone + 'static,
        {
            warn!(error = %err, "ScalingPolicy reconcile error");
            Action::requeue(ctx.error_interval)
        }

        // Start controllers
        let node_pools = Controller::new(np_api, watcher::Config::default().any_semantic())
            .run(reconcile_node_pool, error_policy_node_pool, np_ctx)
            .for_each(|res| async move {
                match res {
                    Ok(_) => {}
                    Err(e) => error!(error = %e, "NodePool controller stream error"),
                }
            });

        let scaling_policies = Controller::new(sp_api, watcher::Config::default().any_semantic())
            .run(
                reconcile_scaling_policy,
                error_policy_scaling_policy,
                sp_ctx,
            )
            .for_each(|res| async move {
                match res {
                    Ok(_) => {}
                    Err(e) => error!(error = %e, "ScalingPolicy controller stream error"),
                }
            });

        // Mark as ready
        health_state.set_ready(true).await;
        info!("Controllers started, autoscaler is ready");

        // Create a task to watch leader status and detect leader election failure
        let health_state_leader = health_state.clone();
        let metrics_leader = Arc::clone(&metrics);
        let mut leader_rx_main = leader_rx.clone();
        let leader_watch_task = async move {
            while leader_rx_main.changed().await.is_ok() {
                let is_leader = *leader_rx_main.borrow();
                health_state_leader.set_leader(is_leader).await;
                metrics_leader.set_is_leader(is_leader);
                if is_leader {
                    info!("Became leader");
                    metrics_leader.record_leader_transition();
                }
            }
            error!("Leader election channel closed, election loop has exited");
        };

        // Run controllers until shutdown
        let shutdown_token = self.shutdown_token.clone();
        tokio::select! {
            _ = shutdown_token.cancelled() => {
                info!("Shutdown signal received");
            }
            _ = futures::future::join(node_pools, scaling_policies) => {
                error!("Controllers exited unexpectedly");
            }
            _ = leader_watch_task => {
                error!("Leader election failed fatally, initiating shutdown");
            }
            result = health_server_handle => {
                match result {
                    Ok(Ok(())) => warn!("Health server exited normally"),
                    Ok(Err(e)) => error!(error = %e, "Health server failed"),
                    Err(e) => error!(error = %e, "Health server task panicked"),
                }
            }
            result = metrics_server_handle => {
                match result {
                    Ok(Ok(())) => warn!("Metrics server exited normally"),
                    Ok(Err(e)) => error!(error = %e, "Metrics server failed"),
                    Err(e) => error!(error = %e, "Metrics server task panicked"),
                }
            }
            _ = tokio::signal::ctrl_c() => {
                info!("Received SIGINT, shutting down");
            }
        }

        // Graceful shutdown
        info!("Initiating graceful shutdown");
        leader_elector.stop().await;
        health_state.set_ready(false).await;

        info!("Shutdown complete");
        Ok(())
    }

    /// Signal shutdown
    pub fn shutdown(&self) {
        self.shutdown_token.cancel();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn runtime_creation() {
        let config = AutoscalerConfig::from_env();
        let runtime = ControllerRuntime::new(config);
        assert!(!runtime.shutdown_token.is_cancelled());
    }
}
