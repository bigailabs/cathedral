use k8s_openapi::api::apps::v1::{Deployment, DeploymentSpec};
use k8s_openapi::api::core::v1::{
    Capabilities, Container, HTTPGetAction, PodSecurityContext, PodSpec, PodTemplateSpec, Probe,
    ResourceRequirements, SecurityContext, Service, ServicePort, ServiceSpec, TCPSocketAction,
    Toleration,
};
use k8s_openapi::api::networking::v1::{
    NetworkPolicy, NetworkPolicyIngressRule, NetworkPolicyPeer, NetworkPolicyPort,
    NetworkPolicySpec,
};
use k8s_openapi::apimachinery::pkg::api::resource::Quantity;
use k8s_openapi::apimachinery::pkg::apis::meta::v1::{LabelSelector, ObjectMeta};
use k8s_openapi::apimachinery::pkg::util::intstr::IntOrString;
use std::collections::BTreeMap;
use std::sync::Arc;

use crate::crd::user_deployment::{EnvVar as CrdEnvVar, UserDeployment, UserDeploymentStatus};
use crate::k8s_client::K8sClient;
use anyhow::Result;

fn to_quantity(s: &str) -> Quantity {
    Quantity(s.to_string())
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

fn build_resources(cpu: &str, memory: &str) -> ResourceRequirements {
    let mut limits = BTreeMap::new();
    let mut requests = BTreeMap::new();
    limits.insert("cpu".to_string(), to_quantity(cpu));
    limits.insert("memory".to_string(), to_quantity(memory));
    requests.insert("cpu".to_string(), to_quantity(cpu));
    requests.insert("memory".to_string(), to_quantity(memory));
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

fn build_tolerations() -> Vec<Toleration> {
    vec![Toleration {
        key: Some("basilica.ai/workloads-only".into()),
        operator: Some("Equal".into()),
        value: Some("true".into()),
        effect: Some("NoSchedule".into()),
        ..Default::default()
    }]
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
            add: Some(vec!["SETUID".into(), "SETGID".into(), "CHOWN".into()]),
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
) -> (Option<Probe>, Option<Probe>) {
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

            (liveness_probe, readiness_probe)
        }
        None => {
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

            (liveness_probe, readiness_probe)
        }
    }
}

pub fn render_deployment(
    instance_name: &str,
    namespace: &str,
    spec: &crate::crd::user_deployment::UserDeploymentSpec,
) -> Deployment {
    let (pod_sc, container_sc) = build_security_contexts();
    let (volumes, volume_mounts) = build_writable_volumes();
    let (liveness_probe, readiness_probe) = build_health_probes(spec.port, &spec.health_check);

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

    let resources = if let Some(ref res) = spec.resources {
        build_resources(&res.cpu, &res.memory)
    } else {
        build_resources("100m", "128Mi")
    };

    let container = Container {
        name: instance_name.to_string(),
        image: Some(spec.image.clone()),
        command: if spec.command.is_empty() {
            None
        } else {
            Some(spec.command.clone())
        },
        args: if spec.args.is_empty() {
            None
        } else {
            Some(spec.args.clone())
        },
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
        ..Default::default()
    };

    let pod_template = PodTemplateSpec {
        metadata: Some(ObjectMeta {
            labels: Some(labels.clone()),
            ..Default::default()
        }),
        spec: Some(PodSpec {
            containers: vec![container],
            security_context: pod_sc,
            node_selector: Some(build_node_selector()),
            tolerations: Some(build_tolerations()),
            restart_policy: Some("Always".into()),
            automount_service_account_token: Some(false),
            volumes: Some(volumes),
            ..Default::default()
        }),
    };

    Deployment {
        metadata: ObjectMeta {
            name: Some(format!("{}-deployment", instance_name)),
            namespace: Some(namespace.to_string()),
            labels: Some(labels.clone()),
            ..Default::default()
        },
        spec: Some(DeploymentSpec {
            replicas: Some(spec.replicas as i32),
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
    }
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

        let deployment_name = format!("{}-deployment", instance_name);
        let service_name = make_service_name(instance_name);

        let deployment_exists = self
            .client
            .get_deployment(ns, &deployment_name)
            .await
            .is_ok();
        if !deployment_exists {
            let deployment = render_deployment(instance_name, ns, spec);
            self.client.create_deployment(ns, &deployment).await?;
        }

        let service_exists = self.client.get_service(ns, &service_name).await.is_ok();
        if !service_exists {
            let service = render_service(instance_name, ns, spec.port);
            self.client.create_service(ns, &service).await?;
        }

        let netpol = render_network_policy(instance_name, ns, spec.port);
        let _ = self.client.create_network_policy(ns, &netpol).await;

        let pods = self
            .client
            .list_pods_with_label(ns, "app", instance_name)
            .await?;

        let (state, replicas_ready) = compute_state_from_pods(&pods, spec.replicas);

        let endpoint = format!("{}.{}:{}", service_name, ns, spec.port);
        let public_url = format!(
            "http://{}:{}{}/",
            self.public_ip, self.public_port, spec.path_prefix
        );

        let mut status = UserDeploymentStatus::new()
            .with_state(&state)
            .with_deployment_name(deployment_name)
            .with_service_name(service_name)
            .with_replicas(spec.replicas, replicas_ready)
            .with_endpoint(endpoint)
            .with_public_url(public_url);

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

fn compute_state_from_pods(
    pods: &[k8s_openapi::api::core::v1::Pod],
    desired_replicas: u32,
) -> (String, u32) {
    if pods.is_empty() {
        return ("Pending".to_string(), 0);
    }

    let mut running_count = 0;
    let mut failed_count = 0;
    let mut _pending_count = 0;

    for pod in pods {
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
        });

        let deployment = render_deployment("my-app", "u-user123", &spec);

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
    fn test_tolerations() {
        let tolerations = build_tolerations();
        assert_eq!(tolerations.len(), 1);
        assert_eq!(
            tolerations[0].key.as_deref(),
            Some("basilica.ai/workloads-only")
        );
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
}
