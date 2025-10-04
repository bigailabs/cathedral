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
    async fn list_rentals(&self, ns: &str) -> Result<Vec<RentalListItemDto>>;
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

    async fn list_rentals(&self, ns: &str) -> Result<Vec<RentalListItemDto>> {
        let st = self.statuses.read().await;
        let mut out = Vec::new();
        if let Some(map) = st.get(ns) {
            for (name, s) in map.iter() {
                out.push(RentalListItemDto { rental_id: name.clone(), status: RentalStatusDto { state: s.phase.clone(), pod_name: s.pod_name.clone(), endpoints: vec![] } });
            }
        }
        Ok(out)
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

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RentalListItemDto {
    pub rental_id: String,
    pub status: RentalStatusDto,
}

// K8s client implementation using kube + dynamic CRDs
#[derive(Clone)]
pub struct K8sClient {
    client: kube::Client,
}

impl K8sClient {
    pub async fn try_default() -> Result<Self> {
        let client = kube::Client::try_default().await.map_err(|e| ApiError::Internal { message: format!("k8s client init failed: {e}") })?;
        Ok(Self { client })
    }

    fn cr_api(&self, ns: &str, group: &str, version: &str, kind: &str) -> kube::Api<kube::core::DynamicObject> {
        use kube::core::{ApiResource, GroupVersionKind};
        let gvk = GroupVersionKind::gvk(group, version, kind);
        let ar = ApiResource::from_gvk(&gvk);
        kube::Api::namespaced_with(self.client.clone(), ns, &ar)
    }

    async fn get_pod_by_label(&self, ns: &str, key: &str, value: &str) -> Result<Option<k8s_openapi::api::core::v1::Pod>> {
        use kube::api::{ListParams, Api};
        let pods: Api<k8s_openapi::api::core::v1::Pod> = Api::namespaced(self.client.clone(), ns);
        let lp = ListParams::default().labels(&format!("{}={}", key, value));
        let list = pods.list(&lp).await.map_err(|e| ApiError::Internal { message: format!("list pods failed: {e}") })?;
        Ok(list.items.into_iter().next())
    }
}

#[async_trait]
impl ApiK8sClient for K8sClient {
    async fn create_job(&self, ns: &str, name: &str, spec: JobSpecDto) -> Result<String> {
        use kube::api::PostParams;
        use serde_json::json;
        let api = self.cr_api(ns, "basilica.io", "v1", "BasilicaJob");
        let obj = json!({
            "apiVersion": "basilica.io/v1",
            "kind": "BasilicaJob",
            "metadata": {"name": name, "namespace": ns},
            "spec": {
                "image": spec.image,
                "command": spec.command,
                "args": spec.args,
                "env": spec.env,
                "resources": {"cpu": spec.resources.cpu, "memory": spec.resources.memory, "gpus": {"count": spec.resources.gpus.count, "model": spec.resources.gpus.model}},
                "ttlSeconds": spec.ttl_seconds,
                "priority": "normal"
            }
        });
        let dynobj: kube::core::DynamicObject = serde_json::from_value(obj).map_err(|e| ApiError::Internal { message: format!("serde dynobj: {e}") })?;
        let _ = api.create(&PostParams::default(), &dynobj).await.map_err(|e| ApiError::Internal { message: format!("create BasilicaJob failed: {e}") })?;
        Ok(name.to_string())
    }

    async fn get_job_status(&self, ns: &str, name: &str) -> Result<JobStatusDto> {
        use serde_json::Value;
        let api = self.cr_api(ns, "basilica.io", "v1", "BasilicaJob");
        let obj = api.get(name).await.map_err(|e| ApiError::NotFound { message: format!("job not found: {e}") })?;
        let val: Value = serde_json::to_value(&obj).map_err(|e| ApiError::Internal { message: format!("to_value: {e}") })?;
        let phase = val.get("status").and_then(|s| s.get("phase")).and_then(|v| v.as_str()).unwrap_or("Pending").to_string();
        let pod_name = val.get("status").and_then(|s| s.get("podName")).and_then(|v| v.as_str()).map(|s| s.to_string());
        Ok(JobStatusDto { phase, pod_name })
    }

    async fn delete_job(&self, ns: &str, name: &str) -> Result<()> {
        use kube::api::{DeleteParams};
        let api = self.cr_api(ns, "basilica.io", "v1", "BasilicaJob");
        let _ = api.delete(name, &DeleteParams::default()).await.map_err(|e| ApiError::Internal { message: format!("delete job failed: {e}") })?;
        Ok(())
    }

    async fn get_job_logs(&self, ns: &str, name: &str) -> Result<String> {
        use kube::api::{Api, LogParams};
        if let Some(pod) = self.get_pod_by_label(ns, "basilica.io/job", name).await? {
            let pods: Api<k8s_openapi::api::core::v1::Pod> = Api::namespaced(self.client.clone(), ns);
            let lp = LogParams { container: None, follow: false, ..Default::default() };
            let pod_name = pod.metadata.name.unwrap_or_default();
            let logs = pods.logs(&pod_name, &lp).await.map_err(|e| ApiError::Internal { message: format!("get logs failed: {e}") })?;
            Ok(logs)
        } else {
            Err(ApiError::NotFound { message: "pod not found".into() })
        }
    }

    async fn create_rental(&self, ns: &str, name: &str, spec: RentalSpecDto) -> Result<String> {
        use kube::api::PostParams;
        use serde_json::json;
        let api = self.cr_api(ns, "basilica.io", "v1", "GpuRental");
        let obj = json!({
            "apiVersion": "basilica.io/v1",
            "kind": "GpuRental",
            "metadata": {"name": name, "namespace": ns},
            "spec": {
                "container": {
                    "image": spec.container_image,
                    "env": [],
                    "command": [],
                    "ports": [],
                    "volumes": [],
                    "resources": {"cpu": spec.resources.cpu, "memory": spec.resources.memory, "gpus": {"count": spec.resources.gpus.count, "model": spec.resources.gpus.model}},
                },
                "duration": {"hours": 0, "autoExtend": false, "maxExtensions": 0},
                "accessType": "Ssh",
                "network": {"ingress": [], "egressPolicy": "restricted", "allowedEgress": [], "publicIpRequired": false },
                "ttlSeconds": 0
            }
        });
        let dynobj: kube::core::DynamicObject = serde_json::from_value(obj).map_err(|e| ApiError::Internal { message: format!("serde dynobj: {e}") })?;
        let _ = api.create(&PostParams::default(), &dynobj).await.map_err(|e| ApiError::Internal { message: format!("create GpuRental failed: {e}") })?;
        Ok(name.to_string())
    }

    async fn get_rental_status(&self, ns: &str, name: &str) -> Result<RentalStatusDto> {
        use serde_json::Value;
        let api = self.cr_api(ns, "basilica.io", "v1", "GpuRental");
        let obj = api.get(name).await.map_err(|e| ApiError::NotFound { message: format!("rental not found: {e}") })?;
        let val: Value = serde_json::to_value(&obj).map_err(|e| ApiError::Internal { message: format!("to_value: {e}") })?;
        let state = val.get("status").and_then(|s| s.get("state")).and_then(|v| v.as_str()).unwrap_or("Provisioning").to_string();
        let pod_name = val.get("status").and_then(|s| s.get("podName")).and_then(|v| v.as_str()).map(|s| s.to_string());
        Ok(RentalStatusDto { state, pod_name, endpoints: vec![] })
    }

    async fn delete_rental(&self, ns: &str, name: &str) -> Result<()> {
        use kube::api::DeleteParams;
        let api = self.cr_api(ns, "basilica.io", "v1", "GpuRental");
        let _ = api.delete(name, &DeleteParams::default()).await.map_err(|e| ApiError::Internal { message: format!("delete rental failed: {e}") })?;
        Ok(())
    }

    async fn get_rental_logs(&self, ns: &str, name: &str) -> Result<String> {
        use kube::api::{Api, LogParams};
        if let Some(pod) = self.get_pod_by_label(ns, "basilica.io/rental", name).await? {
            let pods: Api<k8s_openapi::api::core::v1::Pod> = Api::namespaced(self.client.clone(), ns);
            let lp = LogParams { container: None, follow: false, ..Default::default() };
            let pod_name = pod.metadata.name.unwrap_or_default();
            let logs = pods.logs(&pod_name, &lp).await.map_err(|e| ApiError::Internal { message: format!("get logs failed: {e}") })?;
            Ok(logs)
        } else {
            Err(ApiError::NotFound { message: "pod not found".into() })
        }
    }

    async fn exec_rental(&self, ns: &str, name: &str, command: Vec<String>) -> Result<String> {
        use kube::api::{Api, AttachParams};
        use tokio::io::AsyncReadExt;
        // Find the first pod for the rental
        let pod = self
            .get_pod_by_label(ns, "basilica.io/rental", name)
            .await?
            .ok_or_else(|| ApiError::NotFound { message: "pod not found".into() })?;
        let pod_name = pod.metadata.name.clone().unwrap_or_default();
        let pods: Api<k8s_openapi::api::core::v1::Pod> = Api::namespaced(self.client.clone(), ns);
        let mut params = AttachParams::default();
        params.stdout = true;
        params.stderr = true;
        params.stdin = false;
        params.tty = false;
        // kube expects &str slice for args
        let args: Vec<&str> = command.iter().map(|s| s.as_str()).collect();
        let mut attached = pods
            .exec(&pod_name, args, &params)
            .await
            .map_err(|e| ApiError::Internal { message: format!("exec failed: {e}") })?;

        let mut stdout_buf = Vec::new();
        if let Some(mut out) = attached.stdout().take() {
            out.read_to_end(&mut stdout_buf)
                .await
                .map_err(|e| ApiError::Internal { message: format!("read stdout failed: {e}") })?;
        }
        let mut stderr_buf = Vec::new();
        if let Some(mut err) = attached.stderr().take() {
            err.read_to_end(&mut stderr_buf)
                .await
                .map_err(|e| ApiError::Internal { message: format!("read stderr failed: {e}") })?;
        }
        // Best-effort wait for remote to complete
        let _ = attached.join().await;

        // Return combined output for now (route maps to structured fields)
        let mut out = String::new();
        if !stdout_buf.is_empty() {
            out.push_str(&String::from_utf8_lossy(&stdout_buf));
        }
        if !stderr_buf.is_empty() {
            if !out.is_empty() { out.push('\n'); }
            out.push_str(&String::from_utf8_lossy(&stderr_buf));
        }
        Ok(out)
    }

    async fn extend_rental(&self, ns: &str, name: &str, _additional_hours: u32) -> Result<RentalStatusDto> {
        // For now, return current status (operator handles auto-extend)
        self.get_rental_status(ns, name).await
    }

    async fn list_rentals(&self, ns: &str) -> Result<Vec<RentalListItemDto>> {
        use serde_json::Value;
        use kube::api::ListParams;
        let api = self.cr_api(ns, "basilica.io", "v1", "GpuRental");
        let list = api.list(&ListParams::default()).await.map_err(|e| ApiError::Internal { message: format!("list rentals failed: {e}") })?;
        let mut out = Vec::new();
        for item in list.items {
            let name = item.metadata.name.clone().unwrap_or_default();
            let val: Value = serde_json::to_value(&item).map_err(|e| ApiError::Internal { message: format!("to_value: {e}") })?;
            let state = val.get("status").and_then(|s| s.get("state")).and_then(|v| v.as_str()).unwrap_or("Provisioning").to_string();
            let pod_name = val.get("status").and_then(|s| s.get("podName")).and_then(|v| v.as_str()).map(|s| s.to_string());
            out.push(RentalListItemDto { rental_id: name, status: RentalStatusDto { state, pod_name, endpoints: vec![] } });
        }
        Ok(out)
    }
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
