use regex::Regex;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

use crate::error::{ApiError, Result};

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct CreateDeploymentRequest {
    pub instance_name: String,
    pub image: String,
    pub replicas: u32,
    pub port: u32,
    #[serde(default)]
    pub command: Vec<String>,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: HashMap<String, String>,
    #[serde(default)]
    pub resources: Option<ResourceRequirements>,
    #[serde(default)]
    pub ttl_seconds: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ResourceRequirements {
    pub cpu: String,
    pub memory: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DeploymentResponse {
    pub instance_name: String,
    pub user_id: String,
    pub namespace: String,
    pub state: String,
    pub url: String,
    pub replicas: ReplicaStatus,
    pub created_at: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub updated_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pods: Option<Vec<PodInfo>>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ReplicaStatus {
    pub desired: u32,
    pub ready: u32,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct PodInfo {
    pub name: String,
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub node: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DeleteDeploymentResponse {
    pub instance_name: String,
    pub state: String,
    pub message: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DeploymentListResponse {
    pub deployments: Vec<DeploymentSummary>,
    pub total: usize,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DeploymentSummary {
    pub instance_name: String,
    pub state: String,
    pub url: String,
    pub replicas: ReplicaStatus,
    pub created_at: String,
}

pub fn validate_instance_name(name: &str) -> Result<()> {
    if name.is_empty() {
        return Err(ApiError::InvalidRequest {
            message: "instance_name cannot be empty".to_string(),
        });
    }

    if name.len() > 63 {
        return Err(ApiError::InvalidRequest {
            message: format!("instance_name too long: {} characters (max 63)", name.len()),
        });
    }

    let dns_regex =
        Regex::new(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$").map_err(|e| ApiError::Internal {
            message: format!("Failed to compile regex: {}", e),
        })?;

    if !dns_regex.is_match(name) {
        return Err(ApiError::InvalidRequest {
            message: format!(
                "instance_name '{}' must be a valid DNS label (lowercase alphanumeric and hyphens, \
                 cannot start or end with hyphen)",
                name
            ),
        });
    }

    Ok(())
}

pub fn validate_image(image: &str) -> Result<()> {
    if image.is_empty() {
        return Err(ApiError::InvalidRequest {
            message: "image cannot be empty".to_string(),
        });
    }

    if image.len() > 1024 {
        return Err(ApiError::InvalidRequest {
            message: format!("image URL too long: {} characters (max 1024)", image.len()),
        });
    }

    if image.contains(';') || image.contains('&') || image.contains('|') {
        return Err(ApiError::InvalidRequest {
            message: "image contains invalid characters (;, &, |)".to_string(),
        });
    }

    Ok(())
}

pub fn validate_port(port: u32) -> Result<()> {
    if port == 0 || port > 65535 {
        return Err(ApiError::InvalidRequest {
            message: format!("port must be in range 1-65535, got {}", port),
        });
    }

    Ok(())
}

pub fn validate_replicas(replicas: u32, max_replicas: u32) -> Result<()> {
    if replicas == 0 {
        return Err(ApiError::InvalidRequest {
            message: "replicas must be at least 1".to_string(),
        });
    }

    if replicas > max_replicas {
        return Err(ApiError::InvalidRequest {
            message: format!("replicas {} exceeds maximum of {}", replicas, max_replicas),
        });
    }

    Ok(())
}

pub fn validate_cpu_resource(cpu: &str) -> Result<()> {
    if cpu.is_empty() {
        return Err(ApiError::InvalidRequest {
            message: "cpu cannot be empty".to_string(),
        });
    }

    let cpu_regex = Regex::new(r"^[0-9]+m?$").map_err(|e| ApiError::Internal {
        message: format!("Failed to compile regex: {}", e),
    })?;

    if !cpu_regex.is_match(cpu) {
        return Err(ApiError::InvalidRequest {
            message: format!(
                "invalid cpu format '{}' (expected format: '500m' or '2')",
                cpu
            ),
        });
    }

    Ok(())
}

pub fn validate_memory_resource(memory: &str) -> Result<()> {
    if memory.is_empty() {
        return Err(ApiError::InvalidRequest {
            message: "memory cannot be empty".to_string(),
        });
    }

    let memory_regex = Regex::new(r"^[0-9]+(Mi|Gi|M|G)?$").map_err(|e| ApiError::Internal {
        message: format!("Failed to compile regex: {}", e),
    })?;

    if !memory_regex.is_match(memory) {
        return Err(ApiError::InvalidRequest {
            message: format!(
                "invalid memory format '{}' (expected format: '512Mi' or '2Gi')",
                memory
            ),
        });
    }

    Ok(())
}

pub fn validate_resources(resources: &ResourceRequirements) -> Result<()> {
    validate_cpu_resource(&resources.cpu)?;
    validate_memory_resource(&resources.memory)?;
    Ok(())
}

pub fn validate_create_deployment_request(
    req: &CreateDeploymentRequest,
    max_replicas: u32,
) -> Result<()> {
    validate_instance_name(&req.instance_name)?;
    validate_image(&req.image)?;
    validate_port(req.port)?;
    validate_replicas(req.replicas, max_replicas)?;

    if let Some(ref resources) = req.resources {
        validate_resources(resources)?;
    }

    for key in req.env.keys() {
        if key.is_empty() {
            return Err(ApiError::InvalidRequest {
                message: "environment variable name cannot be empty".to_string(),
            });
        }
        if key.len() > 255 {
            return Err(ApiError::InvalidRequest {
                message: format!("environment variable name too long: {}", key),
            });
        }
    }

    Ok(())
}

pub fn sanitize_user_id(user_id: &str) -> String {
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

pub fn generate_cr_name(instance_name: &str) -> String {
    format!("ud-{}", instance_name)
}

pub fn generate_path_prefix(instance_name: &str) -> String {
    format!("/deployments/{}", instance_name)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_validate_instance_name_valid() {
        assert!(validate_instance_name("my-app").is_ok());
        assert!(validate_instance_name("app123").is_ok());
        assert!(validate_instance_name("a").is_ok());
        assert!(validate_instance_name("my-app-123").is_ok());
    }

    #[test]
    fn test_validate_instance_name_empty() {
        let result = validate_instance_name("");
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("cannot be empty"));
    }

    #[test]
    fn test_validate_instance_name_too_long() {
        let long_name = "a".repeat(64);
        let result = validate_instance_name(&long_name);
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("too long"));
    }

    #[test]
    fn test_validate_instance_name_invalid_format() {
        assert!(validate_instance_name("My-App").is_err());
        assert!(validate_instance_name("my_app").is_err());
        assert!(validate_instance_name("-myapp").is_err());
        assert!(validate_instance_name("myapp-").is_err());
        assert!(validate_instance_name("my.app").is_err());
    }

    #[test]
    fn test_validate_image_valid() {
        assert!(validate_image("nginx:latest").is_ok());
        assert!(validate_image("gcr.io/project/image:tag").is_ok());
        assert!(validate_image("my-registry.com:5000/repo/image@sha256:abc").is_ok());
    }

    #[test]
    fn test_validate_image_empty() {
        assert!(validate_image("").is_err());
    }

    #[test]
    fn test_validate_image_too_long() {
        let long_image = format!("nginx:{}", "a".repeat(1020));
        assert!(validate_image(&long_image).is_err());
    }

    #[test]
    fn test_validate_image_invalid_characters() {
        assert!(validate_image("nginx;rm -rf /").is_err());
        assert!(validate_image("nginx&curl evil.com").is_err());
        assert!(validate_image("nginx|bash").is_err());
    }

    #[test]
    fn test_validate_port_valid() {
        assert!(validate_port(80).is_ok());
        assert!(validate_port(443).is_ok());
        assert!(validate_port(8080).is_ok());
        assert!(validate_port(65535).is_ok());
        assert!(validate_port(1).is_ok());
    }

    #[test]
    fn test_validate_port_invalid() {
        assert!(validate_port(0).is_err());
        assert!(validate_port(65536).is_err());
        assert!(validate_port(100000).is_err());
    }

    #[test]
    fn test_validate_replicas_valid() {
        assert!(validate_replicas(1, 10).is_ok());
        assert!(validate_replicas(5, 10).is_ok());
        assert!(validate_replicas(10, 10).is_ok());
    }

    #[test]
    fn test_validate_replicas_zero() {
        assert!(validate_replicas(0, 10).is_err());
    }

    #[test]
    fn test_validate_replicas_exceeds_max() {
        assert!(validate_replicas(11, 10).is_err());
        assert!(validate_replicas(100, 10).is_err());
    }

    #[test]
    fn test_validate_cpu_resource_valid() {
        assert!(validate_cpu_resource("500m").is_ok());
        assert!(validate_cpu_resource("1").is_ok());
        assert!(validate_cpu_resource("2").is_ok());
        assert!(validate_cpu_resource("1000m").is_ok());
    }

    #[test]
    fn test_validate_cpu_resource_invalid() {
        assert!(validate_cpu_resource("").is_err());
        assert!(validate_cpu_resource("1.5").is_err());
        assert!(validate_cpu_resource("500").is_ok());
        assert!(validate_cpu_resource("abc").is_err());
    }

    #[test]
    fn test_validate_memory_resource_valid() {
        assert!(validate_memory_resource("512Mi").is_ok());
        assert!(validate_memory_resource("2Gi").is_ok());
        assert!(validate_memory_resource("1024M").is_ok());
        assert!(validate_memory_resource("1G").is_ok());
    }

    #[test]
    fn test_validate_memory_resource_invalid() {
        assert!(validate_memory_resource("").is_err());
        assert!(validate_memory_resource("1.5Gi").is_err());
        assert!(validate_memory_resource("512").is_ok());
        assert!(validate_memory_resource("abc").is_err());
    }

    #[test]
    fn test_validate_resources() {
        let valid_resources = ResourceRequirements {
            cpu: "500m".to_string(),
            memory: "512Mi".to_string(),
        };
        assert!(validate_resources(&valid_resources).is_ok());

        let invalid_cpu = ResourceRequirements {
            cpu: "invalid".to_string(),
            memory: "512Mi".to_string(),
        };
        assert!(validate_resources(&invalid_cpu).is_err());

        let invalid_memory = ResourceRequirements {
            cpu: "500m".to_string(),
            memory: "invalid".to_string(),
        };
        assert!(validate_resources(&invalid_memory).is_err());
    }

    #[test]
    fn test_sanitize_user_id() {
        assert_eq!(sanitize_user_id("user123"), "user123");
        assert_eq!(sanitize_user_id("User123"), "user123");
        assert_eq!(sanitize_user_id("user_123"), "user-123");
        assert_eq!(sanitize_user_id("user@domain.com"), "user-domain-com");

        let long_id = "a".repeat(70);
        let sanitized = sanitize_user_id(&long_id);
        assert!(sanitized.len() <= 60);
    }

    #[test]
    fn test_generate_cr_name() {
        assert_eq!(generate_cr_name("my-app"), "ud-my-app");
        assert_eq!(generate_cr_name("test"), "ud-test");
    }

    #[test]
    fn test_generate_path_prefix() {
        assert_eq!(generate_path_prefix("my-app"), "/deployments/my-app");
        assert_eq!(generate_path_prefix("test"), "/deployments/test");
    }

    #[test]
    fn test_validate_create_deployment_request_valid() {
        let req = CreateDeploymentRequest {
            instance_name: "my-app".to_string(),
            image: "nginx:latest".to_string(),
            replicas: 2,
            port: 80,
            command: vec![],
            args: vec![],
            env: HashMap::new(),
            resources: Some(ResourceRequirements {
                cpu: "500m".to_string(),
                memory: "512Mi".to_string(),
            }),
            ttl_seconds: None,
        };
        assert!(validate_create_deployment_request(&req, 10).is_ok());
    }

    #[test]
    fn test_validate_create_deployment_request_invalid_instance_name() {
        let req = CreateDeploymentRequest {
            instance_name: "My-App".to_string(),
            image: "nginx:latest".to_string(),
            replicas: 2,
            port: 80,
            command: vec![],
            args: vec![],
            env: HashMap::new(),
            resources: None,
            ttl_seconds: None,
        };
        assert!(validate_create_deployment_request(&req, 10).is_err());
    }

    #[test]
    fn test_validate_create_deployment_request_env_vars() {
        let mut env = HashMap::new();
        env.insert("".to_string(), "value".to_string());

        let req = CreateDeploymentRequest {
            instance_name: "my-app".to_string(),
            image: "nginx:latest".to_string(),
            replicas: 1,
            port: 80,
            command: vec![],
            args: vec![],
            env,
            resources: None,
            ttl_seconds: None,
        };
        assert!(validate_create_deployment_request(&req, 10).is_err());
    }
}
