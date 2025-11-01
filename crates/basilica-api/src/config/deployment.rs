use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DeploymentConfig {
    /// Public IP address for generating deployment URLs
    pub public_ip: String,

    /// Public port for Envoy ingress
    #[serde(default = "default_public_port")]
    pub public_port: u16,

    /// Maximum replicas per deployment
    #[serde(default = "default_max_replicas")]
    pub max_replicas: u32,

    /// Maximum CPU per deployment (e.g., "4" or "4000m")
    #[serde(default = "default_max_cpu")]
    pub max_cpu: String,

    /// Maximum memory per deployment (e.g., "8Gi")
    #[serde(default = "default_max_memory")]
    pub max_memory: String,

    /// Maximum total deployments per user
    #[serde(default = "default_max_deployments_per_user")]
    pub max_deployments_per_user: u32,

    /// Default TTL for deployments in seconds (0 = no TTL)
    #[serde(default = "default_ttl_seconds")]
    pub default_ttl_seconds: u32,

    /// Envoy ConfigMap namespace
    #[serde(default = "default_envoy_namespace")]
    pub envoy_namespace: String,

    /// Envoy ConfigMap name
    #[serde(default = "default_envoy_configmap_name")]
    pub envoy_configmap_name: String,

    /// Envoy Deployment name (for restarts)
    #[serde(default = "default_envoy_deployment_name")]
    pub envoy_deployment_name: String,
}

fn default_public_port() -> u16 {
    8080
}

fn default_max_replicas() -> u32 {
    10
}

fn default_max_cpu() -> String {
    "4".to_string()
}

fn default_max_memory() -> String {
    "8Gi".to_string()
}

fn default_max_deployments_per_user() -> u32 {
    20
}

fn default_ttl_seconds() -> u32 {
    0
}

fn default_envoy_namespace() -> String {
    "basilica-system".to_string()
}

fn default_envoy_configmap_name() -> String {
    "basilica-envoy-config".to_string()
}

fn default_envoy_deployment_name() -> String {
    "basilica-envoy".to_string()
}

impl Default for DeploymentConfig {
    fn default() -> Self {
        Self {
            public_ip: "localhost".to_string(),
            public_port: default_public_port(),
            max_replicas: default_max_replicas(),
            max_cpu: default_max_cpu(),
            max_memory: default_max_memory(),
            max_deployments_per_user: default_max_deployments_per_user(),
            default_ttl_seconds: default_ttl_seconds(),
            envoy_namespace: default_envoy_namespace(),
            envoy_configmap_name: default_envoy_configmap_name(),
            envoy_deployment_name: default_envoy_deployment_name(),
        }
    }
}

impl DeploymentConfig {
    pub fn generate_public_url(&self, path_prefix: &str) -> String {
        format!(
            "http://{}:{}{}/",
            self.public_ip, self.public_port, path_prefix
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_config() {
        let config = DeploymentConfig::default();
        assert_eq!(config.public_ip, "localhost");
        assert_eq!(config.public_port, 8080);
        assert_eq!(config.max_replicas, 10);
        assert_eq!(config.max_cpu, "4");
        assert_eq!(config.max_memory, "8Gi");
        assert_eq!(config.max_deployments_per_user, 20);
        assert_eq!(config.default_ttl_seconds, 0);
        assert_eq!(config.envoy_namespace, "basilica-system");
        assert_eq!(config.envoy_configmap_name, "basilica-envoy-config");
        assert_eq!(config.envoy_deployment_name, "basilica-envoy");
    }

    #[test]
    fn test_generate_public_url() {
        let config = DeploymentConfig {
            public_ip: "3.21.154.119".to_string(),
            public_port: 8080,
            ..Default::default()
        };

        let url = config.generate_public_url("/deployments/my-app");
        assert_eq!(url, "http://3.21.154.119:8080/deployments/my-app/");
    }

    #[test]
    fn test_config_serialization() {
        let config = DeploymentConfig::default();
        let serialized = toml::to_string(&config).unwrap();
        let deserialized: DeploymentConfig = toml::from_str(&serialized).unwrap();

        assert_eq!(config.public_ip, deserialized.public_ip);
        assert_eq!(config.max_replicas, deserialized.max_replicas);
    }

    #[test]
    fn test_config_from_partial_toml() {
        let toml_str = r#"
            public_ip = "3.21.154.119"
            max_replicas = 5
        "#;
        let config: DeploymentConfig = toml::from_str(toml_str).unwrap();
        assert_eq!(config.public_ip, "3.21.154.119");
        assert_eq!(config.max_replicas, 5);
        assert_eq!(config.public_port, 8080);
        assert_eq!(config.max_cpu, "4");
    }
}
