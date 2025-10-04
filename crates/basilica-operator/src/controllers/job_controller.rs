use k8s_openapi::api::batch::v1::{Job, JobSpec};
use k8s_openapi::api::core::v1::{Affinity, Capabilities, Container, EnvVar, PodSecurityContext, PodSpec, PodTemplateSpec, ResourceRequirements, SecurityContext, Toleration};
use k8s_openapi::apimachinery::pkg::api::resource::Quantity;
use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;
use k8s_openapi::api::core::v1::{NodeAffinity, NodeSelector, NodeSelectorRequirement, NodeSelectorTerm};

use crate::crd::basilica_job::{BasilicaJobSpec, GpuSpec as JobGpuSpec, Resources as JobResources};

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
    let pod_sc = Some(PodSecurityContext { run_as_non_root: Some(true), ..Default::default() });
    let container_sc = Some(SecurityContext {
        allow_privilege_escalation: Some(false),
        read_only_root_filesystem: Some(true),
        capabilities: Some(Capabilities { drop: Some(vec!["ALL".into()]), ..Default::default() }),
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

    let pod_spec = PodSpec {
        containers: vec![container],
        restart_policy: Some("Never".into()),
        tolerations: Some(build_tolerations()),
        security_context: pod_sc,
        affinity: build_node_affinity(&spec.resources.gpus),
        ..Default::default()
    };

    let labels = Some(vec![("basilica.io/type".to_string(), "job".to_string())].into_iter().collect());
    let template = PodTemplateSpec { metadata: Some(ObjectMeta { labels: labels.clone(), ..Default::default() }), spec: Some(pod_spec) };

    let active_deadline_seconds = if spec.ttl_seconds > 0 { Some(spec.ttl_seconds as i64) } else { None };

    Job {
        metadata: ObjectMeta { name: Some(name.to_string()), labels, ..Default::default() },
        spec: Some(JobSpec { template, backoff_limit: Some(0), active_deadline_seconds, ..Default::default() }),
        status: None,
    }
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
        assert!(pod.security_context.unwrap().run_as_non_root.unwrap());
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
}
