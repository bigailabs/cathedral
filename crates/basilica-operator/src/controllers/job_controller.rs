use k8s_openapi::api::batch::v1::{Job, JobSpec};
use k8s_openapi::api::core::v1::{Affinity, Capabilities, Container, EnvVar, PodSecurityContext, PodSpec, PodTemplateSpec, ResourceRequirements, SecurityContext, Toleration, SeccompProfile};
use k8s_openapi::apimachinery::pkg::api::resource::Quantity;
use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;
use k8s_openapi::api::core::v1::{NodeAffinity, NodeSelector, NodeSelectorRequirement, NodeSelectorTerm};

use crate::crd::basilica_job::{BasilicaJob, BasilicaJobSpec, BasilicaJobStatus, GpuSpec as JobGpuSpec, Resources as JobResources};
use crate::k8s_client::K8sClient;
use anyhow::Result;
use std::time::Instant;
use crate::metrics as opmetrics;
use k8s_openapi::api::core::v1::PodStatus;

fn to_quantity(s: &str) -> Quantity { Quantity(s.to_string()) }

fn build_resources(res: &JobResources) -> ResourceRequirements {
    use std::collections::BTreeMap;
    let mut limits = BTreeMap::new();
    let mut requests = BTreeMap::new();

    limits.insert("cpu".to_string(), to_quantity(&res.cpu));
    limits.insert("memory".to_string(), to_quantity(&res.memory));
    requests.insert("cpu".to_string(), to_quantity(&res.cpu));
    requests.insert("memory".to_string(), to_quantity(&res.memory));

    if res.gpus.count > 0 {
        let gpuq = Quantity(res.gpus.count.to_string());
        limits.insert("nvidia.com/gpu".to_string(), gpuq.clone());
        requests.insert("nvidia.com/gpu".to_string(), gpuq);
    }

    ResourceRequirements { limits: Some(limits), requests: Some(requests), claims: None }
}

fn build_env(env: &[(String, String)]) -> Vec<EnvVar> {
    env.iter()
        .map(|(k, v)| EnvVar { name: k.clone(), value: Some(v.clone()), ..Default::default() })
        .collect()
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

fn build_node_affinity(gpu: &JobGpuSpec) -> Option<Affinity> {
    if gpu.model.is_empty() {
        return None;
    }
    let expr = NodeSelectorRequirement {
        key: "basilica.io/gpu-model".into(),
        operator: "In".into(),
        values: Some(gpu.model.clone()),
    };
    let term = NodeSelectorTerm { match_expressions: Some(vec![expr]), match_fields: None };
    let ns = NodeSelector { node_selector_terms: vec![term] };
    Some(Affinity { node_affinity: Some(NodeAffinity { required_during_scheduling_ignored_during_execution: Some(ns), ..Default::default() }), ..Default::default() })
}

fn build_security_contexts() -> (Option<PodSecurityContext>, Option<SecurityContext>) {
    let pod_sc = Some(PodSecurityContext { run_as_non_root: Some(true), seccomp_profile: Some(SeccompProfile { type_: "RuntimeDefault".into(), localhost_profile: None }), ..Default::default() });
    let container_sc = Some(SecurityContext {
        allow_privilege_escalation: Some(false),
        read_only_root_filesystem: Some(true),
        capabilities: Some(Capabilities { drop: Some(vec!["ALL".into()]), ..Default::default() }),
        seccomp_profile: Some(SeccompProfile { type_: "RuntimeDefault".into(), localhost_profile: None }),
        ..Default::default()
    });
    (pod_sc, container_sc)
}

pub fn render_job(name: &str, spec: &BasilicaJobSpec) -> Job {
    let (pod_sc, container_sc) = build_security_contexts();

    let container = Container {
        name: name.to_string(),
        image: Some(spec.image.clone()),
        command: if spec.command.is_empty() { None } else { Some(spec.command.clone()) },
        args: if spec.args.is_empty() { None } else { Some(spec.args.clone()) },
        env: Some(build_env(&spec.env)),
        resources: Some(build_resources(&spec.resources)),
        security_context: container_sc,
        ..Default::default()
    };

    let mut containers = vec![container];
    // Optional artifact sidecar
    if let Some(art) = &spec.artifacts {
        if art.enabled {
            let mut env = Vec::new();
            env.push(EnvVar { name: "DESTINATION".into(), value: Some(art.destination.clone()), ..Default::default() });
            env.push(EnvVar { name: "FROM_PATH".into(), value: Some(art.from_path.clone()), ..Default::default() });
            env.push(EnvVar { name: "PROVIDER".into(), value: Some(if art.provider.is_empty() { "s3".into() } else { art.provider.clone() }), ..Default::default() });
            let sidecar = Container {
                name: format!("artifact-uploader-{}", name),
                image: Some("alpine:3.19".into()),
                command: Some(vec!["/bin/sh".into(), "-c".into(), "tail -f /dev/null".into()]),
                env: Some(env),
                security_context: Some(SecurityContext { allow_privilege_escalation: Some(false), read_only_root_filesystem: Some(true), capabilities: Some(Capabilities { drop: Some(vec!["ALL".into()]), ..Default::default() }), seccomp_profile: Some(SeccompProfile { type_: "RuntimeDefault".into(), localhost_profile: None }), ..Default::default() }),
                ..Default::default()
            };
            containers.push(sidecar);
        }
    }

    let pod_spec = PodSpec {
        containers,
        restart_policy: Some("Never".into()),
        tolerations: Some(build_tolerations()),
        security_context: pod_sc,
        affinity: build_node_affinity(&spec.resources.gpus),
        ..Default::default()
    };

    let labels = Some(
        vec![
            ("basilica.io/type".to_string(), "job".to_string()),
            ("basilica.io/job".to_string(), name.to_string()),
        ]
        .into_iter()
        .collect(),
    );
    let template = PodTemplateSpec { metadata: Some(ObjectMeta { labels: labels.clone(), ..Default::default() }), spec: Some(pod_spec) };

    let active_deadline_seconds = if spec.ttl_seconds > 0 { Some(spec.ttl_seconds as i64) } else { None };

    Job {
        metadata: ObjectMeta { name: Some(name.to_string()), labels, ..Default::default() },
        spec: Some(JobSpec { template, backoff_limit: Some(0), active_deadline_seconds, ..Default::default() }),
        status: None,
    }
}

pub struct JobController<C: K8sClient> {
    pub client: C,
}

impl<C: K8sClient> JobController<C> {
    pub fn new(client: C) -> Self { Self { client } }

    pub async fn reconcile(&self, ns: &str, cr: &BasilicaJob) -> Result<()> {
        let start = Instant::now();
        let name = cr.metadata.name.clone().unwrap_or_default();
        let spec = cr.spec.clone();
        // Observe previous status (if any) to record transitions
        let prev = self
            .client
            .get_basilica_job(ns, &name)
            .await
            .ok()
            .and_then(|bj| bj.status.and_then(|s| s.phase))
            .unwrap_or_else(|| "Unknown".into());

        // Ensure Job exists
        let created = if self.client.get_job(ns, &name).await.is_err() {
            let job = render_job(&name, &spec);
            self.client.create_job(ns, &job).await?;
            true
        } else { false };

        // Derive status from Pods with our label
        let pods = self
            .client
            .list_pods_with_label(ns, "basilica.io/job", &name)
            .await?;

        let (phase, pod_name) = compute_phase_from_pods(&pods);
        let status = BasilicaJobStatus {
            phase: Some(phase),
            pod_name,
            start_time: None,
            completion_time: None,
        };
        let to = status.phase.clone().unwrap_or_else(|| "Unknown".into());
        self.client.update_basilica_job_status(ns, &name, status).await?;
        opmetrics::record_job_reconcile(ns, &name, created, &prev, &to, start);
        let prev_active = prev == "Running";
        let new_active = to == "Running";
        opmetrics::record_job_active_change(ns, prev_active, new_active);
        Ok(())
    }
}

fn compute_phase_from_pods(pods: &[k8s_openapi::api::core::v1::Pod]) -> (String, Option<String>) {
    // Prefer running over pending; succeeded/failed if any pod indicates so.
    let mut running: Option<String> = None;
    let mut succeeded: Option<String> = None;
    let mut failed: Option<String> = None;
    let mut pending: Option<String> = None;

    for p in pods {
        let name = p.metadata.name.clone();
        if let Some(PodStatus { phase: Some(ph), .. }) = &p.status {
            match ph.as_str() {
                "Running" => running = name,
                "Succeeded" => succeeded = name,
                "Failed" => failed = name,
                "Pending" => pending = name,
                _ => {}
            }
        }
    }

    if let Some(n) = running { return ("Running".into(), Some(n)); }
    if let Some(n) = succeeded { return ("Succeeded".into(), Some(n)); }
    if let Some(n) = failed { return ("Failed".into(), Some(n)); }
    if let Some(n) = pending { return ("Pending".into(), Some(n)); }
    ("Pending".into(), None)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_spec() -> BasilicaJobSpec {
        BasilicaJobSpec {
            image: "nvidia/cuda:12.0-base".into(),
            command: vec!["python".into()],
            args: vec!["main.py".into()],
            env: vec![("FOO".into(), "bar".into())],
            resources: crate::crd::basilica_job::Resources { cpu: "4".into(), memory: "16Gi".into(), gpus: JobGpuSpec { count: 1, model: vec!["A100".into()] } },
            storage: None,
            artifacts: None,
            ttl_seconds: 3600,
            priority: "normal".into(),
        }
    }

    #[test]
    fn render_includes_resources_and_security() {
        let spec = sample_spec();
        let job = render_job("job-abc", &spec);
        let tmpl = job.spec.unwrap().template;
        let pod = tmpl.spec.unwrap();
        assert_eq!(pod.restart_policy.unwrap(), "Never");
        assert!(pod.security_context.as_ref().unwrap().run_as_non_root.unwrap());
        let c = &pod.containers[0];
        let res = c.resources.as_ref().unwrap();
        let limits = res.limits.as_ref().unwrap();
        assert_eq!(limits.get("cpu").unwrap().0, "4");
        assert_eq!(limits.get("memory").unwrap().0, "16Gi");
        assert_eq!(limits.get("nvidia.com/gpu").unwrap().0, "1");
        let sc = c.security_context.as_ref().unwrap();
        assert_eq!(sc.allow_privilege_escalation, Some(false));
        assert_eq!(sc.read_only_root_filesystem, Some(true));
        assert!(sc.capabilities.as_ref().unwrap().drop.as_ref().unwrap().contains(&"ALL".into()));
        assert_eq!(sc.seccomp_profile.as_ref().unwrap().type_, "RuntimeDefault");
        assert_eq!(pod.security_context.as_ref().unwrap().seccomp_profile.as_ref().unwrap().type_, "RuntimeDefault");
    }

    #[test]
    fn render_includes_affinity_tolerations_and_ttl() {
        let spec = sample_spec();
        let job = render_job("job-abc", &spec);
        let jobspec = job.spec.as_ref().unwrap();
        assert_eq!(jobspec.active_deadline_seconds, Some(3600));
        let pod = jobspec.template.spec.as_ref().unwrap();
        // Tolerations
        let t = pod.tolerations.as_ref().unwrap();
        assert!(t.iter().any(|x| x.key.as_deref() == Some("basilica.io/workloads-only")));
        // Affinity
        let aff = pod.affinity.as_ref().unwrap();
        let node_aff = aff.node_affinity.as_ref().unwrap();
        let req = node_aff
            .required_during_scheduling_ignored_during_execution
            .as_ref()
            .unwrap()
            .node_selector_terms[0]
            .match_expressions
            .as_ref()
            .unwrap()[0]
            .clone();
        assert_eq!(req.key, "basilica.io/gpu-model");
        assert_eq!(req.operator, "In");
        assert_eq!(req.values.unwrap()[0], "A100");
    }

    #[test]
    fn render_includes_artifact_sidecar_when_enabled() {
        let mut spec = sample_spec();
        spec.artifacts = Some(crate::crd::basilica_job::ArtifactUploadSpec {
            destination: "s3://bucket/prefix".into(),
            from_path: "/outputs".into(),
            provider: "s3".into(),
            credentials_secret: None,
            enabled: true,
        });
        let job = render_job("job-artifacts", &spec);
        let pod = job.spec.unwrap().template.spec.unwrap();
        assert!(pod.containers.iter().any(|c| c.name.starts_with("artifact-uploader-")));
        let sidecar = pod.containers.iter().find(|c| c.name.starts_with("artifact-uploader-")).unwrap();
        let envs = sidecar.env.as_ref().unwrap();
        assert!(envs.iter().any(|e| e.name == "DESTINATION"));
        assert!(envs.iter().any(|e| e.name == "FROM_PATH"));
    }

    use crate::k8s_client::MockK8sClient;
    use k8s_openapi::api::core::v1::PodStatus;

    #[tokio::test]
    async fn reconcile_creates_job_and_updates_status() {
        let _ = metrics_exporter_prometheus::PrometheusBuilder::new().install_recorder();
        let client = MockK8sClient::default();
        let controller = super::JobController::new(client.clone());

        let spec = sample_spec();
        let bj = BasilicaJob::new("bj1", spec);

        // First register CR in mock and reconcile: creates Job, status pending
        controller.client.create_basilica_job("ns", &bj).await.unwrap();
        controller.reconcile("ns", &bj).await.unwrap();
        // Create a running pod labeled for this job
        let mut pod = k8s_openapi::api::core::v1::Pod {
            metadata: ObjectMeta { name: Some("pod1".into()), labels: Some(vec![("basilica.io/job".into(), "bj1".into())].into_iter().collect()), ..Default::default() },
            status: Some(PodStatus { phase: Some("Running".into()), ..Default::default() }),
            ..Default::default()
        };
        controller.client.create_pod("ns", &pod).await.unwrap();

        // Second reconcile: sees running pod, updates status
        controller.reconcile("ns", &bj).await.unwrap();
        let updated = controller.client.get_basilica_job("ns", "bj1").await.unwrap();
        assert_eq!(updated.status.unwrap().phase.unwrap(), "Running");
        // Exercise metrics path (no-op if already installed)
        let _ = metrics_exporter_prometheus::PrometheusBuilder::new().install_recorder();
    }
}
