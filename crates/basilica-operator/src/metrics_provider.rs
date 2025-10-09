use async_trait::async_trait;
use k8s_openapi::api::core::v1::Pod;
use kube::core::{ApiResource, DynamicObject, GroupVersionKind};
use kube::{Api, Client};
use tracing::debug;

use crate::billing::RuntimeMetrics;

#[async_trait]
pub trait RuntimeMetricsProvider: Send + Sync {
    async fn fetch_pod_metrics(&self, namespace: &str, pod_name: &str) -> Option<RuntimeMetrics>;
}

#[derive(Default)]
pub struct NoopRuntimeMetricsProvider;

#[async_trait]
impl RuntimeMetricsProvider for NoopRuntimeMetricsProvider {
    async fn fetch_pod_metrics(&self, _namespace: &str, _pod_name: &str) -> Option<RuntimeMetrics> {
        None
    }
}

pub struct K8sMetricsProvider {
    client: Client,
}

impl K8sMetricsProvider {
    pub fn new(client: Client) -> Self {
        Self { client }
    }

    fn metrics_api(&self, ns: &str) -> Api<DynamicObject> {
        let gvk = GroupVersionKind::gvk("metrics.k8s.io", "v1beta1", "PodMetrics");
        let ar = ApiResource::from_gvk(&gvk);
        Api::namespaced_with(self.client.clone(), ns, &ar)
    }

    fn pod_api(&self, ns: &str) -> Api<Pod> {
        Api::namespaced(self.client.clone(), ns)
    }

    fn parse_memory_quantity(bytes_str: &str) -> Option<f64> {
        // Support Ki, Mi, Gi and raw bytes
        let s = bytes_str.trim();
        if let Some(num) = s.strip_suffix("Ki") {
            num.parse::<f64>()
                .ok()
                .map(|v| v * 1024.0 / 1024.0_f64.powi(3))
        } else if let Some(num) = s.strip_suffix("Mi") {
            num.parse::<f64>()
                .ok()
                .map(|v| v * 1024.0_f64.powi(2) / 1024.0_f64.powi(3))
        } else if let Some(num) = s.strip_suffix("Gi") {
            num.parse::<f64>().ok()
        } else if let Some(num) = s.strip_suffix("Ti") {
            num.parse::<f64>().ok().map(|v| v * 1024.0)
        } else {
            // Try raw bytes
            s.parse::<f64>().ok().map(|v| v / 1024.0_f64.powi(3))
        }
    }

    fn parse_pod_metrics_value(val: &serde_json::Value) -> Option<RuntimeMetrics> {
        let mut max_mem_gb: f64 = 0.0;
        let containers = val.get("containers")?.as_array()?;
        for c in containers {
            if let Some(usage) = c.get("usage") {
                if let Some(mem_str) = usage.get("memory").and_then(|v| v.as_str()) {
                    if let Some(mem_gb) = Self::parse_memory_quantity(mem_str) {
                        if mem_gb > max_mem_gb {
                            max_mem_gb = mem_gb;
                        }
                    }
                }
            }
        }
        Some(RuntimeMetrics {
            gpu_peak_utilization: None,
            memory_peak_gb: if max_mem_gb > 0.0 {
                Some(max_mem_gb)
            } else {
                None
            },
            bandwidth_gbps: None,
        })
    }

    fn parse_annotation_metrics(
        ann: &std::collections::BTreeMap<String, String>,
    ) -> (Option<f64>, Option<f64>) {
        let gpu = ann
            .get("basilica.ai/gpu-peak-utilization")
            .and_then(|s| s.parse::<f64>().ok());
        let bw = ann
            .get("basilica.ai/bandwidth-gbps")
            .and_then(|s| s.parse::<f64>().ok());
        (gpu, bw)
    }

    fn parse_prom_text_metrics(body: &str) -> (Option<f64>, Option<f64>) {
        // Accept either normalized (0..1) or percent for GPU, and gbps or mbps for bandwidth.
        // Known names:
        //  - basilica_gpu_peak_utilization (0..1)
        //  - basilica_gpu_utilization_percent (0..100)
        //  - basilica_bandwidth_gbps
        //  - basilica_network_bandwidth_mbps
        let mut gpu: Option<f64> = None;
        let mut bw_gbps: Option<f64> = None;
        for line in body.lines() {
            let l = line.trim();
            if l.is_empty() || l.starts_with('#') {
                continue;
            }
            // split on whitespace: metric value
            let mut parts = l.split_whitespace();
            if let (Some(name), Some(val)) = (parts.next(), parts.next()) {
                if gpu.is_none() {
                    match name {
                        "basilica_gpu_peak_utilization" => {
                            if let Ok(v) = val.parse::<f64>() {
                                if v.is_finite() {
                                    gpu = Some(v);
                                }
                            }
                        }
                        "basilica_gpu_utilization_percent" => {
                            if let Ok(v) = val.parse::<f64>() {
                                if v.is_finite() {
                                    gpu = Some((v / 100.0).clamp(0.0, 1.0));
                                }
                            }
                        }
                        _ => {}
                    }
                }
                if bw_gbps.is_none() {
                    match name {
                        "basilica_bandwidth_gbps" => {
                            if let Ok(v) = val.parse::<f64>() {
                                if v.is_finite() {
                                    bw_gbps = Some(v.max(0.0));
                                }
                            }
                        }
                        "basilica_network_bandwidth_mbps" => {
                            if let Ok(v) = val.parse::<f64>() {
                                if v.is_finite() {
                                    bw_gbps = Some((v / 1000.0).max(0.0));
                                }
                            }
                        }
                        _ => {}
                    }
                }
            }
        }
        (gpu, bw_gbps)
    }

    fn build_sidecar_metrics_url(pod: &Pod) -> Option<String> {
        // Prefer explicit URL via annotation
        if let Some(ann) = &pod.metadata.annotations {
            if let Some(url) = ann.get("basilica.ai/metrics-url").filter(|s| !s.is_empty()) {
                return Some(url.clone());
            }
        }
        // Else, construct from PodIP + optional port/path annotations
        let ip = pod.status.as_ref().and_then(|s| s.pod_ip.clone())?;
        let mut scheme = "http".to_string();
        let mut port = 2112_u16; // common metrics port
        let mut path = "/metrics".to_string();
        if let Some(ann) = &pod.metadata.annotations {
            if let Some(v) = ann
                .get("basilica.ai/metrics-scheme")
                .filter(|s| !s.is_empty())
            {
                scheme = v.clone();
            }
            if let Some(v) = ann
                .get("basilica.ai/metrics-port")
                .and_then(|s| s.parse::<u16>().ok())
            {
                port = v;
            }
            if let Some(v) = ann
                .get("basilica.ai/metrics-path")
                .filter(|s| !s.is_empty())
            {
                path = v.clone();
            }
        }
        Some(format!("{}://{}:{}{}", scheme, ip, port, path))
    }
}

#[async_trait]
impl RuntimeMetricsProvider for K8sMetricsProvider {
    async fn fetch_pod_metrics(&self, namespace: &str, pod_name: &str) -> Option<RuntimeMetrics> {
        // Try metrics.k8s.io for CPU/memory
        let metrics_api = self.metrics_api(namespace);
        let mem_metrics = match metrics_api.get(pod_name).await {
            Ok(obj) => {
                let val = serde_json::to_value(&obj.data).ok()?;
                Self::parse_pod_metrics_value(&val)
            }
            Err(_) => None,
        };

        // Fetch Pod once for annotations and potential sidecar scrape
        let pod_opt = (self.pod_api(namespace).get(pod_name).await).ok();

        // Try scraping sidecar metrics if configured
        let (scrape_gpu, scrape_bw) = if let Some(p) = &pod_opt {
            if let Some(url) = Self::build_sidecar_metrics_url(p) {
                match reqwest::get(&url).await {
                    Ok(resp) => {
                        if let Ok(text) = resp.text().await {
                            Self::parse_prom_text_metrics(&text)
                        } else {
                            (None, None)
                        }
                    }
                    Err(e) => {
                        debug!("sidecar metrics scrape failed: {}", e);
                        (None, None)
                    }
                }
            } else {
                (None, None)
            }
        } else {
            (None, None)
        };

        // Fallback to Pod annotations for GPU/bandwidth if available
        let (ann_gpu, ann_bw_gbps) = if let Some(pod) = &pod_opt {
            if let Some(ann) = &pod.metadata.annotations {
                Self::parse_annotation_metrics(ann)
            } else {
                (None, None)
            }
        } else {
            (None, None)
        };

        // Combine
        let gpu_u = scrape_gpu.or(ann_gpu);
        let bw_gbps = scrape_bw.or(ann_bw_gbps);
        if mem_metrics.is_none() && gpu_u.is_none() && bw_gbps.is_none() {
            return None;
        }
        let mut out = mem_metrics.unwrap_or_default();
        out.gpu_peak_utilization = out.gpu_peak_utilization.or(gpu_u);
        out.bandwidth_gbps = out.bandwidth_gbps.or(bw_gbps);
        Some(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_pod_metrics_memory() {
        let sample = serde_json::json!({
            "containers": [
                { "name": "c1", "usage": { "cpu": "50m", "memory": "256Mi" } },
                { "name": "c2", "usage": { "cpu": "100m", "memory": "1Gi" } }
            ]
        });
        let rm = K8sMetricsProvider::parse_pod_metrics_value(&sample).unwrap();
        assert_eq!(rm.memory_peak_gb.unwrap_or(0.0), 1.0);
        assert!(rm.gpu_peak_utilization.is_none());
    }

    #[test]
    fn parses_annotation_metrics() {
        let mut ann = std::collections::BTreeMap::new();
        ann.insert("basilica.ai/gpu-peak-utilization".into(), "0.91".into());
        ann.insert("basilica.ai/bandwidth-gbps".into(), "1.5".into());
        let (gpu, bw) = K8sMetricsProvider::parse_annotation_metrics(&ann);
        assert_eq!(gpu.unwrap(), 0.91);
        assert_eq!(bw.unwrap(), 1.5);
    }

    #[test]
    fn parses_prom_text_metrics_percent_and_mbps() {
        let sample = r#"
# HELP basilica_gpu_utilization_percent Current GPU utilization percent
# TYPE basilica_gpu_utilization_percent gauge
basilica_gpu_utilization_percent 75
# HELP basilica_network_bandwidth_mbps Network bandwidth
# TYPE basilica_network_bandwidth_mbps gauge
basilica_network_bandwidth_mbps 1500
"#;
        let (gpu, bw) = K8sMetricsProvider::parse_prom_text_metrics(sample);
        assert!((gpu.unwrap() - 0.75).abs() < 1e-9);
        assert!((bw.unwrap() - 1.5).abs() < 1e-9);
    }

    #[test]
    fn parses_prom_text_metrics_normalized_and_gbps() {
        let sample = r#"
# HELP basilica_gpu_peak_utilization Peak GPU utilization (0..1)
# TYPE basilica_gpu_peak_utilization gauge
basilica_gpu_peak_utilization 0.62
# HELP basilica_bandwidth_gbps Bandwidth in Gbps
# TYPE basilica_bandwidth_gbps gauge
basilica_bandwidth_gbps 2.25
"#;
        let (gpu, bw) = K8sMetricsProvider::parse_prom_text_metrics(sample);
        assert!((gpu.unwrap() - 0.62).abs() < 1e-9);
        assert!((bw.unwrap() - 2.25).abs() < 1e-9);
    }

    #[test]
    fn parses_prom_text_metrics_malformed_ignored() {
        let sample = r#"
basilica_gpu_utilization_percent not_a_number
basilica_network_bandwidth_mbps NaN
"#;
        let (gpu, bw) = K8sMetricsProvider::parse_prom_text_metrics(sample);
        assert!(gpu.is_none());
        assert!(bw.is_none());
    }
}
