//! Jobs API client methods
//!
//! Provides methods for managing stateful jobs on the Basilica platform.
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// Resources specification for a job
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JobResources {
    /// CPU requirement (e.g., "1", "2", "4")
    pub cpu: String,

    /// Memory requirement (e.g., "512Mi", "1Gi", "8Gi")
    pub memory: String,

    /// GPU requirements
    pub gpus: JobGpuRequirements,
}

/// GPU requirements for a job
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JobGpuRequirements {
    /// Number of GPUs required
    pub count: u32,

    /// Specific GPU models (e.g., ["H100", "A100"])
    #[serde(default)]
    pub model: Vec<String>,
}

/// Port specification for a job
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JobPortSpec {
    /// Container port to expose
    #[serde(rename = "containerPort")]
    pub container_port: u16,

    /// Protocol (TCP or UDP)
    #[serde(default = "default_tcp")]
    pub protocol: String,
}

fn default_tcp() -> String {
    "TCP".to_string()
}

/// Storage configuration for a job
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JobStorageConfig {
    /// Storage backend (s3, r2, gcs)
    pub backend: String,

    /// Bucket name
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bucket: Option<String>,

    /// Optional prefix within bucket
    #[serde(skip_serializing_if = "Option::is_none")]
    pub prefix: Option<String>,

    /// Backend-specific credentials
    #[serde(skip_serializing_if = "Option::is_none")]
    pub credentials: Option<HashMap<String, String>>,
}

impl JobStorageConfig {
    /// Create R2 storage configuration
    pub fn r2(account_id: &str, access_key: &str, secret_key: &str, bucket: &str) -> Self {
        let mut credentials = HashMap::new();
        credentials.insert("access_key_id".to_string(), access_key.to_string());
        credentials.insert("secret_access_key".to_string(), secret_key.to_string());
        credentials.insert(
            "endpoint".to_string(),
            format!("https://{}.r2.cloudflarestorage.com", account_id),
        );

        Self {
            backend: "r2".to_string(),
            bucket: Some(bucket.to_string()),
            prefix: None,
            credentials: Some(credentials),
        }
    }

    /// Create S3 storage configuration
    pub fn s3(region: &str, access_key: &str, secret_key: &str, bucket: &str) -> Self {
        let mut credentials = HashMap::new();
        credentials.insert("access_key_id".to_string(), access_key.to_string());
        credentials.insert("secret_access_key".to_string(), secret_key.to_string());
        credentials.insert("region".to_string(), region.to_string());

        Self {
            backend: "s3".to_string(),
            bucket: Some(bucket.to_string()),
            prefix: None,
            credentials: Some(credentials),
        }
    }

    /// Create GCS storage configuration
    pub fn gcs(service_account_key: &str, bucket: &str) -> Self {
        let mut credentials = HashMap::new();
        credentials.insert(
            "service_account_key".to_string(),
            service_account_key.to_string(),
        );

        Self {
            backend: "gcs".to_string(),
            bucket: Some(bucket.to_string()),
            prefix: None,
            credentials: Some(credentials),
        }
    }

    /// Set the prefix for this storage configuration
    pub fn with_prefix(mut self, prefix: &str) -> Self {
        self.prefix = Some(prefix.to_string());
        self
    }
}

/// Request to create a new job
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CreateJobRequest {
    /// Container image to run
    pub image: String,

    /// Optional command to run
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub command: Vec<String>,

    /// Optional command arguments
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub args: Vec<String>,

    /// Environment variables
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub env: Vec<(String, String)>,

    /// Resource requirements
    pub resources: JobResources,

    /// TTL in seconds (0 = no TTL)
    #[serde(default)]
    pub ttl_seconds: u32,

    /// Optional job name
    #[serde(skip_serializing_if = "Option::is_none")]
    pub name: Option<String>,

    /// Optional namespace
    #[serde(skip_serializing_if = "Option::is_none")]
    pub namespace: Option<String>,

    /// Port mappings
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub ports: Vec<JobPortSpec>,

    /// Storage configuration for stateful jobs
    #[serde(skip_serializing_if = "Option::is_none")]
    pub storage: Option<JobStorageConfig>,
}

/// Response after creating a job
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CreateJobResponse {
    /// The created job ID
    pub job_id: String,
}

/// Job status information
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JobStatus {
    /// Current phase of the job (Pending, Running, Succeeded, Failed, Suspended)
    pub phase: String,

    /// Optional message
    #[serde(skip_serializing_if = "Option::is_none")]
    pub message: Option<String>,

    /// Optional reason
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,

    /// Network endpoints (e.g., for exposed ports)
    #[serde(default)]
    pub endpoints: Vec<String>,

    /// Start time
    #[serde(skip_serializing_if = "Option::is_none")]
    pub start_time: Option<String>,

    /// Completion time
    #[serde(skip_serializing_if = "Option::is_none")]
    pub completion_time: Option<String>,
}

/// Response for job status query
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JobStatusResponse {
    /// Job ID
    pub job_id: String,

    /// Job status
    pub status: JobStatus,
}

/// Response after deleting a job
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DeleteJobResponse {
    /// The deleted job ID
    pub job_id: String,
}

/// Response for job logs query
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JobLogsResponse {
    /// Job ID
    pub job_id: String,

    /// Log contents
    pub logs: String,
}

/// Request to read a file from a job's container
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReadFileRequest {
    /// Path to the file to read
    pub file_path: String,
}

/// Response containing file contents
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReadFileResponse {
    /// File contents as string
    pub content: String,
}

/// Response after suspending a job
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SuspendJobResponse {
    /// The suspended job ID
    pub job_id: String,
}

/// Response after resuming a job
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResumeJobResponse {
    /// The resumed job ID
    pub job_id: String,
}
