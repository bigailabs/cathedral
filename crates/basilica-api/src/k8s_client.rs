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
pub struct PortSpec {
    #[serde(alias = "containerPort")]
    pub container_port: u16,
    #[serde(default = "default_protocol")]
    pub protocol: String,
}

fn default_protocol() -> String {
    "TCP".to_string()
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct StorageConfig {
    pub backend: String, // "s3", "gcs", "r2", etc.
    #[serde(default)]
    pub bucket: Option<String>,
    #[serde(default)]
    pub prefix: Option<String>,
    #[serde(default)]
    pub credentials: Option<std::collections::HashMap<String, String>>,
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
    #[serde(default)]
    pub ports: Vec<PortSpec>,
    #[serde(default)]
    pub storage: Option<StorageConfig>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct JobStatusDto {
    pub phase: String,
    pub pod_name: Option<String>,
    #[serde(default)]
    pub endpoints: Vec<String>,
}

#[async_trait]
pub trait ApiK8sClient {
    fn kube_client(&self) -> kube::Client;

    async fn create_job(&self, ns: &str, name: &str, spec: JobSpecDto) -> Result<String>;
    async fn get_job_status(&self, ns: &str, name: &str) -> Result<JobStatusDto>;
    async fn delete_job(&self, ns: &str, name: &str) -> Result<()>;
    async fn get_job_logs(&self, ns: &str, name: &str) -> Result<String>;
    async fn exec_job(
        &self,
        ns: &str,
        name: &str,
        command: Vec<String>,
        stdin: Option<String>,
        tty: bool,
    ) -> Result<ExecResultDto>;
    async fn suspend_job(&self, ns: &str, name: &str) -> Result<()>;
    async fn resume_job(&self, ns: &str, name: &str) -> Result<()>;

    // Rentals (GpuRental) API
    async fn create_rental(&self, ns: &str, name: &str, spec: RentalSpecDto) -> Result<String>;
    async fn get_rental_status(&self, ns: &str, name: &str) -> Result<RentalStatusDto>;
    async fn delete_rental(&self, ns: &str, name: &str) -> Result<()>;
    async fn get_rental_logs(
        &self,
        ns: &str,
        name: &str,
        tail: Option<u32>,
        since_seconds: Option<u32>,
    ) -> Result<String>;
    async fn exec_rental(
        &self,
        ns: &str,
        name: &str,
        command: Vec<String>,
        stdin: Option<String>,
        tty: bool,
    ) -> Result<ExecResultDto>;
    async fn extend_rental(
        &self,
        ns: &str,
        name: &str,
        additional_hours: u32,
    ) -> Result<RentalStatusDto>;
    async fn list_rentals(&self, ns: &str) -> Result<Vec<RentalListItemDto>>;

    // Namespace management
    async fn create_namespace(&self, name: &str) -> Result<()>;
    async fn get_namespace(&self, name: &str) -> Result<()>;

    // ConfigMap management
    async fn get_configmap(
        &self,
        ns: &str,
        name: &str,
    ) -> Result<std::collections::BTreeMap<String, String>>;
    async fn patch_configmap(
        &self,
        ns: &str,
        name: &str,
        data: std::collections::BTreeMap<String, String>,
    ) -> Result<()>;

    // Deployment management
    async fn restart_deployment(&self, ns: &str, name: &str) -> Result<()>;
    async fn list_pods(&self, ns: &str, label_selector: &str) -> Result<Vec<String>>;

    // User Deployment management
    async fn create_user_deployment(
        &self,
        ns: &str,
        name: &str,
        user_id: &str,
        instance_name: &str,
        req: &crate::api::routes::deployments::types::CreateDeploymentRequest,
        path_prefix: &str,
    ) -> Result<()>;
    async fn delete_user_deployment(&self, ns: &str, name: &str) -> Result<()>;
    async fn delete_deployment(&self, ns: &str, name: &str) -> Result<()>;
    async fn delete_service(&self, ns: &str, name: &str) -> Result<()>;
    async fn delete_network_policy(&self, ns: &str, name: &str) -> Result<()>;

    async fn user_deployment_exists(&self, ns: &str, name: &str) -> Result<bool>;

    async fn get_user_deployment_status(&self, ns: &str, name: &str) -> Result<(u32, u32)>;

    async fn get_user_deployment_logs(
        &self,
        ns: &str,
        name: &str,
        tail: Option<u32>,
        since_seconds: Option<u32>,
    ) -> Result<String>;
}

#[derive(Default, Clone)]
pub struct MockK8sClient {
    // ns -> name -> spec/status/logs
    specs: Arc<RwLock<HashMap<String, HashMap<String, JobSpecDto>>>>,
    statuses: Arc<RwLock<HashMap<String, HashMap<String, JobStatusDto>>>>,
    logs: Arc<RwLock<HashMap<String, HashMap<String, String>>>>,
    rental_specs: Arc<RwLock<HashMap<String, HashMap<String, RentalSpecDto>>>>,
}

#[async_trait]
impl ApiK8sClient for MockK8sClient {
    fn kube_client(&self) -> kube::Client {
        panic!("MockK8sClient::kube_client() should not be called in tests. Use the real K8sClient for Gateway API operations.")
    }

    async fn create_job(&self, ns: &str, name: &str, spec: JobSpecDto) -> Result<String> {
        let mut s = self.specs.write().await;
        // Generate mock endpoints based on ports
        let endpoints: Vec<String> = spec
            .ports
            .iter()
            .map(|p| format!("mock-endpoint.local:{}", p.container_port))
            .collect();
        s.entry(ns.to_string())
            .or_default()
            .insert(name.to_string(), spec);
        // default status pending
        let mut st = self.statuses.write().await;
        st.entry(ns.to_string()).or_default().insert(
            name.to_string(),
            JobStatusDto {
                phase: "Pending".into(),
                pod_name: None,
                endpoints,
            },
        );
        Ok(name.to_string())
    }

    async fn get_job_status(&self, ns: &str, name: &str) -> Result<JobStatusDto> {
        let st = self.statuses.read().await;
        st.get(ns)
            .and_then(|m| m.get(name))
            .cloned()
            .ok_or_else(|| ApiError::NotFound {
                message: "job not found".into(),
            })
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
        Ok(l.get(ns)
            .and_then(|m| m.get(name))
            .cloned()
            .unwrap_or_else(|| "".into()))
    }

    async fn exec_job(
        &self,
        _ns: &str,
        _name: &str,
        command: Vec<String>,
        stdin: Option<String>,
        tty: bool,
    ) -> Result<ExecResultDto> {
        let cmd = command.join(" ");
        let mut stdout = String::new();
        let mut stderr = String::new();
        let mut exit_code = 0;

        // Special handling for cat commands (file reading)
        if command.len() == 2 && command[0] == "cat" {
            let file_path = &command[1];

            // Mock metadata file for testing
            if file_path == "/app/basilica-env.json" || file_path == "/basilica-env.json" {
                stdout = r#"{
  "image": "test:latest",
  "protocol_version": "1.0",
  "environments": [
    {
      "name": "test-env",
      "description": "Test environment for unit tests",
      "endpoints": {
        "reset": {"method": "POST", "path": "/reset", "params": ["seed?"]},
        "step": {"method": "POST", "path": "/step", "params": ["action"]},
        "close": {"method": "POST", "path": "/close"}
      },
      "action_space": {"type": "discrete", "n": 4},
      "observation_space": {"type": "box", "shape": [84, 84, 3]}
    }
  ]
}"#
                .to_string();
                return Ok(ExecResultDto {
                    stdout,
                    stderr,
                    exit_code: 0,
                });
            }

            // File not found
            stderr = format!("cat: {}: No such file or directory", file_path);
            return Ok(ExecResultDto {
                stdout,
                stderr,
                exit_code: 1,
            });
        }

        // Simulate other command behaviors
        if cmd.contains("fail") {
            if tty {
                stdout.push_str("simulated error");
            } else {
                stderr.push_str("simulated error");
            }
            exit_code = 1;
        } else if cmd.contains("stderr-only") {
            if tty {
                stdout.push_str("simulated stderr-only output");
            } else {
                stderr.push_str("simulated stderr-only output");
            }
        } else if tty {
            stdout = format!("exec(tty): {}", cmd);
        } else {
            stdout = format!("exec: {}", cmd);
        }

        // Handle stdin
        if let Some(input) = stdin {
            if tty {
                if !stdout.is_empty() {
                    stdout.push('\n');
                }
                stdout.push_str(&input);
            } else {
                if !stdout.is_empty() {
                    stdout.push('\n');
                }
                stdout.push_str(&format!("stdin: {}", input));
            }
        }

        Ok(ExecResultDto {
            stdout,
            stderr,
            exit_code,
        })
    }

    async fn suspend_job(&self, ns: &str, name: &str) -> Result<()> {
        let mut st = self.statuses.write().await;
        let status = st
            .get_mut(ns)
            .and_then(|m| m.get_mut(name))
            .ok_or_else(|| ApiError::NotFound {
                message: "job not found".into(),
            })?;
        status.phase = "Suspended".into();
        Ok(())
    }

    async fn resume_job(&self, ns: &str, name: &str) -> Result<()> {
        let mut st = self.statuses.write().await;
        let status = st
            .get_mut(ns)
            .and_then(|m| m.get_mut(name))
            .ok_or_else(|| ApiError::NotFound {
                message: "job not found".into(),
            })?;
        status.phase = "Running".into();
        Ok(())
    }

    async fn create_rental(&self, ns: &str, name: &str, spec: RentalSpecDto) -> Result<String> {
        // Store spec for tests and reuse job stores for simplicity
        {
            let mut rs = self.rental_specs.write().await;
            rs.entry(ns.to_string())
                .or_default()
                .insert(name.to_string(), spec.clone());
        }
        // Generate mock endpoints based on ingress rules
        let endpoints: Vec<String> = spec
            .network_ingress
            .iter()
            .map(|r| format!("{}:{}", r.exposure, r.port))
            .collect();

        let mut s = self.specs.write().await;
        s.entry(ns.to_string()).or_default().insert(
            name.to_string(),
            JobSpecDto {
                image: spec.container_image,
                command: spec.container_command,
                args: vec![],
                env: spec.container_env,
                resources: spec.resources,
                ttl_seconds: 0,
                ports: vec![],
                storage: None,
            },
        );
        let mut st = self.statuses.write().await;
        st.entry(ns.to_string()).or_default().insert(
            name.to_string(),
            JobStatusDto {
                phase: "Provisioning".into(),
                pod_name: None,
                endpoints,
            },
        );
        Ok(name.to_string())
    }

    async fn get_rental_status(&self, ns: &str, name: &str) -> Result<RentalStatusDto> {
        let st = self.statuses.read().await;
        let job_st = st
            .get(ns)
            .and_then(|m| m.get(name))
            .cloned()
            .ok_or_else(|| ApiError::NotFound {
                message: "rental not found".into(),
            })?;
        let rs = self.rental_specs.read().await;
        let endpoints = rs
            .get(ns)
            .and_then(|m| m.get(name))
            .map(|spec| {
                spec.network_ingress
                    .iter()
                    .map(|r| format!("{}:{}", r.exposure, r.port))
                    .collect::<Vec<_>>()
            })
            .unwrap_or_else(Vec::new);
        Ok(RentalStatusDto {
            state: job_st.phase,
            pod_name: job_st.pod_name,
            endpoints,
        })
    }

    async fn delete_rental(&self, ns: &str, name: &str) -> Result<()> {
        self.delete_job(ns, name).await
    }

    async fn get_rental_logs(
        &self,
        ns: &str,
        name: &str,
        tail: Option<u32>,
        _since_seconds: Option<u32>,
    ) -> Result<String> {
        // Simulate tail by trimming stored logs
        let full = self.get_job_logs(ns, name).await?;
        if let Some(t) = tail {
            if t == 0 {
                return Ok(String::new());
            }
            let lines: Vec<&str> = full.lines().collect();
            let n = lines.len();
            let start = n.saturating_sub(t as usize);
            Ok(lines[start..].join("\n"))
        } else {
            Ok(full)
        }
    }

    async fn exec_rental(
        &self,
        _ns: &str,
        _name: &str,
        command: Vec<String>,
        stdin: Option<String>,
        tty: bool,
    ) -> Result<ExecResultDto> {
        let cmd = command.join(" ");
        let mut stdout = String::new();
        let mut stderr = String::new();
        let mut exit_code = 0;

        // Simulate different behaviors based on command content
        // - "fail": non-zero exit with error text
        // - "stderr-only": produce output on stderr only (unless tty merges)
        // - default: echo executed command
        if cmd.contains("fail") {
            if tty {
                // With TTY enabled, stderr is merged into stdout in Kubernetes exec
                stdout.push_str("simulated error");
            } else {
                stderr.push_str("simulated error");
            }
            exit_code = 1;
        } else if cmd.contains("stderr-only") {
            if tty {
                stdout.push_str("simulated stderr-only output");
            } else {
                stderr.push_str("simulated stderr-only output");
            }
        } else if tty {
            stdout = format!("exec(tty): {}", cmd);
        } else {
            stdout = format!("exec: {}", cmd);
        }

        // If stdin is provided, simulate echo/consumption of input by the remote process
        if let Some(input) = stdin {
            // In many exec scenarios, user input is reflected on stdout when TTY is enabled
            if tty {
                if !stdout.is_empty() {
                    stdout.push('\n');
                }
                stdout.push_str(&input.to_string());
            } else {
                if !stdout.is_empty() {
                    stdout.push('\n');
                }
                stdout.push_str(&format!("stdin: {}", input));
            }
        }

        Ok(ExecResultDto {
            stdout,
            stderr,
            exit_code,
        })
    }

    async fn extend_rental(
        &self,
        ns: &str,
        name: &str,
        _additional_hours: u32,
    ) -> Result<RentalStatusDto> {
        self.get_rental_status(ns, name).await
    }

    async fn list_rentals(&self, ns: &str) -> Result<Vec<RentalListItemDto>> {
        let st = self.statuses.read().await;
        let rs = self.rental_specs.read().await;
        let mut out = Vec::new();
        if let Some(map) = st.get(ns) {
            for (name, s) in map.iter() {
                let endpoints = rs
                    .get(ns)
                    .and_then(|m| m.get(name))
                    .map(|spec| {
                        spec.network_ingress
                            .iter()
                            .map(|r| format!("{}:{}", r.exposure, r.port))
                            .collect::<Vec<_>>()
                    })
                    .unwrap_or_else(Vec::new);
                out.push(RentalListItemDto {
                    rental_id: name.clone(),
                    status: RentalStatusDto {
                        state: s.phase.clone(),
                        pod_name: s.pod_name.clone(),
                        endpoints,
                    },
                });
            }
        }
        Ok(out)
    }

    async fn create_namespace(&self, _name: &str) -> Result<()> {
        Ok(())
    }

    async fn get_namespace(&self, _name: &str) -> Result<()> {
        Ok(())
    }

    async fn get_configmap(
        &self,
        _ns: &str,
        _name: &str,
    ) -> Result<std::collections::BTreeMap<String, String>> {
        Ok(std::collections::BTreeMap::new())
    }

    async fn patch_configmap(
        &self,
        _ns: &str,
        _name: &str,
        _data: std::collections::BTreeMap<String, String>,
    ) -> Result<()> {
        Ok(())
    }

    async fn restart_deployment(&self, _ns: &str, _name: &str) -> Result<()> {
        Ok(())
    }

    async fn list_pods(&self, _ns: &str, _label_selector: &str) -> Result<Vec<String>> {
        Ok(vec!["mock-pod-1".to_string(), "mock-pod-2".to_string()])
    }

    async fn create_user_deployment(
        &self,
        _ns: &str,
        _name: &str,
        _user_id: &str,
        _instance_name: &str,
        _req: &crate::api::routes::deployments::types::CreateDeploymentRequest,
        _path_prefix: &str,
    ) -> Result<()> {
        Ok(())
    }

    async fn delete_user_deployment(&self, _ns: &str, _name: &str) -> Result<()> {
        Ok(())
    }

    async fn delete_deployment(&self, _ns: &str, _name: &str) -> Result<()> {
        Ok(())
    }

    async fn delete_service(&self, _ns: &str, _name: &str) -> Result<()> {
        Ok(())
    }

    async fn delete_network_policy(&self, _ns: &str, _name: &str) -> Result<()> {
        Ok(())
    }

    async fn user_deployment_exists(&self, _ns: &str, _name: &str) -> Result<bool> {
        Ok(false)
    }

    async fn get_user_deployment_status(&self, _ns: &str, _name: &str) -> Result<(u32, u32)> {
        Ok((2, 2))
    }

    async fn get_user_deployment_logs(
        &self,
        _ns: &str,
        _name: &str,
        _tail: Option<u32>,
        _since_seconds: Option<u32>,
    ) -> Result<String> {
        Ok("Mock deployment logs\nLine 1\nLine 2\n".to_string())
    }
}

impl MockK8sClient {
    pub async fn get_rental_spec(&self, ns: &str, name: &str) -> Option<RentalSpecDto> {
        let rs = self.rental_specs.read().await;
        rs.get(ns).and_then(|m| m.get(name)).cloned()
    }

    pub async fn set_logs(&self, ns: &str, name: &str, body: &str) {
        let mut l = self.logs.write().await;
        l.entry(ns.to_string())
            .or_default()
            .insert(name.to_string(), body.to_string());
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ExecResultDto {
    pub stdout: String,
    #[serde(default)]
    pub stderr: String,
    pub exit_code: i32,
}

// Rentals DTOs (simplified)
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RentalSpecDto {
    pub container_image: String,
    pub resources: Resources,
    #[serde(default)]
    pub container_env: Vec<(String, String)>,
    #[serde(default)]
    pub container_command: Vec<String>,
    #[serde(default)]
    pub container_ports: Vec<RentalPortDto>,
    #[serde(default)]
    pub network_ingress: Vec<IngressRuleDto>,
    #[serde(default)]
    pub ssh: Option<RentalSshDto>,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub namespace: Option<String>,
    #[serde(default)]
    pub labels: Option<std::collections::BTreeMap<String, String>>,
    #[serde(default)]
    pub annotations: Option<std::collections::BTreeMap<String, String>>,
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

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RentalPortDto {
    pub container_port: u16,
    pub protocol: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct IngressRuleDto {
    pub port: u16,
    pub exposure: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RentalSshDto {
    pub enabled: bool,
    pub public_key: String,
}

// K8s client implementation using kube + dynamic CRDs
#[derive(Clone)]
pub struct K8sClient {
    client: kube::Client,
}

impl K8sClient {
    pub async fn try_default() -> Result<Self> {
        let client = Self::create_client()
            .await
            .map_err(|e| ApiError::Internal {
                message: format!("k8s client init failed: {e}"),
            })?;
        Ok(Self { client })
    }

    pub fn client(&self) -> &kube::Client {
        &self.client
    }

    async fn create_client() -> anyhow::Result<kube::Client> {
        // Priority 1: KUBECONFIG_CONTENT environment variable (for AWS Secrets Manager)
        if let Ok(kubeconfig_content) = std::env::var("KUBECONFIG_CONTENT") {
            tracing::info!("Loading K8s config from KUBECONFIG_CONTENT environment variable");
            return Self::client_from_kubeconfig_content(&kubeconfig_content).await;
        }

        // Priority 2: KUBECONFIG environment variable (file path)
        if let Ok(kubeconfig_path) = std::env::var("KUBECONFIG") {
            tracing::info!("Loading K8s config from KUBECONFIG: {}", kubeconfig_path);
            let config = kube::Config::from_kubeconfig(&kube::config::KubeConfigOptions {
                context: None,
                cluster: None,
                user: None,
            })
            .await?;
            return Ok(kube::Client::try_from(config)?);
        }

        // Priority 3: Default (in-cluster or ~/.kube/config)
        tracing::info!("Attempting default K8s client initialization");
        kube::Client::try_default().await.map_err(Into::into)
    }

    async fn client_from_kubeconfig_content(content: &str) -> anyhow::Result<kube::Client> {
        use kube::config::Kubeconfig;

        let kubeconfig: Kubeconfig = serde_yaml::from_str(content)
            .map_err(|e| anyhow::anyhow!("Failed to parse kubeconfig YAML: {}", e))?;

        let config = kube::Config::from_custom_kubeconfig(
            kubeconfig,
            &kube::config::KubeConfigOptions {
                context: None,
                cluster: None,
                user: None,
            },
        )
        .await?;

        kube::Client::try_from(config).map_err(Into::into)
    }

    fn cr_api(
        &self,
        ns: &str,
        group: &str,
        version: &str,
        kind: &str,
    ) -> kube::Api<kube::core::DynamicObject> {
        use kube::core::{ApiResource, GroupVersionKind};
        let gvk = GroupVersionKind::gvk(group, version, kind);
        let ar = ApiResource::from_gvk(&gvk);
        kube::Api::namespaced_with(self.client.clone(), ns, &ar)
    }

    async fn get_pod_by_label(
        &self,
        ns: &str,
        key: &str,
        value: &str,
    ) -> Result<Option<k8s_openapi::api::core::v1::Pod>> {
        use kube::api::{Api, ListParams};
        let pods: Api<k8s_openapi::api::core::v1::Pod> = Api::namespaced(self.client.clone(), ns);
        let lp = ListParams::default().labels(&format!("{}={}", key, value));
        let list = pods.list(&lp).await.map_err(|e| ApiError::Internal {
            message: format!("list pods failed: {e}"),
        })?;
        Ok(list.items.into_iter().next())
    }

    fn parse_status_endpoints(val: &serde_json::Value) -> Vec<String> {
        val.get("status")
            .and_then(|s| s.get("endpoints"))
            .and_then(|e| e.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|x| x.as_str().map(|s| s.to_string()))
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default()
    }

    async fn create_reference_grant_for_namespace(&self, user_namespace: &str) -> Result<()> {
        use kube::{
            api::{Api, PostParams},
            core::{ApiResource, DynamicObject, GroupVersionKind},
        };
        use serde_json::json;

        let gvk = GroupVersionKind::gvk("gateway.networking.k8s.io", "v1beta1", "ReferenceGrant");
        let ar = ApiResource::from_gvk(&gvk);
        let api: Api<DynamicObject> =
            Api::namespaced_with(self.client.clone(), "basilica-system", &ar);

        let reference_grant_name = format!("allow-httproutes-{}", user_namespace);
        let reference_grant = DynamicObject::new(&reference_grant_name, &ar).data(json!({
            "apiVersion": "gateway.networking.k8s.io/v1beta1",
            "kind": "ReferenceGrant",
            "metadata": {
                "name": reference_grant_name,
                "namespace": "basilica-system"
            },
            "spec": {
                "from": [{
                    "group": "gateway.networking.k8s.io",
                    "kind": "HTTPRoute",
                    "namespace": user_namespace
                }],
                "to": [{
                    "group": "gateway.networking.k8s.io",
                    "kind": "Gateway",
                    "name": "basilica-gateway"
                }]
            }
        }));

        match api.create(&PostParams::default(), &reference_grant).await {
            Ok(_) => {
                tracing::info!(
                    user_namespace = %user_namespace,
                    reference_grant_name = %reference_grant_name,
                    "ReferenceGrant created for user namespace"
                );
                Ok(())
            }
            Err(kube::Error::Api(ae)) if ae.code == 409 => {
                tracing::debug!(
                    user_namespace = %user_namespace,
                    "ReferenceGrant already exists for namespace"
                );
                Ok(())
            }
            Err(e) => {
                tracing::error!(
                    error = %e,
                    user_namespace = %user_namespace,
                    "Failed to create ReferenceGrant"
                );
                Err(ApiError::Internal {
                    message: format!(
                        "Failed to create ReferenceGrant for namespace {}: {}",
                        user_namespace, e
                    ),
                })
            }
        }
    }
}

#[async_trait]
impl ApiK8sClient for K8sClient {
    fn kube_client(&self) -> kube::Client {
        self.client.clone()
    }

    async fn create_job(&self, ns: &str, name: &str, spec: JobSpecDto) -> Result<String> {
        use kube::api::PostParams;
        use serde_json::json;
        let api = self.cr_api(ns, "basilica.ai", "v1", "BasilicaJob");

        // Convert ports to JSON array
        let ports_json: Vec<serde_json::Value> = spec
            .ports
            .iter()
            .map(|p| json!({"containerPort": p.container_port, "protocol": p.protocol}))
            .collect();

        // Build spec with optional storage
        let mut spec_json = json!({
            "image": spec.image,
            "command": spec.command,
            "args": spec.args,
            "env": spec.env,
            "resources": {"cpu": spec.resources.cpu, "memory": spec.resources.memory, "gpus": {"count": spec.resources.gpus.count, "model": spec.resources.gpus.model}},
            "ttlSeconds": spec.ttl_seconds,
            "priority": "normal",
            "ports": ports_json,
        });

        // Add storage if provided
        if let Some(storage) = spec.storage {
            spec_json["storage"] = json!({
                "backend": storage.backend,
                "bucket": storage.bucket,
                "prefix": storage.prefix,
                "credentials": storage.credentials,
            });
        }

        let obj = json!({
            "apiVersion": "basilica.ai/v1",
            "kind": "BasilicaJob",
            "metadata": {"name": name, "namespace": ns},
            "spec": spec_json
        });
        let dynobj: kube::core::DynamicObject =
            serde_json::from_value(obj).map_err(|e| ApiError::Internal {
                message: format!("serde dynobj: {e}"),
            })?;
        let _ = api
            .create(&PostParams::default(), &dynobj)
            .await
            .map_err(|e| ApiError::Internal {
                message: format!("create BasilicaJob failed: {e}"),
            })?;
        Ok(name.to_string())
    }

    async fn get_job_status(&self, ns: &str, name: &str) -> Result<JobStatusDto> {
        use serde_json::Value;
        let api = self.cr_api(ns, "basilica.ai", "v1", "BasilicaJob");
        let obj = api.get(name).await.map_err(|e| ApiError::NotFound {
            message: format!("job not found: {e}"),
        })?;
        let val: Value = serde_json::to_value(&obj).map_err(|e| ApiError::Internal {
            message: format!("to_value: {e}"),
        })?;
        let phase = val
            .get("status")
            .and_then(|s| s.get("phase"))
            .and_then(|v| v.as_str())
            .unwrap_or("Pending")
            .to_string();
        let pod_name = val
            .get("status")
            .and_then(|s| s.get("podName"))
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let endpoints = Self::parse_status_endpoints(&val);
        Ok(JobStatusDto {
            phase,
            pod_name,
            endpoints,
        })
    }

    async fn delete_job(&self, ns: &str, name: &str) -> Result<()> {
        use kube::api::DeleteParams;
        let api = self.cr_api(ns, "basilica.ai", "v1", "BasilicaJob");
        let _ = api
            .delete(name, &DeleteParams::default())
            .await
            .map_err(|e| ApiError::Internal {
                message: format!("delete job failed: {e}"),
            })?;
        Ok(())
    }

    async fn get_job_logs(&self, ns: &str, name: &str) -> Result<String> {
        use kube::api::{Api, LogParams};
        if let Some(pod) = self.get_pod_by_label(ns, "basilica.ai/job", name).await? {
            let pods: Api<k8s_openapi::api::core::v1::Pod> =
                Api::namespaced(self.client.clone(), ns);
            let lp = LogParams {
                container: None,
                follow: false,
                ..Default::default()
            };
            let pod_name = pod.metadata.name.unwrap_or_default();
            let logs = pods
                .logs(&pod_name, &lp)
                .await
                .map_err(|e| ApiError::Internal {
                    message: format!("get logs failed: {e}"),
                })?;
            Ok(logs)
        } else {
            Err(ApiError::NotFound {
                message: "pod not found".into(),
            })
        }
    }

    async fn exec_job(
        &self,
        ns: &str,
        job_name: &str,
        command: Vec<String>,
        stdin: Option<String>,
        tty: bool,
    ) -> Result<ExecResultDto> {
        use kube::api::{Api, AttachParams};
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        // Find the pod for the job
        let pod = self
            .get_pod_by_label(ns, "basilica.ai/job", job_name)
            .await?
            .ok_or_else(|| ApiError::NotFound {
                message: "pod not found".into(),
            })?;

        let pod_name = pod.metadata.name.unwrap_or_default();
        let pods: Api<k8s_openapi::api::core::v1::Pod> = Api::namespaced(self.client.clone(), ns);

        // Determine container name (first container in spec)
        let container_name = pod
            .spec
            .as_ref()
            .and_then(|spec| spec.containers.first().map(|c| c.name.clone()));

        let params = AttachParams {
            stdout: true,
            stderr: true,
            stdin: stdin.is_some(),
            tty,
            container: container_name,
            ..Default::default()
        };

        // Execute command
        let args: Vec<&str> = command.iter().map(|s| s.as_str()).collect();
        let mut attached =
            pods.exec(&pod_name, args, &params)
                .await
                .map_err(|e| ApiError::Internal {
                    message: format!("exec failed: {e}"),
                })?;

        // Send stdin if provided
        if let (Some(input), Some(mut sin)) = (stdin, attached.stdin()) {
            let _ = sin.write_all(input.as_bytes()).await;
            let _ = sin.shutdown().await;
        }

        // Read stdout
        let mut stdout_buf = Vec::new();
        if let Some(mut out) = attached.stdout() {
            out.read_to_end(&mut stdout_buf)
                .await
                .map_err(|e| ApiError::Internal {
                    message: format!("read stdout failed: {e}"),
                })?;
        }

        // Read stderr
        let mut stderr_buf = Vec::new();
        if let Some(mut err) = attached.stderr() {
            err.read_to_end(&mut stderr_buf)
                .await
                .map_err(|e| ApiError::Internal {
                    message: format!("read stderr failed: {e}"),
                })?;
        }

        // Wait for completion
        let joined = attached.join().await;
        let exit_code = if joined.is_ok() { 0 } else { 1 };

        Ok(ExecResultDto {
            stdout: String::from_utf8_lossy(&stdout_buf).into_owned(),
            stderr: String::from_utf8_lossy(&stderr_buf).into_owned(),
            exit_code,
        })
    }

    async fn suspend_job(&self, ns: &str, name: &str) -> Result<()> {
        use kube::api::{Patch, PatchParams};
        use serde_json::json;

        let api = self.cr_api(ns, "basilica.ai", "v1", "BasilicaJob");
        let patch = json!({
            "spec": {
                "suspended": true
            }
        });

        api.patch(name, &PatchParams::default(), &Patch::Merge(&patch))
            .await
            .map_err(|e| ApiError::Internal {
                message: format!("suspend job failed: {e}"),
            })?;
        Ok(())
    }

    async fn resume_job(&self, ns: &str, name: &str) -> Result<()> {
        use kube::api::{Patch, PatchParams};
        use serde_json::json;

        let api = self.cr_api(ns, "basilica.ai", "v1", "BasilicaJob");
        let patch = json!({
            "spec": {
                "suspended": false
            }
        });

        api.patch(name, &PatchParams::default(), &Patch::Merge(&patch))
            .await
            .map_err(|e| ApiError::Internal {
                message: format!("resume job failed: {e}"),
            })?;
        Ok(())
    }

    async fn create_rental(&self, ns: &str, name: &str, spec: RentalSpecDto) -> Result<String> {
        use kube::api::PostParams;
        use serde_json::json;
        let api = self.cr_api(ns, "basilica.ai", "v1", "GpuRental");
        let env_objs: Vec<serde_json::Value> = spec
            .container_env
            .iter()
            .map(|(k, v)| json!({"name": k, "value": v}))
            .collect();
        let obj = json!({
            "apiVersion": "basilica.ai/v1",
            "kind": "GpuRental",
            "metadata": {
                "name": name,
                "namespace": ns,
                "labels": spec.labels.clone().unwrap_or_default(),
                "annotations": spec.annotations.clone().unwrap_or_default(),
            },
            "spec": {
                "container": {
                    "image": spec.container_image,
                    "env": env_objs,
                    "command": spec.container_command,
                    "ports": spec.container_ports,
                    "volumes": [],
                    "resources": {"cpu": spec.resources.cpu, "memory": spec.resources.memory, "gpus": {"count": spec.resources.gpus.count, "model": spec.resources.gpus.model}},
                },
                "duration": {"hours": 0, "autoExtend": false, "maxExtensions": 0},
                "accessType": "ssh",
                "network": {"ingress": spec.network_ingress, "egressPolicy": "restricted", "allowedEgress": [], "publicIpRequired": false },
                "ssh": spec.ssh,
                "ttlSeconds": 0
            }
        });
        let dynobj: kube::core::DynamicObject =
            serde_json::from_value(obj).map_err(|e| ApiError::Internal {
                message: format!("serde dynobj: {e}"),
            })?;
        let _ = api
            .create(&PostParams::default(), &dynobj)
            .await
            .map_err(|e| ApiError::Internal {
                message: format!("create GpuRental failed: {e}"),
            })?;
        Ok(name.to_string())
    }

    async fn get_rental_status(&self, ns: &str, name: &str) -> Result<RentalStatusDto> {
        use serde_json::Value;
        let api = self.cr_api(ns, "basilica.ai", "v1", "GpuRental");
        let obj = api.get(name).await.map_err(|e| ApiError::NotFound {
            message: format!("rental not found: {e}"),
        })?;
        let val: Value = serde_json::to_value(&obj).map_err(|e| ApiError::Internal {
            message: format!("to_value: {e}"),
        })?;
        let state = val
            .get("status")
            .and_then(|s| s.get("state"))
            .and_then(|v| v.as_str())
            .unwrap_or("Provisioning")
            .to_string();
        let pod_name = val
            .get("status")
            .and_then(|s| s.get("podName"))
            .and_then(|v| v.as_str())
            .map(|s| s.to_string());
        let endpoints = Self::parse_status_endpoints(&val);
        Ok(RentalStatusDto {
            state,
            pod_name,
            endpoints,
        })
    }

    async fn delete_rental(&self, ns: &str, name: &str) -> Result<()> {
        use kube::api::DeleteParams;
        let api = self.cr_api(ns, "basilica.ai", "v1", "GpuRental");
        let _ = api
            .delete(name, &DeleteParams::default())
            .await
            .map_err(|e| ApiError::Internal {
                message: format!("delete rental failed: {e}"),
            })?;
        Ok(())
    }

    async fn get_rental_logs(
        &self,
        ns: &str,
        name: &str,
        tail: Option<u32>,
        since_seconds: Option<u32>,
    ) -> Result<String> {
        use kube::api::{Api, LogParams};
        if let Some(pod) = self
            .get_pod_by_label(ns, "basilica.ai/rental", name)
            .await?
        {
            let pods: Api<k8s_openapi::api::core::v1::Pod> =
                Api::namespaced(self.client.clone(), ns);
            let lp = LogParams {
                container: None,
                follow: false,
                tail_lines: tail.map(|x| x as i64),
                since_seconds: since_seconds.map(|x| x as i64),
                ..Default::default()
            };
            let pod_name = pod.metadata.name.unwrap_or_default();
            let logs = pods
                .logs(&pod_name, &lp)
                .await
                .map_err(|e| ApiError::Internal {
                    message: format!("get logs failed: {e}"),
                })?;
            Ok(logs)
        } else {
            Err(ApiError::NotFound {
                message: "pod not found".into(),
            })
        }
    }

    async fn exec_rental(
        &self,
        ns: &str,
        name: &str,
        command: Vec<String>,
        stdin: Option<String>,
        tty: bool,
    ) -> Result<ExecResultDto> {
        use kube::api::{Api, AttachParams};
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        // Find the first pod for the rental
        let pod = self
            .get_pod_by_label(ns, "basilica.ai/rental", name)
            .await?
            .ok_or_else(|| ApiError::NotFound {
                message: "pod not found".into(),
            })?;
        let pod_name = pod.metadata.name.clone().unwrap_or_default();
        let pods: Api<k8s_openapi::api::core::v1::Pod> = Api::namespaced(self.client.clone(), ns);
        // Determine container name (first container in spec, if any)
        let container_name = pod
            .spec
            .as_ref()
            .and_then(|spec| spec.containers.first().map(|c| c.name.clone()));
        let params = AttachParams {
            stdout: true,
            stderr: true,
            stdin: stdin.is_some(),
            tty,
            container: container_name,
            ..Default::default()
        };
        // kube expects &str slice for args
        let args: Vec<&str> = command.iter().map(|s| s.as_str()).collect();
        let mut attached =
            pods.exec(&pod_name, args, &params)
                .await
                .map_err(|e| ApiError::Internal {
                    message: format!("exec failed: {e}"),
                })?;

        // Send stdin if provided
        if let (Some(input), Some(mut sin)) = (stdin, attached.stdin()) {
            let _ = sin.write_all(input.as_bytes()).await;
            let _ = sin.shutdown().await;
        }

        let mut stdout_buf = Vec::new();
        if let Some(mut out) = attached.stdout() {
            out.read_to_end(&mut stdout_buf)
                .await
                .map_err(|e| ApiError::Internal {
                    message: format!("read stdout failed: {e}"),
                })?;
        }
        let mut stderr_buf = Vec::new();
        if let Some(mut err) = attached.stderr() {
            err.read_to_end(&mut stderr_buf)
                .await
                .map_err(|e| ApiError::Internal {
                    message: format!("read stderr failed: {e}"),
                })?;
        }
        // Best-effort wait for remote to complete
        let joined = attached.join().await;
        let mut exit_code = if joined.is_ok() { 0 } else { 1 };

        // If non-zero or unknown, attempt to read the container's last termination status
        // to recover a more accurate exit code (useful if exec caused container to terminate).
        if exit_code != 0 {
            if let Ok(p) = pods.get(&pod_name).await {
                // Prefer current state.terminated, fallback to last_state.terminated
                if let Some(status) = &p.status {
                    if let Some(cstatuses) = &status.container_statuses {
                        // If AttachParams set a container, prefer that name
                        let prefer = params.container.clone();
                        let iter = cstatuses.iter();
                        let chosen = if let Some(pref_name) = prefer {
                            iter.clone()
                                .find(|cs| cs.name == pref_name)
                                .or_else(|| cstatuses.first())
                        } else {
                            cstatuses.first()
                        };
                        if let Some(cs) = chosen {
                            if let Some(state) = &cs.state {
                                if let Some(term) = &state.terminated {
                                    exit_code = term.exit_code;
                                }
                            }
                            if exit_code != 0 {
                                if let Some(last) = &cs.last_state {
                                    if let Some(term) = &last.terminated {
                                        exit_code = term.exit_code;
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }

        Ok(ExecResultDto {
            stdout: String::from_utf8_lossy(&stdout_buf).into_owned(),
            stderr: String::from_utf8_lossy(&stderr_buf).into_owned(),
            exit_code,
        })
    }

    async fn extend_rental(
        &self,
        ns: &str,
        name: &str,
        _additional_hours: u32,
    ) -> Result<RentalStatusDto> {
        // For now, return current status (operator handles auto-extend)
        self.get_rental_status(ns, name).await
    }

    async fn list_rentals(&self, ns: &str) -> Result<Vec<RentalListItemDto>> {
        use kube::api::ListParams;
        use serde_json::Value;
        let api = self.cr_api(ns, "basilica.ai", "v1", "GpuRental");
        let list = api
            .list(&ListParams::default())
            .await
            .map_err(|e| ApiError::Internal {
                message: format!("list rentals failed: {e}"),
            })?;
        let mut out = Vec::new();
        for item in list.items {
            let name = item.metadata.name.clone().unwrap_or_default();
            let val: Value = serde_json::to_value(&item).map_err(|e| ApiError::Internal {
                message: format!("to_value: {e}"),
            })?;
            let state = val
                .get("status")
                .and_then(|s| s.get("state"))
                .and_then(|v| v.as_str())
                .unwrap_or("Provisioning")
                .to_string();
            let pod_name = val
                .get("status")
                .and_then(|s| s.get("podName"))
                .and_then(|v| v.as_str())
                .map(|s| s.to_string());
            let endpoints = Self::parse_status_endpoints(&val);
            out.push(RentalListItemDto {
                rental_id: name,
                status: RentalStatusDto {
                    state,
                    pod_name,
                    endpoints,
                },
            });
        }
        Ok(out)
    }

    async fn create_namespace(&self, name: &str) -> Result<()> {
        use k8s_openapi::api::core::v1::Namespace;
        use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;
        use kube::api::{Api, PostParams};

        let api: Api<Namespace> = Api::all(self.client.clone());
        let ns = Namespace {
            metadata: ObjectMeta {
                name: Some(name.to_string()),
                ..Default::default()
            },
            ..Default::default()
        };

        match api.create(&PostParams::default(), &ns).await {
            Ok(_) => {
                if name.starts_with("u-") {
                    self.create_reference_grant_for_namespace(name).await?;
                }
                Ok(())
            }
            Err(kube::Error::Api(ae)) if ae.code == 409 => Ok(()),
            Err(e) => Err(ApiError::Internal {
                message: format!("create namespace failed: {e}"),
            }),
        }
    }

    async fn get_namespace(&self, name: &str) -> Result<()> {
        use k8s_openapi::api::core::v1::Namespace;
        use kube::api::Api;

        let api: Api<Namespace> = Api::all(self.client.clone());
        api.get(name).await.map_err(|e| ApiError::Internal {
            message: format!("get namespace failed: {e}"),
        })?;
        Ok(())
    }

    async fn get_configmap(
        &self,
        ns: &str,
        name: &str,
    ) -> Result<std::collections::BTreeMap<String, String>> {
        use k8s_openapi::api::core::v1::ConfigMap;
        use kube::api::Api;

        let api: Api<ConfigMap> = Api::namespaced(self.client.clone(), ns);
        let cm = api.get(name).await.map_err(|e| ApiError::Internal {
            message: format!("get configmap failed: {e}"),
        })?;
        Ok(cm.data.unwrap_or_default())
    }

    async fn patch_configmap(
        &self,
        ns: &str,
        name: &str,
        data: std::collections::BTreeMap<String, String>,
    ) -> Result<()> {
        use k8s_openapi::api::core::v1::ConfigMap;
        use kube::api::{Api, Patch, PatchParams};

        let api: Api<ConfigMap> = Api::namespaced(self.client.clone(), ns);
        let patch = serde_json::json!({ "data": data });
        api.patch(name, &PatchParams::default(), &Patch::Merge(&patch))
            .await
            .map_err(|e| ApiError::Internal {
                message: format!("patch configmap failed: {e}"),
            })?;
        Ok(())
    }

    async fn restart_deployment(&self, ns: &str, name: &str) -> Result<()> {
        use k8s_openapi::api::apps::v1::Deployment;
        use kube::api::{Api, Patch, PatchParams};

        let api: Api<Deployment> = Api::namespaced(self.client.clone(), ns);
        let timestamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs()
            .to_string();

        let patch = serde_json::json!({
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": timestamp
                        }
                    }
                }
            }
        });

        api.patch(name, &PatchParams::default(), &Patch::Strategic(&patch))
            .await
            .map_err(|e| ApiError::Internal {
                message: format!("restart deployment failed: {e}"),
            })?;
        Ok(())
    }

    async fn list_pods(&self, ns: &str, label_selector: &str) -> Result<Vec<String>> {
        use k8s_openapi::api::core::v1::Pod;
        use kube::api::{Api, ListParams};

        let api: Api<Pod> = Api::namespaced(self.client.clone(), ns);
        let lp = ListParams::default().labels(label_selector);

        let pods = api.list(&lp).await.map_err(|e| ApiError::Internal {
            message: format!("list pods failed: {e}"),
        })?;

        let pod_names = pods
            .items
            .iter()
            .filter_map(|pod| pod.metadata.name.clone())
            .collect();

        Ok(pod_names)
    }

    async fn create_user_deployment(
        &self,
        ns: &str,
        name: &str,
        user_id: &str,
        instance_name: &str,
        req: &crate::api::routes::deployments::types::CreateDeploymentRequest,
        path_prefix: &str,
    ) -> Result<()> {
        use kube::api::PostParams;
        use serde_json::json;

        let api = self.cr_api(ns, "basilica.ai", "v1", "UserDeployment");

        if self.user_deployment_exists(ns, name).await? {
            tracing::debug!(
                namespace = ns,
                name = name,
                "UserDeployment already exists, skipping creation"
            );
            return Ok(());
        }

        let env_objs: Vec<serde_json::Value> = req
            .env
            .iter()
            .map(|(k, v)| json!({"name": k, "value": v}))
            .collect();

        let mut spec = json!({
            "userId": user_id,
            "instanceName": instance_name,
            "image": req.image,
            "replicas": req.replicas,
            "port": req.port,
            "command": req.command,
            "args": req.args,
            "env": env_objs,
            "resources": {
                "cpu": req.resources.as_ref().map(|r| r.cpu.clone()).unwrap_or_else(|| "500m".to_string()),
                "memory": req.resources.as_ref().map(|r| r.memory.clone()).unwrap_or_else(|| "512Mi".to_string()),
            },
            "pathPrefix": path_prefix,
            "ttlSeconds": req.ttl_seconds,
        });

        if let Some(ref health_check) = req.health_check {
            let mut health_check_obj = json!({});

            if let Some(ref liveness) = health_check.liveness {
                health_check_obj["liveness"] = json!({
                    "path": liveness.path,
                    "initialDelaySeconds": liveness.initial_delay_seconds,
                    "periodSeconds": liveness.period_seconds,
                    "timeoutSeconds": liveness.timeout_seconds,
                    "failureThreshold": liveness.failure_threshold,
                });
            }

            if let Some(ref readiness) = health_check.readiness {
                health_check_obj["readiness"] = json!({
                    "path": readiness.path,
                    "initialDelaySeconds": readiness.initial_delay_seconds,
                    "periodSeconds": readiness.period_seconds,
                    "timeoutSeconds": readiness.timeout_seconds,
                    "failureThreshold": readiness.failure_threshold,
                });
            }

            spec["healthCheck"] = health_check_obj;
        }

        let obj = json!({
            "apiVersion": "basilica.ai/v1",
            "kind": "UserDeployment",
            "metadata": {
                "name": name,
                "namespace": ns,
            },
            "spec": spec,
        });

        let dynobj: kube::core::DynamicObject =
            serde_json::from_value(obj).map_err(|e| ApiError::Internal {
                message: format!("serde dynobj: {e}"),
            })?;

        api.create(&PostParams::default(), &dynobj)
            .await
            .map_err(|e| ApiError::Internal {
                message: format!("create UserDeployment failed: {e}"),
            })?;

        Ok(())
    }

    async fn delete_user_deployment(&self, ns: &str, name: &str) -> Result<()> {
        use kube::api::DeleteParams;

        let api = self.cr_api(ns, "basilica.ai", "v1", "UserDeployment");

        api.delete(name, &DeleteParams::default())
            .await
            .map_err(|e| ApiError::Internal {
                message: format!("delete UserDeployment failed: {e}"),
            })?;

        tracing::info!(
            namespace = ns,
            name = name,
            "Successfully deleted UserDeployment CR"
        );

        Ok(())
    }

    async fn delete_deployment(&self, ns: &str, name: &str) -> Result<()> {
        use k8s_openapi::api::apps::v1::Deployment;
        use kube::api::{Api, DeleteParams};

        let api: Api<Deployment> = Api::namespaced(self.client.clone(), ns);

        match api.delete(name, &DeleteParams::default()).await {
            Ok(_) => {
                tracing::info!(
                    namespace = ns,
                    name = name,
                    "Successfully deleted Deployment"
                );
                Ok(())
            }
            Err(kube::Error::Api(err)) if err.code == 404 => {
                tracing::debug!(
                    namespace = ns,
                    name = name,
                    "Deployment not found, already deleted"
                );
                Ok(())
            }
            Err(e) => Err(ApiError::Internal {
                message: format!("delete Deployment failed: {e}"),
            }),
        }
    }

    async fn delete_service(&self, ns: &str, name: &str) -> Result<()> {
        use k8s_openapi::api::core::v1::Service;
        use kube::api::{Api, DeleteParams};

        let api: Api<Service> = Api::namespaced(self.client.clone(), ns);

        match api.delete(name, &DeleteParams::default()).await {
            Ok(_) => {
                tracing::info!(namespace = ns, name = name, "Successfully deleted Service");
                Ok(())
            }
            Err(kube::Error::Api(err)) if err.code == 404 => {
                tracing::debug!(
                    namespace = ns,
                    name = name,
                    "Service not found, already deleted"
                );
                Ok(())
            }
            Err(e) => Err(ApiError::Internal {
                message: format!("delete Service failed: {e}"),
            }),
        }
    }

    async fn delete_network_policy(&self, ns: &str, name: &str) -> Result<()> {
        use k8s_openapi::api::networking::v1::NetworkPolicy;
        use kube::api::{Api, DeleteParams};

        let api: Api<NetworkPolicy> = Api::namespaced(self.client.clone(), ns);

        match api.delete(name, &DeleteParams::default()).await {
            Ok(_) => {
                tracing::info!(
                    namespace = ns,
                    name = name,
                    "Successfully deleted NetworkPolicy"
                );
                Ok(())
            }
            Err(kube::Error::Api(err)) if err.code == 404 => {
                tracing::debug!(
                    namespace = ns,
                    name = name,
                    "NetworkPolicy not found, already deleted"
                );
                Ok(())
            }
            Err(e) => Err(ApiError::Internal {
                message: format!("delete NetworkPolicy failed: {e}"),
            }),
        }
    }

    async fn user_deployment_exists(&self, ns: &str, name: &str) -> Result<bool> {
        let api = self.cr_api(ns, "basilica.ai", "v1", "UserDeployment");

        match api.get(name).await {
            Ok(_) => Ok(true),
            Err(kube::Error::Api(err_resp)) if err_resp.code == 404 => Ok(false),
            Err(e) => Err(ApiError::Internal {
                message: format!("check UserDeployment existence failed: {e}"),
            }),
        }
    }

    async fn get_user_deployment_status(&self, ns: &str, name: &str) -> Result<(u32, u32)> {
        let api = self.cr_api(ns, "basilica.ai", "v1", "UserDeployment");

        match api.get(name).await {
            Ok(obj) => {
                tracing::debug!(
                    namespace = ns,
                    name = name,
                    "Retrieved UserDeployment CR, checking status"
                );

                let status = obj.data.get("status");
                if status.is_none() {
                    tracing::warn!(
                        namespace = ns,
                        name = name,
                        "UserDeployment CR has no status field"
                    );
                    return Ok((0, 0));
                }

                let status = status.unwrap();

                let desired = status
                    .get("replicasDesired")
                    .and_then(|v| v.as_u64())
                    .unwrap_or(0) as u32;

                let ready = status
                    .get("replicasReady")
                    .and_then(|v| v.as_u64())
                    .unwrap_or(0) as u32;

                tracing::debug!(
                    namespace = ns,
                    name = name,
                    desired = desired,
                    ready = ready,
                    "Extracted replica counts from UserDeployment status"
                );

                Ok((desired, ready))
            }
            Err(kube::Error::Api(err_resp)) if err_resp.code == 404 => {
                tracing::warn!(
                    namespace = ns,
                    name = name,
                    "UserDeployment CR not found (404)"
                );
                Ok((0, 0))
            }
            Err(e) => {
                tracing::error!(
                    namespace = ns,
                    name = name,
                    error = %e,
                    "Failed to get UserDeployment status"
                );
                Err(ApiError::Internal {
                    message: format!("get UserDeployment status failed: {e}"),
                })
            }
        }
    }

    async fn get_user_deployment_logs(
        &self,
        ns: &str,
        name: &str,
        tail: Option<u32>,
        since_seconds: Option<u32>,
    ) -> Result<String> {
        use k8s_openapi::api::core::v1::Pod;
        use kube::api::{Api, LogParams};

        let api: Api<Pod> = Api::namespaced(self.client.clone(), ns);

        let label_selector = format!("app.kubernetes.io/instance={}", name);
        let pods = api
            .list(&kube::api::ListParams::default().labels(&label_selector))
            .await
            .map_err(|e| {
                tracing::error!(
                    error = %e,
                    namespace = ns,
                    instance = name,
                    "Failed to list pods for deployment"
                );
                ApiError::Internal {
                    message: format!("Failed to list pods: {}", e),
                }
            })?;

        if pods.items.is_empty() {
            return Ok(String::new());
        }

        let pod = &pods.items[0];
        let pod_name = pod
            .metadata
            .name
            .as_ref()
            .ok_or_else(|| ApiError::Internal {
                message: "Pod has no name".to_string(),
            })?;

        let mut log_params = LogParams::default();
        if let Some(t) = tail {
            log_params.tail_lines = Some(t as i64);
        }
        if let Some(s) = since_seconds {
            log_params.since_seconds = Some(s as i64);
        }

        let logs = api.logs(pod_name, &log_params).await.map_err(|e| {
            tracing::error!(error = %e, namespace = ns, pod = pod_name, "Failed to get pod logs");
            ApiError::Internal {
                message: format!("Failed to get logs: {}", e),
            }
        })?;

        Ok(logs)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_endpoints_from_status_value() {
        let val = serde_json::json!({
            "status": {
                "state": "Active",
                "podName": "rental-pod-1",
                "endpoints": ["NodePort:8080", "LoadBalancer:443"]
            }
        });
        let eps = K8sClient::parse_status_endpoints(&val);
        assert_eq!(eps, vec!["NodePort:8080", "LoadBalancer:443"]);
    }

    #[test]
    fn endpoints_absent_defaults_empty() {
        let val = serde_json::json!({ "status": { "state": "Provisioning" } });
        let eps = K8sClient::parse_status_endpoints(&val);
        assert!(eps.is_empty());
    }

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
                    resources: Resources {
                        cpu: "1".into(),
                        memory: "512Mi".into(),
                        gpus: GpuSpec {
                            count: 0,
                            model: vec![],
                        },
                    },
                    ttl_seconds: 0,
                    ports: vec![],
                    storage: None,
                },
            )
            .await
            .unwrap();
        assert_eq!(name, "job1");
        let st = c.get_job_status("ns", "job1").await.unwrap();
        assert_eq!(st.phase, "Pending");
        assert!(st.endpoints.is_empty()); // No ports, no endpoints
        c.delete_job("ns", "job1").await.unwrap();
        assert!(matches!(
            c.get_job_status("ns", "job1").await,
            Err(ApiError::NotFound { message: _ })
        ));
    }
}
