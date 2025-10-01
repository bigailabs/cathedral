//! Validator access management for node-based architecture
//!
//! This module manages validator SSH access to nodes in the direct access model.

use anyhow::Result;
use basilica_common::identity::Hotkey;
use std::sync::Arc;
use tracing::{debug, info};

use crate::node_manager::NodeManager;

/// Service for managing validator SSH access to nodes
#[derive(Clone, Debug)]
pub struct ValidatorAccessService {
    node_manager: Arc<NodeManager>,
}

impl ValidatorAccessService {
    /// Create new validator access service
    pub fn new(node_manager: Arc<NodeManager>) -> Result<Self> {
        Ok(Self { node_manager })
    }

    /// Authorize a validator for node access
    pub async fn authorize_validator(
        &self,
        validator_hotkey: &Hotkey,
        ssh_public_key: &str,
    ) -> Result<()> {
        info!("Authorizing validator {} for node access", validator_hotkey);

        // Store the authorization in the node manager
        self.node_manager
            .authorize_validator(&validator_hotkey.to_string(), ssh_public_key)
            .await?;

        debug!("Validator {} authorized successfully", validator_hotkey);
        Ok(())
    }

    /// Revoke validator access
    pub async fn revoke_validator(&self, validator_hotkey: &Hotkey) -> Result<()> {
        info!("Revoking access for validator {}", validator_hotkey);

        // In the direct access model, the SSH key removal is handled by SshSessionManager
        // This just updates the node manager's authorization state
        self.node_manager
            .revoke_validator(&validator_hotkey.to_string())
            .await?;

        debug!("Validator {} access revoked", validator_hotkey);
        Ok(())
    }

    /// Check if a validator has access
    pub async fn has_access(&self, validator_hotkey: &Hotkey) -> Result<bool> {
        Ok(self
            .node_manager
            .is_validator_authorized(&validator_hotkey.to_string())
            .await)
    }
}
