mod client;
pub mod cluster_tokens;
mod helpers;
pub mod k3s_commands;
mod mock;
mod r#trait;
mod types;

#[cfg(test)]
mod tests;

pub use client::K8sClient;
pub use cluster_tokens::{
    check_k3s_connectivity, cleanup_expired_cluster_tokens, get_or_create_cluster_token,
    revoke_cluster_token, ClusterTokenRecord,
};
pub use helpers::{
    build_node_labels, client_from_kubeconfig_content, create_client,
    create_reference_grant_for_namespace, create_temp_kubeconfig,
    execute_k3s_command_with_kubeconfig, get_k3s_server_url, parse_status_endpoints,
    validate_node_id, NodeLabelParams,
};
pub use mock::MockK8sClient;
pub use r#trait::ApiK8sClient;
pub use types::*;
