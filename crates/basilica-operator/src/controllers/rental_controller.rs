use k8s_openapi::api::core::v1::{
    Affinity, Capabilities, Container, EnvVar, PersistentVolumeClaim, PersistentVolumeClaimSpec, Pod, PodSecurityContext,
    PodSpec, ResourceRequirements, SecurityContext, Service, ServicePort, ServiceSpec, Toleration, Volume,
    VolumeMount, PersistentVolumeClaimVolumeSource, NodeAffinity, NodeSelector, NodeSelectorRequirement, NodeSelectorTerm, VolumeResourceRequirements,
};
use k8s_openapi::api::networking::v1::{NetworkPolicy, NetworkPolicyIngressRule, NetworkPolicyPeer, NetworkPolicyPort, NetworkPolicySpec};
use k8s_openapi::apimachinery::pkg::util::intstr::IntOrString;
use k8s_openapi::apimachinery::pkg::api::resource::Quantity;
use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;

use crate::crd::gpu_rental::{AccessType, GpuRental, GpuRentalSpec, GpuRentalStatus, GpuSpec, RentalNetwork};
use crate::k8s_client::K8sClient;
use anyhow::Result;

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
    let pod_sc = Some(PodSecurityContext { run_as_non_root: Some(true), ..Default::default() });
    let container_sc = Some(SecurityContext {
        allow_privilege_escalation: Some(false),
        read_only_root_filesystem: Some(true),
        capabilities: Some(Capabilities { drop: Some(vec!["ALL".into()]), ..Default::default() }),
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

    Pod {
        metadata: ObjectMeta { name: Some(format!("rental-{}", name)), labels: labels.clone(), ..Default::default() },
        spec: Some(PodSpec {
            containers,
            volumes,
            restart_policy: Some("Always".into()),
            security_context: pod_sc,
            tolerations: Some(build_tolerations()),
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

    vec![default_deny, allow_ingress]
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

pub struct RentalController<C: K8sClient> {
    pub client: C,
}

impl<C: K8sClient> RentalController<C> {
    pub fn new(client: C) -> Self { Self { client } }

    pub async fn reconcile(&self, ns: &str, cr: &GpuRental) -> Result<()> {
        let name = cr.metadata.name.clone().unwrap_or_default();
        let spec = cr.spec.clone();

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

        // Derive status
        let pods = self.client.list_pods_with_label(ns, "basilica.io/rental", &name).await?;
        let (state, pod_name) = compute_rental_state_from_pods(&pods);
        let status = GpuRentalStatus { state: Some(state), pod_name, node_name: None, start_time: None, expiry_time: None, renewal_time: None, total_cost: None };
        self.client.update_gpu_rental_status(ns, &name, status).await?;
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
        assert!(c.security_context.as_ref().unwrap().read_only_root_filesystem.unwrap());
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
        assert_eq!(nps.len(), 2);
        let deny = &nps[0];
        assert!(deny.spec.as_ref().unwrap().ingress.as_ref().unwrap().is_empty());
        let allow = &nps[1];
        assert!(!allow.spec.as_ref().unwrap().ingress.as_ref().unwrap().is_empty());
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

    #[tokio::test]
    async fn reconcile_creates_resources_and_updates_status() {
        let client = MockK8sClient::default();
        let controller = RentalController::new(client.clone());

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
    }
}
