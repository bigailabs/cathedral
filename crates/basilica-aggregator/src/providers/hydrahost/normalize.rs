use basilica_common::types::GpuCategory;
use std::collections::HashMap;

/// Map HydraHost GPU model string to canonical GpuCategory
/// HydraHost models from API categories: "4090", "3090", "a100", "a40", "a5000", "a6000", "gh200", "h100", "mi250", "mi300x"
pub fn normalize_gpu_type(gpu_str: &str) -> GpuCategory {
    // Use GpuCategory's FromStr implementation which handles parsing
    gpu_str
        .parse()
        .unwrap_or_else(|_| GpuCategory::Other(gpu_str.to_string()))
}

/// Get GPU memory in GB based on model
/// Returns default memory size for known GPU models
/// HydraHost API may not always include memory info, so we use standard configurations
pub fn get_gpu_memory(gpu_model: &str) -> u32 {
    // Create lookup table for standard GPU memory configurations
    let memory_map: HashMap<&str, u32> = [
        // NVIDIA GPUs - Consumer/Gaming
        ("4090", 24),
        ("5090", 32), // RTX 5090
        ("3090", 24),
        // NVIDIA GPUs - Data Center
        ("a100", 80), // A100 comes in 40GB and 80GB, default to 80GB
        ("a40", 48),
        ("a5000", 24),
        ("a6000", 48),
        ("h100", 80),  // H100 comes in 80GB (SXM/PCIe) and 94GB (NVL)
        ("h200", 141), // H200 has 141GB HBM3e
        ("b200", 192), // B200 Blackwell
        ("l40s", 48),  // L40S
        ("gh200", 96), // Grace Hopper superchip
        ("v100", 32),  // Tesla V100 comes in 16GB and 32GB variants
        // NVIDIA Workstation GPUs
        ("rtx", 48), // RTX PRO series (catch-all, may need refinement)
        // AMD GPUs
        ("mi250", 128),  // MI250X has 128GB
        ("mi300x", 192), // MI300X has 192GB
    ]
    .iter()
    .cloned()
    .collect();

    // Normalize to lowercase for lookup
    let normalized = gpu_model.to_lowercase();

    // Try direct lookup first
    if let Some(&memory) = memory_map.get(normalized.as_str()) {
        return memory;
    }

    // Try partial match for variants (e.g., "A100-80G" contains "a100")
    // Sort keys by length descending to match more specific patterns first
    let mut keys: Vec<_> = memory_map.keys().collect();
    keys.sort_by_key(|k| std::cmp::Reverse(k.len()));

    for key in keys {
        if normalized.contains(*key) {
            return memory_map[key];
        }
    }

    // If no match found, log warning and return 0
    tracing::warn!(
        "Unknown GPU model for memory lookup: {}, defaulting to 0",
        gpu_model
    );
    0
}

/// Pass through HydraHost location as-is
/// HydraHost locations are like "Arizona", "Nevada", "New York", etc.
/// We store exactly what the API provides without transformation
pub fn normalize_region(region: &str) -> String {
    if region.trim().is_empty() {
        "unknown".to_string()
    } else {
        region.to_string()
    }
}

/// Format storage information from HydraHost specs
/// Preserves raw provider data by including storage type and count
/// Examples: "32646 GB (10x NVMe)", "1000 GB (4x SSD, 2x HDD)", "500 GB (NVMe)"
pub fn format_storage(storage_spec: &super::types::StorageSpec) -> Option<String> {
    let mut parts = Vec::new();

    // Collect storage types with their counts
    if let Some(nvme_count) = storage_spec.nvme_count {
        if nvme_count > 0 {
            if nvme_count == 1 {
                parts.push("NVMe".to_string());
            } else {
                parts.push(format!("{}x NVMe", nvme_count));
            }
        }
    }

    if let Some(ssd_count) = storage_spec.ssd_count {
        if ssd_count > 0 {
            if ssd_count == 1 {
                parts.push("SSD".to_string());
            } else {
                parts.push(format!("{}x SSD", ssd_count));
            }
        }
    }

    if let Some(hdd_count) = storage_spec.hdd_count {
        if hdd_count > 0 {
            if hdd_count == 1 {
                parts.push("HDD".to_string());
            } else {
                parts.push(format!("{}x HDD", hdd_count));
            }
        }
    }

    // If we have a total size and at least one storage type, format it
    if let Some(total) = storage_spec.total {
        if !parts.is_empty() {
            Some(format!("{} GB ({})", total, parts.join(", ")))
        } else {
            // Only total available, no type info
            Some(format!("{} GB", total))
        }
    } else {
        // No total size but we have types - shouldn't happen but handle it
        if !parts.is_empty() {
            Some(parts.join(", "))
        } else {
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_normalize_a100() {
        assert_eq!(normalize_gpu_type("a100"), GpuCategory::A100);
        assert_eq!(normalize_gpu_type("A100"), GpuCategory::A100);
        assert_eq!(normalize_gpu_type("A100-80G"), GpuCategory::A100);
    }

    #[test]
    fn test_normalize_h100() {
        assert_eq!(normalize_gpu_type("h100"), GpuCategory::H100);
        assert_eq!(normalize_gpu_type("H100"), GpuCategory::H100);
    }

    #[test]
    fn test_normalize_unknown() {
        match normalize_gpu_type("RTX-4090") {
            GpuCategory::Other(model) => assert!(model.contains("RTX")),
            _ => panic!("Expected Other variant"),
        }
    }

    #[test]
    fn test_get_memory_known_models() {
        assert_eq!(get_gpu_memory("a100"), 80);
        assert_eq!(get_gpu_memory("h100"), 80);
        assert_eq!(get_gpu_memory("h200"), 141);
        assert_eq!(get_gpu_memory("b200"), 192);
        assert_eq!(get_gpu_memory("4090"), 24);
        assert_eq!(get_gpu_memory("5090"), 32);
        assert_eq!(get_gpu_memory("a40"), 48);
        assert_eq!(get_gpu_memory("l40s"), 48);
        assert_eq!(get_gpu_memory("gh200"), 96);
        assert_eq!(get_gpu_memory("v100"), 32);
        assert_eq!(get_gpu_memory("mi300x"), 192);
    }

    #[test]
    fn test_get_memory_case_insensitive() {
        assert_eq!(get_gpu_memory("A100"), 80);
        assert_eq!(get_gpu_memory("H100"), 80);
        assert_eq!(get_gpu_memory("H200"), 141);
        assert_eq!(get_gpu_memory("B200"), 192);
        assert_eq!(get_gpu_memory("L40S"), 48);
        assert_eq!(get_gpu_memory("MI300X"), 192);
    }

    #[test]
    fn test_get_memory_with_suffix() {
        assert_eq!(get_gpu_memory("A100-80G"), 80);
        assert_eq!(get_gpu_memory("a100-pcie"), 80);
        assert_eq!(get_gpu_memory("NVIDIA B200"), 192);
        assert_eq!(get_gpu_memory("NVIDIA H200"), 141);
        assert_eq!(get_gpu_memory("NVIDIA L40S"), 48);
        assert_eq!(get_gpu_memory("NVIDIA GeForce RTX 5090"), 32);
        assert_eq!(get_gpu_memory("Tesla V100-SXM3-32GB"), 32);
    }

    #[test]
    fn test_get_memory_unknown() {
        assert_eq!(get_gpu_memory("UnknownGPU"), 0);
    }

    #[test]
    fn test_normalize_region() {
        // Store exactly what HydraHost API provides
        assert_eq!(normalize_region("Arizona"), "Arizona");
        assert_eq!(normalize_region("Nevada"), "Nevada");
        assert_eq!(normalize_region("New York"), "New York");
        assert_eq!(normalize_region("Washington"), "Washington");
        assert_eq!(normalize_region(""), "unknown");
    }

    #[test]
    fn test_format_storage_nvme_only() {
        use super::super::types::StorageSpec;

        let storage = StorageSpec {
            hdd_count: None,
            hdd_size: None,
            ssd_count: None,
            ssd_size: None,
            nvme_count: Some(10),
            nvme_size: Some(32646),
            total: Some(32646),
        };

        assert_eq!(
            format_storage(&storage),
            Some("32646 GB (10x NVMe)".to_string())
        );
    }

    #[test]
    fn test_format_storage_mixed_types() {
        use super::super::types::StorageSpec;

        let storage = StorageSpec {
            hdd_count: Some(2),
            hdd_size: Some(2000),
            ssd_count: Some(4),
            ssd_size: Some(1000),
            nvme_count: Some(1),
            nvme_size: Some(500),
            total: Some(7500),
        };

        assert_eq!(
            format_storage(&storage),
            Some("7500 GB (NVMe, 4x SSD, 2x HDD)".to_string())
        );
    }

    #[test]
    fn test_format_storage_single_nvme() {
        use super::super::types::StorageSpec;

        let storage = StorageSpec {
            hdd_count: None,
            hdd_size: None,
            ssd_count: None,
            ssd_size: None,
            nvme_count: Some(1),
            nvme_size: Some(1000),
            total: Some(1000),
        };

        assert_eq!(format_storage(&storage), Some("1000 GB (NVMe)".to_string()));
    }

    #[test]
    fn test_format_storage_total_only() {
        use super::super::types::StorageSpec;

        let storage = StorageSpec {
            hdd_count: None,
            hdd_size: None,
            ssd_count: None,
            ssd_size: None,
            nvme_count: None,
            nvme_size: None,
            total: Some(5000),
        };

        assert_eq!(format_storage(&storage), Some("5000 GB".to_string()));
    }

    #[test]
    fn test_format_storage_no_data() {
        use super::super::types::StorageSpec;

        let storage = StorageSpec {
            hdd_count: None,
            hdd_size: None,
            ssd_count: None,
            ssd_size: None,
            nvme_count: None,
            nvme_size: None,
            total: None,
        };

        assert_eq!(format_storage(&storage), None);
    }

    #[test]
    fn test_format_storage_zero_counts_ignored() {
        use super::super::types::StorageSpec;

        let storage = StorageSpec {
            hdd_count: Some(0),
            hdd_size: Some(0),
            ssd_count: Some(0),
            ssd_size: Some(0),
            nvme_count: Some(2),
            nvme_size: Some(1000),
            total: Some(2000),
        };

        assert_eq!(
            format_storage(&storage),
            Some("2000 GB (2x NVMe)".to_string())
        );
    }
}
