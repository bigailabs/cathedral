use std::sync::Arc;

use chrono::Utc;
use kube::ResourceExt;
use tracing::{debug, error, info, warn};

use crate::api::SecureCloudApi;
use crate::config::{NetworkValidationConfig, PhaseTimeouts};
use crate::crd::{
    NodeManagedBy, NodePool, NodePoolCondition, NodePoolPhase, NodePoolStatus, WireGuardStatus,
    FINALIZER,
};
use crate::error::{AutoscalerError, Result};
use crate::metrics::AutoscalerMetrics;
use crate::provisioner::NodeProvisioner;

use super::k8s_client::AutoscalerK8sClient;

/// NodePool controller handles the lifecycle of GPU node pools
pub struct NodePoolController<K, A, P>
where
    K: AutoscalerK8sClient,
    A: SecureCloudApi,
    P: NodeProvisioner,
{
    k8s: Arc<K>,
    api: Arc<A>,
    provisioner: Arc<P>,
    metrics: Arc<AutoscalerMetrics>,
    network_validation_config: NetworkValidationConfig,
}

impl<K, A, P> Clone for NodePoolController<K, A, P>
where
    K: AutoscalerK8sClient,
    A: SecureCloudApi,
    P: NodeProvisioner,
{
    fn clone(&self) -> Self {
        Self {
            k8s: Arc::clone(&self.k8s),
            api: Arc::clone(&self.api),
            provisioner: Arc::clone(&self.provisioner),
            metrics: Arc::clone(&self.metrics),
            network_validation_config: self.network_validation_config.clone(),
        }
    }
}

impl<K, A, P> NodePoolController<K, A, P>
where
    K: AutoscalerK8sClient,
    A: SecureCloudApi,
    P: NodeProvisioner,
{
    pub fn new(
        k8s: Arc<K>,
        api: Arc<A>,
        provisioner: Arc<P>,
        metrics: Arc<AutoscalerMetrics>,
        network_validation_config: NetworkValidationConfig,
    ) -> Self {
        Self {
            k8s,
            api,
            provisioner,
            metrics,
            network_validation_config,
        }
    }

    /// Main reconciliation entry point
    pub async fn reconcile(&self, ns: &str, pool: &NodePool) -> Result<()> {
        let name = pool.name_any();
        info!(namespace = %ns, pool = %name, "Reconciling NodePool");

        // Handle deletion
        if pool.metadata.deletion_timestamp.is_some() {
            return self.handle_deletion(ns, pool).await;
        }

        // Ensure finalizer
        if !has_finalizer(pool) {
            self.k8s.add_node_pool_finalizer(ns, &name).await?;
            return Ok(());
        }

        // Get or initialize status
        let status = pool.status.clone().unwrap_or_default();
        let phase = status.phase.clone().unwrap_or(NodePoolPhase::Pending);

        // Check phase timeout
        if let Some(entered_at) = &status.phase_entered_at {
            let elapsed = Utc::now().signed_duration_since(*entered_at);
            let timeout = phase_timeout(&phase);
            if elapsed.num_seconds() > timeout as i64 {
                // Check if cleanup is already in progress
                if status.cleanup_in_progress {
                    debug!(pool = %name, "Cleanup already in progress, waiting");
                    return Ok(());
                }

                // Set cleanup flag and perform cleanup
                let mut cleanup_status = status.clone();
                cleanup_status.cleanup_in_progress = true;
                self.k8s
                    .update_node_pool_status(ns, &name, cleanup_status.clone())
                    .await?;

                // Perform phase-specific cleanup
                if let Err(e) = self
                    .perform_timeout_cleanup(ns, pool, &cleanup_status, &phase)
                    .await
                {
                    warn!(pool = %name, error = %e, "Cleanup failed during phase timeout");
                }

                // Clear cleanup flag and transition to failed
                cleanup_status.cleanup_in_progress = false;
                return self
                    .transition_to_failed(
                        ns,
                        &name,
                        cleanup_status,
                        &format!(
                            "Phase {:?} timed out after {}s (limit: {}s)",
                            phase,
                            elapsed.num_seconds(),
                            timeout
                        ),
                    )
                    .await;
            }
        }

        // Dispatch based on phase
        match phase {
            NodePoolPhase::Pending => self.handle_pending(ns, pool, status).await,
            NodePoolPhase::Provisioning => self.handle_provisioning(ns, pool, status).await,
            NodePoolPhase::Configuring => self.handle_configuring(ns, pool, status).await,
            NodePoolPhase::InstallingWireGuard => {
                self.handle_installing_wireguard(ns, pool, status).await
            }
            NodePoolPhase::ValidatingNetwork => {
                self.handle_validating_network(ns, pool, status).await
            }
            NodePoolPhase::JoiningCluster => self.handle_joining_cluster(ns, pool, status).await,
            NodePoolPhase::WaitingForNode => self.handle_waiting_for_node(ns, pool, status).await,
            NodePoolPhase::Ready => self.handle_ready(ns, pool, status).await,
            NodePoolPhase::Unhealthy => self.handle_unhealthy(ns, pool, status).await,
            NodePoolPhase::Draining => self.handle_draining(ns, pool, status).await,
            NodePoolPhase::Terminating => self.handle_terminating(ns, pool, status).await,
            NodePoolPhase::Failed => self.handle_failed(ns, pool, status).await,
            NodePoolPhase::Deleted => Ok(()),
        }
    }

    async fn handle_deletion(&self, ns: &str, pool: &NodePool) -> Result<()> {
        let name = pool.name_any();
        info!(namespace = %ns, pool = %name, "Handling deletion");

        let status = pool.status.clone().unwrap_or_default();

        // Clean up resources based on current state
        if let Some(node_name) = &status.node_name {
            if let Err(e) = self.k8s.delete_node(node_name).await {
                warn!(node = %node_name, error = %e, "Failed to delete K8s node");
            }
        }

        if let Some(rental_id) = &status.rental_id {
            if let Err(e) = self.api.stop_rental(rental_id).await {
                warn!(rental = %rental_id, error = %e, "Failed to stop rental");
            }
        }

        // Note: node_id is in spec.node_id, not status
        if let Some(node_id) = &pool.spec.node_id {
            if let Err(e) = self.api.deregister_node(node_id).await {
                warn!(node = %node_id, error = %e, "Failed to deregister node");
            }
        }

        // Remove finalizer
        self.k8s.remove_node_pool_finalizer(ns, &name).await?;
        self.metrics.record_node_pool_deleted(&name);
        info!(namespace = %ns, pool = %name, "Deletion complete");
        Ok(())
    }

    async fn handle_pending(
        &self,
        ns: &str,
        pool: &NodePool,
        status: NodePoolStatus,
    ) -> Result<()> {
        let name = pool.name_any();
        info!(namespace = %ns, pool = %name, "Starting provisioning");

        // Check for existing node if node_id is specified
        if let Some(node_id) = &pool.spec.node_id {
            if let Some(existing_node) = self.k8s.find_node_by_node_id(node_id).await? {
                if pool.spec.adopt_existing {
                    info!(pool = %name, node_id = %node_id, "Adopting existing node");
                    return self
                        .adopt_existing_node(ns, pool, status, &existing_node)
                        .await;
                }
                return Err(AutoscalerError::NodeAlreadyExists {
                    node_id: node_id.clone(),
                    hint: "Set adoptExisting=true to adopt this node".to_string(),
                });
            }
        }

        // For dynamic mode, start rental
        if pool.spec.secure_cloud.is_some() {
            self.transition_phase(ns, &name, status, NodePoolPhase::Provisioning)
                .await
        } else {
            // Manual mode - skip to configuring
            self.transition_phase(ns, &name, status, NodePoolPhase::Configuring)
                .await
        }
    }

    async fn adopt_existing_node(
        &self,
        ns: &str,
        pool: &NodePool,
        mut status: NodePoolStatus,
        node: &k8s_openapi::api::core::v1::Node,
    ) -> Result<()> {
        use kube::ResourceExt;
        let name = pool.name_any();
        let node_name = node.name_any();

        // Verify datacenter label matches if specified
        if let Some(expected_dc) = &pool.spec.datacenter_id {
            let labels = node.metadata.labels.as_ref();
            let actual_dc = labels.and_then(|l| l.get("basilica.ai/datacenter"));
            if actual_dc.map(|s| s.as_str()) != Some(expected_dc.as_str()) {
                return Err(AutoscalerError::AdoptionFailed {
                    reason: format!(
                        "Datacenter mismatch: expected {}, found {:?}",
                        expected_dc, actual_dc
                    ),
                });
            }
        }

        // Add autoscaler-managed label
        let mut labels = std::collections::BTreeMap::new();
        labels.insert(
            "basilica.ai/autoscaler-managed".to_string(),
            "true".to_string(),
        );
        labels.insert("basilica.ai/nodepool".to_string(), name.clone());
        self.k8s.add_node_labels(&node_name, &labels).await?;

        // Update status with existing node info
        status.node_name = Some(node_name.clone());
        status.node_uid = node.metadata.uid.clone();
        status.managed_by = Some(NodeManagedBy::Autoscaler);
        status.joined_at = Some(Utc::now());

        // Extract WireGuard IP from node annotations if present
        if let Some(annotations) = &node.metadata.annotations {
            if let Some(wg_ip) = annotations.get("basilica.ai/wireguard-ip") {
                status.wireguard = Some(WireGuardStatus {
                    node_ip: Some(wg_ip.clone()),
                    public_key: annotations.get("basilica.ai/wireguard-pubkey").cloned(),
                    endpoint: None,
                });
                status.internal_ip = Some(wg_ip.clone());
            }
        }

        info!(pool = %name, node = %node_name, "Successfully adopted existing node");
        self.transition_phase(ns, &name, status, NodePoolPhase::Ready)
            .await
    }

    async fn handle_provisioning(
        &self,
        ns: &str,
        pool: &NodePool,
        mut status: NodePoolStatus,
    ) -> Result<()> {
        let name = pool.name_any();
        let sc = pool.spec.secure_cloud.as_ref().ok_or_else(|| {
            AutoscalerError::InvalidConfiguration("Missing secure_cloud config".to_string())
        })?;

        // Check if we already have a rental and node_id persisted
        if status.rental_id.is_some() && status.node_id.is_some() {
            debug!(pool = %name, "Rental already exists, checking status");
            return self
                .transition_phase(ns, &name, status, NodePoolPhase::Configuring)
                .await;
        }

        // Start new rental if needed
        let external_ip = if let Some(ip) = &status.external_ip {
            ip.clone()
        } else {
            info!(pool = %name, offering = %sc.offering_id, "Starting rental");
            let rental = self
                .api
                .start_rental(&sc.offering_id, &sc.ssh_key_id)
                .await?;

            status.rental_id = Some(rental.rental_id.clone());
            status.external_ip = Some(rental.external_ip.clone());
            status.provider = Some(rental.provider.clone());
            status.provider_id = Some(rental.provider_id.clone());
            self.metrics.record_rental_started(&name, &rental.provider);

            rental.external_ip
        };

        // Generate deterministic node_id based on external IP and persist it
        let node_id = resolve_node_id(pool, &status, Some(&external_ip))?;
        status.node_id = Some(node_id.clone());

        // Register node with API
        let datacenter_id = pool
            .spec
            .datacenter_id
            .clone()
            .unwrap_or_else(|| "default".to_string());

        let reg_request = crate::api::NodeRegistrationRequest {
            node_id: node_id.clone(),
            datacenter_id,
            managed_by: NodeManagedBy::Autoscaler.as_str().to_string(),
            rental_id: status.rental_id.clone(),
            external_ip: Some(external_ip),
            gpu_info: None,
        };

        let reg_response = self.api.register_node(reg_request).await?;
        status.wireguard = Some(WireGuardStatus {
            node_ip: Some(reg_response.wireguard.node_ip),
            public_key: None,
            endpoint: None,
        });
        status.internal_ip = status.wireguard.as_ref().and_then(|w| w.node_ip.clone());
        status.provisioned_at = Some(Utc::now());

        self.transition_phase(ns, &name, status, NodePoolPhase::Configuring)
            .await
    }

    async fn handle_configuring(
        &self,
        ns: &str,
        pool: &NodePool,
        status: NodePoolStatus,
    ) -> Result<()> {
        let name = pool.name_any();
        info!(pool = %name, "Configuring node via SSH");

        let ssh_config = self.get_ssh_config(ns, pool).await?;
        let (host, port) = self.get_ssh_endpoint(pool, &status)?;

        // Execute preJoinScript if specified
        if let Some(pre_script) = &pool.spec.lifecycle.pre_join_script {
            if !pre_script.is_empty() {
                info!(pool = %name, "Executing preJoinScript");
                self.provisioner
                    .execute_lifecycle_script(&host, port, &ssh_config, pre_script, "preJoinScript")
                    .await?;
            }
        }

        // Configure base system
        self.provisioner
            .configure_base_system(&host, port, &ssh_config)
            .await?;

        self.transition_phase(ns, &name, status, NodePoolPhase::InstallingWireGuard)
            .await
    }

    async fn handle_installing_wireguard(
        &self,
        ns: &str,
        pool: &NodePool,
        mut status: NodePoolStatus,
    ) -> Result<()> {
        let name = pool.name_any();
        info!(pool = %name, "Installing WireGuard");

        let node_id = pool.spec.node_id.clone().ok_or_else(|| {
            AutoscalerError::InvalidConfiguration("Missing node_id in spec".to_string())
        })?;

        let ssh_config = self.get_ssh_config(ns, pool).await?;
        let (host, port) = self.get_ssh_endpoint(pool, &status)?;

        let wg_status = status.wireguard.clone().unwrap_or_default();
        let node_ip = wg_status.node_ip.clone().ok_or_else(|| {
            AutoscalerError::InvalidConfiguration("Missing WireGuard node_ip".to_string())
        })?;

        // Install WireGuard and get public key
        let public_key = self
            .provisioner
            .install_wireguard(&host, port, &ssh_config, &node_ip, &pool.spec.wireguard)
            .await?;

        // Register public key with API
        self.api
            .register_wireguard_key(&node_id, &public_key)
            .await?;

        // Get peers and configure
        let peers = self.api.get_peers(&node_id).await?;
        self.provisioner
            .configure_wireguard_peers(&host, port, &ssh_config, &peers)
            .await?;

        // Update status
        if let Some(ref mut wg) = status.wireguard {
            wg.public_key = Some(public_key);
        }

        self.transition_phase(ns, &name, status, NodePoolPhase::ValidatingNetwork)
            .await
    }

    async fn handle_validating_network(
        &self,
        ns: &str,
        pool: &NodePool,
        status: NodePoolStatus,
    ) -> Result<()> {
        let name = pool.name_any();
        info!(pool = %name, "Validating network connectivity");

        let ssh_config = self.get_ssh_config(ns, pool).await?;
        let (host, port) = self.get_ssh_endpoint(pool, &status)?;

        // Validate WireGuard connectivity
        let connected = self
            .provisioner
            .validate_wireguard_connectivity(&host, port, &ssh_config)
            .await?;

        if !connected {
            return Err(AutoscalerError::WireGuardSetup(
                "WireGuard connectivity validation failed".to_string(),
            ));
        }

        // Validate control plane connectivity (ping control plane IPs and check K3s API)
        let control_plane_ips: Vec<&str> = self
            .network_validation_config
            .control_plane_ips
            .iter()
            .map(|s| s.as_str())
            .collect();
        let api_server_url = &pool.spec.k3s.server_url;

        self.provisioner
            .validate_control_plane_connectivity(
                &host,
                port,
                &ssh_config,
                &control_plane_ips,
                api_server_url,
            )
            .await?;

        self.transition_phase(ns, &name, status, NodePoolPhase::JoiningCluster)
            .await
    }

    async fn handle_joining_cluster(
        &self,
        ns: &str,
        pool: &NodePool,
        mut status: NodePoolStatus,
    ) -> Result<()> {
        let name = pool.name_any();
        info!(pool = %name, "Installing K3s and joining cluster");

        let ssh_config = self.get_ssh_config(ns, pool).await?;
        let (host, port) = self.get_ssh_endpoint(pool, &status)?;

        // Use persisted node_id from status, or resolve it deterministically from host IP
        let node_id = resolve_node_id(pool, &status, Some(&host))?;
        if status.node_id.is_none() {
            status.node_id = Some(node_id.clone());
        }

        // Get K3s token from secret
        let k3s_token = self.get_k3s_token(ns, pool).await?;

        // Install K3s agent
        let k8s_node_name = self
            .provisioner
            .install_k3s_agent(
                &host,
                port,
                &ssh_config,
                &pool.spec.k3s,
                &k3s_token,
                &node_id,
            )
            .await?;

        status.node_name = Some(k8s_node_name);
        status.joined_at = Some(Utc::now());

        self.transition_phase(ns, &name, status, NodePoolPhase::WaitingForNode)
            .await
    }

    async fn handle_waiting_for_node(
        &self,
        ns: &str,
        pool: &NodePool,
        mut status: NodePoolStatus,
    ) -> Result<()> {
        let name = pool.name_any();
        let k8s_node_name = status.node_name.clone().ok_or_else(|| {
            AutoscalerError::InvalidConfiguration("Missing node_name in status".to_string())
        })?;

        info!(pool = %name, node = %k8s_node_name, "Waiting for node to become ready");

        // Check if node exists and is ready
        match self.k8s.get_node(&k8s_node_name).await {
            Ok(node) => {
                if is_node_ready(&node) {
                    info!(pool = %name, node = %k8s_node_name, "Node is ready");

                    // Add labels to the node
                    let mut labels = std::collections::BTreeMap::new();
                    labels.insert("basilica.ai/nodepool".to_string(), name.clone());
                    labels.insert(
                        "basilica.ai/managed-by".to_string(),
                        "autoscaler".to_string(),
                    );
                    if let Some(node_id) = &pool.spec.node_id {
                        labels.insert("basilica.ai/node-id".to_string(), node_id.clone());
                    }
                    self.k8s.add_node_labels(&k8s_node_name, &labels).await?;

                    // Get node UID
                    if let Some(uid) = node.metadata.uid {
                        status.node_uid = Some(uid);
                    }

                    // Execute postJoinScript if specified
                    if let Some(post_script) = &pool.spec.lifecycle.post_join_script {
                        if !post_script.is_empty() {
                            info!(pool = %name, "Executing postJoinScript");
                            let ssh_config = self.get_ssh_config(ns, pool).await?;
                            let (host, port) = self.get_ssh_endpoint(pool, &status)?;
                            self.provisioner
                                .execute_lifecycle_script(
                                    &host,
                                    port,
                                    &ssh_config,
                                    post_script,
                                    "postJoinScript",
                                )
                                .await?;
                        }
                    }

                    return self
                        .transition_phase(ns, &name, status, NodePoolPhase::Ready)
                        .await;
                }
                debug!(pool = %name, node = %k8s_node_name, "Node not yet ready");
            }
            Err(e) => {
                debug!(pool = %name, node = %k8s_node_name, error = %e, "Node not found yet");
            }
        }

        // Keep waiting
        self.k8s.update_node_pool_status(ns, &name, status).await
    }

    async fn handle_ready(
        &self,
        ns: &str,
        pool: &NodePool,
        mut status: NodePoolStatus,
    ) -> Result<()> {
        let name = pool.name_any();
        let k8s_node_name = status.node_name.clone().ok_or_else(|| {
            AutoscalerError::InvalidConfiguration("Missing node_name in status".to_string())
        })?;

        // Periodic health check
        match self.k8s.get_node(&k8s_node_name).await {
            Ok(node) => {
                if !is_node_ready(&node) {
                    warn!(pool = %name, node = %k8s_node_name, "Node became unready");
                    return self
                        .transition_phase(ns, &name, status, NodePoolPhase::Unhealthy)
                        .await;
                }
                status.last_health_check_at = Some(Utc::now());
            }
            Err(e) => {
                error!(pool = %name, node = %k8s_node_name, error = %e, "Failed to check node");
                status.failure_count += 1;
                if status.failure_count > 3 {
                    return self
                        .transition_phase(ns, &name, status, NodePoolPhase::Unhealthy)
                        .await;
                }
            }
        }

        self.k8s.update_node_pool_status(ns, &name, status).await
    }

    async fn handle_unhealthy(
        &self,
        ns: &str,
        pool: &NodePool,
        mut status: NodePoolStatus,
    ) -> Result<()> {
        let name = pool.name_any();
        warn!(pool = %name, "Node is unhealthy");

        // Check if node has recovered
        if let Some(node_name) = &status.node_name {
            if let Ok(node) = self.k8s.get_node(node_name).await {
                if is_node_ready(&node) {
                    info!(pool = %name, "Node recovered");
                    status.failure_count = 0;
                    return self
                        .transition_phase(ns, &name, status, NodePoolPhase::Ready)
                        .await;
                }
            }
        }

        // After too many failures, transition to Failed
        status.failure_count += 1;
        if status.failure_count > 10 {
            return self
                .transition_to_failed(ns, &name, status, "Node failed health checks repeatedly")
                .await;
        }

        self.k8s.update_node_pool_status(ns, &name, status).await
    }

    async fn handle_draining(
        &self,
        ns: &str,
        pool: &NodePool,
        status: NodePoolStatus,
    ) -> Result<()> {
        let name = pool.name_any();
        let k8s_node_name = status.node_name.clone().ok_or_else(|| {
            AutoscalerError::InvalidConfiguration("Missing node_name in status".to_string())
        })?;

        info!(pool = %name, node = %k8s_node_name, "Draining node");

        // Cordon the node
        self.k8s.cordon_node(&k8s_node_name).await?;

        // Get pods on node and evict them
        let pods = self.k8s.list_pods_on_node(&k8s_node_name).await?;
        let mut all_evicted = true;

        for pod in pods {
            let pod_ns = pod.namespace().unwrap_or_else(|| "default".to_string());
            let pod_name = pod.name_any();

            // Skip daemonset pods and mirror pods
            if is_daemonset_pod(&pod) || is_mirror_pod(&pod) {
                continue;
            }

            match self.k8s.evict_pod(&pod_ns, &pod_name, Some(30)).await {
                Ok(true) => debug!(pod = %pod_name, "Evicted pod"),
                Ok(false) => {
                    debug!(pod = %pod_name, "Pod eviction blocked by PDB");
                    all_evicted = false;
                }
                Err(e) => {
                    warn!(pod = %pod_name, error = %e, "Failed to evict pod");
                    all_evicted = false;
                }
            }
        }

        if all_evicted {
            self.transition_phase(ns, &name, status, NodePoolPhase::Terminating)
                .await
        } else {
            self.k8s.update_node_pool_status(ns, &name, status).await
        }
    }

    async fn handle_terminating(
        &self,
        ns: &str,
        pool: &NodePool,
        status: NodePoolStatus,
    ) -> Result<()> {
        let name = pool.name_any();
        info!(pool = %name, "Terminating node");

        // Delete K8s node
        if let Some(k8s_node_name) = &status.node_name {
            if let Err(e) = self.k8s.delete_node(k8s_node_name).await {
                warn!(node = %k8s_node_name, error = %e, "Failed to delete K8s node");
            }
        }

        // Stop rental
        if let Some(rental_id) = &status.rental_id {
            if let Err(e) = self.api.stop_rental(rental_id).await {
                warn!(rental = %rental_id, error = %e, "Failed to stop rental");
            }
            self.metrics.record_rental_stopped(&name);
        }

        // Deregister node
        if let Some(node_id) = &pool.spec.node_id {
            if let Err(e) = self.api.deregister_node(node_id).await {
                warn!(node = %node_id, error = %e, "Failed to deregister node");
            }
        }

        self.transition_phase(ns, &name, status, NodePoolPhase::Deleted)
            .await
    }

    async fn handle_failed(
        &self,
        _ns: &str,
        pool: &NodePool,
        _status: NodePoolStatus,
    ) -> Result<()> {
        let name = pool.name_any();
        debug!(pool = %name, "Node pool in failed state, waiting for manual intervention or deletion");
        Ok(())
    }

    async fn perform_timeout_cleanup(
        &self,
        _ns: &str,
        pool: &NodePool,
        status: &NodePoolStatus,
        phase: &NodePoolPhase,
    ) -> Result<()> {
        let name = pool.name_any();
        info!(pool = %name, phase = ?phase, "Performing timeout cleanup");

        // Phase-specific cleanup
        match phase {
            NodePoolPhase::Provisioning => {
                // Cancel any pending rental
                if let Some(rental_id) = &status.rental_id {
                    if let Err(e) = self.api.stop_rental(rental_id).await {
                        warn!(pool = %name, rental = %rental_id, error = %e, "Failed to stop rental during cleanup");
                    }
                }
            }
            NodePoolPhase::Configuring
            | NodePoolPhase::InstallingWireGuard
            | NodePoolPhase::ValidatingNetwork
            | NodePoolPhase::JoiningCluster => {
                // Deregister node if registered
                if let Some(node_id) = &pool.spec.node_id {
                    if let Err(e) = self.api.deregister_node(node_id).await {
                        warn!(pool = %name, node = %node_id, error = %e, "Failed to deregister node during cleanup");
                    }
                }
                // Cancel rental if exists
                if let Some(rental_id) = &status.rental_id {
                    if let Err(e) = self.api.stop_rental(rental_id).await {
                        warn!(pool = %name, rental = %rental_id, error = %e, "Failed to stop rental during cleanup");
                    }
                }
            }
            NodePoolPhase::WaitingForNode => {
                // Delete the K8s node if it was partially created
                if let Some(node_name) = &status.node_name {
                    if let Err(e) = self.k8s.delete_node(node_name).await {
                        warn!(pool = %name, node = %node_name, error = %e, "Failed to delete node during cleanup");
                    }
                }
                // Deregister and stop rental
                if let Some(node_id) = &pool.spec.node_id {
                    if let Err(e) = self.api.deregister_node(node_id).await {
                        warn!(pool = %name, node = %node_id, error = %e, "Failed to deregister node during cleanup");
                    }
                }
                if let Some(rental_id) = &status.rental_id {
                    if let Err(e) = self.api.stop_rental(rental_id).await {
                        warn!(pool = %name, rental = %rental_id, error = %e, "Failed to stop rental during cleanup");
                    }
                }
            }
            NodePoolPhase::Draining => {
                // Force uncordon if we timed out during drain (leave node as-is for manual intervention)
                if let Some(node_name) = &status.node_name {
                    info!(pool = %name, node = %node_name, "Drain timed out, node left in cordoned state");
                }
            }
            // No cleanup needed for these phases
            NodePoolPhase::Pending
            | NodePoolPhase::Ready
            | NodePoolPhase::Unhealthy
            | NodePoolPhase::Terminating
            | NodePoolPhase::Failed
            | NodePoolPhase::Deleted => {}
        }

        Ok(())
    }

    async fn transition_phase(
        &self,
        ns: &str,
        name: &str,
        mut status: NodePoolStatus,
        new_phase: NodePoolPhase,
    ) -> Result<()> {
        info!(pool = %name, from = ?status.phase, to = ?new_phase, "Phase transition");
        self.metrics.record_phase_transition(name, &new_phase);

        status.phase = Some(new_phase);
        status.phase_entered_at = Some(Utc::now());
        status.last_error = None;

        self.k8s.update_node_pool_status(ns, name, status).await
    }

    async fn transition_to_failed(
        &self,
        ns: &str,
        name: &str,
        mut status: NodePoolStatus,
        message: &str,
    ) -> Result<()> {
        error!(pool = %name, message = %message, "Transitioning to Failed");
        self.metrics
            .record_phase_transition(name, &NodePoolPhase::Failed);

        status.phase = Some(NodePoolPhase::Failed);
        status.phase_entered_at = Some(Utc::now());
        status.last_error = Some(message.to_string());

        add_condition(&mut status, "Failed", "True", "PhaseFailed", message);

        self.k8s.update_node_pool_status(ns, name, status).await
    }

    fn get_ssh_endpoint(&self, pool: &NodePool, status: &NodePoolStatus) -> Result<(String, u16)> {
        // For manual mode, use spec.ssh.host
        if let Some(ssh) = &pool.spec.ssh {
            return Ok((ssh.host.clone(), ssh.port));
        }

        // For dynamic mode, use status.external_ip
        let host = status.external_ip.clone().ok_or_else(|| {
            AutoscalerError::InvalidConfiguration("Missing external_ip".to_string())
        })?;

        Ok((host, 22))
    }

    async fn get_ssh_config(
        &self,
        ns: &str,
        pool: &NodePool,
    ) -> Result<crate::provisioner::SshConnectionConfig> {
        // Get SSH key reference
        let key_ref = if let Some(ssh) = &pool.spec.ssh {
            &ssh.auth_secret_ref
        } else if let Some(sc) = &pool.spec.secure_cloud {
            &sc.ssh_key_secret_ref
        } else {
            return Err(AutoscalerError::InvalidConfiguration(
                "No SSH configuration found".to_string(),
            ));
        };

        let secret_ns = key_ref.namespace.as_deref().unwrap_or(ns);
        let secret = self.k8s.get_secret(secret_ns, &key_ref.name).await?;
        let key_name = key_ref.key.as_deref().unwrap_or("private-key");

        let key_data = secret
            .data
            .as_ref()
            .and_then(|d| d.get(key_name))
            .ok_or_else(|| {
                AutoscalerError::SecretNotFound(format!(
                    "Key {} not found in secret {}/{}",
                    key_name, secret_ns, key_ref.name
                ))
            })?;

        let private_key = String::from_utf8(key_data.0.clone()).map_err(|_| {
            AutoscalerError::InvalidConfiguration("SSH key is not valid UTF-8".to_string())
        })?;

        let username = pool
            .spec
            .ssh
            .as_ref()
            .map(|s| s.user.clone())
            .unwrap_or_else(|| "root".to_string());

        Ok(crate::provisioner::SshConnectionConfig {
            username,
            private_key,
            passphrase: None,
        })
    }

    async fn get_k3s_token(&self, ns: &str, pool: &NodePool) -> Result<String> {
        let token_ref = &pool.spec.k3s.token_secret_ref;
        let secret_ns = token_ref.namespace.as_deref().unwrap_or(ns);
        let secret = self.k8s.get_secret(secret_ns, &token_ref.name).await?;

        let key_name = token_ref.key.as_deref().unwrap_or("token");
        let token_data = secret
            .data
            .as_ref()
            .and_then(|d| d.get(key_name))
            .ok_or_else(|| {
                AutoscalerError::SecretNotFound(format!(
                    "Key {} not found in secret {}/{}",
                    key_name, secret_ns, token_ref.name
                ))
            })?;

        String::from_utf8(token_data.0.clone()).map_err(|_| {
            AutoscalerError::InvalidConfiguration("K3s token is not valid UTF-8".to_string())
        })
    }
}

fn has_finalizer(pool: &NodePool) -> bool {
    pool.metadata
        .finalizers
        .as_ref()
        .map(|f| f.contains(&FINALIZER.to_string()))
        .unwrap_or(false)
}

fn phase_timeout(phase: &NodePoolPhase) -> u64 {
    match phase {
        NodePoolPhase::Pending => 60,
        NodePoolPhase::Provisioning => PhaseTimeouts::PROVISIONING,
        NodePoolPhase::Configuring => PhaseTimeouts::CONFIGURING,
        NodePoolPhase::InstallingWireGuard => PhaseTimeouts::INSTALLING_WIREGUARD,
        NodePoolPhase::ValidatingNetwork => PhaseTimeouts::VALIDATING_NETWORK,
        NodePoolPhase::JoiningCluster => PhaseTimeouts::JOINING_CLUSTER,
        NodePoolPhase::WaitingForNode => PhaseTimeouts::WAITING_FOR_NODE,
        NodePoolPhase::Ready => u64::MAX,
        NodePoolPhase::Unhealthy => 600, // 10 min to recover
        NodePoolPhase::Draining => PhaseTimeouts::DRAINING,
        NodePoolPhase::Terminating => PhaseTimeouts::TERMINATING,
        NodePoolPhase::Failed => u64::MAX,
        NodePoolPhase::Deleted => u64::MAX,
    }
}

fn is_node_ready(node: &k8s_openapi::api::core::v1::Node) -> bool {
    node.status
        .as_ref()
        .and_then(|s| s.conditions.as_ref())
        .map(|conditions| {
            conditions
                .iter()
                .any(|c| c.type_ == "Ready" && c.status == "True")
        })
        .unwrap_or(false)
}

fn is_daemonset_pod(pod: &k8s_openapi::api::core::v1::Pod) -> bool {
    pod.metadata
        .owner_references
        .as_ref()
        .map(|refs| refs.iter().any(|r| r.kind == "DaemonSet"))
        .unwrap_or(false)
}

fn is_mirror_pod(pod: &k8s_openapi::api::core::v1::Pod) -> bool {
    pod.metadata
        .annotations
        .as_ref()
        .map(|a| a.contains_key("kubernetes.io/config.mirror"))
        .unwrap_or(false)
}

fn add_condition(
    status: &mut NodePoolStatus,
    type_: &str,
    status_val: &str,
    reason: &str,
    message: &str,
) {
    let condition = NodePoolCondition {
        type_: type_.to_string(),
        status: status_val.to_string(),
        reason: Some(reason.to_string()),
        message: Some(message.to_string()),
        last_transition_time: Some(Utc::now()),
        last_probe_time: None,
    };

    // Update existing condition or add new one
    if let Some(existing) = status.conditions.iter_mut().find(|c| c.type_ == type_) {
        *existing = condition;
    } else {
        status.conditions.push(condition);
    }
}

/// Generate a deterministic node ID based on pool name and IP address.
/// This ensures idempotent node registration across reconciliation cycles.
/// Uses SHA-256 for stable output across Rust versions (unlike DefaultHasher).
fn generate_deterministic_node_id(pool_name: &str, ip: &str) -> String {
    use sha2::{Digest, Sha256};

    let mut hasher = Sha256::new();
    hasher.update(pool_name.as_bytes());
    hasher.update(b":");
    hasher.update(ip.as_bytes());
    let result = hasher.finalize();

    // Take first 8 bytes (16 hex chars) to match previous format
    format!(
        "node-{:016x}",
        u64::from_be_bytes(result[..8].try_into().unwrap())
    )
}

/// Get or generate a node ID with proper precedence:
/// 1. spec.node_id (user-specified)
/// 2. status.node_id (previously generated and persisted)
/// 3. Generate deterministic ID from pool name + IP
fn resolve_node_id(pool: &NodePool, status: &NodePoolStatus, ip: Option<&str>) -> Result<String> {
    if let Some(spec_id) = &pool.spec.node_id {
        return Ok(spec_id.clone());
    }

    if let Some(status_id) = &status.node_id {
        return Ok(status_id.clone());
    }

    let ip = ip.ok_or_else(|| {
        AutoscalerError::InvalidConfiguration(
            "Cannot generate node_id: no IP available and no node_id in spec or status".to_string(),
        )
    })?;

    Ok(generate_deterministic_node_id(&pool.name_any(), ip))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn phase_timeouts_are_defined() {
        assert!(phase_timeout(&NodePoolPhase::Pending) > 0);
        assert!(phase_timeout(&NodePoolPhase::Provisioning) > 0);
        assert!(phase_timeout(&NodePoolPhase::Unhealthy) > 0);
        assert_eq!(phase_timeout(&NodePoolPhase::Ready), u64::MAX);
    }

    #[test]
    fn deterministic_node_id_is_stable() {
        let id1 = generate_deterministic_node_id("pool-1", "192.168.1.100");
        let id2 = generate_deterministic_node_id("pool-1", "192.168.1.100");
        assert_eq!(id1, id2);
        assert!(id1.starts_with("node-"));
    }

    #[test]
    fn deterministic_node_id_differs_by_ip() {
        let id1 = generate_deterministic_node_id("pool-1", "192.168.1.100");
        let id2 = generate_deterministic_node_id("pool-1", "192.168.1.101");
        assert_ne!(id1, id2);
    }

    #[test]
    fn deterministic_node_id_differs_by_pool_name() {
        let id1 = generate_deterministic_node_id("pool-1", "192.168.1.100");
        let id2 = generate_deterministic_node_id("pool-2", "192.168.1.100");
        assert_ne!(id1, id2);
    }
}
