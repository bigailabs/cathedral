mod client;
mod helpers;
mod mock;
mod r#trait;
mod types;

#[cfg(test)]
mod tests;

pub use client::K8sClient;
pub use helpers::{
    client_from_kubeconfig_content, create_client, create_reference_grant_for_namespace,
    parse_status_endpoints,
};
pub use mock::MockK8sClient;
pub use r#trait::ApiK8sClient;
pub use types::*;
