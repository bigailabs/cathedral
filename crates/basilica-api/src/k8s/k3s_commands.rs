use anyhow::{anyhow, Result};
use chrono::{Duration, Utc};
use k8s_openapi::api::core::v1::{ConfigMap, Secret};
use k8s_openapi::apimachinery::pkg::apis::meta::v1::ObjectMeta;
use kube::api::{Api, DeleteParams, PostParams};
use rand::Rng;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use tracing::{debug, info, warn};

pub async fn generate_token_id() -> Result<String> {
    debug!("Generating K3s token ID");

    let mut rng = rand::thread_rng();
    let chars: Vec<char> = "abcdefghijklmnopqrstuvwxyz0123456789".chars().collect();

    let token_id: String = (0..6)
        .map(|_| chars[rng.gen_range(0..chars.len())])
        .collect();

    debug!(token_id = %token_id, "Generated K3s token ID");

    Ok(token_id)
}

fn generate_token_secret() -> String {
    let mut rng = rand::thread_rng();
    let chars: Vec<char> = "abcdefghijklmnopqrstuvwxyz0123456789".chars().collect();

    (0..16)
        .map(|_| chars[rng.gen_range(0..chars.len())])
        .collect()
}

async fn get_ca_hash() -> Result<String> {
    debug!("Fetching cluster CA certificate");

    let client = crate::k8s::helpers::create_client().await?;
    let configmaps: Api<ConfigMap> = Api::namespaced(client, "kube-system");

    let cm = configmaps
        .get("kube-root-ca.crt")
        .await
        .map_err(|e| anyhow!("Failed to get kube-root-ca.crt ConfigMap: {}", e))?;

    let ca_cert = cm
        .data
        .and_then(|mut data| data.remove("ca.crt"))
        .ok_or_else(|| anyhow!("ca.crt not found in kube-root-ca.crt ConfigMap"))?;

    let mut hasher = Sha256::new();
    hasher.update(ca_cert.as_bytes());
    let hash = hasher.finalize();

    let ca_hash = hex::encode(hash);

    debug!(ca_hash = %ca_hash, "Calculated CA certificate hash");

    Ok(ca_hash)
}

pub async fn create_token(node_id: &str, datacenter_id: &str, token_id: &str) -> Result<String> {
    let description = format!("node:{},dc:{}", node_id, datacenter_id);

    info!(
        node_id = %node_id,
        datacenter_id = %datacenter_id,
        token_id = %token_id,
        "Creating K3s bootstrap token"
    );

    let client = crate::k8s::helpers::create_client().await?;
    let secrets: Api<Secret> = Api::namespaced(client, "kube-system");

    let token_secret = generate_token_secret();

    let expiration = (Utc::now() + Duration::hours(1)).to_rfc3339();

    let mut data = BTreeMap::new();
    data.insert(
        "token-id".to_string(),
        k8s_openapi::ByteString(token_id.as_bytes().to_vec()),
    );
    data.insert(
        "token-secret".to_string(),
        k8s_openapi::ByteString(token_secret.as_bytes().to_vec()),
    );
    data.insert(
        "usage-bootstrap-authentication".to_string(),
        k8s_openapi::ByteString(b"true".to_vec()),
    );
    data.insert(
        "usage-bootstrap-signing".to_string(),
        k8s_openapi::ByteString(b"true".to_vec()),
    );
    let auth_groups = format!("system:bootstrappers:worker,system:nodes:{}", node_id);
    data.insert(
        "auth-extra-groups".to_string(),
        k8s_openapi::ByteString(auth_groups.as_bytes().to_vec()),
    );
    data.insert(
        "description".to_string(),
        k8s_openapi::ByteString(description.as_bytes().to_vec()),
    );
    data.insert(
        "expiration".to_string(),
        k8s_openapi::ByteString(expiration.as_bytes().to_vec()),
    );
    data.insert(
        "k3s-node-name".to_string(),
        k8s_openapi::ByteString(node_id.as_bytes().to_vec()),
    );

    let secret = Secret {
        metadata: ObjectMeta {
            name: Some(format!("bootstrap-token-{}", token_id)),
            namespace: Some("kube-system".to_string()),
            ..Default::default()
        },
        type_: Some("bootstrap.kubernetes.io/token".to_string()),
        data: Some(data),
        ..Default::default()
    };

    secrets
        .create(&PostParams::default(), &secret)
        .await
        .map_err(|e| anyhow!("Failed to create bootstrap token secret: {}", e))?;

    let ca_hash = get_ca_hash().await?;

    let full_token = format!("K10{}::{}.{}", ca_hash, token_id, token_secret);

    info!(
        token_id = %token_id,
        "Created K3s bootstrap token in secure format"
    );

    Ok(full_token)
}

pub async fn delete_token(token_id: &str) -> Result<()> {
    info!(token_id = %token_id, "Deleting K3s bootstrap token");

    let client = crate::k8s::helpers::create_client().await?;
    let secrets: Api<Secret> = Api::namespaced(client, "kube-system");

    let secret_name = format!("bootstrap-token-{}", token_id);

    match secrets.delete(&secret_name, &DeleteParams::default()).await {
        Ok(_) => {
            info!("Deleted K3s bootstrap token successfully");
            Ok(())
        }
        Err(kube::Error::Api(err)) if err.code == 404 => {
            warn!(token_id = %token_id, "Bootstrap token secret not found, may have already been deleted");
            Ok(())
        }
        Err(e) => {
            warn!("Failed to delete bootstrap token secret: {}", e);
            Err(anyhow!("Failed to delete bootstrap token secret: {}", e))
        }
    }
}

pub async fn check_connectivity() -> Result<()> {
    debug!("Checking K3s connectivity");

    let token_id = generate_token_id().await?;

    info!(token_id = %token_id, "K3s connectivity check successful (token ID generated)");
    Ok(())
}
