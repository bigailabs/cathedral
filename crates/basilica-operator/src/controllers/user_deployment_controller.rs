use k8s_openapi::api::apps::v1::{Deployment, DeploymentSpec};
use k8s_openapi::api::core::v1::{
    Affinity, NodeAffinity, NodeSelector, NodeSelectorRequirement, NodeSelectorTerm,
};
use k8s_openapi::api::core::v1::{
    Capabilities, Container, HTTPGetAction, HostPathVolumeSource, PodSecurityContext, PodSpec,
    PodTemplateSpec, Probe, ResourceRequirements, SecurityContext, Service, ServicePort,
    ServiceSpec, TCPSocketAction, Toleration, TopologySpreadConstraint, Volume, VolumeMount,
};
use k8s_openapi::api::networking::v1::{
    NetworkPolicy, NetworkPolicyIngressRule, NetworkPolicyPeer, NetworkPolicyPort,
    NetworkPolicySpec,
};
use k8s_openapi::api::policy::v1::{PodDisruptionBudget, PodDisruptionBudgetSpec};
use k8s_openapi::apimachinery::pkg::api::resource::Quantity;
use k8s_openapi::apimachinery::pkg::apis::meta::v1::{LabelSelector, ObjectMeta};
use k8s_openapi::apimachinery::pkg::util::intstr::IntOrString;
use std::collections::BTreeMap;
use std::sync::Arc;

use crate::controllers::storage_utils;
use crate::crd::user_deployment::{
    DeploymentPhase, EnvVar as CrdEnvVar, PhaseTransition, ProgressInfo, TopologySpreadConfig,
    UserDeployment, UserDeploymentStatus,
};
use crate::k8s_client::K8sClient;
use anyhow::Result;
use k8s_openapi::api::core::v1::Pod;
use tracing::{debug, error};

fn deployment_needs_update(current: &Deployment, desired: &Deployment) -> bool {
    let current_spec = match &current.spec {
        Some(s) => s,
        None => return true,
    };
    let desired_spec = match &desired.spec {
        Some(s) => s,
        None => return false,
    };

    if current_spec.replicas != desired_spec.replicas {
        return true;
    }

    let current_template = &current_spec.template;
    let desired_template = &desired_spec.template;

    let current_pod_spec = match &current_template.spec {
        Some(s) => s,
        None => return true,
    };
    let desired_pod_spec = match &desired_template.spec {
        Some(s) => s,
        None => return false,
    };

    if current_pod_spec.containers.len() != desired_pod_spec.containers.len() {
        return true;
    }

    for (current_c, desired_c) in current_pod_spec
        .containers
        .iter()
        .zip(desired_pod_spec.containers.iter())
    {
        if current_c.image != desired_c.image
            || current_c.command != desired_c.command
            || current_c.args != desired_c.args
        {
            return true;
        }
    }

    false
}

fn service_needs_update(current: &Service, desired: &Service) -> bool {
    let current_spec = match &current.spec {
        Some(s) => s,
        None => return true,
    };
    let desired_spec = match &desired.spec {
        Some(s) => s,
        None => return false,
    };

    current_spec.ports != desired_spec.ports || current_spec.selector != desired_spec.selector
}

fn network_policy_needs_update(current: &NetworkPolicy, desired: &NetworkPolicy) -> bool {
    let current_spec = match &current.spec {
        Some(s) => s,
        None => return true,
    };
    let desired_spec = match &desired.spec {
        Some(s) => s,
        None => return false,
    };

    current_spec.ingress != desired_spec.ingress || current_spec.egress != desired_spec.egress
}

fn pdb_needs_update(current: &PodDisruptionBudget, desired: &PodDisruptionBudget) -> bool {
    let current_spec = match &current.spec {
        Some(s) => s,
        None => return true,
    };
    let desired_spec = match &desired.spec {
        Some(s) => s,
        None => return false,
    };

    current_spec.min_available != desired_spec.min_available
        || current_spec.selector != desired_spec.selector
}

fn to_quantity(s: &str) -> Quantity {
    Quantity(s.to_string())
}

fn parse_cuda_major_version(version: &str) -> Option<u32> {
    version.split('.').next()?.parse().ok()
}

fn scale_cpu_quantity(cpu: &str, ratio: f32) -> Quantity {
    if (ratio - 1.0).abs() < f32::EPSILON {
        return Quantity(cpu.to_string());
    }

    if let Some(millis_str) = cpu.strip_suffix('m') {
        if let Ok(millis) = millis_str.parse::<f64>() {
            let scaled = (millis * ratio as f64).ceil() as u64;
            return Quantity(format!("{}m", scaled));
        }
    }

    if let Ok(cores) = cpu.parse::<f64>() {
        let millis = (cores * 1000.0 * ratio as f64).ceil() as u64;
        return Quantity(format!("{}m", millis));
    }

    Quantity(cpu.to_string())
}

fn sanitize_user_id(user_id: &str) -> String {
    let mut out = String::new();
    for ch in user_id.chars() {
        if ch.is_ascii_lowercase() || ch.is_ascii_digit() || ch == '-' {
            out.push(ch);
        } else if ch.is_ascii_uppercase() {
            out.push(ch.to_ascii_lowercase());
        } else {
            out.push('-');
        }
        if out.len() >= 60 {
            break;
        }
    }
    if out.ends_with('-') {
        out.pop();
    }
    out
}

fn make_service_name(instance_name: &str) -> String {
    format!("s-{}", instance_name)
}

fn build_resources(
    cpu: &str,
    memory: &str,
    gpu: Option<&crate::crd::user_deployment::GpuSpec>,
    cpu_request_ratio: f32,
) -> ResourceRequirements {
    build_resources_with_storage(cpu, memory, gpu, None, cpu_request_ratio)
}

fn build_resources_with_storage(
    cpu: &str,
    memory: &str,
    gpu: Option<&crate::crd::user_deployment::GpuSpec>,
    ephemeral_storage: Option<&str>,
    cpu_request_ratio: f32,
) -> ResourceRequirements {
    let mut limits = BTreeMap::new();
    let mut requests = BTreeMap::new();

    limits.insert("cpu".to_string(), to_quantity(cpu));
    limits.insert("memory".to_string(), to_quantity(memory));

    requests.insert(
        "cpu".to_string(),
        scale_cpu_quantity(cpu, cpu_request_ratio),
    );
    requests.insert("memory".to_string(), to_quantity(memory));

    if let Some(storage) = ephemeral_storage {
        limits.insert("ephemeral-storage".to_string(), to_quantity(storage));
        requests.insert("ephemeral-storage".to_string(), to_quantity(storage));
    }

    if let Some(gpu_spec) = gpu {
        let gpu_count = to_quantity(&gpu_spec.count.to_string());
        limits.insert("nvidia.com/gpu".to_string(), gpu_count.clone());
        requests.insert("nvidia.com/gpu".to_string(), gpu_count);
    }

    ResourceRequirements {
        limits: Some(limits),
        requests: Some(requests),
        claims: None,
    }
}

fn build_env(env: &[CrdEnvVar]) -> Vec<k8s_openapi::api::core::v1::EnvVar> {
    env.iter()
        .map(|e| k8s_openapi::api::core::v1::EnvVar {
            name: e.name.clone(),
            value: Some(e.value.clone()),
            ..Default::default()
        })
        .collect()
}

fn build_node_selector() -> BTreeMap<String, String> {
    let mut selector = BTreeMap::new();
    selector.insert("basilica.ai/workloads-only".to_string(), "true".to_string());
    selector
}

fn build_tolerations(has_gpu: bool) -> Vec<Toleration> {
    let mut tolerations = vec![Toleration {
        key: Some("basilica.ai/workloads-only".into()),
        operator: Some("Equal".into()),
        value: Some("true".into()),
        effect: Some("NoSchedule".into()),
        ..Default::default()
    }];

    if has_gpu {
        tolerations.push(Toleration {
            key: Some("nvidia.com/gpu".into()),
            operator: Some("Exists".into()),
            value: None,
            effect: Some("NoSchedule".into()),
            ..Default::default()
        });
    }

    tolerations
}

fn build_topology_spread(
    instance_name: &str,
    config: Option<&TopologySpreadConfig>,
) -> Option<Vec<TopologySpreadConstraint>> {
    // Always include topology spread constraints regardless of replica count.
    // This ensures the pod template remains stable when scaling, preventing
    // Kubernetes from creating new ReplicaSets on scale operations.
    let max_skew = config.map(|c| c.max_skew).unwrap_or(1);
    let when_unsatisfiable = config
        .map(|c| c.when_unsatisfiable.clone())
        .unwrap_or_else(|| "ScheduleAnyway".to_string());

    Some(vec![TopologySpreadConstraint {
        max_skew,
        topology_key: "kubernetes.io/hostname".to_string(),
        when_unsatisfiable,
        label_selector: Some(LabelSelector {
            match_labels: Some(BTreeMap::from([(
                "app".to_string(),
                instance_name.to_string(),
            )])),
            ..Default::default()
        }),
        ..Default::default()
    }])
}

fn build_writable_volumes() -> (
    Vec<k8s_openapi::api::core::v1::Volume>,
    Vec<k8s_openapi::api::core::v1::VolumeMount>,
) {
    (vec![], vec![])
}

fn build_security_contexts() -> (Option<PodSecurityContext>, Option<SecurityContext>) {
    let pod_sc = Some(PodSecurityContext {
        fs_group: Some(1000),
        seccomp_profile: Some(k8s_openapi::api::core::v1::SeccompProfile {
            type_: "RuntimeDefault".into(),
            localhost_profile: None,
        }),
        ..Default::default()
    });
    let container_sc = Some(SecurityContext {
        allow_privilege_escalation: Some(false),
        capabilities: Some(Capabilities {
            drop: Some(vec!["ALL".into()]),
            add: None,
        }),
        seccomp_profile: Some(k8s_openapi::api::core::v1::SeccompProfile {
            type_: "RuntimeDefault".into(),
            localhost_profile: None,
        }),
        ..Default::default()
    });
    (pod_sc, container_sc)
}

fn build_health_probes(
    port: u32,
    health_check: &Option<crate::crd::user_deployment::HealthCheckConfig>,
    is_gpu_workload: bool,
) -> (Option<Probe>, Option<Probe>, Option<Probe>) {
    match health_check {
        Some(config) => {
            let liveness_probe = config.liveness.as_ref().map(|probe_cfg| Probe {
                http_get: Some(HTTPGetAction {
                    path: Some(probe_cfg.path.clone()),
                    port: IntOrString::Int(port as i32),
                    ..Default::default()
                }),
                initial_delay_seconds: Some(probe_cfg.initial_delay_seconds as i32),
                period_seconds: Some(probe_cfg.period_seconds as i32),
                timeout_seconds: Some(probe_cfg.timeout_seconds as i32),
                failure_threshold: Some(probe_cfg.failure_threshold as i32),
                ..Default::default()
            });

            let readiness_probe = config.readiness.as_ref().map(|probe_cfg| Probe {
                http_get: Some(HTTPGetAction {
                    path: Some(probe_cfg.path.clone()),
                    port: IntOrString::Int(port as i32),
                    ..Default::default()
                }),
                initial_delay_seconds: Some(probe_cfg.initial_delay_seconds as i32),
                period_seconds: Some(probe_cfg.period_seconds as i32),
                timeout_seconds: Some(probe_cfg.timeout_seconds as i32),
                failure_threshold: Some(probe_cfg.failure_threshold as i32),
                ..Default::default()
            });

            (liveness_probe, readiness_probe, None)
        }
        None => {
            // For GPU workloads, use startup probe to allow slow model loading
            // Startup probe: 10s period * 60 failures = 600s (10 min) max startup time
            let startup_probe = if is_gpu_workload {
                Some(Probe {
                    tcp_socket: Some(TCPSocketAction {
                        port: IntOrString::Int(port as i32),
                        ..Default::default()
                    }),
                    initial_delay_seconds: Some(10),
                    period_seconds: Some(10),
                    timeout_seconds: Some(5),
                    failure_threshold: Some(60),
                    ..Default::default()
                })
            } else {
                None
            };

            let liveness_probe = Some(Probe {
                tcp_socket: Some(TCPSocketAction {
                    port: IntOrString::Int(port as i32),
                    ..Default::default()
                }),
                initial_delay_seconds: Some(30),
                period_seconds: Some(10),
                timeout_seconds: Some(5),
                failure_threshold: Some(3),
                ..Default::default()
            });

            let readiness_probe = Some(Probe {
                tcp_socket: Some(TCPSocketAction {
                    port: IntOrString::Int(port as i32),
                    ..Default::default()
                }),
                initial_delay_seconds: Some(10),
                period_seconds: Some(5),
                timeout_seconds: Some(3),
                failure_threshold: Some(2),
                ..Default::default()
            });

            (liveness_probe, readiness_probe, startup_probe)
        }
    }
}

fn build_storage_volumes(
    namespace: &str,
    _storage: &crate::crd::user_deployment::PersistentStorageSpec,
) -> Vec<Volume> {
    // Storage is provided by the FUSE DaemonSet running in basilica-storage namespace.
    // Each user namespace gets its mount at /var/lib/basilica/fuse/{namespace}/.
    // User pods consume this via hostPath volume with HostToContainer propagation.
    vec![Volume {
        name: "basilica-storage".to_string(),
        host_path: Some(HostPathVolumeSource {
            path: format!("/var/lib/basilica/fuse/{}", namespace),
            type_: Some("Directory".to_string()),
        }),
        ..Default::default()
    }]
}

fn build_node_affinity(gpu: &crate::crd::user_deployment::GpuSpec) -> Option<Affinity> {
    let mut match_expressions = vec![
        NodeSelectorRequirement {
            key: "basilica.ai/node-role".to_string(),
            operator: "In".to_string(),
            values: Some(vec!["miner".to_string()]),
        },
        NodeSelectorRequirement {
            key: "basilica.ai/validated".to_string(),
            operator: "In".to_string(),
            values: Some(vec!["true".to_string()]),
        },
        NodeSelectorRequirement {
            key: "basilica.ai/node-group".to_string(),
            operator: "In".to_string(),
            values: Some(vec!["user-deployments".to_string()]),
        },
        NodeSelectorRequirement {
            key: "basilica.ai/gpu-model".to_string(),
            operator: "In".to_string(),
            values: Some(gpu.model.clone()),
        },
    ];

    if let Some(min_cuda) = &gpu.min_cuda_version {
        if let Some(min_major) = parse_cuda_major_version(min_cuda) {
            let acceptable_versions: Vec<String> =
                (min_major..=20).map(|n| n.to_string()).collect();
            match_expressions.push(NodeSelectorRequirement {
                key: "basilica.ai/cuda-major".to_string(),
                operator: "In".to_string(),
                values: Some(acceptable_versions),
            });
        }
    }

    if let Some(min_vram) = gpu.min_gpu_memory_gb {
        let acceptable_memory: Vec<String> = (min_vram..=256).map(|n| n.to_string()).collect();
        match_expressions.push(NodeSelectorRequirement {
            key: "basilica.ai/gpu-memory-gb".to_string(),
            operator: "In".to_string(),
            values: Some(acceptable_memory),
        });
    }

    let acceptable_counts: Vec<String> = (gpu.count..=8).map(|n| n.to_string()).collect();
    match_expressions.push(NodeSelectorRequirement {
        key: "basilica.ai/gpu-count".to_string(),
        operator: "In".to_string(),
        values: Some(acceptable_counts),
    });

    Some(Affinity {
        node_affinity: Some(NodeAffinity {
            required_during_scheduling_ignored_during_execution: Some(NodeSelector {
                node_selector_terms: vec![NodeSelectorTerm {
                    match_expressions: Some(match_expressions),
                    ..Default::default()
                }],
            }),
            ..Default::default()
        }),
        ..Default::default()
    })
}

pub fn render_deployment(
    instance_name: &str,
    namespace: &str,
    spec: &crate::crd::user_deployment::UserDeploymentSpec,
    owner: Option<&UserDeployment>,
) -> anyhow::Result<Deployment> {
    let (pod_sc, container_sc) = build_security_contexts();
    let (mut volumes, mut volume_mounts) = build_writable_volumes();
    let is_gpu_workload = spec
        .resources
        .as_ref()
        .and_then(|r| r.gpus.as_ref())
        .is_some();
    let (liveness_probe, readiness_probe, startup_probe) =
        build_health_probes(spec.port, &spec.health_check, is_gpu_workload);

    let storage_config = spec
        .storage
        .as_ref()
        .and_then(|s| s.persistent.as_ref())
        .filter(|p| p.enabled);

    if let Some(storage) = storage_config {
        volumes.extend(build_storage_volumes(namespace, storage));
        volume_mounts.push(VolumeMount {
            name: "basilica-storage".to_string(),
            mount_path: storage.mount_path.clone(),
            mount_propagation: Some("HostToContainer".to_string()),
            ..Default::default()
        });
    }

    let gpu_config = spec.resources.as_ref().and_then(|r| r.gpus.as_ref());

    let node_affinity = gpu_config.and_then(build_node_affinity);

    let mut labels = BTreeMap::new();
    labels.insert("app".to_string(), instance_name.to_string());
    labels.insert(
        "basilica.ai/type".to_string(),
        "user-deployment".to_string(),
    );
    labels.insert(
        "basilica.ai/instance".to_string(),
        instance_name.to_string(),
    );
    labels.insert(
        "basilica.ai/user-id".to_string(),
        sanitize_user_id(&spec.user_id),
    );
    if spec.public {
        labels.insert(
            "basilica.ai/http-accessible".to_string(),
            "true".to_string(),
        );
    }

    // Add GPU request labels for autoscaler detection
    let mut annotations = BTreeMap::new();
    if let Some(gpu) = gpu_config {
        // Labels for quick pod identification
        labels.insert(
            "gpu-request.basilica.ai/count".to_string(),
            gpu.count.to_string(),
        );
        if !gpu.model.is_empty() {
            labels.insert(
                "gpu-request.basilica.ai/model".to_string(),
                gpu.model.join(","),
            );
        }
        // Annotation with full GPU requirements (JSON) for autoscaler
        if let Ok(json) = serde_json::to_string(gpu) {
            annotations.insert("autoscaler.basilica.ai/gpu-requirements".to_string(), json);
        }
    }

    let resources = if let Some(ref res) = spec.resources {
        build_resources(
            &res.cpu,
            &res.memory,
            res.gpus.as_ref(),
            res.cpu_request_ratio,
        )
    } else {
        build_resources("100m", "128Mi", None, 1.0)
    };

    let (container_command, container_args) = if let Some(storage) = storage_config {
        storage_utils::wrap_command_with_fuse_wait(
            if spec.command.is_empty() {
                None
            } else {
                Some(spec.command.clone())
            },
            if spec.args.is_empty() {
                None
            } else {
                Some(spec.args.clone())
            },
            &storage.mount_path,
        )
        .expect("shell escape should not fail for valid UTF-8 strings")
    } else {
        (
            if spec.command.is_empty() {
                None
            } else {
                Some(spec.command.clone())
            },
            if spec.args.is_empty() {
                None
            } else {
                Some(spec.args.clone())
            },
        )
    };

    let container = Container {
        name: instance_name.to_string(),
        image: Some(spec.image.clone()),
        command: container_command,
        args: container_args,
        env: Some(build_env(&spec.env)),
        ports: Some(vec![k8s_openapi::api::core::v1::ContainerPort {
            container_port: spec.port as i32,
            protocol: Some("TCP".into()),
            ..Default::default()
        }]),
        resources: Some(resources),
        security_context: container_sc,
        volume_mounts: Some(volume_mounts),
        liveness_probe,
        readiness_probe,
        startup_probe,
        ..Default::default()
    };

    let containers = vec![container];

    let pod_template = PodTemplateSpec {
        metadata: Some(ObjectMeta {
            labels: Some(labels.clone()),
            annotations: if annotations.is_empty() {
                None
            } else {
                Some(annotations)
            },
            ..Default::default()
        }),
        spec: Some(PodSpec {
            containers,
            security_context: pod_sc,
            termination_grace_period_seconds: Some(120),
            node_selector: Some(build_node_selector()),
            tolerations: Some(build_tolerations(gpu_config.is_some())),
            affinity: node_affinity,
            topology_spread_constraints: build_topology_spread(
                instance_name,
                spec.topology_spread.as_ref(),
            ),
            restart_policy: Some("Always".into()),
            automount_service_account_token: Some(false),
            volumes: Some(volumes),
            ..Default::default()
        }),
    };

    let replicas = if spec.suspended {
        0
    } else {
        spec.replicas as i32
    };

    let owner_references = owner.and_then(|o| {
        use k8s_openapi::apimachinery::pkg::apis::meta::v1::OwnerReference;
        use kube::ResourceExt;
        let uid = o.metadata.uid.as_ref()?;
        Some(vec![OwnerReference {
            api_version: "basilica.ai/v1".to_string(),
            kind: "UserDeployment".to_string(),
            name: o.name_any(),
            uid: uid.clone(),
            controller: Some(true),
            block_owner_deletion: Some(true),
        }])
    });

    Ok(Deployment {
        metadata: ObjectMeta {
            name: Some(format!("{}-deployment", instance_name)),
            namespace: Some(namespace.to_string()),
            labels: Some(labels.clone()),
            owner_references,
            ..Default::default()
        },
        spec: Some(DeploymentSpec {
            replicas: Some(replicas),
            selector: LabelSelector {
                match_labels: Some({
                    let mut sel = BTreeMap::new();
                    sel.insert("app".to_string(), instance_name.to_string());
                    sel
                }),
                ..Default::default()
            },
            template: pod_template,
            ..Default::default()
        }),
        ..Default::default()
    })
}

pub fn render_service(instance_name: &str, namespace: &str, port: u32) -> Service {
    let mut labels = BTreeMap::new();
    labels.insert("app".to_string(), instance_name.to_string());
    labels.insert(
        "basilica.ai/type".to_string(),
        "user-deployment".to_string(),
    );

    let mut selector = BTreeMap::new();
    selector.insert("app".to_string(), instance_name.to_string());

    let service_name = make_service_name(instance_name);

    Service {
        metadata: ObjectMeta {
            name: Some(service_name),
            namespace: Some(namespace.to_string()),
            labels: Some(labels),
            ..Default::default()
        },
        spec: Some(ServiceSpec {
            type_: Some("ClusterIP".into()),
            selector: Some(selector),
            ports: Some(vec![ServicePort {
                port: port as i32,
                target_port: Some(IntOrString::Int(port as i32)),
                protocol: Some("TCP".into()),
                ..Default::default()
            }]),
            ..Default::default()
        }),
        ..Default::default()
    }
}

pub fn render_network_policy(instance_name: &str, namespace: &str, port: u32) -> NetworkPolicy {
    let mut labels = BTreeMap::new();
    labels.insert("app".to_string(), instance_name.to_string());
    labels.insert(
        "basilica.ai/type".to_string(),
        "user-deployment".to_string(),
    );

    let mut pod_selector_labels = BTreeMap::new();
    pod_selector_labels.insert("app".to_string(), instance_name.to_string());

    let mut envoy_namespace_labels = BTreeMap::new();
    envoy_namespace_labels.insert(
        "kubernetes.io/metadata.name".to_string(),
        "envoy-gateway-system".to_string(),
    );

    let mut envoy_pod_labels = BTreeMap::new();
    envoy_pod_labels.insert(
        "gateway.envoyproxy.io/owning-gateway-name".to_string(),
        "basilica-gateway".to_string(),
    );

    NetworkPolicy {
        metadata: ObjectMeta {
            name: Some(format!("{}-netpol", instance_name)),
            namespace: Some(namespace.to_string()),
            labels: Some(labels),
            ..Default::default()
        },
        spec: Some(NetworkPolicySpec {
            pod_selector: LabelSelector {
                match_labels: Some(pod_selector_labels),
                ..Default::default()
            },
            policy_types: Some(vec!["Ingress".into()]),
            ingress: Some(vec![NetworkPolicyIngressRule {
                from: Some(vec![NetworkPolicyPeer {
                    namespace_selector: Some(LabelSelector {
                        match_labels: Some(envoy_namespace_labels),
                        ..Default::default()
                    }),
                    pod_selector: Some(LabelSelector {
                        match_labels: Some(envoy_pod_labels),
                        ..Default::default()
                    }),
                    ..Default::default()
                }]),
                ports: Some(vec![NetworkPolicyPort {
                    port: Some(IntOrString::Int(port as i32)),
                    protocol: Some("TCP".into()),
                    ..Default::default()
                }]),
            }]),
            egress: None,
        }),
    }
}

/// Render a PodDisruptionBudget for deployments with replicas > 1.
/// Returns None if replicas <= 1 (PDB not needed for single-replica deployments).
/// The PDB includes owner references to the UserDeployment for proper garbage collection.
pub fn render_pdb(
    instance_name: &str,
    namespace: &str,
    replicas: u32,
    owner: &UserDeployment,
) -> Option<PodDisruptionBudget> {
    use k8s_openapi::apimachinery::pkg::apis::meta::v1::OwnerReference;
    use kube::ResourceExt;

    if replicas <= 1 {
        return None;
    }

    let mut labels = BTreeMap::new();
    labels.insert("app".to_string(), instance_name.to_string());
    labels.insert(
        "basilica.ai/type".to_string(),
        "user-deployment".to_string(),
    );
    labels.insert(
        "basilica.ai/instance".to_string(),
        instance_name.to_string(),
    );

    // Use more specific selector to avoid affecting other deployments
    let mut selector_labels = BTreeMap::new();
    selector_labels.insert("app".to_string(), instance_name.to_string());
    selector_labels.insert(
        "basilica.ai/instance".to_string(),
        instance_name.to_string(),
    );

    // Calculate minAvailable based on replica count:
    // - 2-3 replicas: minAvailable = 1 (ensure at least 1 pod survives)
    // - 4+ replicas: minAvailable = 50% (allow half to be disrupted)
    let min_available = if replicas <= 3 {
        IntOrString::Int(1)
    } else {
        IntOrString::String("50%".to_string())
    };

    // Owner UID is required for valid owner references - skip PDB if missing
    let owner_uid = owner.metadata.uid.as_ref()?;

    let owner_references = vec![OwnerReference {
        api_version: "basilica.ai/v1".to_string(),
        kind: "UserDeployment".to_string(),
        name: owner.name_any(),
        uid: owner_uid.clone(),
        controller: Some(true),
        block_owner_deletion: Some(true),
    }];

    Some(PodDisruptionBudget {
        metadata: ObjectMeta {
            name: Some(format!("{}-pdb", instance_name)),
            namespace: Some(namespace.to_string()),
            labels: Some(labels),
            owner_references: Some(owner_references),
            ..Default::default()
        },
        spec: Some(PodDisruptionBudgetSpec {
            min_available: Some(min_available),
            selector: Some(LabelSelector {
                match_labels: Some(selector_labels),
                ..Default::default()
            }),
            ..Default::default()
        }),
        ..Default::default()
    })
}

#[derive(Clone)]
pub struct UserDeploymentController {
    client: Arc<dyn K8sClient>,
    public_ip: String,
    public_port: u16,
}

impl UserDeploymentController {
    pub fn new(client: Arc<dyn K8sClient>, public_ip: String, public_port: u16) -> Self {
        Self {
            client,
            public_ip,
            public_port,
        }
    }

    pub async fn reconcile(&self, ns: &str, cr: &UserDeployment) -> Result<()> {
        let name = cr
            .metadata
            .name
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("UserDeployment missing metadata.name"))?;
        let spec = &cr.spec;
        let instance_name = &spec.instance_name;

        if let Err(e) = crate::security::validate_storage_spec(ns, name, &spec.storage) {
            error!(
                namespace = %ns,
                deployment = %name,
                error = %e,
                "Storage spec validation failed, rejecting deployment"
            );
            return Err(e);
        }

        let deployment_name = format!("{}-deployment", instance_name);
        let service_name = make_service_name(instance_name);
        let netpol_name = format!("{}-netpol", instance_name);
        let pdb_name = format!("{}-pdb", instance_name);

        let desired_deployment = render_deployment(instance_name, ns, spec, Some(cr))?;
        let current_deployment = self.client.get_deployment(ns, &deployment_name).await.ok();
        if current_deployment
            .as_ref()
            .map(|c| deployment_needs_update(c, &desired_deployment))
            .unwrap_or(true)
        {
            debug!("Deployment {} needs update, patching", deployment_name);
            self.client
                .patch_deployment(ns, &deployment_name, &desired_deployment)
                .await?;
        }

        let desired_service = render_service(instance_name, ns, spec.port);
        let current_service = self.client.get_service(ns, &service_name).await.ok();
        if current_service
            .as_ref()
            .map(|c| service_needs_update(c, &desired_service))
            .unwrap_or(true)
        {
            debug!("Service {} needs update, patching", service_name);
            self.client
                .patch_service(ns, &service_name, &desired_service)
                .await?;
        }

        let desired_netpol = render_network_policy(instance_name, ns, spec.port);
        let current_netpol = self.client.get_network_policy(ns, &netpol_name).await.ok();
        if current_netpol
            .as_ref()
            .map(|c| network_policy_needs_update(c, &desired_netpol))
            .unwrap_or(true)
        {
            debug!("NetworkPolicy {} needs update, patching", netpol_name);
            self.client
                .patch_network_policy(ns, &netpol_name, &desired_netpol)
                .await?;
        }

        // PDB: Create for multi-replica deployments, delete for single-replica
        let desired_pdb = render_pdb(instance_name, ns, spec.replicas, cr);
        let current_pdb = self.client.get_pdb(ns, &pdb_name).await.ok();

        match (desired_pdb, current_pdb) {
            (Some(desired), Some(current)) => {
                if pdb_needs_update(&current, &desired) {
                    debug!("PodDisruptionBudget {} needs update, patching", pdb_name);
                    self.client.patch_pdb(ns, &pdb_name, &desired).await?;
                }
            }
            (Some(desired), None) => {
                debug!(
                    "Creating PodDisruptionBudget {} for {} replicas",
                    pdb_name, spec.replicas
                );
                self.client.create_pdb(ns, &desired).await?;
            }
            (None, Some(_)) => {
                debug!(
                    "Deleting PodDisruptionBudget {} (replicas reduced to {})",
                    pdb_name, spec.replicas
                );
                self.client.delete_pdb(ns, &pdb_name).await?;
            }
            (None, None) => {}
        }

        let pods = self
            .client
            .list_pods_with_label(ns, "app", instance_name)
            .await?;

        let (state, replicas_ready) = compute_state_from_pods(&pods, spec.replicas);
        let (phase, _) = phase_detection::determine_phase(&pods, spec.replicas);

        let endpoint = format!("{}.{}:{}", service_name, ns, spec.port);
        let public_url = format!(
            "http://{}:{}{}/",
            self.public_ip, self.public_port, spec.path_prefix
        );

        let previous_phase = cr.status.as_ref().and_then(|s| s.phase.as_ref()).cloned();

        let progress = if previous_phase.as_ref() == Some(&phase) {
            // Phase unchanged - preserve existing timer
            let phase_start = cr
                .status
                .as_ref()
                .and_then(|s| s.progress.as_ref())
                .map(|p| p.started_at.as_str());
            phase_detection::calculate_progress(&phase, phase_start)
        } else {
            // New phase - start fresh timer with current timestamp
            ProgressInfo {
                current_step: phase_detection::build_progress_message(&phase),
                started_at: k8s_openapi::chrono::Utc::now().to_rfc3339(),
                elapsed_seconds: 0,
                bytes_synced: None,
                bytes_total: None,
                percentage: None,
            }
        };

        let mut status = UserDeploymentStatus::new()
            .with_state(&state)
            .with_deployment_name(deployment_name)
            .with_service_name(service_name)
            .with_replicas(spec.replicas, replicas_ready)
            .with_endpoint(endpoint)
            .with_public_url(public_url)
            .with_phase(phase.clone())
            .with_progress(progress);

        // First restore existing phase history, then add new transition (add_phase_transition handles trimming)
        if let Some(existing_status) = &cr.status {
            status.phase_history = existing_status.phase_history.clone();
            if status.phase_history.len() > UserDeploymentStatus::MAX_PHASE_HISTORY {
                let excess = status.phase_history.len() - UserDeploymentStatus::MAX_PHASE_HISTORY;
                status.phase_history.drain(0..excess);
            }
        }

        if let Some(prev) = &previous_phase {
            if prev != &phase {
                let transition = PhaseTransition::new(phase.clone());
                status.add_phase_transition(transition);
            }
        }

        if state == "Active" {
            if cr.status.as_ref().map(|s| s.state.as_str()).unwrap_or("") != "Active" {
                status.start_time = Some(k8s_openapi::chrono::Utc::now().to_rfc3339());
            } else if let Some(existing_status) = &cr.status {
                status.start_time = existing_status.start_time.clone();
            }
        }

        status.last_updated = k8s_openapi::chrono::Utc::now().to_rfc3339();

        self.client
            .update_user_deployment_status(ns, name, status)
            .await?;

        Ok(())
    }
}

fn is_pod_terminating(pod: &k8s_openapi::api::core::v1::Pod) -> bool {
    pod.metadata.deletion_timestamp.is_some()
}

fn compute_state_from_pods(
    pods: &[k8s_openapi::api::core::v1::Pod],
    desired_replicas: u32,
) -> (String, u32) {
    // Filter out terminating pods
    let active_pods: Vec<_> = pods.iter().filter(|p| !is_pod_terminating(p)).collect();

    if active_pods.is_empty() {
        return ("Pending".to_string(), 0);
    }

    let mut running_count = 0;
    let mut failed_count = 0;
    let mut _pending_count = 0;

    for pod in active_pods {
        if let Some(status) = &pod.status {
            match status.phase.as_deref() {
                Some("Running") => {
                    let ready = status
                        .conditions
                        .as_ref()
                        .and_then(|conditions| {
                            conditions
                                .iter()
                                .find(|c| c.type_ == "Ready" && c.status == "True")
                        })
                        .is_some();
                    if ready {
                        running_count += 1;
                    } else {
                        _pending_count += 1;
                    }
                }
                Some("Failed") => failed_count += 1,
                Some("Succeeded") => {}
                _ => _pending_count += 1,
            }
        } else {
            _pending_count += 1;
        }
    }

    if failed_count > 0 {
        ("Failed".to_string(), running_count)
    } else if running_count == desired_replicas {
        ("Active".to_string(), running_count)
    } else {
        ("Pending".to_string(), running_count)
    }
}

mod phase_detection {
    use super::*;
    use k8s_openapi::api::core::v1::PodStatus;

    /// Timeout for ImagePullBackOff/ErrImagePull before treating as failure (5 minutes)
    const IMAGE_PULL_FAILURE_TIMEOUT_SECS: u64 = 300;

    /// Calculate pod age in seconds from creation timestamp
    pub fn get_pod_age_seconds(pod: &Pod) -> Option<u64> {
        pod.metadata.creation_timestamp.as_ref().map(|ts| {
            let now = k8s_openapi::chrono::Utc::now();
            let created = ts.0;
            (now - created).num_seconds().max(0) as u64
        })
    }

    pub fn determine_phase(pods: &[Pod], desired_replicas: u32) -> (DeploymentPhase, u32) {
        // Filter out terminating pods
        let active_pods: Vec<_> = pods
            .iter()
            .filter(|p| !super::is_pod_terminating(p))
            .collect();

        if active_pods.is_empty() {
            return (DeploymentPhase::Pending, 0);
        }

        let mut ready_count = 0u32;
        let mut scheduling_count = 0u32;
        let mut pulling_count = 0u32;
        let mut starting_count = 0u32;
        let mut failed_count = 0u32;
        let mut health_check_count = 0u32;

        for pod in active_pods {
            let status = pod.status.as_ref();
            let pod_age = get_pod_age_seconds(pod);

            if !is_scheduled(status) {
                scheduling_count += 1;
                continue;
            }

            if is_pulling_image(status, pod_age) {
                pulling_count += 1;
                continue;
            }

            if has_pending_init_containers(status) {
                starting_count += 1;
                continue;
            }

            if is_failed(status, pod_age) {
                failed_count += 1;
                continue;
            }

            if is_container_starting(status) {
                starting_count += 1;
                continue;
            }

            if is_ready(status) {
                ready_count += 1;
            } else {
                health_check_count += 1;
            }
        }

        let phase = if failed_count > 0 && ready_count == 0 {
            DeploymentPhase::Failed
        } else if failed_count > 0 {
            DeploymentPhase::Degraded
        } else if ready_count == desired_replicas {
            DeploymentPhase::Ready
        } else if scheduling_count > 0 {
            DeploymentPhase::Scheduling
        } else if pulling_count > 0 {
            DeploymentPhase::Pulling
        } else if starting_count > 0 {
            DeploymentPhase::Starting
        } else if health_check_count > 0 {
            DeploymentPhase::HealthCheck
        } else {
            DeploymentPhase::Pending
        };

        (phase, ready_count)
    }

    fn is_scheduled(status: Option<&PodStatus>) -> bool {
        status
            .and_then(|s| s.conditions.as_ref())
            .map(|conditions| {
                conditions
                    .iter()
                    .any(|c| c.type_ == "PodScheduled" && c.status == "True")
            })
            .unwrap_or(false)
    }

    fn is_pulling_image(status: Option<&PodStatus>, pod_age: Option<u64>) -> bool {
        let is_image_pull_state = status
            .and_then(|s| s.container_statuses.as_ref())
            .map(|statuses| {
                statuses.iter().any(|cs| {
                    cs.state
                        .as_ref()
                        .and_then(|state| state.waiting.as_ref())
                        .and_then(|w| w.reason.as_deref())
                        .map(|reason| {
                            reason == "Pulling"
                                || reason == "PullBackOff"
                                || reason == "ImagePullBackOff"
                                || reason == "ErrImagePull"
                        })
                        .unwrap_or(false)
                })
            })
            .unwrap_or(false);

        if !is_image_pull_state {
            return false;
        }

        // If pod is older than timeout and in ImagePullBackOff/ErrImagePull, treat as failure instead
        let is_prolonged_failure = has_prolonged_image_pull_failure(status, pod_age);
        !is_prolonged_failure
    }

    fn has_prolonged_image_pull_failure(status: Option<&PodStatus>, pod_age: Option<u64>) -> bool {
        let exceeds_timeout = pod_age
            .map(|age| age > IMAGE_PULL_FAILURE_TIMEOUT_SECS)
            .unwrap_or(false);

        if !exceeds_timeout {
            return false;
        }

        status
            .and_then(|s| s.container_statuses.as_ref())
            .map(|statuses| {
                statuses.iter().any(|cs| {
                    cs.state
                        .as_ref()
                        .and_then(|state| state.waiting.as_ref())
                        .and_then(|w| w.reason.as_deref())
                        .map(|reason| reason == "ImagePullBackOff" || reason == "ErrImagePull")
                        .unwrap_or(false)
                })
            })
            .unwrap_or(false)
    }

    fn has_pending_init_containers(status: Option<&PodStatus>) -> bool {
        status
            .and_then(|s| s.init_container_statuses.as_ref())
            .map(|statuses| {
                statuses.iter().any(|cs| {
                    cs.state
                        .as_ref()
                        .map(|state| state.running.is_some() || state.waiting.is_some())
                        .unwrap_or(false)
                })
            })
            .unwrap_or(false)
    }

    fn is_container_starting(status: Option<&PodStatus>) -> bool {
        status
            .and_then(|s| s.container_statuses.as_ref())
            .map(|statuses| {
                statuses.iter().any(|cs| {
                    cs.state
                        .as_ref()
                        .map(|state| state.running.is_none() && state.terminated.is_none())
                        .unwrap_or(true)
                })
            })
            .unwrap_or(false)
    }

    pub(crate) fn is_failed(status: Option<&PodStatus>, pod_age: Option<u64>) -> bool {
        let status = match status {
            Some(s) => s,
            None => return false,
        };

        if status.phase.as_deref() == Some("Failed") {
            return true;
        }

        // Check for prolonged image pull failures (ImagePullBackOff/ErrImagePull after timeout)
        if has_prolonged_image_pull_failure(Some(status), pod_age) {
            return true;
        }

        status
            .container_statuses
            .as_ref()
            .map(|statuses| {
                statuses.iter().any(|cs| {
                    let waiting_failed = cs
                        .state
                        .as_ref()
                        .and_then(|state| state.waiting.as_ref())
                        .and_then(|w| w.reason.as_deref())
                        .map(|reason| {
                            reason == "CrashLoopBackOff"
                                || reason == "Error"
                                || reason == "CreateContainerError"
                                || reason == "CreateContainerConfigError"
                        })
                        .unwrap_or(false);

                    let terminated_failed = cs
                        .state
                        .as_ref()
                        .and_then(|state| state.terminated.as_ref())
                        .and_then(|t| t.reason.as_deref())
                        .map(|reason| reason == "OOMKilled" || reason == "Error")
                        .unwrap_or(false);

                    waiting_failed || terminated_failed
                })
            })
            .unwrap_or(false)
    }

    fn is_ready(status: Option<&PodStatus>) -> bool {
        status
            .and_then(|s| s.conditions.as_ref())
            .map(|conditions| {
                conditions
                    .iter()
                    .any(|c| c.type_ == "Ready" && c.status == "True")
            })
            .unwrap_or(false)
    }

    pub fn build_progress_message(phase: &DeploymentPhase) -> String {
        match phase {
            DeploymentPhase::Pending => "Waiting for resources".to_string(),
            DeploymentPhase::Scheduling => "Waiting for node assignment".to_string(),
            DeploymentPhase::Pulling => "Pulling container image".to_string(),
            DeploymentPhase::Initializing => "Initializing containers".to_string(),
            DeploymentPhase::StorageSync => "Syncing storage".to_string(),
            DeploymentPhase::Starting => "Starting container".to_string(),
            DeploymentPhase::HealthCheck => "Waiting for health check".to_string(),
            DeploymentPhase::Ready => "Deployment ready".to_string(),
            DeploymentPhase::Degraded => "Deployment degraded".to_string(),
            DeploymentPhase::Failed => "Deployment failed".to_string(),
            DeploymentPhase::Suspended => "Deployment suspended".to_string(),
            DeploymentPhase::Terminating => "Deployment terminating".to_string(),
        }
    }

    pub fn calculate_progress(phase: &DeploymentPhase, phase_start: Option<&str>) -> ProgressInfo {
        let elapsed = phase_start
            .and_then(|s| k8s_openapi::chrono::DateTime::parse_from_rfc3339(s).ok())
            .map(|start| {
                let now = k8s_openapi::chrono::Utc::now();
                (now.signed_duration_since(start.with_timezone(&k8s_openapi::chrono::Utc)))
                    .num_seconds()
                    .max(0) as u64
            })
            .unwrap_or(0);

        let current_step = build_progress_message(phase);

        ProgressInfo {
            bytes_synced: None,
            bytes_total: None,
            percentage: None,
            current_step,
            started_at: phase_start.unwrap_or_default().to_string(),
            elapsed_seconds: elapsed,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::crd::user_deployment::{ResourceRequirements, UserDeploymentSpec};

    #[test]
    fn test_render_deployment() {
        let spec = UserDeploymentSpec::new(
            "user123".to_string(),
            "my-app".to_string(),
            "nginx:latest".to_string(),
            2,
            80,
            "/deployments/my-app".to_string(),
        )
        .with_resources(ResourceRequirements {
            cpu: "500m".to_string(),
            memory: "512Mi".to_string(),
            gpus: None,
            cpu_request_ratio: 1.0,
        });

        let deployment = render_deployment("my-app", "u-user123", &spec, None).unwrap();

        assert_eq!(
            deployment.metadata.name,
            Some("my-app-deployment".to_string())
        );
        assert_eq!(deployment.metadata.namespace, Some("u-user123".to_string()));

        let spec = deployment.spec.unwrap();
        assert_eq!(spec.replicas, Some(2));

        let template = spec.template;
        let pod_spec = template.spec.unwrap();

        assert_eq!(pod_spec.automount_service_account_token, Some(false));

        assert!(pod_spec.node_selector.is_some());
        let node_selector = pod_spec.node_selector.unwrap();
        assert_eq!(
            node_selector.get("basilica.ai/workloads-only"),
            Some(&"true".to_string())
        );

        assert!(pod_spec.security_context.is_some());
        let pod_sc = pod_spec.security_context.unwrap();
        assert_eq!(pod_sc.fs_group, Some(1000));

        assert_eq!(pod_spec.containers.len(), 1);
        let container = &pod_spec.containers[0];
        assert_eq!(container.name, "my-app");
        assert_eq!(container.image, Some("nginx:latest".to_string()));

        let container_sc = container.security_context.as_ref().unwrap();
        assert_eq!(container_sc.allow_privilege_escalation, Some(false));
        assert!(container_sc.capabilities.is_some());
        let caps = container_sc.capabilities.as_ref().unwrap();
        assert_eq!(caps.drop, Some(vec!["ALL".to_string()]));
    }

    #[test]
    fn test_render_service() {
        let service = render_service("my-app", "u-user123", 80);

        assert_eq!(service.metadata.name, Some("s-my-app".to_string()));
        assert_eq!(service.metadata.namespace, Some("u-user123".to_string()));

        let spec = service.spec.unwrap();
        assert_eq!(spec.type_, Some("ClusterIP".to_string()));

        let selector = spec.selector.unwrap();
        assert_eq!(selector.get("app"), Some(&"my-app".to_string()));

        let ports = spec.ports.unwrap();
        assert_eq!(ports.len(), 1);
        assert_eq!(ports[0].port, 80);
        assert_eq!(ports[0].target_port, Some(IntOrString::Int(80)));
    }

    #[test]
    fn test_render_network_policy() {
        let netpol = render_network_policy("my-app", "u-user123", 80);

        assert_eq!(netpol.metadata.name, Some("my-app-netpol".to_string()));
        assert_eq!(netpol.metadata.namespace, Some("u-user123".to_string()));

        let spec = netpol.spec.unwrap();
        assert_eq!(spec.policy_types, Some(vec!["Ingress".to_string()]));

        let pod_selector = spec.pod_selector;
        assert_eq!(
            pod_selector.match_labels.unwrap().get("app"),
            Some(&"my-app".to_string())
        );

        let ingress_rules = spec.ingress.unwrap();
        assert_eq!(ingress_rules.len(), 1);

        let rule = &ingress_rules[0];
        assert!(rule.from.is_some());
        let from_peers = rule.from.as_ref().unwrap();
        assert_eq!(from_peers.len(), 1);

        let peer = &from_peers[0];
        assert!(peer.namespace_selector.is_some());
        assert!(peer.pod_selector.is_some());

        let ports = rule.ports.as_ref().unwrap();
        assert_eq!(ports.len(), 1);
        assert_eq!(ports[0].port, Some(IntOrString::Int(80)));
        assert_eq!(ports[0].protocol, Some("TCP".to_string()));

        assert!(spec.egress.is_none());
    }

    #[test]
    fn test_render_pdb_single_replica() {
        let owner = UserDeployment {
            metadata: ObjectMeta {
                name: Some("test-deploy".to_string()),
                namespace: Some("u-test".to_string()),
                uid: Some("test-uid-123".to_string()),
                ..Default::default()
            },
            spec: UserDeploymentSpec::new(
                "user123".to_string(),
                "my-app".to_string(),
                "nginx:latest".to_string(),
                1,
                80,
                "/deployments/my-app".to_string(),
            ),
            status: None,
        };

        // PDB should not be created for single replica deployments
        let pdb = render_pdb("my-app", "u-test", 1, &owner);
        assert!(pdb.is_none());
    }

    #[test]
    fn test_render_pdb_multi_replica() {
        let owner = UserDeployment {
            metadata: ObjectMeta {
                name: Some("test-deploy".to_string()),
                namespace: Some("u-test".to_string()),
                uid: Some("test-uid-123".to_string()),
                ..Default::default()
            },
            spec: UserDeploymentSpec::new(
                "user123".to_string(),
                "my-app".to_string(),
                "nginx:latest".to_string(),
                3,
                80,
                "/deployments/my-app".to_string(),
            ),
            status: None,
        };

        let pdb = render_pdb("my-app", "u-test", 3, &owner);
        assert!(pdb.is_some());

        let pdb = pdb.unwrap();
        assert_eq!(pdb.metadata.name, Some("my-app-pdb".to_string()));
        assert_eq!(pdb.metadata.namespace, Some("u-test".to_string()));

        // Check owner references
        let owner_refs = pdb.metadata.owner_references.unwrap();
        assert_eq!(owner_refs.len(), 1);
        assert_eq!(owner_refs[0].uid, "test-uid-123");
        assert_eq!(owner_refs[0].kind, "UserDeployment");
        assert_eq!(owner_refs[0].controller, Some(true));
        assert_eq!(owner_refs[0].block_owner_deletion, Some(true));

        // Check minAvailable = 1 for 3 replicas
        let spec = pdb.spec.unwrap();
        assert_eq!(spec.min_available, Some(IntOrString::Int(1)));
    }

    #[test]
    fn test_render_pdb_missing_uid() {
        let owner = UserDeployment {
            metadata: ObjectMeta {
                name: Some("test-deploy".to_string()),
                namespace: Some("u-test".to_string()),
                uid: None, // Missing UID
                ..Default::default()
            },
            spec: UserDeploymentSpec::new(
                "user123".to_string(),
                "my-app".to_string(),
                "nginx:latest".to_string(),
                3,
                80,
                "/deployments/my-app".to_string(),
            ),
            status: None,
        };

        // PDB should not be created when owner UID is missing
        let pdb = render_pdb("my-app", "u-test", 3, &owner);
        assert!(pdb.is_none());
    }

    #[test]
    fn test_render_pdb_large_replica_count() {
        let owner = UserDeployment {
            metadata: ObjectMeta {
                name: Some("test-deploy".to_string()),
                namespace: Some("u-test".to_string()),
                uid: Some("test-uid-123".to_string()),
                ..Default::default()
            },
            spec: UserDeploymentSpec::new(
                "user123".to_string(),
                "my-app".to_string(),
                "nginx:latest".to_string(),
                4,
                80,
                "/deployments/my-app".to_string(),
            ),
            status: None,
        };

        let pdb = render_pdb("my-app", "u-test", 4, &owner);
        assert!(pdb.is_some());

        let pdb = pdb.unwrap();
        let spec = pdb.spec.unwrap();
        // For 4+ replicas, minAvailable should be 50%
        assert_eq!(
            spec.min_available,
            Some(IntOrString::String("50%".to_string()))
        );
    }

    #[test]
    fn test_compute_state_from_pods_empty() {
        let pods = vec![];
        let (state, ready) = compute_state_from_pods(&pods, 2);
        assert_eq!(state, "Pending");
        assert_eq!(ready, 0);
    }

    #[test]
    fn test_compute_state_from_pods_all_ready() {
        use k8s_openapi::api::core::v1::{Pod, PodCondition, PodStatus};

        let pod1 = Pod {
            status: Some(PodStatus {
                phase: Some("Running".to_string()),
                conditions: Some(vec![PodCondition {
                    type_: "Ready".to_string(),
                    status: "True".to_string(),
                    ..Default::default()
                }]),
                ..Default::default()
            }),
            ..Default::default()
        };

        let pod2 = pod1.clone();

        let (state, ready) = compute_state_from_pods(&[pod1, pod2], 2);
        assert_eq!(state, "Active");
        assert_eq!(ready, 2);
    }

    #[test]
    fn test_compute_state_from_pods_partial_ready() {
        use k8s_openapi::api::core::v1::{Pod, PodCondition, PodStatus};

        let ready_pod = Pod {
            status: Some(PodStatus {
                phase: Some("Running".to_string()),
                conditions: Some(vec![PodCondition {
                    type_: "Ready".to_string(),
                    status: "True".to_string(),
                    ..Default::default()
                }]),
                ..Default::default()
            }),
            ..Default::default()
        };

        let pending_pod = Pod {
            status: Some(PodStatus {
                phase: Some("Pending".to_string()),
                ..Default::default()
            }),
            ..Default::default()
        };

        let (state, ready) = compute_state_from_pods(&[ready_pod, pending_pod], 2);
        assert_eq!(state, "Pending");
        assert_eq!(ready, 1);
    }

    #[test]
    fn test_compute_state_from_pods_with_failure() {
        use k8s_openapi::api::core::v1::{Pod, PodCondition, PodStatus};

        let ready_pod = Pod {
            status: Some(PodStatus {
                phase: Some("Running".to_string()),
                conditions: Some(vec![PodCondition {
                    type_: "Ready".to_string(),
                    status: "True".to_string(),
                    ..Default::default()
                }]),
                ..Default::default()
            }),
            ..Default::default()
        };

        let failed_pod = Pod {
            status: Some(PodStatus {
                phase: Some("Failed".to_string()),
                ..Default::default()
            }),
            ..Default::default()
        };

        let (state, ready) = compute_state_from_pods(&[ready_pod, failed_pod], 2);
        assert_eq!(state, "Failed");
        assert_eq!(ready, 1);
    }

    #[test]
    fn test_security_contexts() {
        let (pod_sc, container_sc) = build_security_contexts();

        let pod_sc = pod_sc.unwrap();
        assert_eq!(pod_sc.fs_group, Some(1000));
        assert!(pod_sc.seccomp_profile.is_some());

        let container_sc = container_sc.unwrap();
        assert_eq!(container_sc.allow_privilege_escalation, Some(false));
        assert!(container_sc.capabilities.is_some());
        let caps = container_sc.capabilities.unwrap();
        assert_eq!(caps.drop, Some(vec!["ALL".to_string()]));
    }

    #[test]
    fn test_node_selector() {
        let selector = build_node_selector();
        assert_eq!(selector.len(), 1);
        assert_eq!(
            selector.get("basilica.ai/workloads-only"),
            Some(&"true".to_string())
        );
    }

    #[test]
    fn test_tolerations_without_gpu() {
        let tolerations = build_tolerations(false);
        assert_eq!(tolerations.len(), 1);
        assert_eq!(
            tolerations[0].key.as_deref(),
            Some("basilica.ai/workloads-only")
        );
    }

    #[test]
    fn test_tolerations_with_gpu() {
        let tolerations = build_tolerations(true);
        assert_eq!(tolerations.len(), 2);
        assert_eq!(
            tolerations[0].key.as_deref(),
            Some("basilica.ai/workloads-only")
        );
        assert_eq!(tolerations[1].key.as_deref(), Some("nvidia.com/gpu"));
        assert_eq!(tolerations[1].operator.as_deref(), Some("Exists"));
        assert_eq!(tolerations[0].operator.as_deref(), Some("Equal"));
        assert_eq!(tolerations[0].value.as_deref(), Some("true"));
        assert_eq!(tolerations[0].effect.as_deref(), Some("NoSchedule"));
    }

    #[test]
    fn test_make_service_name() {
        assert_eq!(make_service_name("my-app"), "s-my-app");
        assert_eq!(
            make_service_name("30b9d5fe-3285-43dd-847d-2a02736ef23a"),
            "s-30b9d5fe-3285-43dd-847d-2a02736ef23a"
        );
        assert_eq!(make_service_name("abc123"), "s-abc123");
    }

    #[test]
    fn test_render_service_with_numeric_instance_name() {
        let service = render_service("30b9d5fe-3285-43dd-847d-2a02736ef23a", "u-user123", 80);

        let name = service.metadata.name.unwrap();
        assert!(name.starts_with("s-"));
        assert_eq!(name, "s-30b9d5fe-3285-43dd-847d-2a02736ef23a");

        let first_char = name.chars().next().unwrap();
        assert!(first_char.is_ascii_alphabetic());
    }

    #[test]
    fn test_render_deployment_with_gpu_node_affinity() {
        use crate::crd::user_deployment::GpuSpec;

        let spec = UserDeploymentSpec::new(
            "user123".to_string(),
            "gpu-app".to_string(),
            "pytorch:latest".to_string(),
            1,
            8080,
            "/deployments/gpu-app".to_string(),
        )
        .with_resources(ResourceRequirements {
            cpu: "4000m".to_string(),
            memory: "16Gi".to_string(),
            gpus: Some(GpuSpec {
                count: 2,
                model: vec!["A100".to_string(), "H100".to_string()],
                min_cuda_version: Some("12.2".to_string()),
                min_gpu_memory_gb: Some(40),
            }),
            cpu_request_ratio: 1.0,
        });

        let deployment = render_deployment("gpu-app", "u-user123", &spec, None).unwrap();
        let pod_spec = deployment.spec.unwrap().template.spec.unwrap();

        assert!(pod_spec.affinity.is_some());
        let affinity = pod_spec.affinity.unwrap();
        assert!(affinity.node_affinity.is_some());

        let node_affinity = affinity.node_affinity.unwrap();
        assert!(node_affinity
            .required_during_scheduling_ignored_during_execution
            .is_some());

        let node_selector = node_affinity
            .required_during_scheduling_ignored_during_execution
            .unwrap();
        let terms = node_selector.node_selector_terms;
        assert_eq!(terms.len(), 1);

        let expressions = &terms[0].match_expressions;
        assert!(expressions.is_some());
        let exprs = expressions.as_ref().unwrap();

        let gpu_model_expr = exprs
            .iter()
            .find(|e| e.key == "basilica.ai/gpu-model")
            .unwrap();
        assert_eq!(gpu_model_expr.operator, "In");
        assert_eq!(
            gpu_model_expr.values.as_ref().unwrap(),
            &vec!["A100".to_string(), "H100".to_string()]
        );

        let cuda_expr = exprs
            .iter()
            .find(|e| e.key == "basilica.ai/cuda-major")
            .unwrap();
        assert_eq!(cuda_expr.operator, "In");
        let cuda_values = cuda_expr.values.as_ref().unwrap();
        assert!(cuda_values.contains(&"12".to_string()));
        assert!(cuda_values.contains(&"13".to_string()));
        assert!(cuda_values.contains(&"20".to_string()));
        assert!(!cuda_values.contains(&"11".to_string()));

        let gpu_memory_expr = exprs
            .iter()
            .find(|e| e.key == "basilica.ai/gpu-memory-gb")
            .unwrap();
        assert_eq!(gpu_memory_expr.operator, "In");
        let memory_values = gpu_memory_expr.values.as_ref().unwrap();
        assert!(memory_values.contains(&"40".to_string()));
        assert!(memory_values.contains(&"256".to_string()));
        assert_eq!(memory_values.len(), 217);

        let gpu_count_expr = exprs
            .iter()
            .find(|e| e.key == "basilica.ai/gpu-count")
            .unwrap();
        assert_eq!(gpu_count_expr.operator, "In");
        let count_values = gpu_count_expr.values.as_ref().unwrap();
        assert_eq!(
            count_values,
            &vec![
                "2".to_string(),
                "3".to_string(),
                "4".to_string(),
                "5".to_string(),
                "6".to_string(),
                "7".to_string(),
                "8".to_string()
            ]
        );

        let container = &pod_spec.containers[0];
        let resources = container.resources.as_ref().unwrap();
        let limits = resources.limits.as_ref().unwrap();
        assert_eq!(limits.get("nvidia.com/gpu").unwrap().0, "2");
    }

    #[test]
    fn test_render_deployment_with_daemonset_storage() {
        use crate::crd::user_deployment::{PersistentStorageSpec, StorageBackend, StorageSpec};

        let spec = UserDeploymentSpec::new(
            "user123".to_string(),
            "storage-app".to_string(),
            "nginx:latest".to_string(),
            1,
            80,
            "/deployments/storage-app".to_string(),
        )
        .with_storage(StorageSpec {
            ephemeral: None,
            persistent: Some(PersistentStorageSpec {
                enabled: true,
                backend: StorageBackend::R2,
                bucket: "my-bucket".to_string(),
                region: Some("us-west-2".to_string()),
                endpoint: Some("https://r2.example.com".to_string()),
                credentials_secret: Some("r2-creds".to_string()),
                sync_interval_ms: 1000,
                cache_size_mb: 2048,
                mount_path: "/data".to_string(),
            }),
        });

        let deployment = render_deployment("storage-app", "u-user123", &spec, None).unwrap();
        let pod_spec = deployment.spec.unwrap().template.spec.unwrap();

        // DaemonSet pattern: only 1 container (no sidecar)
        assert_eq!(pod_spec.containers.len(), 1);

        let main_container = &pod_spec.containers[0];
        assert_eq!(main_container.name, "storage-app");

        // Verify storage volume mount with HostToContainer propagation
        let main_mounts = main_container.volume_mounts.as_ref().unwrap();
        let data_mount = main_mounts
            .iter()
            .find(|m| m.name == "basilica-storage")
            .unwrap();
        assert_eq!(data_mount.mount_path, "/data");
        assert_eq!(
            data_mount.mount_propagation,
            Some("HostToContainer".to_string())
        );

        // Verify hostPath volume points to namespace-scoped path
        let volumes = pod_spec.volumes.as_ref().unwrap();
        let storage_volume = volumes
            .iter()
            .find(|v| v.name == "basilica-storage")
            .unwrap();
        let host_path = storage_volume.host_path.as_ref().unwrap();
        assert_eq!(
            host_path.path, "/var/lib/basilica/fuse/u-user123",
            "Path should use namespace, not instance_name"
        );
        assert_eq!(
            host_path.type_.as_deref(),
            Some("Directory"),
            "Type should be Directory (mount must exist from DaemonSet)"
        );

        // No fuse-device volume needed (DaemonSet handles /dev/fuse)
        assert!(
            !volumes.iter().any(|v| v.name == "fuse-device"),
            "fuse-device volume should not be present with DaemonSet pattern"
        );

        // Main container should still wait for FUSE mount
        let main_command = main_container.command.as_ref().unwrap();
        assert_eq!(main_command[0], "/bin/sh");
        assert_eq!(main_command[1], "-c");

        let main_args = main_container.args.as_ref().unwrap();
        assert_eq!(main_args.len(), 1);
        let wrapped_script = &main_args[0];
        assert!(
            wrapped_script.contains("Waiting for FUSE mount at /data"),
            "Script should contain FUSE wait logic"
        );
        assert!(
            wrapped_script.contains(".fuse_ready"),
            "Script should check for .fuse_ready marker"
        );
    }

    #[test]
    fn test_render_deployment_suspended() {
        let spec = UserDeploymentSpec::new(
            "user123".to_string(),
            "suspended-app".to_string(),
            "nginx:latest".to_string(),
            3,
            80,
            "/deployments/suspended-app".to_string(),
        )
        .suspended();

        let deployment = render_deployment("suspended-app", "u-user123", &spec, None).unwrap();
        let deployment_spec = deployment.spec.unwrap();

        assert_eq!(deployment_spec.replicas, Some(0));
    }

    #[test]
    fn test_render_deployment_not_suspended() {
        let spec = UserDeploymentSpec::new(
            "user123".to_string(),
            "active-app".to_string(),
            "nginx:latest".to_string(),
            3,
            80,
            "/deployments/active-app".to_string(),
        );

        let deployment = render_deployment("active-app", "u-user123", &spec, None).unwrap();
        let deployment_spec = deployment.spec.unwrap();

        assert_eq!(deployment_spec.replicas, Some(3));
    }

    #[test]
    fn test_render_deployment_gpu_without_optional_fields() {
        use crate::crd::user_deployment::GpuSpec;

        let spec = UserDeploymentSpec::new(
            "user123".to_string(),
            "minimal-gpu-app".to_string(),
            "tensorflow:latest".to_string(),
            1,
            8080,
            "/deployments/minimal-gpu-app".to_string(),
        )
        .with_resources(ResourceRequirements {
            cpu: "2000m".to_string(),
            memory: "8Gi".to_string(),
            gpus: Some(GpuSpec {
                count: 1,
                model: vec!["V100".to_string()],
                min_cuda_version: None,
                min_gpu_memory_gb: None,
            }),
            cpu_request_ratio: 1.0,
        });

        let deployment = render_deployment("minimal-gpu-app", "u-user123", &spec, None).unwrap();
        let pod_spec = deployment.spec.unwrap().template.spec.unwrap();

        assert!(pod_spec.affinity.is_some());
        let affinity = pod_spec.affinity.unwrap();
        let node_affinity = affinity.node_affinity.unwrap();
        let node_selector = node_affinity
            .required_during_scheduling_ignored_during_execution
            .unwrap();
        let terms = node_selector.node_selector_terms;
        let expressions = terms[0].match_expressions.as_ref().unwrap();

        let gpu_model_expr = expressions
            .iter()
            .find(|e| e.key == "basilica.ai/gpu-model")
            .unwrap();
        assert_eq!(
            gpu_model_expr.values.as_ref().unwrap(),
            &vec!["V100".to_string()]
        );

        let cuda_expr = expressions
            .iter()
            .find(|e| e.key == "basilica.ai/cuda-major");
        assert!(cuda_expr.is_none());

        let gpu_memory_expr = expressions
            .iter()
            .find(|e| e.key == "basilica.ai/gpu-memory-gb");
        assert!(gpu_memory_expr.is_none());

        let gpu_count_expr = expressions
            .iter()
            .find(|e| e.key == "basilica.ai/gpu-count")
            .unwrap();
        assert_eq!(gpu_count_expr.operator, "In");
        let count_values = gpu_count_expr.values.as_ref().unwrap();
        assert_eq!(
            count_values,
            &vec![
                "1".to_string(),
                "2".to_string(),
                "3".to_string(),
                "4".to_string(),
                "5".to_string(),
                "6".to_string(),
                "7".to_string(),
                "8".to_string()
            ]
        );

        let container = &pod_spec.containers[0];
        let resources = container.resources.as_ref().unwrap();
        let limits = resources.limits.as_ref().unwrap();
        assert_eq!(limits.get("nvidia.com/gpu").unwrap().0, "1");
    }

    #[test]
    fn test_scale_cpu_quantity() {
        assert_eq!(scale_cpu_quantity("1000m", 0.75).0, "750m");
        assert_eq!(scale_cpu_quantity("2000m", 0.75).0, "1500m");
        assert_eq!(scale_cpu_quantity("4", 0.75).0, "3000m");
        assert_eq!(scale_cpu_quantity("2", 0.5).0, "1000m");
        assert_eq!(scale_cpu_quantity("500m", 1.0).0, "500m");
        assert_eq!(scale_cpu_quantity("333m", 0.75).0, "250m");
    }

    #[test]
    fn test_burstable_cpu_resources() {
        let spec = UserDeploymentSpec::new(
            "user123".to_string(),
            "burstable-app".to_string(),
            "nginx:latest".to_string(),
            1,
            80,
            "/deployments/burstable-app".to_string(),
        )
        .with_resources(ResourceRequirements {
            cpu: "2000m".to_string(),
            memory: "4Gi".to_string(),
            gpus: None,
            cpu_request_ratio: 0.75,
        });

        let deployment = render_deployment("burstable-app", "u-user123", &spec, None).unwrap();
        let pod_spec = deployment.spec.unwrap().template.spec.unwrap();
        let container = &pod_spec.containers[0];
        let resources = container.resources.as_ref().unwrap();

        let limits = resources.limits.as_ref().unwrap();
        let requests = resources.requests.as_ref().unwrap();

        assert_eq!(limits.get("cpu").unwrap().0, "2000m");
        assert_eq!(limits.get("memory").unwrap().0, "4Gi");

        assert_eq!(requests.get("cpu").unwrap().0, "1500m");
        assert_eq!(requests.get("memory").unwrap().0, "4Gi");
    }

    #[test]
    fn test_topology_spread_always_applied() {
        // Topology spread is always applied to ensure pod template stability during scaling
        let result = build_topology_spread("my-app", None);
        assert!(result.is_some());

        let constraints = result.unwrap();
        assert_eq!(constraints.len(), 1);
        assert_eq!(constraints[0].max_skew, 1);
        assert_eq!(constraints[0].topology_key, "kubernetes.io/hostname");
        assert_eq!(constraints[0].when_unsatisfiable, "ScheduleAnyway");

        let label_selector = constraints[0].label_selector.as_ref().unwrap();
        let match_labels = label_selector.match_labels.as_ref().unwrap();
        assert_eq!(match_labels.get("app"), Some(&"my-app".to_string()));
    }

    #[test]
    fn test_topology_spread_with_custom_config() {
        let config = TopologySpreadConfig {
            max_skew: 2,
            when_unsatisfiable: "DoNotSchedule".to_string(),
        };

        let result = build_topology_spread("my-app", Some(&config));
        assert!(result.is_some());

        let constraints = result.unwrap();
        assert_eq!(constraints[0].max_skew, 2);
        assert_eq!(constraints[0].when_unsatisfiable, "DoNotSchedule");
    }

    #[test]
    fn test_deployment_with_topology_spread() {
        use crate::crd::user_deployment::TopologySpreadConfig;

        let spec = UserDeploymentSpec::new(
            "user123".to_string(),
            "spread-app".to_string(),
            "nginx:latest".to_string(),
            3,
            80,
            "/deployments/spread-app".to_string(),
        )
        .with_topology_spread(TopologySpreadConfig {
            max_skew: 1,
            when_unsatisfiable: "ScheduleAnyway".to_string(),
        });

        let deployment = render_deployment("spread-app", "u-user123", &spec, None).unwrap();
        let pod_spec = deployment.spec.unwrap().template.spec.unwrap();

        assert!(pod_spec.topology_spread_constraints.is_some());
        let constraints = pod_spec.topology_spread_constraints.unwrap();
        assert_eq!(constraints.len(), 1);
        assert_eq!(constraints[0].max_skew, 1);
    }

    #[test]
    fn test_deployment_with_gpu_has_gpu_toleration() {
        use crate::crd::user_deployment::GpuSpec;

        let spec = UserDeploymentSpec::new(
            "user123".to_string(),
            "gpu-app".to_string(),
            "pytorch/pytorch:latest".to_string(),
            1,
            8080,
            "/deployments/gpu-app".to_string(),
        )
        .with_resources(ResourceRequirements {
            cpu: "4000m".to_string(),
            memory: "16Gi".to_string(),
            gpus: Some(GpuSpec {
                count: 1,
                model: vec!["A100".to_string()],
                min_cuda_version: None,
                min_gpu_memory_gb: None,
            }),
            cpu_request_ratio: 1.0,
        });

        let deployment = render_deployment("gpu-app", "u-user123", &spec, None).unwrap();
        let pod_spec = deployment.spec.unwrap().template.spec.unwrap();
        let tolerations = pod_spec.tolerations.unwrap();

        assert!(tolerations
            .iter()
            .any(|t| t.key.as_deref() == Some("nvidia.com/gpu")));
    }

    mod phase_detection_tests {
        use super::*;
        use crate::crd::user_deployment::DeploymentPhase;
        use k8s_openapi::api::core::v1::{
            ContainerState, ContainerStateWaiting, ContainerStatus, Pod, PodCondition, PodStatus,
        };

        fn make_scheduled_pod() -> Pod {
            Pod {
                status: Some(PodStatus {
                    conditions: Some(vec![PodCondition {
                        type_: "PodScheduled".to_string(),
                        status: "True".to_string(),
                        ..Default::default()
                    }]),
                    ..Default::default()
                }),
                ..Default::default()
            }
        }

        fn make_ready_pod() -> Pod {
            Pod {
                status: Some(PodStatus {
                    phase: Some("Running".to_string()),
                    conditions: Some(vec![
                        PodCondition {
                            type_: "PodScheduled".to_string(),
                            status: "True".to_string(),
                            ..Default::default()
                        },
                        PodCondition {
                            type_: "Ready".to_string(),
                            status: "True".to_string(),
                            ..Default::default()
                        },
                    ]),
                    container_statuses: Some(vec![ContainerStatus {
                        ready: true,
                        state: Some(ContainerState {
                            running: Some(Default::default()),
                            ..Default::default()
                        }),
                        ..Default::default()
                    }]),
                    ..Default::default()
                }),
                ..Default::default()
            }
        }

        #[test]
        fn test_determine_phase_empty_pods() {
            let pods: Vec<Pod> = vec![];
            let (phase, ready) = phase_detection::determine_phase(&pods, 2);
            assert_eq!(phase, DeploymentPhase::Pending);
            assert_eq!(ready, 0);
        }

        #[test]
        fn test_determine_phase_scheduling() {
            let pod = Pod {
                status: Some(PodStatus {
                    conditions: Some(vec![PodCondition {
                        type_: "PodScheduled".to_string(),
                        status: "False".to_string(),
                        ..Default::default()
                    }]),
                    ..Default::default()
                }),
                ..Default::default()
            };

            let (phase, ready) = phase_detection::determine_phase(&[pod], 1);
            assert_eq!(phase, DeploymentPhase::Scheduling);
            assert_eq!(ready, 0);
        }

        #[test]
        fn test_determine_phase_container_creating() {
            let mut pod = make_scheduled_pod();
            pod.status.as_mut().unwrap().container_statuses = Some(vec![ContainerStatus {
                state: Some(ContainerState {
                    waiting: Some(ContainerStateWaiting {
                        reason: Some("ContainerCreating".to_string()),
                        ..Default::default()
                    }),
                    ..Default::default()
                }),
                ..Default::default()
            }]);

            let (phase, ready) = phase_detection::determine_phase(&[pod], 1);
            assert_eq!(phase, DeploymentPhase::Starting);
            assert_eq!(ready, 0);
        }

        #[test]
        fn test_determine_phase_pulling() {
            let mut pod = make_scheduled_pod();
            pod.status.as_mut().unwrap().container_statuses = Some(vec![ContainerStatus {
                state: Some(ContainerState {
                    waiting: Some(ContainerStateWaiting {
                        reason: Some("Pulling".to_string()),
                        ..Default::default()
                    }),
                    ..Default::default()
                }),
                ..Default::default()
            }]);

            let (phase, ready) = phase_detection::determine_phase(&[pod], 1);
            assert_eq!(phase, DeploymentPhase::Pulling);
            assert_eq!(ready, 0);
        }

        #[test]
        fn test_determine_phase_image_pull_backoff() {
            let mut pod = make_scheduled_pod();
            pod.status.as_mut().unwrap().container_statuses = Some(vec![ContainerStatus {
                state: Some(ContainerState {
                    waiting: Some(ContainerStateWaiting {
                        reason: Some("ImagePullBackOff".to_string()),
                        ..Default::default()
                    }),
                    ..Default::default()
                }),
                ..Default::default()
            }]);

            let (phase, ready) = phase_detection::determine_phase(&[pod], 1);
            assert_eq!(phase, DeploymentPhase::Pulling);
            assert_eq!(ready, 0);
        }

        #[test]
        fn test_determine_phase_ready() {
            let pod = make_ready_pod();
            let (phase, ready) = phase_detection::determine_phase(&[pod], 1);
            assert_eq!(phase, DeploymentPhase::Ready);
            assert_eq!(ready, 1);
        }

        #[test]
        fn test_determine_phase_all_ready() {
            let pods = vec![make_ready_pod(), make_ready_pod()];
            let (phase, ready) = phase_detection::determine_phase(&pods, 2);
            assert_eq!(phase, DeploymentPhase::Ready);
            assert_eq!(ready, 2);
        }

        #[test]
        fn test_determine_phase_failed() {
            let pod = Pod {
                status: Some(PodStatus {
                    phase: Some("Failed".to_string()),
                    conditions: Some(vec![PodCondition {
                        type_: "PodScheduled".to_string(),
                        status: "True".to_string(),
                        ..Default::default()
                    }]),
                    ..Default::default()
                }),
                ..Default::default()
            };

            let (phase, ready) = phase_detection::determine_phase(&[pod], 1);
            assert_eq!(phase, DeploymentPhase::Failed);
            assert_eq!(ready, 0);
        }

        #[test]
        fn test_determine_phase_degraded() {
            let ready_pod = make_ready_pod();
            let failed_pod = Pod {
                status: Some(PodStatus {
                    phase: Some("Failed".to_string()),
                    conditions: Some(vec![PodCondition {
                        type_: "PodScheduled".to_string(),
                        status: "True".to_string(),
                        ..Default::default()
                    }]),
                    ..Default::default()
                }),
                ..Default::default()
            };

            let (phase, ready) = phase_detection::determine_phase(&[ready_pod, failed_pod], 2);
            assert_eq!(phase, DeploymentPhase::Degraded);
            assert_eq!(ready, 1);
        }

        #[test]
        fn test_determine_phase_health_check() {
            let mut pod = make_scheduled_pod();
            pod.status.as_mut().unwrap().phase = Some("Running".to_string());
            pod.status.as_mut().unwrap().container_statuses = Some(vec![ContainerStatus {
                ready: false,
                state: Some(ContainerState {
                    running: Some(Default::default()),
                    ..Default::default()
                }),
                ..Default::default()
            }]);

            let (phase, ready) = phase_detection::determine_phase(&[pod], 1);
            assert_eq!(phase, DeploymentPhase::HealthCheck);
            assert_eq!(ready, 0);
        }

        #[test]
        fn test_build_progress_message() {
            assert_eq!(
                phase_detection::build_progress_message(&DeploymentPhase::Scheduling),
                "Waiting for node assignment"
            );
            assert_eq!(
                phase_detection::build_progress_message(&DeploymentPhase::Pulling),
                "Pulling container image"
            );
            assert_eq!(
                phase_detection::build_progress_message(&DeploymentPhase::Ready),
                "Deployment ready"
            );
            assert_eq!(
                phase_detection::build_progress_message(&DeploymentPhase::Failed),
                "Deployment failed"
            );
        }

        #[test]
        fn test_deployment_phase_requeue_intervals() {
            use std::time::Duration;

            assert_eq!(
                DeploymentPhase::Scheduling.requeue_interval(),
                Duration::from_secs(5)
            );
            assert_eq!(
                DeploymentPhase::Pulling.requeue_interval(),
                Duration::from_secs(5)
            );
            assert_eq!(
                DeploymentPhase::Ready.requeue_interval(),
                Duration::from_secs(120)
            );
            assert_eq!(
                DeploymentPhase::Failed.requeue_interval(),
                Duration::from_secs(60)
            );
            assert_eq!(
                DeploymentPhase::Degraded.requeue_interval(),
                Duration::from_secs(30)
            );
        }

        #[test]
        fn test_deployment_phase_to_state_string() {
            assert_eq!(DeploymentPhase::Ready.to_state_string(), "Active");
            assert_eq!(DeploymentPhase::Failed.to_state_string(), "Failed");
            assert_eq!(
                DeploymentPhase::Terminating.to_state_string(),
                "Terminating"
            );
            assert_eq!(DeploymentPhase::Suspended.to_state_string(), "Suspended");
            assert_eq!(DeploymentPhase::Pending.to_state_string(), "Pending");
            assert_eq!(DeploymentPhase::Scheduling.to_state_string(), "Pending");
            assert_eq!(DeploymentPhase::Pulling.to_state_string(), "Pending");
        }

        #[test]
        fn test_is_failed_crash_loop_backoff() {
            use k8s_openapi::api::core::v1::ContainerStateWaiting;

            let status = PodStatus {
                phase: Some("Running".to_string()),
                container_statuses: Some(vec![ContainerStatus {
                    name: "main".to_string(),
                    state: Some(ContainerState {
                        waiting: Some(ContainerStateWaiting {
                            reason: Some("CrashLoopBackOff".to_string()),
                            message: Some("Back-off restarting failed container".to_string()),
                        }),
                        ..Default::default()
                    }),
                    ..Default::default()
                }]),
                ..Default::default()
            };

            assert!(phase_detection::is_failed(Some(&status), None));
        }

        #[test]
        fn test_is_failed_oom_killed() {
            use k8s_openapi::api::core::v1::ContainerStateTerminated;

            let status = PodStatus {
                phase: Some("Running".to_string()),
                container_statuses: Some(vec![ContainerStatus {
                    name: "main".to_string(),
                    state: Some(ContainerState {
                        terminated: Some(ContainerStateTerminated {
                            reason: Some("OOMKilled".to_string()),
                            exit_code: 137,
                            ..Default::default()
                        }),
                        ..Default::default()
                    }),
                    ..Default::default()
                }]),
                ..Default::default()
            };

            assert!(phase_detection::is_failed(Some(&status), None));
        }

        #[test]
        fn test_is_failed_image_pull_backoff_within_timeout() {
            use k8s_openapi::api::core::v1::ContainerStateWaiting;

            let status = PodStatus {
                phase: Some("Pending".to_string()),
                container_statuses: Some(vec![ContainerStatus {
                    name: "main".to_string(),
                    state: Some(ContainerState {
                        waiting: Some(ContainerStateWaiting {
                            reason: Some("ImagePullBackOff".to_string()),
                            message: Some("Back-off pulling image".to_string()),
                        }),
                        ..Default::default()
                    }),
                    ..Default::default()
                }]),
                ..Default::default()
            };

            // ImagePullBackOff within 5 minute timeout is NOT a failure (transient pulling state)
            assert!(!phase_detection::is_failed(Some(&status), Some(60))); // 1 minute old
            assert!(!phase_detection::is_failed(Some(&status), Some(299))); // Just under 5 minutes
            assert!(!phase_detection::is_failed(Some(&status), None)); // No age info
        }

        #[test]
        fn test_is_failed_image_pull_backoff_after_timeout() {
            use k8s_openapi::api::core::v1::ContainerStateWaiting;

            let status = PodStatus {
                phase: Some("Pending".to_string()),
                container_statuses: Some(vec![ContainerStatus {
                    name: "main".to_string(),
                    state: Some(ContainerState {
                        waiting: Some(ContainerStateWaiting {
                            reason: Some("ImagePullBackOff".to_string()),
                            message: Some("Back-off pulling image".to_string()),
                        }),
                        ..Default::default()
                    }),
                    ..Default::default()
                }]),
                ..Default::default()
            };

            // ImagePullBackOff after 5 minute timeout IS a failure
            assert!(phase_detection::is_failed(Some(&status), Some(301))); // Just over 5 minutes
            assert!(phase_detection::is_failed(Some(&status), Some(600))); // 10 minutes
        }

        #[test]
        fn test_is_failed_err_image_pull_within_timeout() {
            use k8s_openapi::api::core::v1::ContainerStateWaiting;

            let status = PodStatus {
                phase: Some("Pending".to_string()),
                container_statuses: Some(vec![ContainerStatus {
                    name: "main".to_string(),
                    state: Some(ContainerState {
                        waiting: Some(ContainerStateWaiting {
                            reason: Some("ErrImagePull".to_string()),
                            message: Some("Failed to pull image".to_string()),
                        }),
                        ..Default::default()
                    }),
                    ..Default::default()
                }]),
                ..Default::default()
            };

            // ErrImagePull within 5 minute timeout is NOT a failure (transient pulling state)
            assert!(!phase_detection::is_failed(Some(&status), Some(60)));
            assert!(!phase_detection::is_failed(Some(&status), None));
        }

        #[test]
        fn test_is_failed_err_image_pull_after_timeout() {
            use k8s_openapi::api::core::v1::ContainerStateWaiting;

            let status = PodStatus {
                phase: Some("Pending".to_string()),
                container_statuses: Some(vec![ContainerStatus {
                    name: "main".to_string(),
                    state: Some(ContainerState {
                        waiting: Some(ContainerStateWaiting {
                            reason: Some("ErrImagePull".to_string()),
                            message: Some("Failed to pull image".to_string()),
                        }),
                        ..Default::default()
                    }),
                    ..Default::default()
                }]),
                ..Default::default()
            };

            // ErrImagePull after 5 minute timeout IS a failure
            assert!(phase_detection::is_failed(Some(&status), Some(301)));
            assert!(phase_detection::is_failed(Some(&status), Some(600)));
        }

        #[test]
        fn test_determine_phase_image_pull_backoff_within_timeout() {
            // Pod in ImagePullBackOff state but within timeout should be Pulling phase
            let pod = Pod {
                metadata: ObjectMeta {
                    creation_timestamp: Some(k8s_openapi::apimachinery::pkg::apis::meta::v1::Time(
                        k8s_openapi::chrono::Utc::now()
                            - k8s_openapi::chrono::Duration::seconds(60),
                    )),
                    ..Default::default()
                },
                status: Some(PodStatus {
                    conditions: Some(vec![PodCondition {
                        type_: "PodScheduled".to_string(),
                        status: "True".to_string(),
                        ..Default::default()
                    }]),
                    container_statuses: Some(vec![ContainerStatus {
                        state: Some(ContainerState {
                            waiting: Some(ContainerStateWaiting {
                                reason: Some("ImagePullBackOff".to_string()),
                                ..Default::default()
                            }),
                            ..Default::default()
                        }),
                        ..Default::default()
                    }]),
                    ..Default::default()
                }),
                ..Default::default()
            };

            let (phase, ready) = phase_detection::determine_phase(&[pod], 1);
            assert_eq!(phase, DeploymentPhase::Pulling);
            assert_eq!(ready, 0);
        }

        #[test]
        fn test_determine_phase_image_pull_backoff_after_timeout() {
            // Pod in ImagePullBackOff state past timeout should be Failed phase
            let pod = Pod {
                metadata: ObjectMeta {
                    creation_timestamp: Some(k8s_openapi::apimachinery::pkg::apis::meta::v1::Time(
                        k8s_openapi::chrono::Utc::now()
                            - k8s_openapi::chrono::Duration::seconds(400),
                    )),
                    ..Default::default()
                },
                status: Some(PodStatus {
                    conditions: Some(vec![PodCondition {
                        type_: "PodScheduled".to_string(),
                        status: "True".to_string(),
                        ..Default::default()
                    }]),
                    container_statuses: Some(vec![ContainerStatus {
                        state: Some(ContainerState {
                            waiting: Some(ContainerStateWaiting {
                                reason: Some("ImagePullBackOff".to_string()),
                                ..Default::default()
                            }),
                            ..Default::default()
                        }),
                        ..Default::default()
                    }]),
                    ..Default::default()
                }),
                ..Default::default()
            };

            let (phase, ready) = phase_detection::determine_phase(&[pod], 1);
            assert_eq!(phase, DeploymentPhase::Failed);
            assert_eq!(ready, 0);
        }
    }
}
