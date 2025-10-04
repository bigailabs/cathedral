use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::RwLock;

use crate::error::{ApiError, Result};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct GpuSpec {
    pub count: u32,
    #[serde(default)]
    pub model: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Resources {
    pub cpu: String,
    pub memory: String,
    pub gpus: GpuSpec,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct JobSpecDto {
    pub image: String,
    #[serde(default)]
    pub command: Vec<String>,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: Vec<(String, String)>,
    pub resources: Resources,
    #[serde(default)]
    pub ttl_seconds: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct JobStatusDto {
    pub phase: String,
    pub pod_name: Option<String>,
}

#[async_trait]
pub trait ApiK8sClient {
    async fn create_job(&self, ns: &str, name: &str, spec: JobSpecDto) -> Result<String>;
    async fn get_job_status(&self, ns: &str, name: &str) -> Result<JobStatusDto>;
    async fn delete_job(&self, ns: &str, name: &str) -> Result<()>;
    async fn get_job_logs(&self, ns: &str, name: &str) -> Result<String>;

    // Rentals (GpuRental) API
    async fn create_rental(&self, ns: &str, name: &str, spec: RentalSpecDto) -> Result<String>;
    async fn get_rental_status(&self, ns: &str, name: &str) -> Result<RentalStatusDto>;
    async fn delete_rental(&self, ns: &str, name: &str) -> Result<()>;
    async fn get_rental_logs(&self, ns: &str, name: &str) -> Result<String>;
    async fn exec_rental(&self, ns: &str, name: &str, command: Vec<String>) -> Result<String>;
    async fn extend_rental(&self, ns: &str, name: &str, additional_hours: u32) -> Result<RentalStatusDto>;
}

#[derive(Default, Clone)]
pub struct MockK8sClient {
    // ns -> name -> spec/status/logs
    specs: Arc<RwLock<HashMap<String, HashMap<String, JobSpecDto>>>>,
    statuses: Arc<RwLock<HashMap<String, HashMap<String, JobStatusDto>>>>,
    logs: Arc<RwLock<HashMap<String, HashMap<String, String>>>>,
}

#[async_trait]
impl ApiK8sClient for MockK8sClient {
    async fn create_job(&self, ns: &str, name: &str, spec: JobSpecDto) -> Result<String> {
        let mut s = self.specs.write().await;
        s.entry(ns.to_string()).or_default().insert(name.to_string(), spec);
        // default status pending
        let mut st = self.statuses.write().await;
        st.entry(ns.to_string()).or_default().insert(name.to_string(), JobStatusDto { phase: "Pending".into(), pod_name: None });
        Ok(name.to_string())
    }

    async fn get_job_status(&self, ns: &str, name: &str) -> Result<JobStatusDto> {
        let st = self.statuses.read().await;
        st.get(ns)
            .and_then(|m| m.get(name))
            .cloned()
            .ok_or_else(|| ApiError::NotFound { message: "job not found".into() })
    }

    async fn delete_job(&self, ns: &str, name: &str) -> Result<()> {
        let mut s = self.specs.write().await;
        s.get_mut(ns).and_then(|m| m.remove(name));
        let mut st = self.statuses.write().await;
        st.get_mut(ns).and_then(|m| m.remove(name));
        Ok(())
    }

    async fn get_job_logs(&self, ns: &str, name: &str) -> Result<String> {
        let l = self.logs.read().await;
        Ok(l.get(ns).and_then(|m| m.get(name)).cloned().unwrap_or_else(|| "".into()))
    }

    async fn create_rental(&self, ns: &str, name: &str, spec: RentalSpecDto) -> Result<String> {
        // Reuse job stores for simplicity
        let mut s = self.specs.write().await;
        s.entry(ns.to_string()).or_default().insert(name.to_string(), JobSpecDto {
            image: spec.container_image,
            command: vec![], args: vec![], env: vec![], resources: spec.resources, ttl_seconds: 0,
        });
        let mut st = self.statuses.write().await;
        st.entry(ns.to_string()).or_default().insert(name.to_string(), JobStatusDto { phase: "Provisioning".into(), pod_name: None });
        Ok(name.to_string())
    }

    async fn get_rental_status(&self, ns: &str, name: &str) -> Result<RentalStatusDto> {
        let st = self.statuses.read().await;
        let job_st = st.get(ns).and_then(|m| m.get(name)).cloned().ok_or_else(|| ApiError::NotFound { message: "rental not found".into() })?;
        Ok(RentalStatusDto { state: job_st.phase, pod_name: job_st.pod_name, endpoints: vec![] })
    }

    async fn delete_rental(&self, ns: &str, name: &str) -> Result<()> {
        self.delete_job(ns, name).await
    }

    async fn get_rental_logs(&self, ns: &str, name: &str) -> Result<String> {
        self.get_job_logs(ns, name).await
    }

    async fn exec_rental(&self, _ns: &str, _name: &str, command: Vec<String>) -> Result<String> {
        Ok(format!("exec: {}", command.join(" ")))
    }

    async fn extend_rental(&self, ns: &str, name: &str, _additional_hours: u32) -> Result<RentalStatusDto> {
        self.get_rental_status(ns, name).await
    }
}

// Rentals DTOs (simplified)
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RentalSpecDto {
    pub container_image: String,
    pub resources: Resources,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub namespace: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RentalStatusDto {
    pub state: String,
    pub pod_name: Option<String>,
    #[serde(default)]
    pub endpoints: Vec<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn mock_k8s_create_get_delete() {
        let c = MockK8sClient::default();
        let name = c
            .create_job(
                "ns",
                "job1",
                JobSpecDto {
                    image: "img".into(),
                    command: vec![],
                    args: vec![],
                    env: vec![],
                    resources: Resources { cpu: "1".into(), memory: "512Mi".into(), gpus: GpuSpec { count: 0, model: vec![] } },
                    ttl_seconds: 0,
                },
            )
            .await
            .unwrap();
        assert_eq!(name, "job1");
        let st = c.get_job_status("ns", "job1").await.unwrap();
        assert_eq!(st.phase, "Pending");
        c.delete_job("ns", "job1").await.unwrap();
        assert!(matches!(c.get_job_status("ns", "job1").await, Err(ApiError::NotFound { message: _ })));
    }
}
