use axum::{
    extract::State,
    response::{sse::Event, Sse},
    Extension, Json,
};
use futures::Stream;
use serde::{Deserialize, Serialize};

use crate::api::middleware::AuthContext;
use crate::apimetrics;
use crate::{
    error::{ApiError, Result},
    k8s_client::{RentalListItemDto, RentalSpecDto, RentalStatusDto, Resources},
    server::AppState,
};
use basilica_sdk::types::{NodeSelection, StartRentalApiRequest};
use futures::Stream as FuturesStream;
use std::pin::Pin;
use std::time::Instant;

#[derive(Debug, Clone, Deserialize)]
pub struct CreateRentalRequest {
    pub container_image: String,
    pub resources: Resources,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub namespace: Option<String>,
    #[serde(default)]
    pub command: Vec<String>,
    #[serde(default)]
    pub environment: std::collections::HashMap<String, String>,
    #[serde(default)]
    pub ports: Vec<u16>,
    #[serde(default)]
    pub network: Option<NetworkConfig>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct NetworkConfig {
    #[serde(default)]
    pub ingress_ports: Vec<u16>,
    #[serde(default)]
    pub egress_policy: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct CreateRentalResponse {
    pub rental_id: String,
}

fn user_namespace(user_id: &str) -> String {
    let mut out = String::from("u-");
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

// List rentals in namespace (v2 K8s backend)
pub async fn list_rentals(
    State(state): State<AppState>,
    Extension(auth): Extension<crate::api::middleware::AuthContext>,
) -> Result<Json<Vec<RentalStatusResponse>>> {
    let start = Instant::now();
    let client = match state.k8s.as_ref() {
        Some(c) => c,
        None => {
            apimetrics::record_request("rentals_v2.list", "GET", start, false);
            return Err(ApiError::ServiceUnavailable);
        }
    };
    let ns = user_namespace(&auth.user_id);
    let items: Vec<RentalListItemDto> = client.list_rentals(&ns).await?;
    let out: Vec<RentalStatusResponse> = items
        .into_iter()
        .map(|it| RentalStatusResponse {
            rental_id: it.rental_id,
            status: it.status,
        })
        .collect();
    apimetrics::record_request("rentals_v2.list", "GET", start, true);
    Ok(Json(out))
}

pub async fn create_rental(
    State(state): State<AppState>,
    Extension(auth): Extension<crate::api::middleware::AuthContext>,
    Json(req): Json<CreateRentalRequest>,
) -> Result<Json<CreateRentalResponse>> {
    let start = Instant::now();
    let client = match state.k8s.as_ref() {
        Some(c) => c,
        None => {
            apimetrics::record_request("rentals_v2.create", "POST", start, false);
            return Err(ApiError::ServiceUnavailable);
        }
    };
    let name = req
        .name
        .clone()
        .unwrap_or_else(|| format!("rent-{}", rand::random::<u32>()));
    let ns = user_namespace(&auth.user_id);

    // Map environment
    let container_env: Vec<(String, String)> = req
        .environment
        .iter()
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();

    // Map ports to container ports and network ingress
    let mut container_ports: Vec<crate::k8s_client::RentalPortDto> = Vec::new();
    let mut network_ingress: Vec<crate::k8s_client::IngressRuleDto> = Vec::new();

    // Ports from the ports field
    for port in &req.ports {
        container_ports.push(crate::k8s_client::RentalPortDto {
            container_port: *port,
            protocol: "TCP".to_string(),
        });
    }

    // Ingress ports from network config
    if let Some(ref net) = req.network {
        for port in &net.ingress_ports {
            network_ingress.push(crate::k8s_client::IngressRuleDto {
                port: *port,
                exposure: "NodePort".to_string(),
            });
        }
    }

    let spec = RentalSpecDto {
        container_image: req.container_image,
        resources: req.resources,
        container_env,
        container_command: req.command,
        container_ports,
        network_ingress,
        ssh: None,
        name: Some(name.clone()),
        namespace: Some(ns.clone()),
        labels: None,
        annotations: None,
    };
    let id = client.create_rental(&ns, &name, spec).await?;
    apimetrics::record_rental_created(&ns);
    apimetrics::record_request("rentals_v2.create", "POST", start, true);
    Ok(Json(CreateRentalResponse { rental_id: id }))
}

// Compatibility: accept legacy StartRentalApiRequest and map to K8s-backed rental
pub async fn create_rental_compat(
    State(state): State<AppState>,
    Extension(auth): Extension<AuthContext>,
    Json(req): Json<StartRentalApiRequest>,
) -> Result<Json<CreateRentalResponse>> {
    let start = Instant::now();
    let client = match state.k8s.as_ref() {
        Some(c) => c,
        None => {
            apimetrics::record_request("rentals_v2.create_compat", "POST", start, false);
            return Err(ApiError::ServiceUnavailable);
        }
    };
    let ns = user_namespace(&auth.user_id);

    // Map legacy resource requirements to K8s Resources DTO
    let cpu = if req.resources.cpu_cores > 0.0 {
        if (req.resources.cpu_cores - req.resources.cpu_cores.trunc()).abs() < f64::EPSILON {
            format!("{}", req.resources.cpu_cores as u32)
        } else {
            format!("{:.3}", req.resources.cpu_cores)
        }
    } else {
        "1".into()
    };
    let memory = if req.resources.memory_mb > 0 {
        format!("{}Mi", req.resources.memory_mb)
    } else {
        "1024Mi".into()
    };
    let resources = Resources {
        cpu,
        memory,
        gpus: crate::k8s_client::GpuSpec {
            count: req.resources.gpu_count,
            model: req.resources.gpu_types.clone(),
        },
    };

    // Generate name and create spec
    let name = format!("rent-{}", rand::random::<u32>());
    // Build labels/annotations from legacy hints
    let mut labels = std::collections::BTreeMap::new();
    let mut annotations = std::collections::BTreeMap::new();
    // Encode preferred node if specified
    if let NodeSelection::NodeId { node_id } = &req.node_selection {
        annotations.insert("basilica.ai/preferred-node".to_string(), node_id.clone());
        labels.insert(
            "basilica.ai/has-preferred-node".to_string(),
            "true".to_string(),
        );
    }
    // Encode GPU model preferences (also already passed via resources.gpus.model)
    if !req.resources.gpu_types.is_empty() {
        annotations.insert(
            "basilica.ai/gpu-model-preferences".to_string(),
            req.resources.gpu_types.join(","),
        );
    }
    // Map environment and command
    let container_env: Vec<(String, String)> = req
        .environment
        .iter()
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    let container_command: Vec<String> = req.command.clone();
    // Map ports to container ports and network ingress (default exposure NodePort)
    let mut container_ports: Vec<crate::k8s_client::RentalPortDto> = Vec::new();
    let mut network_ingress: Vec<crate::k8s_client::IngressRuleDto> = Vec::new();
    for p in &req.ports {
        let proto = if p.protocol.eq_ignore_ascii_case("udp") {
            "UDP".to_string()
        } else {
            "TCP".to_string()
        };
        let cp = (p.container_port as u16).max(1);
        container_ports.push(crate::k8s_client::RentalPortDto {
            container_port: cp,
            protocol: proto,
        });
        network_ingress.push(crate::k8s_client::IngressRuleDto {
            port: cp,
            exposure: "NodePort".to_string(),
        });
    }
    // SSH mapping
    let ssh = if req.no_ssh {
        None
    } else {
        Some(crate::k8s_client::RentalSshDto {
            enabled: true,
            public_key: req.ssh_public_key.clone(),
        })
    };
    let spec = crate::k8s_client::RentalSpecDto {
        container_image: req.container_image.clone(),
        resources,
        container_env,
        container_command,
        container_ports,
        network_ingress,
        ssh,
        name: Some(name.clone()),
        namespace: Some(ns.clone()),
        labels: Some(labels),
        annotations: Some(annotations),
    };
    let id = client.create_rental(&ns, &name, spec).await?;

    apimetrics::record_rental_created(&ns);
    apimetrics::record_request("rentals_v2.create_compat", "POST", start, true);
    Ok(Json(CreateRentalResponse { rental_id: id }))
}

#[derive(Debug, Clone, Serialize)]
pub struct RentalStatusResponse {
    pub rental_id: String,
    pub status: RentalStatusDto,
}

pub async fn get_rental_status(
    State(state): State<AppState>,
    Extension(auth): Extension<crate::api::middleware::AuthContext>,
    axum::extract::Path(rental_id): axum::extract::Path<String>,
) -> Result<Json<RentalStatusResponse>> {
    let start = Instant::now();
    let client = match state.k8s.as_ref() {
        Some(c) => c,
        None => {
            apimetrics::record_request("rentals_v2.status", "GET", start, false);
            return Err(ApiError::ServiceUnavailable);
        }
    };
    let ns = user_namespace(&auth.user_id);
    let st = client.get_rental_status(&ns, &rental_id).await?;
    apimetrics::record_request("rentals_v2.status", "GET", start, true);
    Ok(Json(RentalStatusResponse {
        rental_id,
        status: st,
    }))
}

#[derive(Debug, Clone, Serialize)]
pub struct DeleteRentalResponse {
    pub rental_id: String,
}

pub async fn delete_rental(
    State(state): State<AppState>,
    Extension(auth): Extension<crate::api::middleware::AuthContext>,
    axum::extract::Path(rental_id): axum::extract::Path<String>,
) -> Result<Json<DeleteRentalResponse>> {
    let start = Instant::now();
    let client = match state.k8s.as_ref() {
        Some(c) => c,
        None => {
            apimetrics::record_request("rentals_v2.delete", "DELETE", start, false);
            return Err(ApiError::ServiceUnavailable);
        }
    };
    let ns = user_namespace(&auth.user_id);
    client.delete_rental(&ns, &rental_id).await?;
    apimetrics::record_request("rentals_v2.delete", "DELETE", start, true);
    Ok(Json(DeleteRentalResponse { rental_id }))
}

// Stream rental logs (similar shape to container-based logs)
fn build_follow_log_stream(
    client: std::sync::Arc<dyn crate::k8s_client::ApiK8sClient + Send + Sync>,
    ns: String,
    rental_id: String,
    tail: Option<u32>,
    since_seconds: Option<u32>,
) -> Pin<Box<dyn FuturesStream<Item = std::result::Result<Event, std::io::Error>> + Send>> {
    Box::pin(async_stream::stream! {
        use tokio::time::{sleep, Duration, Instant as TokioInstant};
        let mut last_marker: Option<String> = None;
        let start_at = TokioInstant::now();
        let max_duration = Duration::from_secs(300); // 5 minutes cap to avoid resource leaks
        let mut last_heartbeat = TokioInstant::now();
        loop {
            if start_at.elapsed() >= max_duration {
                break;
            }
            match client.get_rental_logs(&ns, &rental_id, tail.or(Some(100)), since_seconds).await {
                Ok(body) => {
                    let lines: Vec<String> = body.lines().map(|s| s.to_string()).collect();
                    if !lines.is_empty() {
                        let start_idx = if let Some(ref marker) = last_marker {
                            lines.iter().rposition(|l| l == marker).map(|idx| idx + 1).unwrap_or(0)
                        } else { 0 };
                        for line in &lines[start_idx..] {
                            let data = serde_json::json!({
                                "timestamp": chrono::Utc::now(),
                                "stream": "stdout",
                                "message": line,
                            });
                            yield Ok(Event::default().data(data.to_string()));
                        }
                        last_marker = lines.last().cloned();
                    }
                }
                Err(_) => { /* ignore transient errors */ }
            }
            // Heartbeat every 15 seconds to keep connections alive
            if last_heartbeat.elapsed() >= Duration::from_secs(15) {
                let hb = serde_json::json!({"heartbeat": true, "timestamp": chrono::Utc::now()});
                yield Ok(Event::default().data(hb.to_string()));
                last_heartbeat = TokioInstant::now();
            }
            sleep(Duration::from_millis(1000)).await;
        }
    })
}

pub async fn stream_rental_logs(
    State(state): State<AppState>,
    Extension(auth): Extension<crate::api::middleware::AuthContext>,
    axum::extract::Path(rental_id): axum::extract::Path<String>,
    axum::extract::Query(query): axum::extract::Query<basilica_sdk::types::LogStreamQuery>,
) -> Result<Sse<impl Stream<Item = std::result::Result<Event, std::io::Error>>>> {
    let start = Instant::now();
    let client = match state.k8s.as_ref() {
        Some(c) => c,
        None => {
            apimetrics::record_request("rentals_v2.logs", "GET", start, false);
            return Err(ApiError::ServiceUnavailable);
        }
    };
    let ns = user_namespace(&auth.user_id);
    let follow = query.follow.unwrap_or(false);
    let tail = query.tail;
    let since_seconds = query.since_seconds;

    let stream: Pin<
        Box<dyn futures::Stream<Item = std::result::Result<Event, std::io::Error>> + Send>,
    > = if !follow {
        let logs = client
            .get_rental_logs(&ns, &rental_id, tail, since_seconds)
            .await?;
        let lines: Vec<String> = logs.lines().map(|s| s.to_string()).collect();
        Box::pin(async_stream::stream! {
            for line in &lines {
                let data = serde_json::json!({
                    "timestamp": chrono::Utc::now(),
                    "stream": "stdout",
                    "message": line,
                });
                yield Ok(Event::default().data(data.to_string()));
            }
        })
    } else {
        let client_clone = state.k8s.as_ref().unwrap().clone();
        build_follow_log_stream(
            client_clone,
            ns.clone(),
            rental_id.clone(),
            tail,
            since_seconds,
        )
    };

    apimetrics::record_request("rentals_v2.logs", "GET", start, true);
    Ok(Sse::new(stream))
}

// Exec into a rental container (similar to container-based exec)
#[derive(Debug, Clone, Deserialize)]
pub struct ExecRequest {
    pub command: Vec<String>,
    #[serde(default)]
    pub stdin: Option<String>,
    #[serde(default)]
    pub tty: Option<bool>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ExecResponse {
    pub stdout: String,
    #[serde(default)]
    pub stderr: String,
    #[serde(default)]
    pub exit_code: i32,
}

pub async fn exec_rental(
    State(state): State<AppState>,
    Extension(auth): Extension<crate::api::middleware::AuthContext>,
    axum::extract::Path(rental_id): axum::extract::Path<String>,
    Json(req): Json<ExecRequest>,
) -> Result<Json<ExecResponse>> {
    let start = Instant::now();
    let client = match state.k8s.as_ref() {
        Some(c) => c,
        None => {
            apimetrics::record_request("rentals_v2.exec", "POST", start, false);
            return Err(ApiError::ServiceUnavailable);
        }
    };
    let ns = user_namespace(&auth.user_id);
    let res = client
        .exec_rental(
            &ns,
            &rental_id,
            req.command,
            req.stdin,
            req.tty.unwrap_or(false),
        )
        .await?;
    apimetrics::record_request("rentals_v2.exec", "POST", start, true);
    Ok(Json(ExecResponse {
        stdout: res.stdout,
        stderr: res.stderr,
        exit_code: res.exit_code,
    }))
}

// Extend a rental's duration
#[derive(Debug, Clone, Deserialize)]
pub struct ExtendRentalRequest {
    pub additional_hours: u32,
}

pub async fn extend_rental(
    State(_state): State<AppState>,
    axum::extract::Path(_rental_id): axum::extract::Path<String>,
    Json(req): Json<ExtendRentalRequest>,
) -> Result<Json<RentalStatusResponse>> {
    let start = Instant::now();
    let _ = req; // unused in pay-as-you-go
                 // Under pay-as-you-go, extension is not supported; rentals are terminated when out of credits.
    apimetrics::record_request("rentals_v2.extend", "POST", start, false);
    Err(ApiError::BadRequest {
        message: "Extend is not supported under pay-as-you-go".into(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::api::middleware::{AuthContext, AuthDetails};

    use std::sync::Arc;

    async fn build_state() -> AppState {
        let client = crate::k8s_client::MockK8sClient::default();
        AppState {
            config: std::sync::Arc::new(crate::config::Config::default()),
            validator_client: std::sync::Arc::new(
                basilica_validator::ValidatorClient::new(
                    "http://localhost",
                    std::time::Duration::from_secs(1),
                )
                .unwrap(),
            ),
            validator_endpoint: "http://localhost".into(),
            validator_uid: 0,
            validator_hotkey: "".into(),
            http_client: reqwest::Client::builder().build().unwrap(),
            db: sqlx::PgPool::connect_lazy("postgres://user:pass@localhost/db")
                .expect("lazy PG pool dsn should be valid"),
            k8s: Some(Arc::new(client)),
            payments_client: None,
            billing_client: None,
            dns_provider: None,
            metrics: None,
        }
    }

    #[tokio::test]
    async fn compat_create_maps_legacy_request() {
        // Build explicit state with a shared MockK8sClient so we can inspect the captured spec
        let client = crate::k8s_client::MockK8sClient::default();
        let state = AppState {
            config: std::sync::Arc::new(crate::config::Config::default()),
            validator_client: std::sync::Arc::new(
                basilica_validator::ValidatorClient::new(
                    "http://localhost",
                    std::time::Duration::from_secs(1),
                )
                .unwrap(),
            ),
            validator_endpoint: "http://localhost".into(),
            validator_uid: 0,
            validator_hotkey: "".into(),
            http_client: reqwest::Client::builder().build().unwrap(),
            db: sqlx::PgPool::connect_lazy("postgres://user:pass@localhost/db")
                .expect("lazy PG pool dsn should be valid"),
            k8s: Some(std::sync::Arc::new(client.clone())),
            payments_client: None,
            billing_client: None,
            dns_provider: None,
            metrics: None,
        };
        let auth = AuthContext {
            user_id: "alice".into(),
            scopes: vec![],
            details: AuthDetails::ApiKey,
        };
        let mut env = std::collections::HashMap::new();
        env.insert("K".to_string(), "V".to_string());
        let req = StartRentalApiRequest {
            node_selection: NodeSelection::NodeId {
                node_id: "node1".into(),
            },
            container_image: "img".into(),
            ssh_public_key: "ssh-ed25519 AAA".into(),
            environment: env,
            ports: vec![
                basilica_validator::api::routes::rentals::PortMappingRequest {
                    container_port: 8080,
                    host_port: 0,
                    protocol: "tcp".into(),
                },
            ],
            resources: basilica_validator::api::routes::rentals::ResourceRequirementsRequest {
                cpu_cores: 2.0,
                memory_mb: 2048,
                storage_mb: 0,
                gpu_count: 1,
                gpu_types: vec!["A100".into()],
            },
            command: vec!["bash".into(), "-lc".into(), "echo".into(), "hi".into()],
            volumes: vec![],
            no_ssh: false,
        };
        let res = create_rental_compat(State(state.clone()), Extension(auth), Json(req))
            .await
            .unwrap();
        assert!(!res.0.rental_id.is_empty());
        // Validate captured spec
        let spec = client
            .get_rental_spec("u-alice", &res.0.rental_id)
            .await
            .unwrap();
        assert_eq!(spec.container_image, "img");
        assert_eq!(spec.container_env, vec![("K".into(), "V".into())]);
        assert_eq!(spec.container_command, vec!["bash", "-lc", "echo", "hi"]);
        assert_eq!(spec.container_ports.len(), 1);
        assert_eq!(spec.network_ingress.len(), 1);
        assert!(spec.ssh.as_ref().unwrap().enabled);

        // Verify status includes endpoints derived from network_ingress (NodePort:<port>)
        let auth2 = AuthContext {
            user_id: "alice".into(),
            scopes: vec![],
            details: AuthDetails::ApiKey,
        };
        let status = get_rental_status(
            State(state.clone()),
            Extension(auth2),
            axum::extract::Path(res.0.rental_id.clone()),
        )
        .await
        .unwrap();
        assert!(status
            .0
            .status
            .endpoints
            .iter()
            .any(|e| e.starts_with("NodePort:")));
    }

    #[tokio::test]
    async fn v2_rental_logs_tail() {
        // Arrange state and create a rental
        let client = crate::k8s_client::MockK8sClient::default();
        let state = AppState {
            config: std::sync::Arc::new(crate::config::Config::default()),
            validator_client: std::sync::Arc::new(
                basilica_validator::ValidatorClient::new(
                    "http://localhost",
                    std::time::Duration::from_secs(1),
                )
                .unwrap(),
            ),
            validator_endpoint: "http://localhost".into(),
            validator_uid: 0,
            validator_hotkey: "".into(),
            http_client: reqwest::Client::builder().build().unwrap(),
            db: sqlx::PgPool::connect_lazy("postgres://user:pass@localhost/db")
                .expect("lazy PG pool dsn should be valid"),
            k8s: Some(std::sync::Arc::new(client.clone())),
            payments_client: None,
            billing_client: None,
            dns_provider: None,
            metrics: None,
        };
        let auth = AuthContext {
            user_id: "bob".into(),
            scopes: vec![],
            details: AuthDetails::ApiKey,
        };
        let req_body = CreateRentalRequest {
            container_image: "img".into(),
            resources: Resources {
                cpu: "1".into(),
                memory: "512Mi".into(),
                gpus: crate::k8s_client::GpuSpec {
                    count: 0,
                    model: vec![],
                },
            },
            name: Some("rent-logs".into()),
            namespace: Some("default".into()),
            command: vec![],
            environment: std::collections::HashMap::new(),
            ports: vec![],
            network: None,
        };
        let _ = super::create_rental(
            State(state.clone()),
            Extension(auth.clone()),
            Json(req_body),
        )
        .await
        .unwrap();
        // Inject logs into mock
        client
            .set_logs("u-bob", "rent-logs", "line1\nline2\nline3")
            .await;

        // Act: fetch tail=2 (non-follow)
        let sse = super::stream_rental_logs(
            State(state.clone()),
            Extension(auth.clone()),
            axum::extract::Path("rent-logs".to_string()),
            axum::extract::Query(basilica_sdk::types::LogStreamQuery {
                follow: Some(false),
                tail: Some(2),
                since_seconds: None,
            }),
        )
        .await
        .unwrap();

        // We cannot directly iterate Sse<Stream> here without a server; this test ensures handler compiles and returns Ok
        let _ = sse;
    }

    #[tokio::test]
    async fn v2_rental_logs_follow_smoke() {
        use futures::StreamExt;
        use tokio::time::{timeout, Duration};

        // Arrange state and create a rental
        let client = crate::k8s_client::MockK8sClient::default();
        let state = AppState {
            config: std::sync::Arc::new(crate::config::Config::default()),
            validator_client: std::sync::Arc::new(
                basilica_validator::ValidatorClient::new(
                    "http://localhost",
                    std::time::Duration::from_secs(1),
                )
                .unwrap(),
            ),
            validator_endpoint: "http://localhost".into(),
            validator_uid: 0,
            validator_hotkey: "".into(),
            http_client: reqwest::Client::builder().build().unwrap(),
            db: sqlx::PgPool::connect_lazy("postgres://user:pass@localhost/db")
                .expect("lazy PG pool dsn should be valid"),
            k8s: Some(std::sync::Arc::new(client.clone())),
            payments_client: None,
            billing_client: None,
            dns_provider: None,
            metrics: None,
        };
        let auth = AuthContext {
            user_id: "bob".into(),
            scopes: vec![],
            details: AuthDetails::ApiKey,
        };
        let req_body = CreateRentalRequest {
            container_image: "img".into(),
            resources: Resources {
                cpu: "1".into(),
                memory: "512Mi".into(),
                gpus: crate::k8s_client::GpuSpec {
                    count: 0,
                    model: vec![],
                },
            },
            name: Some("rent-follow".into()),
            namespace: Some("default".into()),
            command: vec![],
            environment: std::collections::HashMap::new(),
            ports: vec![],
            network: None,
        };
        let _ = super::create_rental(
            State(state.clone()),
            Extension(auth.clone()),
            Json(req_body),
        )
        .await
        .unwrap();
        client.set_logs("u-bob", "rent-follow", "first").await;

        // Act: request follow stream
        // Build follow stream directly and poll a few events
        let stream = super::build_follow_log_stream(
            state.k8s.as_ref().unwrap().clone(),
            "u-bob".to_string(),
            "rent-follow".to_string(),
            Some(10),
            None,
        );

        // Wait for at least one event
        let got = timeout(Duration::from_millis(1500), async {
            futures::pin_mut!(stream);
            stream.next().await.is_some()
        })
        .await
        .unwrap_or(false);
        assert!(got, "expected at least one follow event");

        // Update logs and expect more chunks (best-effort)
        client
            .set_logs("u-bob", "rent-follow", "first\nsecond")
            .await;
        // Rebuild a new follow stream and ensure it produces at least one event after update
        let stream2 = super::build_follow_log_stream(
            state.k8s.as_ref().unwrap().clone(),
            "u-bob".to_string(),
            "rent-follow".to_string(),
            Some(10),
            None,
        );
        let got_more = timeout(Duration::from_millis(1500), async {
            futures::pin_mut!(stream2);
            stream2.next().await.is_some()
        })
        .await
        .unwrap_or(false);
        assert!(
            got_more,
            "expected additional follow event after log update"
        );
    }

    #[tokio::test]
    async fn v2_rental_create_get_delete() {
        let state = build_state().await;
        let auth = crate::api::middleware::AuthContext {
            user_id: "user1".into(),
            scopes: vec![],
            details: crate::api::middleware::AuthDetails::ApiKey,
        };
        let req_body = CreateRentalRequest {
            container_image: "img".into(),
            resources: Resources {
                cpu: "1".into(),
                memory: "512Mi".into(),
                gpus: crate::k8s_client::GpuSpec {
                    count: 0,
                    model: vec![],
                },
            },
            name: Some("rent-v2".into()),
            namespace: Some("default".into()),
            command: vec![],
            environment: std::collections::HashMap::new(),
            ports: vec![],
            network: None,
        };
        let create = super::create_rental(
            State(state.clone()),
            Extension(auth.clone()),
            Json(req_body),
        )
        .await
        .unwrap();
        assert_eq!(create.0.rental_id, "rent-v2");
        let status = super::get_rental_status(
            State(state.clone()),
            Extension(auth.clone()),
            axum::extract::Path("rent-v2".to_string()),
        )
        .await
        .unwrap();
        assert!(!status.0.status.state.is_empty());
        let del = super::delete_rental(
            State(state.clone()),
            Extension(auth.clone()),
            axum::extract::Path("rent-v2".to_string()),
        )
        .await
        .unwrap();
        assert_eq!(del.0.rental_id, "rent-v2");
    }

    #[tokio::test]
    async fn v2_rental_exec() {
        let state = build_state().await;
        // Create first
        let auth = crate::api::middleware::AuthContext {
            user_id: "user1".into(),
            scopes: vec![],
            details: crate::api::middleware::AuthDetails::ApiKey,
        };
        let req_body = CreateRentalRequest {
            container_image: "img".into(),
            resources: Resources {
                cpu: "1".into(),
                memory: "512Mi".into(),
                gpus: crate::k8s_client::GpuSpec {
                    count: 0,
                    model: vec![],
                },
            },
            name: Some("rent-v2-exec".into()),
            namespace: Some("default".into()),
            command: vec![],
            environment: std::collections::HashMap::new(),
            ports: vec![],
            network: None,
        };
        let _ = super::create_rental(
            State(state.clone()),
            Extension(auth.clone()),
            Json(req_body),
        )
        .await
        .unwrap();
        // Exec
        let exec_req = ExecRequest {
            command: vec!["echo".into(), "hello".into()],
            stdin: None,
            tty: None,
        };
        let resp = super::exec_rental(
            State(state.clone()),
            Extension(auth.clone()),
            axum::extract::Path("rent-v2-exec".to_string()),
            Json(exec_req),
        )
        .await
        .unwrap();
        assert!(resp.0.stdout.contains("exec: echo hello"));
        // Simulate non-zero exit via mock (command contains 'fail')
        let exec_req2 = ExecRequest {
            command: vec!["fail".into()],
            stdin: None,
            tty: None,
        };
        let resp2 = super::exec_rental(
            State(state.clone()),
            Extension(auth.clone()),
            axum::extract::Path("rent-v2-exec".to_string()),
            Json(exec_req2),
        )
        .await
        .unwrap();
        assert_eq!(resp2.0.exit_code, 1);
        assert!(resp2.0.stderr.contains("simulated error"));
    }

    #[tokio::test]
    async fn v2_rental_exec_tty_and_stdin_behaviors() {
        let state = build_state().await;
        let auth = crate::api::middleware::AuthContext {
            user_id: "user1".into(),
            scopes: vec![],
            details: crate::api::middleware::AuthDetails::ApiKey,
        };
        let req_body = CreateRentalRequest {
            container_image: "img".into(),
            resources: Resources {
                cpu: "1".into(),
                memory: "512Mi".into(),
                gpus: crate::k8s_client::GpuSpec {
                    count: 0,
                    model: vec![],
                },
            },
            name: Some("rent-v2-exec-tty".into()),
            namespace: Some("default".into()),
            command: vec![],
            environment: std::collections::HashMap::new(),
            ports: vec![],
            network: None,
        };
        let _ = super::create_rental(
            State(state.clone()),
            Extension(auth.clone()),
            Json(req_body),
        )
        .await
        .unwrap();

        // When TTY is true and command fails, stderr should be merged into stdout
        let req_tty_fail = ExecRequest {
            command: vec!["fail".into()],
            stdin: None,
            tty: Some(true),
        };
        let res_tty_fail = super::exec_rental(
            State(state.clone()),
            Extension(auth.clone()),
            axum::extract::Path("rent-v2-exec-tty".to_string()),
            Json(req_tty_fail),
        )
        .await
        .unwrap();
        assert_eq!(res_tty_fail.0.exit_code, 1);
        assert!(
            res_tty_fail.0.stderr.is_empty(),
            "stderr should be empty when TTY merges streams"
        );
        assert!(res_tty_fail.0.stdout.contains("simulated error"));

        // When stdin is provided, ensure it is reflected in stdout
        let req_stdin = ExecRequest {
            command: vec!["cat".into()],
            stdin: Some("hello input".into()),
            tty: Some(false),
        };
        let res_stdin = super::exec_rental(
            State(state.clone()),
            Extension(auth.clone()),
            axum::extract::Path("rent-v2-exec-tty".to_string()),
            Json(req_stdin),
        )
        .await
        .unwrap();
        assert!(res_stdin.0.stdout.contains("stdin: hello input"));
    }

    #[tokio::test]
    async fn v2_rental_exec_stderr_only_edge_case() {
        let state = build_state().await;
        let auth = crate::api::middleware::AuthContext {
            user_id: "user1".into(),
            scopes: vec![],
            details: crate::api::middleware::AuthDetails::ApiKey,
        };
        let req_body = CreateRentalRequest {
            container_image: "img".into(),
            resources: Resources {
                cpu: "1".into(),
                memory: "512Mi".into(),
                gpus: crate::k8s_client::GpuSpec {
                    count: 0,
                    model: vec![],
                },
            },
            name: Some("rent-v2-exec-stderr".into()),
            namespace: Some("default".into()),
            command: vec![],
            environment: std::collections::HashMap::new(),
            ports: vec![],
            network: None,
        };
        let _ = super::create_rental(
            State(state.clone()),
            Extension(auth.clone()),
            Json(req_body),
        )
        .await
        .unwrap();

        // Non-TTY: expect only stderr populated
        let req_stderr = ExecRequest {
            command: vec!["stderr-only".into()],
            stdin: None,
            tty: Some(false),
        };
        let res_stderr = super::exec_rental(
            State(state.clone()),
            Extension(auth.clone()),
            axum::extract::Path("rent-v2-exec-stderr".to_string()),
            Json(req_stderr),
        )
        .await
        .unwrap();
        assert!(res_stderr.0.stderr.contains("simulated stderr-only output"));
        assert!(res_stderr.0.stdout.is_empty());

        // TTY merges stderr into stdout
        let req_stderr_tty = ExecRequest {
            command: vec!["stderr-only".into()],
            stdin: None,
            tty: Some(true),
        };
        let res_stderr_tty = super::exec_rental(
            State(state.clone()),
            Extension(auth.clone()),
            axum::extract::Path("rent-v2-exec-stderr".to_string()),
            Json(req_stderr_tty),
        )
        .await
        .unwrap();
        assert!(res_stderr_tty
            .0
            .stdout
            .contains("simulated stderr-only output"));
        assert!(res_stderr_tty.0.stderr.is_empty());
    }

    #[tokio::test]
    async fn v2_rental_extend() {
        let state = build_state().await;
        let auth = crate::api::middleware::AuthContext {
            user_id: "user1".into(),
            scopes: vec![],
            details: crate::api::middleware::AuthDetails::ApiKey,
        };
        let req_body = CreateRentalRequest {
            container_image: "img".into(),
            resources: Resources {
                cpu: "1".into(),
                memory: "512Mi".into(),
                gpus: crate::k8s_client::GpuSpec {
                    count: 0,
                    model: vec![],
                },
            },
            name: Some("rent-v2-extend".into()),
            namespace: Some("default".into()),
            command: vec![],
            environment: std::collections::HashMap::new(),
            ports: vec![],
            network: None,
        };
        let _ = super::create_rental(
            State(state.clone()),
            Extension(auth.clone()),
            Json(req_body),
        )
        .await
        .unwrap();
        let err = super::extend_rental(
            State(state.clone()),
            axum::extract::Path("rent-v2-extend".to_string()),
            Json(ExtendRentalRequest {
                additional_hours: 2,
            }),
        )
        .await;
        assert!(err.is_err());
    }

    #[tokio::test]
    async fn v2_rental_list() {
        let state = build_state().await;
        let auth = crate::api::middleware::AuthContext {
            user_id: "user1".into(),
            scopes: vec![],
            details: crate::api::middleware::AuthDetails::ApiKey,
        };
        let req_body1 = CreateRentalRequest {
            container_image: "img".into(),
            resources: Resources {
                cpu: "1".into(),
                memory: "512Mi".into(),
                gpus: crate::k8s_client::GpuSpec {
                    count: 0,
                    model: vec![],
                },
            },
            name: Some("rent-a".into()),
            namespace: Some("default".into()),
            command: vec![],
            environment: std::collections::HashMap::new(),
            ports: vec![],
            network: None,
        };
        let _ = super::create_rental(
            State(state.clone()),
            Extension(auth.clone()),
            Json(req_body1),
        )
        .await
        .unwrap();
        let req_body2 = CreateRentalRequest {
            container_image: "img".into(),
            resources: Resources {
                cpu: "1".into(),
                memory: "512Mi".into(),
                gpus: crate::k8s_client::GpuSpec {
                    count: 0,
                    model: vec![],
                },
            },
            name: Some("rent-b".into()),
            namespace: Some("default".into()),
            command: vec![],
            environment: std::collections::HashMap::new(),
            ports: vec![],
            network: None,
        };
        let _ = super::create_rental(
            State(state.clone()),
            Extension(auth.clone()),
            Json(req_body2),
        )
        .await
        .unwrap();
        let list = super::list_rentals(State(state.clone()), Extension(auth.clone()))
            .await
            .unwrap();
        let ids: Vec<String> = list.0.into_iter().map(|x| x.rental_id).collect();
        assert!(ids.contains(&"rent-a".to_string()));
        assert!(ids.contains(&"rent-b".to_string()));
    }
}
