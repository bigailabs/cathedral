use kube::CustomResource;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

#[derive(CustomResource, Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[kube(
    group = "basilica.ai",
    version = "v1",
    kind = "UserDeployment",
    namespaced
)]
#[kube(status = "UserDeploymentStatus")]
#[serde(rename_all = "camelCase")]
pub struct UserDeploymentSpec {
    pub user_id: String,
    pub instance_name: String,
    pub image: String,
    pub replicas: u32,
    pub port: u32,
    #[serde(default)]
    pub command: Vec<String>,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: Vec<EnvVar>,
    #[serde(default)]
    pub resources: Option<ResourceRequirements>,
    pub path_prefix: String,
    #[serde(default)]
    pub ttl_seconds: Option<u32>,
    #[serde(default)]
    pub health_check: Option<HealthCheckConfig>,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct EnvVar {
    pub name: String,
    pub value: String,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct ResourceRequirements {
    pub cpu: String,
    pub memory: String,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct HealthCheckConfig {
    #[serde(default)]
    pub liveness: Option<ProbeConfig>,
    #[serde(default)]
    pub readiness: Option<ProbeConfig>,
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema)]
#[serde(rename_all = "camelCase")]
pub struct ProbeConfig {
    pub path: String,
    #[serde(default = "default_initial_delay")]
    pub initial_delay_seconds: u32,
    #[serde(default = "default_period")]
    pub period_seconds: u32,
    #[serde(default = "default_timeout")]
    pub timeout_seconds: u32,
    #[serde(default = "default_failure_threshold")]
    pub failure_threshold: u32,
}

fn default_initial_delay() -> u32 {
    30
}

fn default_period() -> u32 {
    10
}

fn default_timeout() -> u32 {
    5
}

fn default_failure_threshold() -> u32 {
    3
}

#[derive(Serialize, Deserialize, Clone, Debug, JsonSchema, Default)]
#[serde(rename_all = "camelCase")]
pub struct UserDeploymentStatus {
    #[serde(default)]
    pub state: String,
    #[serde(default)]
    pub deployment_name: String,
    #[serde(default)]
    pub service_name: String,
    #[serde(default)]
    pub replicas_ready: u32,
    #[serde(default)]
    pub replicas_desired: u32,
    #[serde(default)]
    pub endpoint: String,
    #[serde(default)]
    pub public_url: String,
    #[serde(default)]
    pub message: Option<String>,
    #[serde(default)]
    pub start_time: Option<String>,
    #[serde(default)]
    pub last_updated: String,
}

impl UserDeploymentSpec {
    pub fn new(
        user_id: String,
        instance_name: String,
        image: String,
        replicas: u32,
        port: u32,
        path_prefix: String,
    ) -> Self {
        Self {
            user_id,
            instance_name,
            image,
            replicas,
            port,
            command: Vec::new(),
            args: Vec::new(),
            env: Vec::new(),
            resources: None,
            path_prefix,
            ttl_seconds: None,
            health_check: None,
        }
    }

    pub fn with_command(mut self, command: Vec<String>) -> Self {
        self.command = command;
        self
    }

    pub fn with_args(mut self, args: Vec<String>) -> Self {
        self.args = args;
        self
    }

    pub fn with_env(mut self, env: Vec<EnvVar>) -> Self {
        self.env = env;
        self
    }

    pub fn with_resources(mut self, resources: ResourceRequirements) -> Self {
        self.resources = Some(resources);
        self
    }

    pub fn with_ttl(mut self, ttl_seconds: u32) -> Self {
        self.ttl_seconds = Some(ttl_seconds);
        self
    }

    pub fn with_health_check(mut self, health_check: HealthCheckConfig) -> Self {
        self.health_check = Some(health_check);
        self
    }
}

impl UserDeploymentStatus {
    pub fn new() -> Self {
        Self {
            state: "Pending".to_string(),
            deployment_name: String::new(),
            service_name: String::new(),
            replicas_ready: 0,
            replicas_desired: 0,
            endpoint: String::new(),
            public_url: String::new(),
            message: None,
            start_time: None,
            last_updated: String::new(),
        }
    }

    pub fn with_state(mut self, state: &str) -> Self {
        self.state = state.to_string();
        self
    }

    pub fn with_deployment_name(mut self, name: String) -> Self {
        self.deployment_name = name;
        self
    }

    pub fn with_service_name(mut self, name: String) -> Self {
        self.service_name = name;
        self
    }

    pub fn with_replicas(mut self, desired: u32, ready: u32) -> Self {
        self.replicas_desired = desired;
        self.replicas_ready = ready;
        self
    }

    pub fn with_endpoint(mut self, endpoint: String) -> Self {
        self.endpoint = endpoint;
        self
    }

    pub fn with_public_url(mut self, url: String) -> Self {
        self.public_url = url;
        self
    }

    pub fn with_message(mut self, message: String) -> Self {
        self.message = Some(message);
        self
    }

    pub fn is_pending(&self) -> bool {
        self.state == "Pending"
    }

    pub fn is_active(&self) -> bool {
        self.state == "Active"
    }

    pub fn is_failed(&self) -> bool {
        self.state == "Failed"
    }

    pub fn is_terminating(&self) -> bool {
        self.state == "Terminating"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_spec_builder() {
        let spec = UserDeploymentSpec::new(
            "user123".to_string(),
            "my-nginx".to_string(),
            "nginx:latest".to_string(),
            2,
            80,
            "/deployments/my-nginx".to_string(),
        )
        .with_command(vec!["/bin/sh".to_string()])
        .with_env(vec![EnvVar {
            name: "ENV_VAR".to_string(),
            value: "value".to_string(),
        }])
        .with_ttl(3600);

        assert_eq!(spec.user_id, "user123");
        assert_eq!(spec.instance_name, "my-nginx");
        assert_eq!(spec.replicas, 2);
        assert_eq!(spec.port, 80);
        assert_eq!(spec.command, vec!["/bin/sh"]);
        assert_eq!(spec.env.len(), 1);
        assert_eq!(spec.ttl_seconds, Some(3600));
    }

    #[test]
    fn test_status_builder() {
        let status = UserDeploymentStatus::new()
            .with_state("Active")
            .with_replicas(2, 2)
            .with_public_url("http://3.21.154.119:8080/deployments/test/".to_string());

        assert_eq!(status.state, "Active");
        assert_eq!(status.replicas_desired, 2);
        assert_eq!(status.replicas_ready, 2);
        assert!(status.is_active());
        assert!(!status.is_pending());
    }

    #[test]
    fn test_status_predicates() {
        let pending = UserDeploymentStatus::new().with_state("Pending");
        assert!(pending.is_pending());
        assert!(!pending.is_active());

        let active = UserDeploymentStatus::new().with_state("Active");
        assert!(active.is_active());
        assert!(!active.is_failed());

        let failed = UserDeploymentStatus::new().with_state("Failed");
        assert!(failed.is_failed());
        assert!(!failed.is_terminating());

        let terminating = UserDeploymentStatus::new().with_state("Terminating");
        assert!(terminating.is_terminating());
    }

    #[test]
    fn test_env_var_equality() {
        let env1 = EnvVar {
            name: "KEY".to_string(),
            value: "value".to_string(),
        };
        let env2 = EnvVar {
            name: "KEY".to_string(),
            value: "value".to_string(),
        };
        assert_eq!(env1, env2);
    }

    #[test]
    fn test_resource_requirements() {
        let resources = ResourceRequirements {
            cpu: "500m".to_string(),
            memory: "512Mi".to_string(),
        };

        let spec = UserDeploymentSpec::new(
            "user123".to_string(),
            "test".to_string(),
            "nginx:latest".to_string(),
            1,
            80,
            "/deployments/test".to_string(),
        )
        .with_resources(resources.clone());

        assert!(spec.resources.is_some());
        let res = spec.resources.unwrap();
        assert_eq!(res.cpu, "500m");
        assert_eq!(res.memory, "512Mi");
    }
}
