use anyhow::Result;
use async_trait::async_trait;

use crate::crd::gpu_rental::{GpuRental, GpuRentalStatus};
use kube::ResourceExt;
use crate::crd::basilica_job::{BasilicaJob, BasilicaJobStatus};

/// BillingClient for pay-as-you-go: no extension concept.
#[async_trait]
pub trait BillingClient: Send + Sync {
    /// Returns true if the rental should be terminated due to insufficient credits/balance.
    async fn should_terminate(&self, _rental: &GpuRental, _status: &GpuRentalStatus) -> Result<bool>;

    /// Emit a usage/lifecycle event (best-effort)
    async fn emit_usage_event(&self, _rental: &GpuRental, _status: &GpuRentalStatus) -> Result<()> {
        Ok(())
    }

    /// Emit a job lifecycle/usage event (best-effort)
    async fn emit_job_event(&self, _job: &BasilicaJob, _status: &BasilicaJobStatus) -> Result<()> {
        Ok(())
    }
}

#[derive(Default, Clone)]
pub struct MockBillingClient {
    /// Map of rental name -> should_terminate flag
    pub terminate: std::sync::Arc<tokio::sync::RwLock<std::collections::HashMap<String, bool>>>,
    pub events: std::sync::Arc<tokio::sync::RwLock<Vec<(String, String)>>>,
}

#[async_trait]
impl BillingClient for MockBillingClient {
    async fn should_terminate(&self, rental: &GpuRental, _status: &GpuRentalStatus) -> Result<bool> {
        let t = self.terminate.read().await;
        Ok(t.get(&rental.name_any()).cloned().unwrap_or(false))
    }
    async fn emit_usage_event(&self, rental: &GpuRental, status: &GpuRentalStatus) -> Result<()> {
        let mut ev = self.events.write().await;
        ev.push((rental.name_any(), status.state.clone().unwrap_or_default()));
        Ok(())
    }

    async fn emit_job_event(&self, job: &BasilicaJob, status: &BasilicaJobStatus) -> Result<()> {
        let mut ev = self.events.write().await;
        ev.push((job.name_any(), status.phase.clone().unwrap_or_default()));
        Ok(())
    }
}

#[derive(Clone)]
pub struct HttpBillingClient {
    pub base_url: String,
    pub http: reqwest::Client,
}

impl HttpBillingClient {
    pub fn new<S: Into<String>>(base_url: S) -> Self {
        let http = reqwest::Client::builder().build().expect("http client");
        Self { base_url: base_url.into(), http }
    }
}

#[async_trait]
impl BillingClient for HttpBillingClient {
    async fn should_terminate(&self, rental: &GpuRental, _status: &GpuRentalStatus) -> Result<bool> {
        // Require tenancy.user_id; without it, do not terminate.
        let user_id = rental
            .spec
            .tenancy
            .as_ref()
            .map(|t| t.user_id.clone())
            .unwrap_or_default();
        if user_id.is_empty() {
            return Ok(false);
        }
        let url = format!("{}/credits/{}/balance", self.base_url.trim_end_matches('/'), user_id);
        let resp = self.http.get(url).send().await?;
        if !resp.status().is_success() {
            return Ok(false);
        }
        let v: serde_json::Value = resp.json().await?;
        // Expect shape: { "balance": <number> }
        let bal = v
            .get("balance")
            .and_then(|b| b.as_f64())
            .unwrap_or(0.0);
        Ok(bal <= 0.0)
    }

    async fn emit_usage_event(&self, rental: &GpuRental, status: &GpuRentalStatus) -> Result<()> {
        #[derive(serde::Serialize)]
        struct UsagePayload<'a> {
            event_type: &'static str,
            rental_id: &'a str,
            user_id: Option<&'a str>,
            state: Option<&'a str>,
            pod_name: Option<&'a str>,
            start_time: Option<&'a str>,
            expiry_time: Option<&'a str>,
            cpu: &'a str,
            memory: &'a str,
            gpu_model: Option<&'a str>,
            gpu_count: u32,
            bandwidth_mbps: Option<u32>,
            duration_seconds: Option<i64>,
        }
        let name = rental.name_any();
        let gpu_model = rental.spec.container.resources.gpus.model.get(0).map(|s| s.as_str());
        let duration_seconds = match (&status.start_time, &status.expiry_time) {
            (Some(s), Some(e)) => {
                let s = k8s_openapi::chrono::DateTime::parse_from_rfc3339(s).ok().map(|dt| dt.with_timezone(&k8s_openapi::chrono::Utc));
                let e = k8s_openapi::chrono::DateTime::parse_from_rfc3339(e).ok().map(|dt| dt.with_timezone(&k8s_openapi::chrono::Utc));
                match (s, e) { (Some(s), Some(e)) => Some((e - s).num_seconds()), _ => None }
            }
            _ => None,
        };
        let payload = UsagePayload {
            event_type: "rental",
            rental_id: &name,
            user_id: rental.spec.tenancy.as_ref().map(|t| t.user_id.as_str()),
            state: status.state.as_deref(),
            pod_name: status.pod_name.as_deref(),
            start_time: status.start_time.as_deref(),
            expiry_time: status.expiry_time.as_deref(),
            cpu: &rental.spec.container.resources.cpu,
            memory: &rental.spec.container.resources.memory,
            gpu_model,
            gpu_count: rental.spec.container.resources.gpus.count,
            bandwidth_mbps: rental.spec.network.bandwidth_mbps,
            duration_seconds,
        };
        let url = format!("{}/events/usage", self.base_url.trim_end_matches('/'));
        let _ = self.http.post(url).json(&payload).send().await?;
        Ok(())
    }

    async fn emit_job_event(&self, job: &BasilicaJob, status: &BasilicaJobStatus) -> Result<()> {
        #[derive(serde::Serialize)]
        struct JobPayload<'a> {
            event_type: &'static str,
            job_id: &'a str,
            namespace: String,
            phase: Option<&'a str>,
            start_time: Option<&'a str>,
            completion_time: Option<&'a str>,
            cpu: &'a str,
            memory: &'a str,
            gpu_model: Option<&'a str>,
            gpu_count: u32,
        }
        let gpu_model = job.spec.resources.gpus.model.get(0).map(|s| s.as_str());
        let payload = JobPayload {
            event_type: "job",
            job_id: &job.name_any(),
            namespace: job.namespace().unwrap_or_else(|| "default".into()),
            phase: status.phase.as_deref(),
            start_time: status.start_time.as_deref(),
            completion_time: status.completion_time.as_deref(),
            cpu: &job.spec.resources.cpu,
            memory: &job.spec.resources.memory,
            gpu_model,
            gpu_count: job.spec.resources.gpus.count,
        };
        let url = format!("{}/events/usage", self.base_url.trim_end_matches('/'));
        let _ = self.http.post(url).json(&payload).send().await?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{routing::{get, post}, Router, Json};
    use std::net::SocketAddr;
    use tokio::task::JoinHandle;

    async fn spawn_test_server() -> (String, JoinHandle<()>, std::sync::Arc<tokio::sync::RwLock<Vec<serde_json::Value>>>) {
        let events = std::sync::Arc::new(tokio::sync::RwLock::new(Vec::new()));
        let events_clone = events.clone();
        let app = Router::new()
            .route("/credits/:user/balance", get(|axum::extract::Path(user): axum::extract::Path<String>| async move {
                let bal = if user == "zero" { 0.0 } else { 12.34 };
                Json(serde_json::json!({"balance": bal}))
            }))
            .route("/events/usage", post({
                move |body: axum::Json<serde_json::Value>| {
                    let events = events_clone.clone();
                    async move {
                        events.write().await.push(body.0);
                        Json(serde_json::json!({"ok": true}))
                    }
                }
            }));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr: SocketAddr = listener.local_addr().unwrap();
        let handle = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        (format!("http://{}", addr), handle, events)
    }

    fn sample_rental(user_id: &str) -> GpuRental {
        use crate::crd::gpu_rental::{AccessType, GpuRentalSpec, RentalContainer, RentalDuration, RentalNetwork, Resources, GpuSpec, TenancyRef};
        GpuRental::new("r1", GpuRentalSpec {
            container: RentalContainer { image: "img".into(), env: vec![], command: vec![], ports: vec![], volumes: vec![], resources: Resources { cpu: "1".into(), memory: "1Gi".into(), gpus: GpuSpec { count: 0, model: vec![] } } },
            duration: RentalDuration { hours: 1, auto_extend: false, max_extensions: 0 },
            access_type: AccessType::Ssh,
            network: RentalNetwork::default(),
            storage: None,
            artifacts: None,
            ssh: None,
            jupyter_access: None,
            environment: None,
            miner_selector: None,
            billing: None,
            ttl_seconds: 0,
            tenancy: Some(TenancyRef { user_id: user_id.into(), project_id: "p1".into() }),
            exclusive: false,
        })
    }

    #[tokio::test]
    async fn http_billing_should_terminate_when_zero_balance() {
        let (base, _h, _ev) = spawn_test_server().await;
        let client = HttpBillingClient::new(base);
        let rental = sample_rental("zero");
        let st = GpuRentalStatus::default();
        let term = client.should_terminate(&rental, &st).await.unwrap();
        assert!(term);
    }

    #[tokio::test]
    async fn http_billing_emit_usage_event() {
        let (base, _h, events) = spawn_test_server().await;
        let client = HttpBillingClient::new(base);
        let rental = sample_rental("u1");
        let mut st = GpuRentalStatus::default();
        st.state = Some("Active".into());
        client.emit_usage_event(&rental, &st).await.unwrap();
        let stored = events.read().await;
        assert!(!stored.is_empty());
        let first = &stored[0];
        assert_eq!(first.get("rental_id").and_then(|v| v.as_str()).unwrap(), "r1");
        assert_eq!(first.get("user_id").and_then(|v| v.as_str()).unwrap(), "u1");
        assert_eq!(first.get("cpu").and_then(|v| v.as_str()).unwrap(), "1");
        assert_eq!(first.get("memory").and_then(|v| v.as_str()).unwrap(), "1Gi");
        assert_eq!(first.get("gpu_count").and_then(|v| v.as_u64()).unwrap(), 0);
    }

    #[tokio::test]
    async fn http_billing_emit_job_event() {
        let (base, _h, events) = spawn_test_server().await;
        let client = HttpBillingClient::new(base);
        use crate::crd::basilica_job::{BasilicaJob, BasilicaJobSpec, Resources, GpuSpec};
        let job = BasilicaJob::new("job1", BasilicaJobSpec {
            image: "img".into(), command: vec![], args: vec![], env: vec![],
            resources: Resources { cpu: "2".into(), memory: "2Gi".into(), gpus: GpuSpec { count: 1, model: vec!["A100".into()] } },
            storage: None, artifacts: None, ttl_seconds: 0, priority: "normal".into(),
        });
        let mut st = BasilicaJobStatus::default();
        st.phase = Some("Running".into());
        client.emit_job_event(&job, &st).await.unwrap();
        let stored = events.read().await;
        assert!(!stored.is_empty());
        let last = stored.last().unwrap();
        assert_eq!(last.get("job_id").and_then(|v| v.as_str()).unwrap(), "job1");
        assert_eq!(last.get("gpu_count").and_then(|v| v.as_u64()).unwrap(), 1);
    }
}
