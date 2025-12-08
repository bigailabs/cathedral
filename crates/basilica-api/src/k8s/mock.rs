use async_trait::async_trait;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::sync::RwLock;

use super::r#trait::ApiK8sClient;
use super::types::*;
use crate::error::{ApiError, Result};

#[derive(Default, Clone)]
pub struct MockK8sClient {
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
        let endpoints: Vec<String> = spec
            .ports
            .iter()
            .map(|p| format!("mock-endpoint.local:{}", p.container_port))
            .collect();
        s.entry(ns.to_string())
            .or_default()
            .insert(name.to_string(), spec);

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
            .unwrap_or_default())
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

        if command.len() == 2 && command[0] == "cat" {
            let file_path = &command[1];

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

            stderr = format!("cat: {}: No such file or directory", file_path);
            return Ok(ExecResultDto {
                stdout,
                stderr,
                exit_code: 1,
            });
        }

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
        {
            let mut rs = self.rental_specs.write().await;
            rs.entry(ns.to_string())
                .or_default()
                .insert(name.to_string(), spec.clone());
        }

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

        if let Some(input) = stdin {
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

    async fn get_user_deployment_phase(
        &self,
        _ns: &str,
        _name: &str,
    ) -> Result<DeploymentPhaseDto> {
        Ok(DeploymentPhaseDto {
            phase: "ready".to_string(),
            progress: None,
            phase_history: vec![],
            replicas_desired: 1,
            replicas_ready: 1,
        })
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

    async fn get_user_deployment_events(
        &self,
        _ns: &str,
        _instance_name: &str,
        _limit: Option<u32>,
    ) -> Result<Vec<DeploymentEventDto>> {
        Ok(vec![])
    }

    async fn secret_exists(&self, _ns: &str, _name: &str) -> Result<bool> {
        // Mock always returns true - secrets exist
        Ok(true)
    }

    async fn get_namespace_resource_quota(&self, _ns: &str) -> Result<Option<ResourceQuotaDto>> {
        // Mock returns no quota (unlimited)
        Ok(None)
    }

    async fn check_cluster_capacity(
        &self,
        _cpu_request: &str,
        _memory_request: &str,
        _gpu_count: Option<u32>,
    ) -> Result<ClusterCapacityResult> {
        // Mock always returns sufficient capacity
        Ok(ClusterCapacityResult {
            has_capacity: true,
            message: None,
            available_cpu: Some("8000m".to_string()),
            available_memory: Some("32Gi".to_string()),
            available_gpus: Some(4),
        })
    }

    async fn get_image_pull_secrets(&self, _ns: &str) -> Result<Vec<String>> {
        // Mock returns empty list (no private registry secrets)
        Ok(vec![])
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
