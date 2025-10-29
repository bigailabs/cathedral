use anyhow::Result;
use basilica_protocol::billing::{
    GpuUsage as ProtoGpuUsage, ResourceUsage as ProtoResourceUsage, TelemetryData,
};
use chrono::Utc;

use crate::rental::types::ResourceUsage;

pub fn resource_usage_to_telemetry(
    rental_id: String,
    node_id: String,
    usage: ResourceUsage,
) -> Result<TelemetryData> {
    let gpu_usage = usage
        .gpu_usage
        .into_iter()
        .map(|gpu| ProtoGpuUsage {
            index: gpu.gpu_index,
            utilization_percent: gpu.utilization_percent,
            memory_used_mb: gpu.memory_mb as u64,
            temperature_celsius: gpu.temperature_celsius,
            power_watts: 0,
        })
        .collect();

    let resource_usage = ProtoResourceUsage {
        cpu_percent: usage.cpu_percent,
        memory_mb: usage.memory_mb as u64,
        network_rx_bytes: usage.network_rx_bytes as u64,
        network_tx_bytes: usage.network_tx_bytes as u64,
        disk_read_bytes: usage.disk_read_bytes as u64,
        disk_write_bytes: usage.disk_write_bytes as u64,
        gpu_usage,
    };

    Ok(TelemetryData {
        rental_id,
        node_id,
        timestamp: Some(prost_types::Timestamp {
            seconds: Utc::now().timestamp(),
            nanos: Utc::now().timestamp_subsec_nanos() as i32,
        }),
        resource_usage: Some(resource_usage),
        custom_metrics: std::collections::HashMap::new(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::rental::types::GpuUsage;

    #[test]
    fn test_resource_usage_conversion() {
        let usage = ResourceUsage {
            cpu_percent: 45.5,
            memory_mb: 2048,
            disk_read_bytes: 1024000,
            disk_write_bytes: 512000,
            network_rx_bytes: 8192000,
            network_tx_bytes: 4096000,
            gpu_usage: vec![GpuUsage {
                gpu_index: 0,
                utilization_percent: 75.0,
                memory_mb: 8192,
                temperature_celsius: 65.0,
            }],
        };

        let telemetry = resource_usage_to_telemetry(
            "rental-123".to_string(),
            "node-456".to_string(),
            usage.clone(),
        )
        .unwrap();

        assert_eq!(telemetry.rental_id, "rental-123");
        assert_eq!(telemetry.node_id, "node-456");
        assert!(telemetry.timestamp.is_some());
        assert!(telemetry.resource_usage.is_some());

        let proto_usage = telemetry.resource_usage.unwrap();
        assert_eq!(proto_usage.cpu_percent, usage.cpu_percent);
        assert_eq!(proto_usage.memory_mb, usage.memory_mb as u64);
        assert_eq!(proto_usage.disk_read_bytes, usage.disk_read_bytes as u64);
        assert_eq!(proto_usage.disk_write_bytes, usage.disk_write_bytes as u64);
        assert_eq!(proto_usage.network_rx_bytes, usage.network_rx_bytes as u64);
        assert_eq!(proto_usage.network_tx_bytes, usage.network_tx_bytes as u64);
        assert_eq!(proto_usage.gpu_usage.len(), 1);
        assert_eq!(proto_usage.gpu_usage[0].index, 0);
        assert_eq!(proto_usage.gpu_usage[0].utilization_percent, 75.0);
    }

    #[test]
    fn test_empty_gpu_usage() {
        let usage = ResourceUsage {
            cpu_percent: 25.0,
            memory_mb: 1024,
            disk_read_bytes: 0,
            disk_write_bytes: 0,
            network_rx_bytes: 0,
            network_tx_bytes: 0,
            gpu_usage: vec![],
        };

        let telemetry =
            resource_usage_to_telemetry("rental-789".to_string(), "node-101".to_string(), usage)
                .unwrap();

        let proto_usage = telemetry.resource_usage.unwrap();
        assert!(proto_usage.gpu_usage.is_empty());
    }
}
