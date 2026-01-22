use anyhow::Result;
use chrono::{DateTime, Utc};
use serde::Serialize;
use std::path::PathBuf;
use tokio::fs;
use uuid::Uuid;

#[derive(Debug, Serialize)]
pub struct SlashEvidence {
    pub rental_id: String,
    pub misbehaviour_type: String,
    pub timestamp: DateTime<Utc>,
    pub details: String,
    pub miner_hotkey: String,
    pub node_id: String,
    pub validator_hotkey: String,
    pub shadow_mode: bool,
}

#[derive(Clone)]
pub struct EvidenceStore {
    base_url: String,
    storage_path: PathBuf,
}

impl EvidenceStore {
    pub fn new(base_url: String, storage_path: PathBuf) -> Self {
        Self {
            base_url,
            storage_path,
        }
    }

    pub async fn store(&self, evidence: &SlashEvidence) -> Result<(String, Vec<u8>)> {
        fs::create_dir_all(&self.storage_path).await?;
        let file_id = format!("evidence-{}.json", Uuid::new_v4());
        let path = self.storage_path.join(&file_id);
        let json = serde_json::to_vec_pretty(evidence)?;
        fs::write(&path, &json).await?;
        let url = self.build_url(&file_id);
        Ok((url, json))
    }

    fn build_url(&self, file_name: &str) -> String {
        let base = self.base_url.trim_end_matches('/');
        format!("{}/{}", base, file_name)
    }

}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[tokio::test]
    async fn test_store_evidence() {
        let temp = tempdir().unwrap();
        let store = EvidenceStore::new(
            "https://validator.example.com/evidence".to_string(),
            temp.path().to_path_buf(),
        );
        let evidence = SlashEvidence {
            rental_id: "rental-1".to_string(),
            misbehaviour_type: "bid_won_deployment_failed".to_string(),
            timestamp: Utc::now(),
            details: "{}".to_string(),
            miner_hotkey: "hk".to_string(),
            node_id: "node".to_string(),
            validator_hotkey: "vhk".to_string(),
            shadow_mode: true,
        };
        let (url, json) = store.store(&evidence).await.unwrap();
        assert!(url.contains("validator.example.com"));
        assert!(!json.is_empty());
    }
}

