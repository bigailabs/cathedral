use k8s_openapi::api::core::v1::{
    Affinity, Capabilities, Container, EmptyDirVolumeSource, EnvFromSource, EnvVar,
    HostPathVolumeSource, NodeAffinity, NodeSelector, NodeSelectorRequirement, NodeSelectorTerm,
    PersistentVolumeClaim, PersistentVolumeClaimSpec, PersistentVolumeClaimVolumeSource, Pod,
    PodSecurityContext, PodSpec, ResourceRequirements, SecretEnvSource, SecurityContext, Service,
    ServicePort, ServiceSpec, Toleration, Volume, VolumeMount, VolumeResourceRequirements,
};
use k8s_openapi::api::networking::v1::{
    IPBlock, NetworkPolicy, NetworkPolicyEgressRule, NetworkPolicyIngressRule, NetworkPolicyPeer,
    NetworkPolicyPort, NetworkPolicySpec,
};
use k8s_openapi::apimachinery::pkg::api::resource::Quantity;
use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;
use k8s_openapi::apimachinery::pkg::util::intstr::IntOrString;

use crate::billing::{BillingClient, RuntimeMetrics};
use crate::crd::gpu_rental::{AccessType, GpuRental, GpuRentalSpec, GpuRentalStatus, GpuSpec};
use crate::k8s_client::K8sClient;
use crate::metrics as opmetrics;
use crate::metrics_provider::{NoopRuntimeMetricsProvider, RuntimeMetricsProvider};
use anyhow::Result;
use k8s_openapi::chrono::{DateTime, Utc};
use kube::core::DynamicObject;
use std::sync::Arc;
use std::time::Instant;

fn to_quantity(s: &str) -> Quantity {
    Quantity(s.to_string())
}

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
    ResourceRequirements {
        limits: Some(limits),
        requests: Some(requests),
        claims: None,
    }
}

fn sanitize_ns(ns: &str) -> String {
    ns.chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() {
                c.to_ascii_uppercase()
            } else {
                '_'
            }
        })
        .collect::<String>()
}

// compute_backoff_secs_for_rental provides deterministic per-rental jittered backoff.

fn compute_backoff_secs_for_rental(ns: &str, rental_name: &str) -> i64 {
    let base = std::env::var("BASILICA_QUEUE_ADMIT_BACKOFF_SECS")
        .ok()
        .and_then(|v| v.parse::<i64>().ok())
        .unwrap_or(10);
    let ns_key = format!("BASILICA_QUEUE_BACKOFF_NS_{}", sanitize_ns(ns));
    let per_ns = std::env::var(&ns_key)
        .ok()
        .and_then(|v| v.parse::<i64>().ok())
        .unwrap_or(base);
    let jitter_range = std::env::var("BASILICA_QUEUE_JITTER_SECS")
        .ok()
        .and_then(|v| v.parse::<i64>().ok())
        .unwrap_or(5)
        .max(0);
    if jitter_range == 0 {
        return per_ns;
    }
    use std::hash::{Hash, Hasher};
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    ns.hash(&mut hasher);
    rental_name.hash(&mut hasher);
    let h = (hasher.finish() % ((jitter_range as u64) + 1)) as i64;
    per_ns + h
}

fn build_env(env: &[crate::crd::gpu_rental::EnvVar]) -> Vec<EnvVar> {
    env.iter()
        .map(|e| EnvVar {
            name: e.name.clone(),
            value: Some(e.value.clone()),
            ..Default::default()
        })
        .collect()
}

fn build_tolerations() -> Vec<Toleration> {
    vec![Toleration {
        key: Some("basilica.ai/workloads-only".into()),
        operator: Some("Equal".into()),
        value: Some("true".into()),
        effect: Some("NoSchedule".into()),
        ..Default::default()
    }]
}

fn build_node_affinity(gpu: &GpuSpec) -> Option<Affinity> {
    if gpu.model.is_empty() {
        return None;
    }
    let expr = NodeSelectorRequirement {
        key: "basilica.ai/gpu-model".into(),
        operator: "In".into(),
        values: Some(gpu.model.clone()),
    };
    let term = NodeSelectorTerm {
        match_expressions: Some(vec![expr]),
        match_fields: None,
    };
    let ns = NodeSelector {
        node_selector_terms: vec![term],
    };
    Some(Affinity {
        node_affinity: Some(NodeAffinity {
            required_during_scheduling_ignored_during_execution: Some(ns),
            ..Default::default()
        }),
        ..Default::default()
    })
}

fn build_security_contexts() -> (Option<PodSecurityContext>, Option<SecurityContext>) {
    let pod_sc = Some(PodSecurityContext {
        run_as_non_root: Some(true),
        seccomp_profile: Some(k8s_openapi::api::core::v1::SeccompProfile {
            type_: "RuntimeDefault".into(),
            localhost_profile: None,
        }),
        ..Default::default()
    });
    let container_sc = Some(SecurityContext {
        allow_privilege_escalation: Some(false),
        read_only_root_filesystem: Some(true),
        capabilities: Some(Capabilities {
            drop: Some(vec!["ALL".into()]),
            ..Default::default()
        }),
        seccomp_profile: Some(k8s_openapi::api::core::v1::SeccompProfile {
            type_: "RuntimeDefault".into(),
            localhost_profile: None,
        }),
        ..Default::default()
    });
    (pod_sc, container_sc)
}

fn merge_labels(
    base: Vec<(String, String)>,
    extra: Option<Vec<(String, String)>>,
) -> std::collections::BTreeMap<String, String> {
    let mut map: std::collections::BTreeMap<String, String> = base.into_iter().collect();
    if let Some(extra) = extra {
        for (k, v) in extra {
            map.insert(k, v);
        }
    }
    map
}

pub fn render_rental_pod(
    name: &str,
    spec: &GpuRentalSpec,
    extra_labels: Option<Vec<(String, String)>>,
) -> Pod {
    let (pod_sc, container_sc) = build_security_contexts();

    // Main container
    let mut containers = vec![Container {
        name: format!("rental-{}", name),
        image: Some(spec.container.image.clone()),
        command: if spec.container.command.is_empty() {
            None
        } else {
            Some(spec.container.command.clone())
        },
        env: Some(build_env(&spec.container.env)),
        ports: Some(
            spec.container
                .ports
                .iter()
                .map(|p| k8s_openapi::api::core::v1::ContainerPort {
                    container_port: p.container_port as i32,
                    protocol: Some(p.protocol.clone()),
                    ..Default::default()
                })
                .collect(),
        ),
        resources: Some(build_resources(
            &spec.container.resources.gpus,
            &spec.container.resources.cpu,
            &spec.container.resources.memory,
        )),
        volume_mounts: None,
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
                    ports: Some(vec![k8s_openapi::api::core::v1::ContainerPort {
                        container_port: 22,
                        ..Default::default()
                    }]),
                    security_context: container_sc.clone(),
                    ..Default::default()
                });
            }
        }
        AccessType::Jupyter => {
            containers.push(Container {
                name: "jupyter".into(),
                image: Some(
                    spec.jupyter_access
                        .as_ref()
                        .and_then(|j| j.base_image.clone())
                        .unwrap_or_else(|| "jupyter/tensorflow-notebook:latest".into()),
                ),
                ports: Some(vec![k8s_openapi::api::core::v1::ContainerPort {
                    container_port: 8888,
                    ..Default::default()
                }]),
                security_context: container_sc.clone(),
                ..Default::default()
            });
        }
        AccessType::Vscode | AccessType::Custom => {}
    }

    // Optional artifacts sidecar
    if let Some(art) = &spec.artifacts {
        if art.enabled {
            let mut env = Vec::new();
            env.push(EnvVar {
                name: "DESTINATION".into(),
                value: Some(art.destination.clone()),
                ..Default::default()
            });
            env.push(EnvVar {
                name: "FROM_PATH".into(),
                value: Some(art.from_path.clone()),
                ..Default::default()
            });
            env.push(EnvVar {
                name: "PROVIDER".into(),
                value: Some(if art.provider.is_empty() {
                    "s3".into()
                } else {
                    art.provider.clone()
                }),
                ..Default::default()
            });
            let env_from: Option<Vec<EnvFromSource>> =
                art.credentials_secret.as_ref().map(|name| {
                    vec![EnvFromSource {
                        secret_ref: Some(SecretEnvSource {
                            name: Some(name.clone()),
                            optional: Some(false),
                        }),
                        ..Default::default()
                    }]
                });
            containers.push(Container {
                name: format!("artifact-uploader-{}", name),
                image: Some("basilica/artifact-uploader:latest".into()),
                command: Some(vec!["/uploader".into()]),
                env: Some(env),
                env_from,
                security_context: container_sc.clone(),
                ..Default::default()
            });
        }
    }

    // Volumes and mounts
    let mut volumes: Vec<Volume> = Vec::new();
    let mut mounts: Vec<VolumeMount> = Vec::new();
    if let Some(st) = &spec.storage {
        volumes.push(Volume {
            name: "data".into(),
            persistent_volume_claim: Some(PersistentVolumeClaimVolumeSource {
                claim_name: format!("rental-pvc-{}", name),
                ..Default::default()
            }),
            ..Default::default()
        });
        mounts.push(VolumeMount {
            name: "data".into(),
            mount_path: st.mount_path.clone(),
            read_only: Some(false),
            ..Default::default()
        });
    }

    // Additional volumes from spec.container.volumes
    let allow_hostpath = std::env::var("BASILICA_ALLOW_HOSTPATH")
        .ok()
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(false);
    for (idx, v) in spec.container.volumes.iter().enumerate() {
        let vol_name = format!("vol-{}", idx);
        if let Some(hp) = v.host_path.as_ref() {
            if allow_hostpath {
                volumes.push(Volume {
                    name: vol_name.clone(),
                    host_path: Some(HostPathVolumeSource {
                        path: hp.clone(),
                        type_: None,
                    }),
                    ..Default::default()
                });
                mounts.push(VolumeMount {
                    name: vol_name,
                    mount_path: v.container_path.clone(),
                    read_only: Some(v.read_only),
                    ..Default::default()
                });
            } else {
                // HostPath not allowed; skip
            }
        } else {
            // Ephemeral emptyDir volume
            volumes.push(Volume {
                name: vol_name.clone(),
                empty_dir: Some(EmptyDirVolumeSource {
                    ..Default::default()
                }),
                ..Default::default()
            });
            mounts.push(VolumeMount {
                name: vol_name,
                mount_path: v.container_path.clone(),
                read_only: Some(v.read_only),
                ..Default::default()
            });
        }
    }

    if !mounts.is_empty() {
        if let Some(vm) = &mut containers[0].volume_mounts {
            vm.extend(mounts);
        } else {
            containers[0].volume_mounts = Some(mounts);
        }
    }

    let mut base_labels: std::collections::BTreeMap<String, String> = vec![
        ("basilica.ai/type".to_string(), "rental".to_string()),
        ("basilica.ai/rental".to_string(), name.to_string()),
    ]
    .into_iter()
    .collect();
    let gpu_bound = (spec.container.resources.gpus.count > 0).to_string();
    base_labels.insert("basilica.ai/gpu-bound".to_string(), gpu_bound);
    let labels = Some(merge_labels(
        base_labels.into_iter().collect(),
        extra_labels.clone(),
    ));

    let mut tolerations = build_tolerations();
    if spec.exclusive {
        tolerations.push(Toleration {
            key: Some("basilica.ai/rental-exclusive".into()),
            operator: Some("Equal".into()),
            value: Some("true".into()),
            effect: Some("NoSchedule".into()),
            ..Default::default()
        });
    }

    Pod {
        metadata: ObjectMeta {
            name: Some(format!("rental-{}", name)),
            labels: labels.clone(),
            ..Default::default()
        },
        spec: Some(PodSpec {
            containers,
            volumes: if volumes.is_empty() {
                None
            } else {
                Some(volumes)
            },
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
    if spec.network.ingress.is_empty() {
        return None;
    }
    let svc_type = if spec.network.public_ip_required
        || spec
            .network
            .ingress
            .iter()
            .any(|r| r.exposure.eq_ignore_ascii_case("LoadBalancer"))
    {
        "LoadBalancer"
    } else {
        "NodePort"
    };

    let ports: Vec<ServicePort> = spec
        .network
        .ingress
        .iter()
        .map(|r| ServicePort {
            port: r.port as i32,
            target_port: Some(IntOrString::Int(r.port as i32)),
            protocol: Some("TCP".into()),
            ..Default::default()
        })
        .collect();
    let selector = Some(
        vec![("basilica.ai/rental".to_string(), name.to_string())]
            .into_iter()
            .collect(),
    );
    Some(Service {
        metadata: ObjectMeta {
            name: Some(format!("rental-svc-{}", name)),
            labels: Some(
                vec![("basilica.ai/rental".into(), name.into())]
                    .into_iter()
                    .collect(),
            ),
            ..Default::default()
        },
        spec: Some(ServiceSpec {
            type_: Some(svc_type.into()),
            selector,
            ports: Some(ports),
            ..Default::default()
        }),
        ..Default::default()
    })
}

fn sanitize_name_part(s: &str) -> String {
    let mut out = String::new();
    for ch in s.chars() {
        if ch.is_ascii_lowercase() || ch.is_ascii_digit() || ch == '-' {
            out.push(ch);
        } else if ch.is_ascii_uppercase() {
            out.push(ch.to_ascii_lowercase());
        } else {
            out.push('-');
        }
        if out.len() >= 40 {
            break;
        }
    }
    while out.ends_with('-') {
        out.pop();
    }
    if out.is_empty() {
        "grp".into()
    } else {
        out
    }
}

pub fn render_discovery_headless_service(group: &str, ports: &[(u16, &str)]) -> Service {
    let name = format!("rental-disc-{}", sanitize_name_part(group));
    let svc_ports: Vec<ServicePort> = ports
        .iter()
        .map(|(p, proto)| ServicePort {
            port: *p as i32,
            target_port: Some(IntOrString::Int(*p as i32)),
            protocol: Some(proto.to_string()),
            ..Default::default()
        })
        .collect();
    let labels = vec![
        (
            "basilica.ai/type".to_string(),
            "rental-discovery".to_string(),
        ),
        ("basilica.ai/discovery-group".to_string(), group.to_string()),
    ]
    .into_iter()
    .collect();
    Service {
        metadata: ObjectMeta {
            name: Some(name),
            labels: Some(labels),
            ..Default::default()
        },
        spec: Some(ServiceSpec {
            cluster_ip: Some("None".into()),
            selector: Some(
                vec![("basilica.ai/discovery-group".to_string(), group.to_string())]
                    .into_iter()
                    .collect(),
            ),
            ports: if svc_ports.is_empty() {
                None
            } else {
                Some(svc_ports)
            },
            ..Default::default()
        }),
        ..Default::default()
    }
}

fn render_http_route(
    name: &str,
    ns: &str,
    gateway_name: &str,
    hostnames: &[String],
    backend_service: &str,
    port: u16,
) -> anyhow::Result<DynamicObject> {
    let route_name = format!("rental-route-{}", name);
    let val = serde_json::json!({
        "apiVersion": "gateway.networking.k8s.io/v1",
        "kind": "HTTPRoute",
        "metadata": {"name": route_name, "namespace": ns},
        "spec": {
            "parentRefs": [{"name": gateway_name}],
            "hostnames": hostnames,
            "rules": [
                {
                    "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                    "backendRefs": [{"name": backend_service, "port": port}]
                }
            ]
        }
    });
    let obj: DynamicObject = serde_json::from_value(val)?;
    Ok(obj)
}

pub fn render_network_policies(name: &str, spec: &GpuRentalSpec) -> Vec<NetworkPolicy> {
    let pod_selector = Some(
        vec![("basilica.ai/rental".to_string(), name.to_string())]
            .into_iter()
            .collect(),
    );

    // Default deny for both ingress and egress
    let default_deny = NetworkPolicy {
        metadata: ObjectMeta {
            name: Some(format!("rental-np-deny-{}", name)),
            ..Default::default()
        },
        spec: Some(NetworkPolicySpec {
            pod_selector: k8s_openapi::apimachinery::pkg::apis::meta::v1::LabelSelector {
                match_labels: pod_selector.clone(),
                ..Default::default()
            },
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
            .map(|r| NetworkPolicyPort {
                port: Some(IntOrString::Int(r.port as i32)),
                protocol: Some("TCP".into()),
                ..Default::default()
            })
            .collect();
        rules.push(NetworkPolicyIngressRule {
            ports: Some(ports),
            from: Some(vec![NetworkPolicyPeer::default()]),
        });
    }

    let allow_ingress = NetworkPolicy {
        metadata: ObjectMeta {
            name: Some(format!("rental-np-allow-{}", name)),
            ..Default::default()
        },
        spec: Some(NetworkPolicySpec {
            pod_selector: k8s_openapi::apimachinery::pkg::apis::meta::v1::LabelSelector {
                match_labels: pod_selector,
                ..Default::default()
            },
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
            "open" | "egress-only" => vec![NetworkPolicyEgressRule {
                to: None,
                ports: None,
            }],
            // Restricted: allow only listed CIDRs
            _ => {
                let mut rules: Vec<NetworkPolicyEgressRule> = Vec::new();
                for dest in &spec.network.allowed_egress {
                    // Only CIDR strings are supported in NetworkPolicy IPBlock
                    if dest.contains('/') {
                        let peer = NetworkPolicyPeer {
                            ip_block: Some(IPBlock {
                                cidr: dest.clone(),
                                except: None,
                            }),
                            ..Default::default()
                        };
                        rules.push(NetworkPolicyEgressRule {
                            to: Some(vec![peer]),
                            ports: None,
                        });
                    }
                }
                rules
            }
        };
        egress_policies.push(NetworkPolicy {
            metadata: ObjectMeta {
                name: Some(format!("rental-np-egress-{}", name)),
                ..Default::default()
            },
            spec: Some(NetworkPolicySpec {
                pod_selector: k8s_openapi::apimachinery::pkg::apis::meta::v1::LabelSelector {
                    match_labels: Some(
                        vec![("basilica.ai/rental".into(), name.into())]
                            .into_iter()
                            .collect(),
                    ),
                    ..Default::default()
                },
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
    requests.insert(
        "storage".into(),
        Quantity(format!("{}Gi", st.persistent_volume_gb)),
    );
    Some(PersistentVolumeClaim {
        metadata: ObjectMeta {
            name: Some(format!("rental-pvc-{}", name)),
            ..Default::default()
        },
        spec: Some(PersistentVolumeClaimSpec {
            access_modes: Some(vec!["ReadWriteOnce".into()]),
            resources: Some(VolumeResourceRequirements {
                requests: Some(requests),
                ..Default::default()
            }),
            storage_class_name: st.storage_class.clone(),
            ..Default::default()
        }),
        ..Default::default()
    })
}

#[derive(Clone)]
pub struct RentalController<C: K8sClient> {
    pub client: C,
    pub billing: Arc<dyn BillingClient + Send + Sync>,
    pub metrics_provider: Arc<dyn RuntimeMetricsProvider + Send + Sync>,
}

impl<C: K8sClient> RentalController<C> {
    pub fn new(client: C, billing: impl BillingClient + 'static) -> Self {
        Self {
            client,
            billing: Arc::new(billing),
            metrics_provider: Arc::new(NoopRuntimeMetricsProvider),
        }
    }
    pub fn new_with_arc(client: C, billing: Arc<dyn BillingClient + Send + Sync>) -> Self {
        Self {
            client,
            billing,
            metrics_provider: Arc::new(NoopRuntimeMetricsProvider),
        }
    }
    pub fn with_metrics_provider(
        mut self,
        provider: Arc<dyn RuntimeMetricsProvider + Send + Sync>,
    ) -> Self {
        self.metrics_provider = provider;
        self
    }

    pub async fn reconcile(&self, ns: &str, cr: &GpuRental) -> Result<()> {
        let start = Instant::now();
        let name = cr.metadata.name.clone().unwrap_or_default();
        let spec = cr.spec.clone();
        let prev_status = self
            .client
            .get_gpu_rental(ns, &name)
            .await
            .ok()
            .and_then(|r| r.status)
            .unwrap_or_default();
        let prev_state = prev_status
            .state
            .clone()
            .unwrap_or_else(|| "Unknown".into());

        // Backoff to avoid thrash: if previously Queued, wait a short interval before admitting
        if prev_state == "Queued" {
            let backoff_secs = compute_backoff_secs_for_rental(ns, &name);
            if backoff_secs > 0 {
                if let Some(ts) = prev_status
                    .renewal_time
                    .as_ref()
                    .and_then(|s| DateTime::parse_from_rfc3339(s).ok())
                    .map(|dt| dt.with_timezone(&Utc))
                {
                    if (Utc::now() - ts).num_seconds() < backoff_secs {
                        // Remain queued; do not update renewal_time to preserve window start
                        return Ok(());
                    }
                }
            }
        }

        // Enforce BasilicaQueue concurrency and GPU model limits (if configured) before creating resources
        if let Ok(queues) = self.client.list_basilica_queues(ns).await {
            if let Some(q) = queues.first() {
                // Count Running + Pending rental pods in namespace
                let pods = self
                    .client
                    .list_pods_with_label(ns, "basilica.ai/type", "rental")
                    .await
                    .unwrap_or_default();
                let running_or_pending = pods
                    .iter()
                    .filter(|p| {
                        p.status
                            .as_ref()
                            .and_then(|s| s.phase.as_deref())
                            .map(|ph| ph == "Running" || ph == "Pending")
                            .unwrap_or(false)
                    })
                    .count() as u32;
                if running_or_pending >= q.spec.concurrency {
                    let queued = GpuRentalStatus {
                        state: Some("Queued".into()),
                        pod_name: None,
                        node_name: None,
                        start_time: None,
                        expiry_time: None,
                        renewal_time: Some(Utc::now().to_rfc3339()),
                        total_cost: None,
                        total_extensions: None,
                        endpoints: None,
                    };
                    self.client
                        .update_gpu_rental_status(ns, &name, queued)
                        .await?;
                    return Ok(());
                }

                // Optional: enforce GPU total and per-model limits
                if let Some(ref limits) = q.spec.gpu_limits {
                    // Sum current GPUs across active/provisioning pods
                    let mut total_gpus_in_use: u32 = 0;
                    let mut model_gpus: std::collections::BTreeMap<String, u32> =
                        std::collections::BTreeMap::new();

                    for p in &pods {
                        // Count GPUs requested by containers
                        let mut pod_gpu_count: u32 = 0;
                        if let Some(spec) = &p.spec {
                            for c in spec.containers.iter() {
                                if let Some(res) = &c.resources {
                                    if let Some(req) = &res.requests {
                                        if let Some(q) = req.get("nvidia.com/gpu") {
                                            if let Ok(v) = q.0.parse::<u32>() {
                                                pod_gpu_count += v;
                                            }
                                        }
                                    }
                                }
                            }

                            // Attribute GPUs to models based on node affinity selector
                            if let Some(aff) = &spec.affinity {
                                if let Some(na) = &aff.node_affinity {
                                    if let Some(req) =
                                        &na.required_during_scheduling_ignored_during_execution
                                    {
                                        for term in &req.node_selector_terms {
                                            if let Some(exprs) = &term.match_expressions {
                                                for expr in exprs {
                                                    if expr.key == "basilica.ai/gpu-model" {
                                                        if let Some(values) = &expr.values {
                                                            if pod_gpu_count > 0 {
                                                                for m in values {
                                                                    *model_gpus
                                                                        .entry(m.clone())
                                                                        .or_insert(0) +=
                                                                        pod_gpu_count;
                                                                }
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        if pod_gpu_count > 0 {
                            total_gpus_in_use += pod_gpu_count;
                        }
                    }

                    // Include the current request
                    let requested_gpu_count = spec.container.resources.gpus.count;
                    let requested_models = if spec.container.resources.gpus.model.is_empty() {
                        vec!["any".to_string()]
                    } else {
                        spec.container.resources.gpus.model.clone()
                    };

                    if requested_gpu_count > 0 {
                        if limits.total > 0
                            && total_gpus_in_use.saturating_add(requested_gpu_count) > limits.total
                        {
                            let queued = GpuRentalStatus {
                                state: Some("Queued".into()),
                                pod_name: None,
                                node_name: None,
                                start_time: None,
                                expiry_time: None,
                                renewal_time: Some(Utc::now().to_rfc3339()),
                                total_cost: None,
                                total_extensions: None,
                                endpoints: None,
                            };
                            self.client
                                .update_gpu_rental_status(ns, &name, queued)
                                .await?;
                            return Ok(());
                        }

                        if let Some(ref per_model) = limits.models {
                            for m in requested_models {
                                if let Some(&cap) = per_model.get(&m) {
                                    let current = *model_gpus.get(&m).unwrap_or(&0);
                                    if current.saturating_add(requested_gpu_count) > cap {
                                        let queued = GpuRentalStatus {
                                            state: Some("Queued".into()),
                                            pod_name: None,
                                            node_name: None,
                                            start_time: None,
                                            expiry_time: None,
                                            renewal_time: Some(Utc::now().to_rfc3339()),
                                            total_cost: None,
                                            total_extensions: None,
                                            endpoints: None,
                                        };
                                        self.client
                                            .update_gpu_rental_status(ns, &name, queued)
                                            .await?;
                                        return Ok(());
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }

        // Ensure PVC if requested
        if spec.storage.is_some() {
            if let Some(pvc) = render_rental_pvc(&name, &spec) {
                // Best-effort create; mock will overwrite in-memory
                let _ = self.client.create_pvc(ns, &pvc).await;
            }
        }

        // Ensure Pod exists
        // Optional discovery group for peer DNS: use CR metadata label
        let discovery_label = cr
            .metadata
            .labels
            .as_ref()
            .and_then(|m| m.get("basilica.ai/discovery-group").cloned());
        let extra_labels = discovery_label
            .as_ref()
            .map(|g| vec![("basilica.ai/discovery-group".to_string(), g.clone())]);

        let pod = render_rental_pod(&name, &spec, extra_labels.clone());
        // Create or replace
        let _ = self.client.create_pod(ns, &pod).await;

        // Ensure Service if ingress requested
        if let Some(svc) = render_rental_service(&name, &spec) {
            let _ = self.client.create_service(ns, &svc).await;
        }
        // Create headless discovery Service when group label is present and ports exist
        if let Some(group) = discovery_label.as_deref() {
            if !spec.container.ports.is_empty() {
                let ports: Vec<(u16, &str)> = spec
                    .container
                    .ports
                    .iter()
                    .map(|p| (p.container_port, p.protocol.as_str()))
                    .collect();
                let disc = render_discovery_headless_service(group, &ports);
                let _ = self.client.create_service(ns, &disc).await;
            }
        }

        // Auto-generate HTTPRoute when annotation is present
        if let Some(ann) = cr.metadata.annotations.as_ref() {
            if let Some(hosts_str) = ann.get("basilica.ai/route-host") {
                let hostnames: Vec<String> = hosts_str
                    .split(',')
                    .map(|s| s.trim().to_string())
                    .filter(|s| !s.is_empty())
                    .collect();
                if !hostnames.is_empty() {
                    let gw_name = ann
                        .get("basilica.ai/route-gateway")
                        .cloned()
                        .unwrap_or_else(|| "basilica-gw".to_string());
                    let port: u16 = ann
                        .get("basilica.ai/route-port")
                        .and_then(|v| v.parse::<u16>().ok())
                        .or_else(|| spec.network.ingress.first().map(|r| r.port))
                        .or_else(|| spec.container.ports.first().map(|p| p.container_port))
                        .unwrap_or(80);
                    let backend = format!("rental-svc-{}", name);
                    if let Ok(route) =
                        render_http_route(&name, ns, &gw_name, &hostnames, &backend, port)
                    {
                        let _ = self.client.create_http_route(ns, &route).await;
                    }
                }
            }
        }

        // Ensure NetworkPolicies
        for np in render_network_policies(&name, &spec) {
            let _ = self.client.create_network_policy(ns, &np).await;
        }
        // Record netpol mode (egress policy label)
        opmetrics::record_rental_netpol(&spec.network.egress_policy, ns);

        // Derive status and enforce pay-as-you-go billing policy
        let pods = self
            .client
            .list_pods_with_label(ns, "basilica.ai/rental", &name)
            .await?;
        let (state, pod_name) = compute_rental_state_from_pods(&pods);
        let mut start_time_str = prev_status.start_time.clone();
        if state == "Active" && start_time_str.is_none() {
            start_time_str = Some(Utc::now().to_rfc3339());
        }

        // Compute endpoints from Services
        let mut endpoints: Option<Vec<String>> = None;
        let services = self
            .client
            .list_services_with_label(ns, "basilica.ai/rental", &name)
            .await
            .unwrap_or_default();
        if !services.is_empty() {
            let mut eps = Vec::new();
            for svc in services {
                if let Some(specsvc) = svc.spec.as_ref() {
                    let ty = specsvc.type_.as_deref().unwrap_or("ClusterIP");
                    if let Some(ports) = specsvc.ports.as_ref() {
                        for p in ports {
                            let port = p.port;
                            eps.push(format!("{}:{}", ty, port));
                        }
                    }
                }
            }
            if !eps.is_empty() {
                endpoints = Some(eps);
            }
        }

        let mut status = GpuRentalStatus {
            state: Some(state.clone()),
            pod_name: pod_name.clone(),
            node_name: None,
            start_time: start_time_str.clone(),
            expiry_time: None,
            renewal_time: None,
            total_cost: None,
            total_extensions: None,
            endpoints,
        };

        // Pay-as-you-go: terminate if out of credits
        if state == "Active"
            && self
                .billing
                .should_terminate(cr, &status)
                .await
                .unwrap_or(false)
        {
            // Best-effort delete workload resources
            if let Some(pn) = pod_name.as_deref() {
                let _ = self.client.delete_pod(ns, pn).await;
            }
            let _ = self
                .client
                .delete_service(ns, &format!("rental-svc-{}", name))
                .await;
            status.state = Some("Terminated".into());
            status.expiry_time = Some(Utc::now().to_rfc3339());
            self.client
                .update_gpu_rental_status(ns, &name, status.clone())
                .await?;
            opmetrics::record_rental_termination(ns, "OutOfCredits");
            opmetrics::record_rental_active_change(ns, true, false);
            // Emit usage event
            let rm: Option<RuntimeMetrics> = if let Some(pn) = status.pod_name.as_deref() {
                self.metrics_provider.fetch_pod_metrics(ns, pn).await
            } else {
                None
            };
            let _ = self
                .billing
                .emit_usage_event(cr, &status, rm.as_ref())
                .await;
            return Ok(());
        }

        // Persist status
        let status_state = status.state.clone().unwrap_or_else(|| "Unknown".into());
        self.client
            .update_gpu_rental_status(ns, &name, status.clone())
            .await?;
        // Emit usage event (best-effort)
        let rm: Option<RuntimeMetrics> = if let Some(pn) = status.pod_name.as_deref() {
            self.metrics_provider.fetch_pod_metrics(ns, pn).await
        } else {
            None
        };
        let _ = self
            .billing
            .emit_usage_event(cr, &status, rm.as_ref())
            .await;
        let created = true; // Pod and associated resources are ensured; for metrics, treat as created/ensured event
        opmetrics::record_rental_reconcile(ns, &name, created, &prev_state, &status_state, start);
        let prev_active = prev_state == "Active";
        let new_active = status_state == "Active";
        opmetrics::record_rental_active_change(ns, prev_active, new_active);
        // If terminated from Active, record duration and termination counter
        if prev_active && !new_active {
            if let Some(st) = prev_status
                .start_time
                .as_ref()
                .and_then(|s| DateTime::parse_from_rfc3339(s).ok())
                .map(|dt| dt.with_timezone(&Utc))
            {
                let seconds = (Utc::now() - st).num_seconds().max(0) as f64;
                opmetrics::record_rental_active_duration(ns, seconds);
            }
            // reason is the new state (Suspended/Failed/etc.)
            opmetrics::record_rental_termination(ns, &status_state);
        }
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
        if let Some(st) = &p.status {
            if let Some(ph) = &st.phase {
                match ph.as_str() {
                    "Running" => running = n,
                    "Failed" => failed = n,
                    "Pending" => pending = n,
                    _ => {}
                }
            }
        }
    }
    if let Some(n) = running {
        return ("Active".into(), Some(n));
    }
    if let Some(n) = failed {
        return ("Failed".into(), Some(n));
    }
    if let Some(n) = pending {
        return ("Provisioning".into(), Some(n));
    }
    ("Provisioning".into(), None)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::billing::MockBillingClient;
    use crate::crd::gpu_rental::{
        RentalContainer, RentalNetwork, RentalPort, RentalStorage, Resources as RResources,
    };
    use crate::k8s_client::MockK8sClient;
    use k8s_openapi::api::core::v1::PodStatus;

    fn base_spec() -> GpuRentalSpec {
        GpuRentalSpec {
            container: RentalContainer {
                image: "img".into(),
                env: vec![crate::crd::gpu_rental::EnvVar {
                    name: "K".into(),
                    value: "V".into(),
                }],
                command: vec!["bash".into()],
                ports: vec![RentalPort {
                    container_port: 8080,
                    protocol: "TCP".into(),
                }],
                volumes: vec![],
                resources: RResources {
                    cpu: "2".into(),
                    memory: "4Gi".into(),
                    gpus: GpuSpec {
                        count: 1,
                        model: vec!["A100".into()],
                    },
                },
            },
            duration: crate::crd::gpu_rental::RentalDuration {
                hours: 24,
                auto_extend: false,
                max_extensions: 0,
            },
            access_type: AccessType::Ssh,
            network: RentalNetwork {
                ingress: vec![],
                egress_policy: "restricted".into(),
                allowed_egress: vec![],
                public_ip_required: false,
                bandwidth_mbps: None,
            },
            storage: None,
            artifacts: None,
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
        let pod = render_rental_pod("r1", &spec, None);
        let p = pod.spec.unwrap();
        let c = &p.containers[0];
        assert_eq!(c.image.as_deref(), Some("img"));
        let res = c.resources.as_ref().unwrap();
        assert_eq!(
            res.limits
                .as_ref()
                .unwrap()
                .get("nvidia.com/gpu")
                .unwrap()
                .0,
            "1"
        );
        assert_eq!(c.ports.as_ref().unwrap()[0].container_port, 8080);
        let csc = c.security_context.as_ref().unwrap();
        assert!(csc.read_only_root_filesystem.unwrap());
        assert_eq!(
            csc.seccomp_profile.as_ref().unwrap().type_,
            "RuntimeDefault"
        );
        assert_eq!(
            p.security_context
                .as_ref()
                .unwrap()
                .seccomp_profile
                .as_ref()
                .unwrap()
                .type_,
            "RuntimeDefault"
        );
        // Labels present
        assert_eq!(
            pod.metadata
                .labels
                .as_ref()
                .unwrap()
                .get("basilica.ai/rental")
                .unwrap(),
            "r1"
        );
    }

    #[test]
    fn service_chooses_type_and_ports() {
        let mut spec = base_spec();
        spec.network.ingress = vec![crate::crd::gpu_rental::IngressRule {
            port: 8080,
            exposure: "NodePort".into(),
        }];
        let svc = render_rental_service("r1", &spec).unwrap();
        assert_eq!(
            svc.spec.as_ref().unwrap().type_.as_deref(),
            Some("NodePort")
        );
        assert_eq!(
            svc.spec.as_ref().unwrap().ports.as_ref().unwrap()[0].port,
            8080
        );

        spec.network.public_ip_required = true;
        let svc2 = render_rental_service("r1", &spec).unwrap();
        assert_eq!(
            svc2.spec.as_ref().unwrap().type_.as_deref(),
            Some("LoadBalancer")
        );
    }

    #[test]
    fn network_policies_default_deny_and_allow_ingress() {
        let mut spec = base_spec();
        spec.network.ingress = vec![crate::crd::gpu_rental::IngressRule {
            port: 8080,
            exposure: "NodePort".into(),
        }];
        let nps = render_network_policies("r1", &spec);
        assert_eq!(nps.len(), 3);
        let deny = &nps[0];
        assert!(deny
            .spec
            .as_ref()
            .unwrap()
            .ingress
            .as_ref()
            .unwrap()
            .is_empty());
        let allow = &nps[1];
        assert!(!allow
            .spec
            .as_ref()
            .unwrap()
            .ingress
            .as_ref()
            .unwrap()
            .is_empty());
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
        assert!(egress_np
            .spec
            .as_ref()
            .unwrap()
            .policy_types
            .as_ref()
            .unwrap()
            .contains(&"Egress".into()));
    }

    #[test]
    fn pvc_renders_when_storage_specified() {
        let mut spec = base_spec();
        spec.storage = Some(RentalStorage {
            persistent_volume_gb: 200,
            storage_class: Some("fast-ssd".into()),
            mount_path: "/data".into(),
        });
        let pvc = render_rental_pvc("r1", &spec).unwrap();
        assert_eq!(pvc.metadata.name.as_deref(), Some("rental-pvc-r1"));
        let pod = render_rental_pod("r1", &spec, None);
        assert_eq!(
            pod.spec.as_ref().unwrap().volumes.as_ref().unwrap()[0].name,
            "data"
        );
    }

    #[test]
    fn emptydir_volume_from_container_volumes_is_mounted() {
        let mut spec = base_spec();
        // Add a non-hostPath volume
        spec.container
            .volumes
            .push(crate::crd::gpu_rental::VolumeMount {
                host_path: None,
                container_path: "/work".into(),
                read_only: false,
            });
        let pod = render_rental_pod("r2", &spec, None);
        let podspec = pod.spec.as_ref().unwrap();
        let vols = podspec.volumes.as_ref().unwrap();
        assert!(vols
            .iter()
            .any(|v| v.name.starts_with("vol-") && v.empty_dir.is_some()));
        let mounts = podspec.containers[0].volume_mounts.as_ref().unwrap();
        assert!(mounts.iter().any(|m| m.mount_path == "/work"));
    }

    #[test]
    fn hostpath_volume_is_gated_by_env() {
        let mut spec = base_spec();
        // hostPath provided
        spec.container
            .volumes
            .push(crate::crd::gpu_rental::VolumeMount {
                host_path: Some("/var/scratch".into()),
                container_path: "/scratch".into(),
                read_only: true,
            });

        // By default, hostPath not allowed => should not appear
        std::env::remove_var("BASILICA_ALLOW_HOSTPATH");
        let pod = render_rental_pod("r3", &spec, None);
        let podspec = pod.spec.as_ref().unwrap();
        if let Some(vols) = &podspec.volumes {
            assert!(!vols.iter().any(|v| v.host_path.is_some()));
        }
        if let Some(mounts) = &podspec.containers[0].volume_mounts {
            // No mount for /scratch if hostPath excluded
            assert!(!mounts.iter().any(|m| m.mount_path == "/scratch"));
        }

        // Enable hostPath
        std::env::set_var("BASILICA_ALLOW_HOSTPATH", "true");
        let pod2 = render_rental_pod("r3", &spec, None);
        let ps2 = pod2.spec.as_ref().unwrap();
        let vols2 = ps2.volumes.as_ref().unwrap();
        assert!(vols2
            .iter()
            .any(|v| v.host_path.as_ref().map(|h| h.path.as_str()) == Some("/var/scratch")));
        let mounts2 = ps2.containers[0].volume_mounts.as_ref().unwrap();
        assert!(mounts2
            .iter()
            .any(|m| m.mount_path == "/scratch" && m.read_only == Some(true)));
        // Cleanup env
        std::env::remove_var("BASILICA_ALLOW_HOSTPATH");
    }
    #[test]
    fn artifacts_sidecar_renders_when_enabled() {
        let mut spec = base_spec();
        spec.artifacts = Some(crate::crd::gpu_rental::RentalArtifacts {
            destination: "s3://bucket/prefix".into(),
            from_path: "/outputs".into(),
            provider: "s3".into(),
            credentials_secret: None,
            enabled: true,
        });
        let pod = render_rental_pod("artifacts", &spec, None);
        let containers = &pod.spec.as_ref().unwrap().containers;
        assert!(containers
            .iter()
            .any(|c| c.name.starts_with("artifact-uploader-")));
        let sidecar = containers
            .iter()
            .find(|c| c.name.starts_with("artifact-uploader-"))
            .unwrap();
        let envs = sidecar.env.as_ref().unwrap();
        assert!(envs.iter().any(|e| e.name == "DESTINATION"));
        assert!(envs.iter().any(|e| e.name == "FROM_PATH"));
    }

    #[test]
    fn exclusive_rental_adds_toleration() {
        let mut spec = base_spec();
        spec.exclusive = true;
        let pod = render_rental_pod("rX", &spec, None);
        let tols = pod.spec.as_ref().unwrap().tolerations.as_ref().unwrap();
        assert!(tols
            .iter()
            .any(|t| t.key.as_deref() == Some("basilica.ai/rental-exclusive")
                && t.value.as_deref() == Some("true")));
    }

    #[tokio::test]
    async fn reconcile_creates_resources_and_updates_status() {
        let _ = metrics_exporter_prometheus::PrometheusBuilder::new().install_recorder();
        let client = MockK8sClient::default();
        let controller = RentalController::new(client.clone(), MockBillingClient::default());

        let mut spec = base_spec();
        spec.network.ingress = vec![crate::crd::gpu_rental::IngressRule {
            port: 8080,
            exposure: "NodePort".into(),
        }];
        let cr = GpuRental::new("rent1", spec);
        controller
            .client
            .create_gpu_rental("ns", &cr)
            .await
            .unwrap();

        // First reconcile, no pods yet -> Provisioning
        controller.reconcile("ns", &cr).await.unwrap();
        let updated = controller
            .client
            .get_gpu_rental("ns", "rent1")
            .await
            .unwrap();
        assert_eq!(
            updated.status.as_ref().unwrap().state.as_deref(),
            Some("Provisioning")
        );

        // Create a running pod and reconcile again
        let pod = Pod {
            metadata: ObjectMeta {
                name: Some("p1".into()),
                labels: Some(
                    vec![("basilica.ai/rental".into(), "rent1".into())]
                        .into_iter()
                        .collect(),
                ),
                ..Default::default()
            },
            status: Some(PodStatus {
                phase: Some("Running".into()),
                ..Default::default()
            }),
            ..Default::default()
        };
        controller.client.create_pod("ns", &pod).await.unwrap();
        controller.reconcile("ns", &cr).await.unwrap();
        let updated2 = controller
            .client
            .get_gpu_rental("ns", "rent1")
            .await
            .unwrap();
        assert_eq!(
            updated2.status.as_ref().unwrap().state.as_deref(),
            Some("Active")
        );
        assert_eq!(
            updated2.status.as_ref().unwrap().pod_name.as_deref(),
            Some("p1")
        );
        // Service should exist
        let svcs = controller
            .client
            .list_services_with_label("ns", "basilica.ai/rental", "rent1")
            .await
            .unwrap();
        assert!(!svcs.is_empty());
        // Endpoints should include the service port
        let eps = updated2
            .status
            .as_ref()
            .unwrap()
            .endpoints
            .as_ref()
            .unwrap();
        assert!(eps.iter().any(|e| e.contains("8080")));
        // Metrics present
        // Exercise metrics path (no-op if already installed)
        let _ = metrics_exporter_prometheus::PrometheusBuilder::new().install_recorder();
    }

    #[tokio::test]
    async fn queued_backoff_respected() {
        std::env::set_var("BASILICA_QUEUE_ADMIT_BACKOFF_SECS", "30");
        let _ = metrics_exporter_prometheus::PrometheusBuilder::new().install_recorder();
        let client = MockK8sClient::default();
        let controller = RentalController::new(client.clone(), MockBillingClient::default());

        // Queue CR with recent renewal_time
        let cr = GpuRental::new("rent3", base_spec());
        controller
            .client
            .create_gpu_rental("ns", &cr)
            .await
            .unwrap();
        let queued = GpuRentalStatus {
            state: Some("Queued".into()),
            pod_name: None,
            node_name: None,
            start_time: None,
            expiry_time: None,
            renewal_time: Some(k8s_openapi::chrono::Utc::now().to_rfc3339()),
            total_cost: None,
            total_extensions: None,
            endpoints: None,
        };
        controller
            .client
            .update_gpu_rental_status("ns", "rent3", queued)
            .await
            .unwrap();

        // No pods running; without backoff it would proceed; with backoff it should remain queued
        controller.reconcile("ns", &cr).await.unwrap();
        let updated = controller
            .client
            .get_gpu_rental("ns", "rent3")
            .await
            .unwrap();
        assert_eq!(
            updated.status.as_ref().unwrap().state.as_deref(),
            Some("Queued")
        );
    }

    #[tokio::test]
    async fn queue_gates_on_running_plus_pending() {
        let _ = metrics_exporter_prometheus::PrometheusBuilder::new().install_recorder();
        let client = MockK8sClient::default();
        let controller = RentalController::new(client.clone(), MockBillingClient::default());

        // Create a queue with concurrency 1
        let q = crate::crd::basilica_queue::BasilicaQueue::new(
            "q1",
            crate::crd::basilica_queue::BasilicaQueueSpec {
                concurrency: 1,
                gpu_limits: None,
            },
        );
        controller
            .client
            .create_basilica_queue("ns", &q)
            .await
            .unwrap();

        // Existing Pending pod (consumes the one slot)
        let p = Pod {
            metadata: ObjectMeta {
                name: Some("p-exist".into()),
                labels: Some(
                    vec![("basilica.ai/type".into(), "rental".into())]
                        .into_iter()
                        .collect(),
                ),
                ..Default::default()
            },
            status: Some(PodStatus {
                phase: Some("Pending".into()),
                ..Default::default()
            }),
            ..Default::default()
        };
        controller.client.create_pod("ns", &p).await.unwrap();

        // New rental should be queued
        let spec = base_spec();
        let cr = GpuRental::new("rent-q", spec);
        controller
            .client
            .create_gpu_rental("ns", &cr)
            .await
            .unwrap();
        controller.reconcile("ns", &cr).await.unwrap();
        let updated = controller
            .client
            .get_gpu_rental("ns", "rent-q")
            .await
            .unwrap();
        assert_eq!(
            updated.status.as_ref().unwrap().state.as_deref(),
            Some("Queued")
        );
    }

    #[tokio::test]
    async fn queue_enforces_gpu_model_limits() {
        let _ = metrics_exporter_prometheus::PrometheusBuilder::new().install_recorder();
        let client = MockK8sClient::default();
        let controller = RentalController::new(client.clone(), MockBillingClient::default());

        // Queue with per-model limit A100:1
        let mut models = std::collections::BTreeMap::new();
        models.insert("A100".to_string(), 1);
        let limits = crate::crd::basilica_queue::GpuLimits {
            total: 0,
            models: Some(models),
        };
        let q = crate::crd::basilica_queue::BasilicaQueue::new(
            "q2",
            crate::crd::basilica_queue::BasilicaQueueSpec {
                concurrency: 10,
                gpu_limits: Some(limits),
            },
        );
        controller
            .client
            .create_basilica_queue("ns", &q)
            .await
            .unwrap();

        // Existing Pod requesting A100 with 1 GPU
        let aff = super::build_node_affinity(&GpuSpec {
            count: 1,
            model: vec!["A100".into()],
        });
        let c = k8s_openapi::api::core::v1::Container {
            name: "main".into(),
            resources: Some(k8s_openapi::api::core::v1::ResourceRequirements {
                limits: None,
                requests: Some(
                    vec![("nvidia.com/gpu".into(), Quantity("1".into()))]
                        .into_iter()
                        .collect(),
                ),
                claims: None,
            }),
            ..Default::default()
        };
        let p = Pod {
            metadata: ObjectMeta {
                name: Some("p-a100".into()),
                labels: Some(
                    vec![("basilica.ai/type".into(), "rental".into())]
                        .into_iter()
                        .collect(),
                ),
                ..Default::default()
            },
            spec: Some(PodSpec {
                containers: vec![c],
                affinity: aff,
                ..Default::default()
            }),
            status: Some(PodStatus {
                phase: Some("Running".into()),
                ..Default::default()
            }),
        };
        controller.client.create_pod("ns", &p).await.unwrap();

        // New rental also requests A100; should be Queued due to limit 1
        let mut spec = base_spec();
        spec.container.resources.gpus = GpuSpec {
            count: 1,
            model: vec!["A100".into()],
        };
        let cr = GpuRental::new("rent-a100", spec);
        controller
            .client
            .create_gpu_rental("ns", &cr)
            .await
            .unwrap();
        controller.reconcile("ns", &cr).await.unwrap();
        let updated = controller
            .client
            .get_gpu_rental("ns", "rent-a100")
            .await
            .unwrap();
        assert_eq!(
            updated.status.as_ref().unwrap().state.as_deref(),
            Some("Queued")
        );
    }

    #[tokio::test]
    async fn autogenerate_http_route_on_annotation() {
        let _ = metrics_exporter_prometheus::PrometheusBuilder::new().install_recorder();
        let client = MockK8sClient::default();
        let controller = RentalController::new(client.clone(), MockBillingClient::default());

        // Rental with network ingress and route host annotation
        let mut spec = base_spec();
        spec.network.ingress = vec![crate::crd::gpu_rental::IngressRule {
            port: 8888,
            exposure: "NodePort".into(),
        }];
        let mut cr = GpuRental::new("rent-route", spec);
        let mut annotations = std::collections::BTreeMap::new();
        annotations.insert(
            "basilica.ai/route-host".to_string(),
            "demo.u-test.local".to_string(),
        );
        cr.metadata.annotations = Some(annotations);

        controller
            .client
            .create_gpu_rental("ns", &cr)
            .await
            .unwrap();
        controller.reconcile("ns", &cr).await.unwrap();

        // Ensure HTTPRoute exists in mock
        let routes = controller.client.list_http_routes("ns").await;
        assert!(
            routes
                .iter()
                .any(|r| r.metadata.name.as_deref() == Some("rental-route-rent-route")),
            "Expected an HTTPRoute for the rental"
        );
        let route = routes
            .into_iter()
            .find(|r| r.metadata.name.as_deref() == Some("rental-route-rent-route"))
            .unwrap();
        let spec = route.data.get("spec").unwrap();
        let hostnames = spec.get("hostnames").unwrap().as_array().unwrap();
        assert_eq!(hostnames[0].as_str().unwrap(), "demo.u-test.local");
    }

    #[tokio::test]
    async fn cpu_only_rental_bypasses_gpu_caps() {
        let _ = metrics_exporter_prometheus::PrometheusBuilder::new().install_recorder();
        let client = MockK8sClient::default();
        let controller = RentalController::new(client.clone(), MockBillingClient::default());

        // Queue with total GPU limit 1 and high concurrency
        let limits = crate::crd::basilica_queue::GpuLimits {
            total: 1,
            models: None,
        };
        let q = crate::crd::basilica_queue::BasilicaQueue::new(
            "q-total",
            crate::crd::basilica_queue::BasilicaQueueSpec {
                concurrency: 10,
                gpu_limits: Some(limits),
            },
        );
        controller
            .client
            .create_basilica_queue("ns", &q)
            .await
            .unwrap();

        // Existing Pod consuming 1 GPU
        let c = k8s_openapi::api::core::v1::Container {
            name: "main".into(),
            resources: Some(k8s_openapi::api::core::v1::ResourceRequirements {
                limits: None,
                requests: Some(
                    vec![("nvidia.com/gpu".into(), Quantity("1".into()))]
                        .into_iter()
                        .collect(),
                ),
                claims: None,
            }),
            ..Default::default()
        };
        let p = Pod {
            metadata: ObjectMeta {
                name: Some("p-gpu".into()),
                labels: Some(
                    vec![("basilica.ai/type".into(), "rental".into())]
                        .into_iter()
                        .collect(),
                ),
                ..Default::default()
            },
            spec: Some(PodSpec {
                containers: vec![c],
                ..Default::default()
            }),
            status: Some(PodStatus {
                phase: Some("Running".into()),
                ..Default::default()
            }),
        };
        controller.client.create_pod("ns", &p).await.unwrap();

        // New rental requests 0 GPUs (CPU-only) and should NOT be queued by GPU cap
        let mut spec = base_spec();
        spec.container.resources.gpus = GpuSpec {
            count: 0,
            model: vec![],
        };
        let cr = GpuRental::new("rent-cpu", spec);
        controller
            .client
            .create_gpu_rental("ns", &cr)
            .await
            .unwrap();
        controller.reconcile("ns", &cr).await.unwrap();
        let updated = controller
            .client
            .get_gpu_rental("ns", "rent-cpu")
            .await
            .unwrap();
        assert_ne!(
            updated.status.as_ref().unwrap().state.as_deref(),
            Some("Queued"),
            "CPU-only rental should bypass GPU caps"
        );
    }

    #[tokio::test]
    async fn zero_gpu_pod_not_counted_towards_model_or_total() {
        let _ = metrics_exporter_prometheus::PrometheusBuilder::new().install_recorder();
        let client = MockK8sClient::default();
        let controller = RentalController::new(client.clone(), MockBillingClient::default());

        // Queue with per-model A100 cap=1
        let mut models = std::collections::BTreeMap::new();
        models.insert("A100".to_string(), 1);
        let limits = crate::crd::basilica_queue::GpuLimits {
            total: 0,
            models: Some(models),
        };
        let q = crate::crd::basilica_queue::BasilicaQueue::new(
            "q-model",
            crate::crd::basilica_queue::BasilicaQueueSpec {
                concurrency: 10,
                gpu_limits: Some(limits),
            },
        );
        controller
            .client
            .create_basilica_queue("ns", &q)
            .await
            .unwrap();

        // Existing Pod with A100 affinity but 0 GPU requested
        let aff = super::build_node_affinity(&GpuSpec {
            count: 0,
            model: vec!["A100".into()],
        });
        let c = k8s_openapi::api::core::v1::Container {
            name: "main".into(),
            resources: Some(k8s_openapi::api::core::v1::ResourceRequirements {
                limits: None,
                requests: Some(std::collections::BTreeMap::new()),
                claims: None,
            }),
            ..Default::default()
        };
        let p = Pod {
            metadata: ObjectMeta {
                name: Some("p-a100-0gpu".into()),
                labels: Some(
                    vec![("basilica.ai/type".into(), "rental".into())]
                        .into_iter()
                        .collect(),
                ),
                ..Default::default()
            },
            spec: Some(PodSpec {
                containers: vec![c],
                affinity: aff,
                ..Default::default()
            }),
            status: Some(PodStatus {
                phase: Some("Running".into()),
                ..Default::default()
            }),
        };
        controller.client.create_pod("ns", &p).await.unwrap();

        // New rental requests 1x A100; should NOT be queued because the 0-GPU pod shouldn't count
        let mut spec = base_spec();
        spec.container.resources.gpus = GpuSpec {
            count: 1,
            model: vec!["A100".into()],
        };
        let cr = GpuRental::new("rent-a100-allow", spec);
        controller
            .client
            .create_gpu_rental("ns", &cr)
            .await
            .unwrap();
        controller.reconcile("ns", &cr).await.unwrap();
        let updated = controller
            .client
            .get_gpu_rental("ns", "rent-a100-allow")
            .await
            .unwrap();
        assert_ne!(
            updated.status.as_ref().unwrap().state.as_deref(),
            Some("Queued"),
            "Zero-GPU pods must not consume model capacity"
        );
    }

    #[tokio::test]
    async fn discovery_headless_service_created_with_group_label() {
        let _ = metrics_exporter_prometheus::PrometheusBuilder::new().install_recorder();
        let client = MockK8sClient::default();
        let controller = RentalController::new(client.clone(), MockBillingClient::default());

        // Rental with a discovery group label
        let spec = base_spec();
        let mut cr = GpuRental::new("rent-disc", spec);
        let mut labels = std::collections::BTreeMap::new();
        labels.insert(
            "basilica.ai/discovery-group".to_string(),
            "team-1".to_string(),
        );
        cr.metadata.labels = Some(labels);

        controller
            .client
            .create_gpu_rental("ns", &cr)
            .await
            .unwrap();
        controller.reconcile("ns", &cr).await.unwrap();

        // Pod should have discovery label
        let pod = controller
            .client
            .get_pod("ns", "rental-rent-disc")
            .await
            .unwrap();
        let pod_labels = pod.metadata.labels.unwrap_or_default();
        assert_eq!(
            pod_labels
                .get("basilica.ai/discovery-group")
                .map(|s| s.as_str()),
            Some("team-1")
        );

        // A headless discovery service should exist for the group
        let svcs = controller
            .client
            .list_services_with_label("ns", "basilica.ai/discovery-group", "team-1")
            .await
            .unwrap();
        assert!(
            !svcs.is_empty(),
            "expected a headless discovery service for the group"
        );
    }

    #[test]
    fn per_namespace_backoff_override_and_deterministic_jitter() {
        // Base 10s; per-namespace override to 20s; jitter up to 5s
        std::env::set_var("BASILICA_QUEUE_ADMIT_BACKOFF_SECS", "10");
        std::env::set_var("BASILICA_QUEUE_BACKOFF_NS_U_TEST_NS", "20");
        std::env::set_var("BASILICA_QUEUE_JITTER_SECS", "5");

        let ns = "u-test.ns"; // sanitizes to U_TEST_NS
        let r1a = super::compute_backoff_secs_for_rental(ns, "rent-a");
        let r1b = super::compute_backoff_secs_for_rental(ns, "rent-a");
        assert_eq!(r1a, r1b, "backoff must be deterministic per rental");
        assert!(
            (20..=25).contains(&r1a),
            "backoff within overridden + jitter range"
        );

        let r2 = super::compute_backoff_secs_for_rental(ns, "rent-b");
        assert!((20..=25).contains(&r2));
        // Different rentals may have different jitter; if equal it's acceptable, but ensure in range
    }
    #[tokio::test]
    async fn terminate_when_out_of_credits() {
        let _ = metrics_exporter_prometheus::PrometheusBuilder::new().install_recorder();
        let client = MockK8sClient::default();
        let billing = MockBillingClient::default();
        {
            let mut t = billing.terminate.write().await;
            t.insert("rent2".into(), true);
        }
        let controller = RentalController::new(client.clone(), billing);

        // Active pod
        let spec = base_spec();
        let cr = GpuRental::new("rent2", spec);
        controller
            .client
            .create_gpu_rental("ns", &cr)
            .await
            .unwrap();
        let pod = Pod {
            metadata: ObjectMeta {
                name: Some("p2".into()),
                labels: Some(
                    vec![("basilica.ai/rental".into(), "rent2".into())]
                        .into_iter()
                        .collect(),
                ),
                ..Default::default()
            },
            status: Some(PodStatus {
                phase: Some("Running".into()),
                ..Default::default()
            }),
            ..Default::default()
        };
        controller.client.create_pod("ns", &pod).await.unwrap();

        controller.reconcile("ns", &cr).await.unwrap();
        let updated = controller
            .client
            .get_gpu_rental("ns", "rent2")
            .await
            .unwrap();
        assert_eq!(
            updated.status.as_ref().unwrap().state.as_deref(),
            Some("Terminated")
        );
    }
}
