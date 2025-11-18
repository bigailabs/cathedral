use super::config::K3sSshConfig;
use super::types::{K3sServer, TokenResponse};
use crate::error::ApiError;
use async_ssh2_tokio::client::{AuthMethod, Client, ServerCheckMethod};
use base64::Engine;
use once_cell::sync::Lazy;
use regex::Regex;
use scrypt::Params;
use std::path::PathBuf;
use std::time::Duration;
use tracing::{info, warn};

#[allow(dead_code)]
pub(super) const MAX_DESCRIPTION_LENGTH: usize = 200;
const MIN_TOKEN_ID_LENGTH: usize = 6;
const MAX_TOKEN_ID_LENGTH: usize = 16;
pub(super) const K3S_TOKEN_PREFIX: &str = "K10";

#[allow(dead_code)]
pub(super) static TTL_REGEX: Lazy<Regex> = Lazy::new(|| Regex::new(r"^\d+(h|m|s)$").unwrap());
pub(super) static TOKEN_FORMAT_REGEX: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"^K10[^:]+::(.+\..+|server:.+)$").unwrap());

pub struct K3sSshClient {
    servers: Vec<K3sServer>,
    ssh_key_path: PathBuf,
    #[allow(dead_code)]
    known_hosts_path: Option<PathBuf>,
    username: String,
    timeout: Duration,
    enabled: bool,
}

impl K3sSshClient {
    pub fn new(config: &K3sSshConfig) -> Result<Self, ApiError> {
        if !config.enabled {
            return Ok(Self::disabled());
        }

        if config.servers.is_empty() {
            return Err(ApiError::ConfigError(
                "No K3s servers configured for SSH".into(),
            ));
        }

        let ssh_key_path = PathBuf::from(&config.ssh_key_path);
        if !ssh_key_path.exists() {
            return Err(ApiError::ConfigError(format!(
                "SSH key not found at {}",
                ssh_key_path.display()
            )));
        }

        let known_hosts_path = config.known_hosts_path.as_ref().map(PathBuf::from);

        Ok(Self {
            servers: config.servers.clone(),
            ssh_key_path,
            known_hosts_path,
            username: config.username.clone(),
            timeout: Duration::from_secs(config.timeout_secs),
            enabled: true,
        })
    }

    pub fn disabled() -> Self {
        Self {
            servers: vec![],
            ssh_key_path: PathBuf::new(),
            known_hosts_path: None,
            username: String::new(),
            timeout: Duration::from_secs(30),
            enabled: false,
        }
    }

    pub fn is_enabled(&self) -> bool {
        self.enabled
    }

    async fn connect_to_server(&self, server: &K3sServer) -> Result<Client, ApiError> {
        info!(
            server = %server.host,
            port = %server.port,
            username = %self.username,
            ssh_key_path = %self.ssh_key_path.display(),
            "Attempting SSH connection to K3s server"
        );

        let auth = AuthMethod::with_key_file(&self.ssh_key_path, None);

        let connect_result = tokio::time::timeout(
            self.timeout,
            Client::connect(
                (server.host.as_str(), server.port),
                &self.username,
                auth,
                ServerCheckMethod::NoCheck,
            ),
        )
        .await;

        match connect_result {
            Ok(Ok(client)) => {
                info!(
                    server = %server.host,
                    "SSH connection established successfully"
                );
                Ok(client)
            }
            Ok(Err(e)) => {
                warn!(
                    server = %server.host,
                    error = %e,
                    "SSH connection failed"
                );
                Err(ApiError::SshConnectionFailed(
                    server.host.clone(),
                    e.to_string(),
                ))
            }
            Err(_) => {
                warn!(
                    server = %server.host,
                    timeout_secs = ?self.timeout,
                    "SSH connection timed out"
                );
                Err(ApiError::SshCommandTimeout)
            }
        }
    }

    pub async fn create_token(
        &self,
        node_id: &str,
        datacenter_id: &str,
        ttl: &str,
    ) -> Result<TokenResponse, ApiError> {
        if !self.enabled {
            return Err(ApiError::ConfigError(
                "SSH token creation is disabled".into(),
            ));
        }

        let description = format!("GPU node {} for datacenter {}", node_id, datacenter_id);

        let mut last_error = None;
        for server in &self.servers {
            match self
                .try_create_token_and_node_password(server, node_id, ttl, &description)
                .await
            {
                Ok(response) => {
                    info!(
                        node_id = %node_id,
                        server = %server.host,
                        "Successfully retrieved K3s server token and created node password via SSH"
                    );
                    return Ok(response);
                }
                Err(e) => {
                    warn!(
                        node_id = %node_id,
                        server = %server.host,
                        error = %e,
                        "Failed to create token on server"
                    );
                    last_error = Some(e);
                    continue;
                }
            }
        }

        Err(last_error.unwrap_or_else(|| ApiError::NoK3sServersAvailable))
    }

    async fn try_create_token_and_node_password(
        &self,
        server: &K3sServer,
        node_id: &str,
        _ttl: &str,
        _description: &str,
    ) -> Result<TokenResponse, ApiError> {
        let token_response = self.get_server_token(server).await?;

        let datacenter_id = _description
            .split("datacenter ")
            .nth(1)
            .unwrap_or("unknown");
        let node_password = self
            .create_node_password_secret(server, node_id, datacenter_id)
            .await?;

        Ok(TokenResponse::with_node_password(
            token_response.token,
            token_response.token_id,
            node_password,
        ))
    }

    async fn create_node_password_secret(
        &self,
        server: &K3sServer,
        node_id: &str,
        datacenter_id: &str,
    ) -> Result<String, ApiError> {
        use rand::Rng;

        let (password, password_hash) = {
            let mut rng = rand::thread_rng();
            let password_bytes: [u8; 32] = rng.gen();
            let password = base64::engine::general_purpose::STANDARD.encode(password_bytes);

            let mut salt_bytes = [0u8; 8];
            rng.fill(&mut salt_bytes);

            let params = Params::new(15, 8, 1, 64)
                .map_err(|e| ApiError::ConfigError(format!("Invalid scrypt params: {}", e)))?;

            let mut hash_output = [0u8; 64];
            scrypt::scrypt(password.as_bytes(), &salt_bytes, &params, &mut hash_output)
                .map_err(|e| ApiError::ConfigError(format!("Failed to hash password: {}", e)))?;

            let salt_hex = hex::encode(salt_bytes);
            let hash_b64 = base64::engine::general_purpose::STANDARD
                .encode(hash_output)
                .trim_end_matches('=')
                .to_string();
            let password_hash = format!("\\$1:{}:15:8:1:{}", salt_hex, hash_b64);

            (password, password_hash)
        };

        let client = self.connect_to_server(server).await?;

        let created_at = chrono::Utc::now().to_rfc3339();
        let expires_at = (chrono::Utc::now() + chrono::Duration::days(30)).to_rfc3339();

        let command = format!(
            r#"sudo k3s kubectl create secret generic '{}.node-password.k3s' -n kube-system --from-literal="hash={}" --dry-run=client -o yaml | sudo k3s kubectl apply -f - && \
            sudo k3s kubectl label secret '{}.node-password.k3s' -n kube-system \
              basilica.ai/managed-by=basilica-api \
              'basilica.ai/node-id={}' \
              --overwrite && \
            sudo k3s kubectl annotate secret '{}.node-password.k3s' -n kube-system \
              'basilica.ai/datacenter-id={}' \
              basilica.ai/created-at='{}' \
              basilica.ai/expires-at='{}' \
              --overwrite"#,
            node_id,
            password_hash,
            node_id,
            node_id,
            node_id,
            datacenter_id,
            created_at,
            expires_at
        );

        info!(
            server = %server.host,
            node_id = %node_id,
            datacenter_id = %datacenter_id,
            "Creating node password secret with scrypt hash, labels, and annotations"
        );

        let result = tokio::time::timeout(self.timeout, client.execute(&command))
            .await
            .map_err(|_| ApiError::SshCommandTimeout)?
            .map_err(|e| ApiError::SshCommandFailed(e.to_string()))?;

        if result.exit_status != 0 {
            return Err(ApiError::K3sTokenCreationFailed {
                stderr: result.stderr,
                exit_code: result.exit_status,
            });
        }

        info!(
            server = %server.host,
            node_id = %node_id,
            "Node password secret created successfully"
        );

        Ok(password)
    }

    async fn get_server_token(&self, server: &K3sServer) -> Result<TokenResponse, ApiError> {
        let client = self.connect_to_server(server).await?;

        let command = "sudo cat /var/lib/rancher/k3s/server/token";

        info!(
            server = %server.host,
            "Retrieving K3s server token"
        );

        let result = tokio::time::timeout(self.timeout, client.execute(command))
            .await
            .map_err(|_| ApiError::SshCommandTimeout)?
            .map_err(|e| ApiError::SshCommandFailed(e.to_string()))?;

        info!(
            server = %server.host,
            exit_code = result.exit_status,
            stdout_len = result.stdout.len(),
            stderr_len = result.stderr.len(),
            "K3s server token retrieval completed"
        );

        if result.exit_status != 0 {
            return Err(ApiError::K3sTokenCreationFailed {
                stderr: result.stderr,
                exit_code: result.exit_status,
            });
        }

        let token = result.stdout.trim().to_string();
        if token.is_empty() {
            return Err(ApiError::K3sTokenCreationFailed {
                stderr: "Server token was empty".to_string(),
                exit_code: 0,
            });
        }

        Self::validate_token_format(&token)?;

        Ok(TokenResponse::new(token, "server".to_string()))
    }

    pub async fn delete_token(&self, token_id: &str) -> Result<(), ApiError> {
        if !self.enabled {
            return Err(ApiError::ConfigError(
                "SSH token deletion is disabled".into(),
            ));
        }

        Self::validate_token_id(token_id)?;

        let mut last_error = None;
        for server in &self.servers {
            match self.try_delete_token_on_server(server, token_id).await {
                Ok(()) => {
                    info!(
                        token_id = %token_id,
                        server = %server.host,
                        "Successfully deleted K3s token via SSH"
                    );
                    return Ok(());
                }
                Err(e) => {
                    warn!(
                        token_id = %token_id,
                        server = %server.host,
                        error = %e,
                        "Failed to delete token on server"
                    );
                    last_error = Some(e);
                    continue;
                }
            }
        }

        Err(last_error.unwrap_or_else(|| ApiError::NoK3sServersAvailable))
    }

    async fn try_delete_token_on_server(
        &self,
        server: &K3sServer,
        token_id: &str,
    ) -> Result<(), ApiError> {
        let client = self.connect_to_server(server).await?;

        let command = format!("sudo k3s token delete '{}'", token_id);

        let result = tokio::time::timeout(self.timeout, client.execute(&command))
            .await
            .map_err(|_| ApiError::SshCommandTimeout)?
            .map_err(|e| ApiError::SshCommandFailed(e.to_string()))?;

        if result.exit_status != 0 {
            if result.stderr.contains("not found") || result.stderr.contains("does not exist") {
                return Ok(());
            }
            return Err(ApiError::K3sTokenDeletionFailed {
                stderr: result.stderr,
                exit_code: result.exit_status,
            });
        }

        Ok(())
    }

    #[allow(dead_code)]
    fn validate_ttl(ttl: &str) -> Result<(), ApiError> {
        if !TTL_REGEX.is_match(ttl) {
            return Err(ApiError::InvalidTtlFormat(ttl.to_string()));
        }
        Ok(())
    }

    #[allow(dead_code)]
    fn validate_description(desc: &str) -> Result<(), ApiError> {
        if desc.contains('\'') || desc.contains('\\') || desc.contains('$') || desc.contains('`') {
            return Err(ApiError::InvalidDescription(
                "Description contains forbidden shell metacharacters".into(),
            ));
        }
        if desc.len() > MAX_DESCRIPTION_LENGTH {
            return Err(ApiError::InvalidDescription(format!(
                "Description too long (max {} chars)",
                MAX_DESCRIPTION_LENGTH
            )));
        }
        Ok(())
    }

    fn validate_token_format(token: &str) -> Result<(), ApiError> {
        if !token.starts_with(K3S_TOKEN_PREFIX) {
            return Err(ApiError::InvalidTokenFormat);
        }
        if !TOKEN_FORMAT_REGEX.is_match(token) {
            return Err(ApiError::InvalidTokenFormat);
        }
        Ok(())
    }

    fn validate_token_id(token_id: &str) -> Result<(), ApiError> {
        if !token_id.chars().all(|c| c.is_alphanumeric() || c == '-') {
            return Err(ApiError::InvalidTokenId);
        }
        if token_id.len() < MIN_TOKEN_ID_LENGTH || token_id.len() > MAX_TOKEN_ID_LENGTH {
            return Err(ApiError::InvalidTokenId);
        }
        Ok(())
    }

    #[allow(dead_code)]
    fn extract_token_id(token: &str) -> Result<String, ApiError> {
        let parts: Vec<&str> = token.split("::").collect();
        if parts.len() != 2 {
            return Err(ApiError::InvalidTokenFormat);
        }

        let credentials = parts[1];
        let token_parts: Vec<&str> = credentials.split('.').collect();

        if token_parts.len() != 2 {
            return Err(ApiError::InvalidTokenFormat);
        }

        let token_id = token_parts[0];
        Self::validate_token_id(token_id)?;

        Ok(token_id.to_string())
    }
}

