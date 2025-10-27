use sha2::{Digest, Sha256};
use uuid::Uuid;

pub fn generate_idempotency_key(rental_id: Uuid, event_data: &serde_json::Value) -> String {
    let timestamp_str = event_data
        .get("timestamp")
        .and_then(|t| {
            t.as_str()
                .map(|s| s.to_string())
                .or_else(|| t.as_i64().map(|n| n.to_string()))
        })
        .unwrap_or_default();

    let data_str = serde_json::to_string(event_data).unwrap_or_default();
    let mut hasher = Sha256::new();
    hasher.update(data_str.as_bytes());
    let hash = format!("{:x}", hasher.finalize());

    format!("{}:{}:{}", rental_id, timestamp_str, &hash[0..8])
}
