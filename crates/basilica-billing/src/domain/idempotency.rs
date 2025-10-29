use sha2::{Digest, Sha256};
use uuid::Uuid;

pub fn prepare_event_data_for_idempotency(event_data: &serde_json::Value) -> serde_json::Value {
    match event_data {
        serde_json::Value::Object(m) => {
            let mut clean = m.clone();
            clean.remove("timestamp");
            serde_json::Value::Object(clean)
        }
        _ => event_data.clone(),
    }
}

pub fn generate_idempotency_key(rental_id: Uuid, event_data: &serde_json::Value) -> String {
    let timestamp_str = event_data
        .get("timestamp")
        .and_then(|t| {
            t.as_str()
                .map(|s| s.to_string())
                .or_else(|| t.as_i64().map(|n| n.to_string()))
        })
        .unwrap_or_default();

    let event_data_for_hash = prepare_event_data_for_idempotency(event_data);
    let data_str = serde_json::to_string(&event_data_for_hash).unwrap_or_default();
    let mut hasher = Sha256::new();
    hasher.update(data_str.as_bytes());
    let hash = format!("{:x}", hasher.finalize());

    format!("{}:{}:{}", rental_id, timestamp_str, &hash[0..8])
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn test_prepare_event_data_removes_timestamp() {
        let event_data = json!({
            "field1": "value1",
            "field2": 42,
            "timestamp": "1234567890"
        });

        let cleaned = prepare_event_data_for_idempotency(&event_data);

        assert!(cleaned.get("field1").is_some());
        assert!(cleaned.get("field2").is_some());
        assert!(cleaned.get("timestamp").is_none());
    }

    #[test]
    fn test_prepare_event_data_preserves_non_object_values() {
        let event_data = json!("string value");
        let cleaned = prepare_event_data_for_idempotency(&event_data);
        assert_eq!(cleaned, event_data);

        let event_data = json!(123);
        let cleaned = prepare_event_data_for_idempotency(&event_data);
        assert_eq!(cleaned, event_data);
    }

    #[test]
    fn test_idempotency_key_stable_without_timestamp() {
        let rental_id = Uuid::parse_str("550e8400-e29b-41d4-a716-446655440000").unwrap();

        let data1 = json!({
            "field": "value",
            "amount": 100
        });

        let data2 = json!({
            "field": "value",
            "amount": 100
        });

        let cleaned1 = prepare_event_data_for_idempotency(&data1);
        let cleaned2 = prepare_event_data_for_idempotency(&data2);

        let key1 = generate_idempotency_key(rental_id, &cleaned1);
        let key2 = generate_idempotency_key(rental_id, &cleaned2);

        assert_eq!(key1, key2);
    }

    #[test]
    fn test_idempotency_key_includes_timestamp_but_excludes_from_hash() {
        let rental_id = Uuid::parse_str("550e8400-e29b-41d4-a716-446655440000").unwrap();

        let data1 = json!({
            "field": "value",
            "amount": 100,
            "timestamp": "1234567890"
        });

        let data2 = json!({
            "field": "value",
            "amount": 100,
            "timestamp": "9876543210"
        });

        let key1 = generate_idempotency_key(rental_id, &data1);
        let key2 = generate_idempotency_key(rental_id, &data2);

        assert_ne!(
            key1, key2,
            "Keys should differ when timestamps differ (timestamp in key format)"
        );

        assert!(
            key1.contains("1234567890"),
            "Key should contain timestamp: {}",
            key1
        );
        assert!(
            key2.contains("9876543210"),
            "Key should contain timestamp: {}",
            key2
        );

        assert_eq!(
            key1.split(':').nth(2),
            key2.split(':').nth(2),
            "Hash portion should be identical (timestamp excluded from hash)"
        );
    }

    #[test]
    fn test_idempotency_key_changes_with_different_data() {
        let rental_id = Uuid::parse_str("550e8400-e29b-41d4-a716-446655440000").unwrap();

        let data1 = json!({
            "field": "value1",
            "amount": 100
        });

        let data2 = json!({
            "field": "value2",
            "amount": 100
        });

        let cleaned1 = prepare_event_data_for_idempotency(&data1);
        let cleaned2 = prepare_event_data_for_idempotency(&data2);

        let key1 = generate_idempotency_key(rental_id, &cleaned1);
        let key2 = generate_idempotency_key(rental_id, &cleaned2);

        assert_ne!(key1, key2, "Keys should differ when data is different");
    }

    #[test]
    fn test_idempotency_key_changes_with_different_rental_id() {
        let rental_id1 = Uuid::parse_str("550e8400-e29b-41d4-a716-446655440000").unwrap();
        let rental_id2 = Uuid::parse_str("660e8400-e29b-41d4-a716-446655440000").unwrap();

        let data = json!({
            "field": "value",
            "amount": 100
        });

        let cleaned = prepare_event_data_for_idempotency(&data);

        let key1 = generate_idempotency_key(rental_id1, &cleaned);
        let key2 = generate_idempotency_key(rental_id2, &cleaned);

        assert_ne!(key1, key2, "Keys should differ when rental_id is different");
    }

    #[test]
    fn test_idempotency_key_format_with_timestamp() {
        let rental_id = Uuid::parse_str("550e8400-e29b-41d4-a716-446655440000").unwrap();

        let data = json!({
            "field": "value",
            "amount": 100,
            "timestamp": "1730194445000"
        });

        let key = generate_idempotency_key(rental_id, &data);

        let parts: Vec<&str> = key.split(':').collect();
        assert_eq!(
            parts.len(),
            3,
            "Key should have format rental_id:timestamp:hash"
        );
        assert_eq!(parts[0], "550e8400-e29b-41d4-a716-446655440000");
        assert_eq!(parts[1], "1730194445000");
        assert_eq!(parts[2].len(), 8, "Hash should be 8 characters");
    }
}
