use basilica_common::types::GpuCategory;

/// Map HydraHost GPU model string to canonical GpuCategory
/// HydraHost models from API categories: "4090", "3090", "a100", "a40", "a5000", "a6000", "gh200", "h100", "mi250", "mi300x"
pub fn normalize_gpu_type(gpu_str: &str) -> GpuCategory {
    // Use GpuCategory's FromStr implementation which handles parsing
    gpu_str
        .parse()
        .unwrap_or_else(|_| GpuCategory::Other(gpu_str.to_string()))
}

/// Parse GPU memory from HydraHost GPU model string
/// HydraHost provides strings like "NVIDIA H100 80GB HBM3" or "RTX 4090 24GB"
/// Returns None if memory cannot be parsed from the string
pub fn parse_gpu_memory(gpu_model: &str) -> Option<u32> {
    // Common patterns: "80GB", "80 GB", "24GB", "V100-SXM3-32GB" etc.
    // Strategy: Look for patterns like "XGB" or "X GB" where X is a number

    // First, try to find "GB" or "gb" in the string
    let upper = gpu_model.to_uppercase();
    if !upper.contains("GB") {
        return None;
    }

    // Split by common delimiters (space, dash, underscore)
    let parts: Vec<&str> = gpu_model.split(&[' ', '-', '_'][..]).collect();

    // Look for a part that contains "GB"
    for (i, part) in parts.iter().enumerate() {
        let part_upper = part.to_uppercase();
        if part_upper.contains("GB") {
            // Try to parse the number from this part (e.g., "80GB" -> 80)
            let num_str = part.trim_end_matches(|c: char| !c.is_numeric());
            if !num_str.is_empty() {
                if let Ok(memory) = num_str.parse::<u32>() {
                    return Some(memory);
                }
            }

            // If the current part is just "GB", try the previous part for the number
            if i > 0 && (part_upper == "GB" || part_upper == "gb") {
                if let Ok(memory) = parts[i - 1].parse::<u32>() {
                    return Some(memory);
                }
            }
        }
    }

    None
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
    fn test_parse_gpu_memory_with_gb() {
        // Test various formats with GB in the string
        assert_eq!(parse_gpu_memory("NVIDIA H100 80GB HBM3"), Some(80));
        assert_eq!(parse_gpu_memory("RTX 4090 24GB"), Some(24));
        assert_eq!(parse_gpu_memory("A100 80GB"), Some(80));
        assert_eq!(parse_gpu_memory("Tesla V100-SXM3-32GB"), Some(32));
    }

    #[test]
    fn test_parse_gpu_memory_with_space() {
        // Test format with space between number and GB
        assert_eq!(parse_gpu_memory("H100 80 GB"), Some(80));
        assert_eq!(parse_gpu_memory("A100 40 GB"), Some(40));
    }

    #[test]
    fn test_parse_gpu_memory_no_memory() {
        // Test cases where memory is not in the string
        assert_eq!(parse_gpu_memory("H100"), None);
        assert_eq!(parse_gpu_memory("A100"), None);
        assert_eq!(parse_gpu_memory("RTX 4090"), None);
        assert_eq!(parse_gpu_memory("UnknownGPU"), None);
    }

    #[test]
    fn test_parse_gpu_memory_from_name_field() {
        // Test parsing from HydraHost name field patterns
        assert_eq!(
            parse_gpu_memory("Lenovo NVIDIA H100 80GB HBM3-1063"),
            Some(80)
        );
        assert_eq!(
            parse_gpu_memory("Quanta Cloud Technology Inc. NVIDIA GH200 480GB-237"),
            Some(480)
        );
        // Cases without memory in name
        assert_eq!(parse_gpu_memory("Tencent NVIDIA L40S-291"), None);
        assert_eq!(parse_gpu_memory("OOB IPMI-1754"), None);
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
