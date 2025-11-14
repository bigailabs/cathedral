use crate::k8s::helpers::execute_k3s_command_with_kubeconfig;
use anyhow::{anyhow, Result};
use tracing::{debug, info, warn};

pub async fn generate_token_id() -> Result<String> {
    debug!("Generating K3s token ID");

    let output = tokio::process::Command::new("k3s")
        .args(["token", "generate"])
        .output()
        .await?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        warn!("k3s token generate failed: {}", stderr);
        return Err(anyhow!("k3s token generate failed: {}", stderr));
    }

    let token_id = String::from_utf8(output.stdout)?.trim().to_string();

    debug!(token_id = %token_id, "Generated K3s token ID");

    Ok(token_id)
}

pub async fn create_token(node_id: &str, datacenter_id: &str, token_id: &str) -> Result<String> {
    let description = format!("node:{},dc:{}", node_id, datacenter_id);

    info!(
        node_id = %node_id,
        datacenter_id = %datacenter_id,
        token_id = %token_id,
        "Creating K3s token"
    );

    let args = vec![
        "token",
        "create",
        token_id,
        "--ttl",
        "1h",
        "--description",
        &description,
    ];
    let output = execute_k3s_command_with_kubeconfig(&args).await?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        warn!("k3s token create failed: {}", stderr);
        return Err(anyhow!("k3s token create failed: {}", stderr));
    }

    let full_token = String::from_utf8(output.stdout)?.trim().to_string();

    info!("Created K3s token successfully");

    Ok(full_token)
}

pub async fn delete_token(token_id: &str) -> Result<()> {
    info!(token_id = %token_id, "Deleting K3s token");

    let args = vec!["token", "delete", token_id];
    let output = execute_k3s_command_with_kubeconfig(&args).await?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        if stderr.contains("not found") {
            warn!(token_id = %token_id, "Token not found in k3s, may have already been deleted");
            return Ok(());
        }
        warn!("k3s token delete failed: {}", stderr);
        return Err(anyhow!("k3s token delete failed: {}", stderr));
    }

    info!("Deleted K3s token successfully");

    Ok(())
}

pub async fn check_connectivity() -> Result<()> {
    debug!("Checking K3s connectivity");

    let token_id = generate_token_id().await?;

    info!(token_id = %token_id, "K3s connectivity check successful");
    Ok(())
}
