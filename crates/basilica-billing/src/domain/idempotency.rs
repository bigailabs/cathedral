use sha2::{Digest, Sha256};
use uuid::Uuid;

pub fn generate_idempotency_key(rental_id: Uuid, event_data: &serde_json::Value) -> String {
    let timestamp = event_data
        .get("timestamp")
        .and_then(|t| t.as_str())
        .or_else(|| {
            event_data
                .get("timestamp")
                .and_then(|t| t.as_i64())
                .map(|_| "")
        })
        .unwrap_or("");

    let data_str = serde_json::to_string(event_data).unwrap_or_default();
    let mut hasher = Sha256::new();
    hasher.update(data_str.as_bytes());
    let hash = format!("{:x}", hasher.finalize());

    format!("{}:{}:{}", rental_id, timestamp, &hash[0..8])
}
