use k8s_openapi::api::core::v1::{
    Affinity, Capabilities, Container, EnvVar, PersistentVolumeClaim, PersistentVolumeClaimSpec, Pod, PodSecurityContext,
    PodSpec, ResourceRequirements, SecurityContext, Service, ServicePort, ServiceSpec, Toleration, Volume,
    VolumeMount, PersistentVolumeClaimVolumeSource, NodeAffinity, NodeSelector, NodeSelectorRequirement, NodeSelectorTerm, VolumeResourceRequirements,
};
use k8s_openapi::api::networking::v1::{IPBlock, NetworkPolicy, NetworkPolicyEgressRule, NetworkPolicyIngressRule, NetworkPolicyPeer, NetworkPolicyPort, NetworkPolicySpec};
use k8s_openapi::apimachinery::pkg::util::intstr::IntOrString;
use k8s_openapi::apimachinery::pkg::api::resource::Quantity;
use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;

use crate::crd::gpu_rental::{AccessType, GpuRental, GpuRentalSpec, GpuRentalStatus, GpuSpec, RentalNetwork};
use crate::k8s_client::K8sClient;
use crate::billing::BillingClient;
use k8s_openapi::chrono::{DateTime, Duration, Utc};
use anyhow::Result;
use std::time::Instant;
use crate::metrics as opmetrics;

fn to_quantity(s: &str) -> Quantity { Quantity(s.to_string()) }

fn build_resources(gpu: &GpuSpec, cpu: &str, memory: &str) -> ResourceRequirements {
    use std::collections::BTreeMap;
    let mut limits = BTreeMap::new();
    let mut requests = BTreeMap::new();
    limits.insert("cpu".to_string(), to_quantity(cpu));
    limits.insert("memory".to_string(), to_quantity(memory));
    requests.insert("cpu".to_string(), to_quantity(cpu));
    requests.insert("memory".to_string(), to_quantity(memory));
    if gpu.count > 0 {
        let q = Quantity(gpu.count.to_string());
        limits.insert("nvidia.com/gpu".to_string(), q.clone());
        requests.insert("nvidia.com/gpu".to_string(), q);
    }
    ResourceRequirements { limits: Some(limits), requests: Some(requests), claims: None }
}

fn build_env(env: &[(String, String)]) -> Vec<EnvVar> {
    env.iter().map(|(k, v)| EnvVar { name: k.clone(), value: Some(v.clone()), ..Default::default() }).collect()
}

fn build_tolerations() -> Vec<Toleration> {
    vec![Toleration {
        key: Some("basilica.io/workloads-only".into()),
        operator: Some("Equal".into()),
        value: Some("true".into()),
        effect: Some("NoSchedule".into()),
        ..Default::default()
    }]
}

fn build_node_affinity(gpu: &GpuSpec) -> Option<Affinity> {
    if gpu.model.is_empty() { return None; }
    let expr = NodeSelectorRequirement { key: "basilica.io/gpu-model".into(), operator: "In".into(), values: Some(gpu.model.clone()) };
    let term = NodeSelectorTerm { match_expressions: Some(vec![expr]), match_fields: None };
    let ns = NodeSelector { node_selector_terms: vec![term] };
    Some(Affinity { node_affinity: Some(NodeAffinity { required_during_scheduling_ignored_during_execution: Some(ns), ..Default::default() }), ..Default::default() })
}

fn build_security_contexts() -> (Option<PodSecurityContext>, Option<SecurityContext>) {
    let pod_sc = Some(PodSecurityContext { run_as_non_root: Some(true), seccomp_profile: Some(k8s_openapi::api::core::v1::SeccompProfile { type_: "RuntimeDefault".into(), localhost_profile: None }), ..Default::default() });
    let container_sc = Some(SecurityContext {
        allow_privilege_escalation: Some(false),
        read_only_root_filesystem: Some(true),
        capabilities: Some(Capabilities { drop: Some(vec!["ALL".into()]), ..Default::default() }),
        seccomp_profile: Some(k8s_openapi::api::core::v1::SeccompProfile { type_: "RuntimeDefault".into(), localhost_profile: None }),
        ..Default::default()
    });
    (pod_sc, container_sc)
}

pub fn render_rental_pod(name: &str, spec: &GpuRentalSpec) -> Pod {
    let (pod_sc, container_sc) = build_security_contexts();

    // Main container
    let mut containers = vec![Container {
        name: format!("rental-{}", name),
        image: Some(spec.container.image.clone()),
        command: if spec.container.command.is_empty() { None } else { Some(spec.container.command.clone()) },
        env: Some(build_env(&spec.container.env)),
        ports: Some(spec.container.ports.iter().map(|p| k8s_openapi::api::core::v1::ContainerPort { container_port: p.container_port as i32, protocol: Some(p.protocol.clone()), ..Default::default() }).collect()),
        resources: Some(build_resources(&spec.container.resources.gpus, &spec.container.resources.cpu, &spec.container.resources.memory)),
        volume_mounts: if spec.storage.is_some() { Some(vec![VolumeMount { name: "data".into(), mount_path: spec.storage.as_ref().unwrap().mount_path.clone(), read_only: Some(false), ..Default::default() }]) } else { None },
        security_context: container_sc.clone(),
        ..Default::default()
    }];

    // Access-specific sidecars
    match spec.access_type {
        AccessType::Ssh => {
            if spec.ssh.as_ref().map(|s| s.enabled).unwrap_or(false) {
                containers.push(Container {
                    name: "sshd".into(),
                    image: Some("linuxserver/openssh-server:latest".into()),
                    ports: Some(vec![k8s_openapi::api::core::v1::ContainerPort { container_port: 22, ..Default::default() }]),
                    security_context: container_sc.clone(),
                    ..Default::default()
                });
            }
        }
        AccessType::Jupyter => {
            containers.push(Container {
                name: "jupyter".into(),
                image: Some(spec.jupyter_access.as_ref().and_then(|j| j.base_image.clone()).unwrap_or_else(|| "jupyter/tensorflow-notebook:latest".into())),
                ports: Some(vec![k8s_openapi::api::core::v1::ContainerPort { container_port: 8888, ..Default::default() }]),
                security_context: container_sc.clone(),
                ..Default::default()
            });
        }
        AccessType::Vscode | AccessType::Custom => {}
    }

    // Volumes
    let volumes = if let Some(st) = &spec.storage {
        Some(vec![Volume {
            name: "data".into(),
            persistent_volume_claim: Some(PersistentVolumeClaimVolumeSource { claim_name: format!("rental-pvc-{}", name), ..Default::default() }),
            ..Default::default()
        }])
    } else { None };

    let labels = Some(
        vec![
            ("basilica.io/type".to_string(), "rental".to_string()),
            ("basilica.io/rental".to_string(), name.to_string()),
        ]
        .into_iter()
        .collect(),
    );

    let mut tolerations = build_tolerations();
    if spec.exclusive {
        tolerations.push(Toleration {
            key: Some("basilica.io/rental-exclusive".into()),
            operator: Some("Equal".into()),
            value: Some("true".into()),
            effect: Some("NoSchedule".into()),
            ..Default::default()
        });
    }

    Pod {
        metadata: ObjectMeta { name: Some(format!("rental-{}", name)), labels: labels.clone(), ..Default::default() },
        spec: Some(PodSpec {
            containers,
            volumes,
            restart_policy: Some("Always".into()),
            security_context: pod_sc,
            tolerations: Some(tolerations),
            affinity: build_node_affinity(&spec.container.resources.gpus),
            ..Default::default()
        }),
        ..Default::default()
    }
}

pub fn render_rental_service(name: &str, spec: &GpuRentalSpec) -> Option<Service> {
    if spec.network.ingress.is_empty() { return None; }
    let svc_type = if spec.network.public_ip_required || spec.network.ingress.iter().any(|r| r.exposure.eq_ignore_ascii_case("LoadBalancer")) {
        "LoadBalancer"
    } else { "NodePort" };

    let ports: Vec<ServicePort> = spec
        .network
        .ingress
        .iter()
        .map(|r| ServicePort { port: r.port as i32, target_port: Some(IntOrString::Int(r.port as i32)), protocol: Some("TCP".into()), ..Default::default() })
        .collect();
    let selector = Some(vec![("basilica.io/rental".to_string(), name.to_string())].into_iter().collect());
    Some(Service {
        metadata: ObjectMeta { name: Some(format!("rental-svc-{}", name)), labels: Some(vec![("basilica.io/rental".into(), name.into())].into_iter().collect()), ..Default::default() },
        spec: Some(ServiceSpec { type_: Some(svc_type.into()), selector, ports: Some(ports), ..Default::default() }),
        ..Default::default()
    })
}

pub fn render_network_policies(name: &str, spec: &GpuRentalSpec) -> Vec<NetworkPolicy> {
    let pod_selector = Some(vec![("basilica.io/rental".to_string(), name.to_string())].into_iter().collect());

    // Default deny for both ingress and egress
    let default_deny = NetworkPolicy {
        metadata: ObjectMeta { name: Some(format!("rental-np-deny-{}", name)), ..Default::default() },
        spec: Some(NetworkPolicySpec {
            pod_selector: k8s_openapi::apimachinery::pkg::apis::meta::v1::LabelSelector { match_labels: pod_selector.clone(), ..Default::default() },
            ingress: Some(vec![]),
            egress: Some(vec![]),
            policy_types: Some(vec!["Ingress".into(), "Egress".into()]),
        }),
    };

    // Allow specified ingress ports
    let mut rules: Vec<NetworkPolicyIngressRule> = vec![];
    if !spec.network.ingress.is_empty() {
        let ports: Vec<NetworkPolicyPort> = spec
            .network
            .ingress
            .iter()
            .map(|r| NetworkPolicyPort { port: Some(IntOrString::Int(r.port as i32)), protocol: Some("TCP".into()), ..Default::default() })
            .collect();
        rules.push(NetworkPolicyIngressRule { ports: Some(ports), from: Some(vec![NetworkPolicyPeer::default()]) });
    }

    let allow_ingress = NetworkPolicy {
        metadata: ObjectMeta { name: Some(format!("rental-np-allow-{}", name)), ..Default::default() },
        spec: Some(NetworkPolicySpec {
            pod_selector: k8s_openapi::apimachinery::pkg::apis::meta::v1::LabelSelector { match_labels: pod_selector, ..Default::default() },
            ingress: Some(rules),
            egress: None,
            policy_types: Some(vec!["Ingress".into()]),
        }),
    };

    // Egress rules based on policy
    let mut egress_policies: Vec<NetworkPolicy> = Vec::new();
    let policy = spec.network.egress_policy.to_lowercase();
    if policy == "open" || policy == "egress-only" || policy == "restricted" {
        let egress_rules: Vec<NetworkPolicyEgressRule> = match policy.as_str() {
            // Allow all egress
            "open" | "egress-only" => vec![NetworkPolicyEgressRule { to: None, ports: None }],
            // Restricted: allow only listed CIDRs
            _ => {
                let mut rules: Vec<NetworkPolicyEgressRule> = Vec::new();
                for dest in &spec.network.allowed_egress {
                    // Only CIDR strings are supported in NetworkPolicy IPBlock
                    if dest.contains('/') {
                        let peer = NetworkPolicyPeer { ip_block: Some(IPBlock { cidr: dest.clone(), except: None }), ..Default::default() };
                        rules.push(NetworkPolicyEgressRule { to: Some(vec![peer]), ports: None });
                    }
                }
                rules
            }
        };
        egress_policies.push(NetworkPolicy {
            metadata: ObjectMeta { name: Some(format!("rental-np-egress-{}", name)), ..Default::default() },
            spec: Some(NetworkPolicySpec {
                pod_selector: k8s_openapi::apimachinery::pkg::apis::meta::v1::LabelSelector { match_labels: Some(vec![("basilica.io/rental".into(), name.into())].into_iter().collect()), ..Default::default() },
                ingress: None,
                egress: Some(egress_rules),
                policy_types: Some(vec!["Egress".into()]),
            }),
        });
    }

    let mut out = vec![default_deny, allow_ingress];
    out.extend(egress_policies);
    out
}

pub fn render_rental_pvc(name: &str, spec: &GpuRentalSpec) -> Option<PersistentVolumeClaim> {
    let st = spec.storage.as_ref()?;
    let mut requests = std::collections::BTreeMap::new();
    requests.insert("storage".into(), Quantity(format!("{}Gi", st.persistent_volume_gb)));
    Some(PersistentVolumeClaim {
        metadata: ObjectMeta { name: Some(format!("rental-pvc-{}", name)), ..Default::default() },
        spec: Some(PersistentVolumeClaimSpec { access_modes: Some(vec!["ReadWriteOnce".into()]), resources: Some(VolumeResourceRequirements { requests: Some(requests), ..Default::default() }), storage_class_name: st.storage_class.clone(), ..Default::default() }),
        ..Default::default()
    })
}

pub struct RentalController<C: K8sClient, B: BillingClient> {
    pub client: C,
    pub billing: B,
}

impl<C: K8sClient, B: BillingClient> RentalController<C, B> {
    pub fn new(client: C, billing: B) -> Self { Self { client, billing } }

    pub async fn reconcile(&self, ns: &str, cr: &GpuRental) -> Result<()> {
        let start = Instant::now();
        let name = cr.metadata.name.clone().unwrap_or_default();
        let spec = cr.spec.clone();
        let prev_state = self
            .client
            .get_gpu_rental(ns, &name)
            .await
            .ok()
            .and_then(|r| r.status.and_then(|s| s.state))
            .unwrap_or_else(|| "Unknown".into());

        // Ensure PVC if requested
        if spec.storage.is_some() {
            if let Some(pvc) = render_rental_pvc(&name, &spec) {
                // Best-effort create; mock will overwrite in-memory
                let _ = self.client.create_pvc(ns, &pvc).await;
            }
        }

        // Ensure Pod exists
        let pod = render_rental_pod(&name, &spec);
        // Create or replace
        let _ = self.client.create_pod(ns, &pod).await;

        // Ensure Service if ingress requested
        if let Some(svc) = render_rental_service(&name, &spec) {
            let _ = self.client.create_service(ns, &svc).await;
        }

        // Ensure NetworkPolicies
        for np in render_network_policies(&name, &spec) {
            let _ = self.client.create_network_policy(ns, &np).await;
        }
        // Record netpol mode (egress policy label)
        opmetrics::record_rental_netpol(&spec.network.egress_policy, ns);

        // Derive status and expiry
        let pods = self.client.list_pods_with_label(ns, "basilica.io/rental", &name).await?;
        let (state, pod_name) = compute_rental_state_from_pods(&pods);
        // Establish or extend expiry
        let mut expiry_time_str = None;
        let mut renewal_time_str = None;
        let mut total_extensions = None;
        if spec.duration.hours > 0 {
            // Get current status if exists
            let mut current = self.client.get_gpu_rental(ns, &name).await?.status.unwrap_or_default();
            if current.expiry_time.is_none() {
                let exp = Utc::now() + Duration::hours(spec.duration.hours as i64);
                expiry_time_str = Some(exp.to_rfc3339());
            } else if spec.duration.auto_extend {
                if let Some(expiry_str) = current.expiry_time.clone() {
                    if let Ok(expiry) = DateTime::parse_from_rfc3339(&expiry_str).map(|dt| dt.with_timezone(&Utc)) {
                        if Utc::now() >= expiry {
                            // Attempt renewal
                            if self.billing.approve_extension(cr, spec.duration.hours).await.unwrap_or(false) {
                                let new_exp = Utc::now() + Duration::hours(spec.duration.hours as i64);
                                expiry_time_str = Some(new_exp.to_rfc3339());
                                renewal_time_str = Some(Utc::now().to_rfc3339());
                                total_extensions = Some(current.total_extensions.unwrap_or(0) + 1);
                                opmetrics::record_rental_extension(ns, true);
                            } else {
                                // mark suspended when cannot extend
                                let suspended = GpuRentalStatus { state: Some("Suspended".into()), pod_name: pod_name.clone(), node_name: None, start_time: None, expiry_time: current.expiry_time.clone(), renewal_time: current.renewal_time.clone(), total_cost: None, total_extensions: current.total_extensions };
                                self.client.update_gpu_rental_status(ns, &name, suspended).await?;
                                opmetrics::record_rental_extension(ns, false);
                                return Ok(());
                            }
                        }
                    }
                }
            }
        }
        let status = GpuRentalStatus { state: Some(state.clone()), pod_name, node_name: None, start_time: None, expiry_time: expiry_time_str, renewal_time: renewal_time_str, total_cost: None, total_extensions };
        self.client.update_gpu_rental_status(ns, &name, status).await?;
        let created = true; // Pod and associated resources are ensured; for metrics, treat as created/ensured event
        opmetrics::record_rental_reconcile(ns, &name, created, &prev_state, &state, start);
        let prev_active = prev_state == "Active";
        let new_active = state == "Active";
        opmetrics::record_rental_active_change(ns, prev_active, new_active);
        Ok(())
    }
}

fn compute_rental_state_from_pods(pods: &[Pod]) -> (String, Option<String>) {
    // Map Pod phases to rental state
    let mut running: Option<String> = None;
    let mut failed: Option<String> = None;
    let mut pending: Option<String> = None;
    for p in pods {
        let n = p.metadata.name.clone();
        if let Some(st) = &p.status { if let Some(ph) = &st.phase {
            match ph.as_str() {
                "Running" => running = n,
                "Failed" => failed = n,
                "Pending" => pending = n,
                _ => {}
            }
        }}
    }
    if let Some(n) = running { return ("Active".into(), Some(n)); }
    if let Some(n) = failed { return ("Failed".into(), Some(n)); }
    if let Some(n) = pending { return ("Provisioning".into(), Some(n)); }
    ("Provisioning".into(), None)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::crd::gpu_rental::{RentalContainer, RentalPort, RentalStorage, Resources as RResources};
    use crate::k8s_client::MockK8sClient;
    use crate::billing::MockBillingClient;
    use k8s_openapi::api::core::v1::PodStatus;

    fn base_spec() -> GpuRentalSpec {
        GpuRentalSpec {
            container: RentalContainer {
                image: "img".into(),
                env: vec![("K".into(), "V".into())],
                command: vec!["bash".into()],
                ports: vec![RentalPort { container_port: 8080, protocol: "TCP".into() }],
                volumes: vec![],
                resources: RResources { cpu: "2".into(), memory: "4Gi".into(), gpus: GpuSpec { count: 1, model: vec!["A100".into()] } },
            },
            duration: crate::crd::gpu_rental::RentalDuration { hours: 24, auto_extend: false, max_extensions: 0 },
            access_type: AccessType::Ssh,
            network: RentalNetwork { ingress: vec![], egress_policy: "restricted".into(), allowed_egress: vec![], public_ip_required: false, bandwidth_mbps: None },
            storage: None,
            ssh: None,
            jupyter_access: None,
            environment: None,
            miner_selector: None,
            billing: None,
            ttl_seconds: 0,
            tenancy: None,
            exclusive: false,
        }
    }

    #[test]
    fn pod_renders_with_resources_security_and_ports() {
        let spec = base_spec();
        let pod = render_rental_pod("r1", &spec);
        let p = pod.spec.unwrap();
        let c = &p.containers[0];
        assert_eq!(c.image.as_deref(), Some("img"));
        let res = c.resources.as_ref().unwrap();
        assert_eq!(res.limits.as_ref().unwrap().get("nvidia.com/gpu").unwrap().0, "1");
        assert_eq!(c.ports.as_ref().unwrap()[0].container_port, 8080);
        let csc = c.security_context.as_ref().unwrap();
        assert!(csc.read_only_root_filesystem.unwrap());
        assert_eq!(csc.seccomp_profile.as_ref().unwrap().type_, "RuntimeDefault");
        assert_eq!(p.security_context.as_ref().unwrap().seccomp_profile.as_ref().unwrap().type_, "RuntimeDefault");
        // Labels present
        assert_eq!(pod.metadata.labels.as_ref().unwrap().get("basilica.io/rental").unwrap(), "r1");
    }

    #[test]
    fn service_chooses_type_and_ports() {
        let mut spec = base_spec();
        spec.network.ingress = vec![crate::crd::gpu_rental::IngressRule { port: 8080, exposure: "NodePort".into() }];
        let svc = render_rental_service("r1", &spec).unwrap();
        assert_eq!(svc.spec.as_ref().unwrap().type_.as_deref(), Some("NodePort"));
        assert_eq!(svc.spec.as_ref().unwrap().ports.as_ref().unwrap()[0].port, 8080);

        spec.network.public_ip_required = true;
        let svc2 = render_rental_service("r1", &spec).unwrap();
        assert_eq!(svc2.spec.as_ref().unwrap().type_.as_deref(), Some("LoadBalancer"));
    }

    #[test]
    fn network_policies_default_deny_and_allow_ingress() {
        let mut spec = base_spec();
        spec.network.ingress = vec![crate::crd::gpu_rental::IngressRule { port: 8080, exposure: "NodePort".into() }];
        let nps = render_network_policies("r1", &spec);
        assert_eq!(nps.len(), 3);
        let deny = &nps[0];
        assert!(deny.spec.as_ref().unwrap().ingress.as_ref().unwrap().is_empty());
        let allow = &nps[1];
        assert!(!allow.spec.as_ref().unwrap().ingress.as_ref().unwrap().is_empty());
    }

    #[test]
    fn network_policies_include_egress_allowlist_restricted() {
        let mut spec = base_spec();
        spec.network.egress_policy = "restricted".into();
        spec.network.allowed_egress = vec!["10.0.0.0/8".into(), "0.0.0.0/0".into()];
        let nps = render_network_policies("r1", &spec);
        assert_eq!(nps.len(), 3);
        let egress_np = &nps[2];
        let egress = egress_np.spec.as_ref().unwrap().egress.as_ref().unwrap();
        assert_eq!(egress.len(), 2);
        let first = &egress[0];
        let peers = first.to.as_ref().unwrap();
        assert!(peers[0].ip_block.as_ref().is_some());
        assert_eq!(peers[0].ip_block.as_ref().unwrap().cidr, "10.0.0.0/8");
    }

    #[test]
    fn network_policies_open_egress_rule() {
        let mut spec = base_spec();
        spec.network.egress_policy = "open".into();
        let nps = render_network_policies("r1", &spec);
        assert_eq!(nps.len(), 3);
        let egress_np = &nps[2];
        let egress = egress_np.spec.as_ref().unwrap().egress.as_ref().unwrap();
        assert_eq!(egress.len(), 1);
        assert!(egress[0].to.is_none());
        assert!(egress_np.spec.as_ref().unwrap().policy_types.as_ref().unwrap().contains(&"Egress".into()));
    }

    #[test]
    fn pvc_renders_when_storage_specified() {
        let mut spec = base_spec();
        spec.storage = Some(RentalStorage { persistent_volume_gb: 200, storage_class: Some("fast-ssd".into()), mount_path: "/data".into() });
        let pvc = render_rental_pvc("r1", &spec).unwrap();
        assert_eq!(pvc.metadata.name.as_deref(), Some("rental-pvc-r1"));
        let pod = render_rental_pod("r1", &spec);
        assert_eq!(pod.spec.as_ref().unwrap().volumes.as_ref().unwrap()[0].name, "data");
    }

    #[test]
    fn exclusive_rental_adds_toleration() {
        let mut spec = base_spec();
        spec.exclusive = true;
        let pod = render_rental_pod("rX", &spec);
        let tols = pod.spec.as_ref().unwrap().tolerations.as_ref().unwrap();
        assert!(tols.iter().any(|t| t.key.as_deref() == Some("basilica.io/rental-exclusive") && t.value.as_deref() == Some("true")));
    }

    #[tokio::test]
    async fn reconcile_creates_resources_and_updates_status() {
        let _ = metrics_exporter_prometheus::PrometheusBuilder::new().install_recorder();
        let client = MockK8sClient::default();
        let controller = RentalController::new(client.clone(), MockBillingClient::default());

        let mut spec = base_spec();
        spec.network.ingress = vec![crate::crd::gpu_rental::IngressRule { port: 8080, exposure: "NodePort".into() }];
        let cr = GpuRental::new("rent1", spec);
        controller.client.create_gpu_rental("ns", &cr).await.unwrap();

        // First reconcile, no pods yet -> Provisioning
        controller.reconcile("ns", &cr).await.unwrap();
        let updated = controller.client.get_gpu_rental("ns", "rent1").await.unwrap();
        assert_eq!(updated.status.as_ref().unwrap().state.as_deref(), Some("Provisioning"));

        // Create a running pod and reconcile again
        let pod = Pod { metadata: ObjectMeta { name: Some("p1".into()), labels: Some(vec![("basilica.io/rental".into(), "rent1".into())].into_iter().collect()), ..Default::default() }, status: Some(PodStatus { phase: Some("Running".into()), ..Default::default() }), ..Default::default() };
        controller.client.create_pod("ns", &pod).await.unwrap();
        controller.reconcile("ns", &cr).await.unwrap();
        let updated2 = controller.client.get_gpu_rental("ns", "rent1").await.unwrap();
        assert_eq!(updated2.status.as_ref().unwrap().state.as_deref(), Some("Active"));
        assert_eq!(updated2.status.as_ref().unwrap().pod_name.as_deref(), Some("p1"));
        // Service should exist
        let svcs = controller.client.list_services_with_label("ns", "basilica.io/rental", "rent1").await.unwrap();
        assert!(!svcs.is_empty());
        // Metrics present
        // Exercise metrics path (no-op if already installed)
        let _ = metrics_exporter_prometheus::PrometheusBuilder::new().install_recorder();
    }
    #[tokio::test]
    async fn auto_extend_on_expired_rental_when_approved() {
        let _ = metrics_exporter_prometheus::PrometheusBuilder::new().install_recorder();
        let client = MockK8sClient::default();
        let billing = MockBillingClient::default();
        let controller = RentalController::new(client.clone(), billing);

        let mut spec = base_spec();
        spec.duration.auto_extend = true;
        spec.duration.max_extensions = 1;
        let cr = GpuRental::new("rent2", spec);
        controller.client.create_gpu_rental("ns", &cr).await.unwrap();

        // First reconcile to set expiry
        controller.reconcile("ns", &cr).await.unwrap();
        let mut current = controller.client.get_gpu_rental("ns", "rent2").await.unwrap();
        let mut st = current.status.take().unwrap();
        // Set expiry to the past to trigger extension
        st.expiry_time = Some((Utc::now() - Duration::hours(1)).to_rfc3339());
        controller.client.update_gpu_rental_status("ns", "rent2", st).await.unwrap();

        controller.reconcile("ns", &cr).await.unwrap();
        let updated = controller.client.get_gpu_rental("ns", "rent2").await.unwrap();
        assert!(updated.status.as_ref().unwrap().expiry_time.is_some());
        assert!(updated.status.as_ref().unwrap().renewal_time.is_some());
    }

    #[tokio::test]
    async fn suspend_on_expired_rental_when_denied() {
        let client = MockK8sClient::default();
        let mut billing = MockBillingClient::default();
        {
            let mut approvals = billing.approvals.write().await;
            approvals.insert("rent3".into(), false);
        }
        let controller = RentalController::new(client.clone(), billing);

        let mut spec = base_spec();
        spec.duration.auto_extend = true;
        let cr = GpuRental::new("rent3", spec);
        controller.client.create_gpu_rental("ns", &cr).await.unwrap();
        controller.reconcile("ns", &cr).await.unwrap();
        // Force expiry
        let mut current = controller.client.get_gpu_rental("ns", "rent3").await.unwrap();
        let mut st = current.status.take().unwrap();
        st.expiry_time = Some((Utc::now() - Duration::hours(1)).to_rfc3339());
        controller.client.update_gpu_rental_status("ns", "rent3", st).await.unwrap();
        controller.reconcile("ns", &cr).await.unwrap();

        let updated = controller.client.get_gpu_rental("ns", "rent3").await.unwrap();
        assert_eq!(updated.status.as_ref().unwrap().state.as_deref(), Some("Suspended"));
    }
}
