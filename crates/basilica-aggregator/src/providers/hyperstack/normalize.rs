use basilica_common::types::GpuCategory;

/// Parse Hyperstack GPU string to extract model and memory
/// Format examples: "A100-80G-PCIe", "H100", "A100-40G"
/// Returns: (gpu_model, memory_gb_option)
pub fn parse_gpu_string(gpu_str: &str) -> (String, Option<u32>) {
    // Split by '-' to separate components
    let parts: Vec<&str> = gpu_str.split('-').collect();

    if parts.is_empty() {
        return (gpu_str.to_string(), None);
    }

    // First part is always the model (e.g., "A100", "H100")
    let model = parts[0].to_string();

    // Look for memory specification (e.g., "80G", "40G")
    let memory = parts.iter().find_map(|part| {
        if part.ends_with('G') || part.ends_with("GB") {
            part.trim_end_matches("GB")
                .trim_end_matches('G')
                .parse::<u32>()
                .ok()
        } else {
            None
        }
    });

    (model, memory)
}

/// Map Hyperstack GPU model to canonical GpuCategory
/// Hyperstack models: "A100-80G-PCIe", "H100", "B200", etc.
pub fn normalize_gpu_type(gpu_str: &str) -> GpuCategory {
    // Extract just the model part (before first '-')
    let (model, _) = parse_gpu_string(gpu_str);

    // Use GpuCategory's FromStr implementation which handles parsing
    model
        .parse()
        .unwrap_or_else(|_| GpuCategory::Other(gpu_str.to_string()))
}

/// Fallback lookup table for GPU models without memory in their name
/// Maps GPU model prefix to standard memory size in GB
fn get_fallback_gpu_memory(gpu_model: &str) -> Option<u32> {
    // Extract base model name (before any suffixes like -spot, -NVLink, etc.)
    let base_model = gpu_model.split('-').next()?;

    match base_model {
        "L40" => Some(48), // NVIDIA L40: 48GB GDDR6
        "RTX" => {
            // Handle RTX variants
            if gpu_model.contains("A4000") {
                Some(16) // RTX A4000: 16GB GDDR6
            } else if gpu_model.contains("A6000") || gpu_model.contains("PRO6000") {
                Some(48) // RTX A6000 and RTX PRO6000-SE: 48GB GDDR6
            } else {
                None
            }
        }
        _ => None,
    }
}

/// Extract GPU memory in GB from Hyperstack GPU string
/// First tries to parse from string (e.g., "A100-80G-PCIe" -> 80)
/// Falls back to lookup table for known models without memory in name
pub fn parse_gpu_memory(gpu_str: &str) -> Option<u32> {
    let (_, memory) = parse_gpu_string(gpu_str);

    // If memory was found in string, return it
    if memory.is_some() {
        return memory;
    }

    // Otherwise, try fallback lookup for known GPU models
    get_fallback_gpu_memory(gpu_str)
}

/// Parse interconnect type from Hyperstack GPU string
/// Format: "H100-80G-PCIe" -> Some("PCIe")
/// Format: "H100-80G-SXM5" -> Some("SXM5")
/// Format: "A100-80G-PCIe-NVLink" -> Some("PCIe-NVLink")
/// Format: "A100-80G-PCIe-spot" -> Some("PCIe")
/// Returns None if interconnect type not found
pub fn parse_interconnect(gpu_str: &str) -> Option<String> {
    // Split by '-' to separate components
    let parts: Vec<&str> = gpu_str.split('-').collect();

    // Known interconnect types (order matters for multi-part matches like PCIe-NVLink)
    let multi_part_interconnects = [("PCIe", "NVLink")];
    let single_interconnects = ["SXM4", "SXM5", "SXM6", "PCIe", "PCIE"];

    // Check for multi-part interconnects first (e.g., PCIe-NVLink)
    for i in 0..parts.len().saturating_sub(1) {
        let part1 = parts[i].to_uppercase();
        let part2 = parts[i + 1].to_uppercase();

        for (first, second) in &multi_part_interconnects {
            if (part1 == first.to_uppercase() || part1 == "PCIE") && part2 == second.to_uppercase()
            {
                return Some(format!("{}-{}", first, second));
            }
        }
    }

    // Check for single-part interconnects, excluding known non-interconnect suffixes
    let ignore_parts = ["SPOT", "GB"];
    for part in &parts {
        let part_upper = part.to_uppercase();

        // Skip ignored parts
        if ignore_parts.contains(&part_upper.as_str()) {
            continue;
        }

        // Skip memory specifications (e.g., "80G")
        if part_upper.ends_with('G') && part_upper[..part_upper.len() - 1].parse::<u32>().is_ok() {
            continue;
        }

        // Check if it matches a known interconnect
        for interconnect in &single_interconnects {
            if part_upper == *interconnect {
                // Normalize PCIE to PCIe
                return Some(if part_upper == "PCIE" {
                    "PCIe".to_string()
                } else {
                    part_upper
                });
            }
        }
    }

    None
}

/// Pass through Hyperstack region name as-is
/// Hyperstack regions are like "CANADA-1", "US-WEST-1", "NORWAY-1" etc.
/// We store exactly what the API provides without transformation
pub fn normalize_region(region: &str) -> String {
    if region.trim().is_empty() {
        "unknown".to_string()
    } else {
        region.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_a100_80g() {
        let (model, memory) = parse_gpu_string("A100-80G-PCIe");
        assert_eq!(model, "A100");
        assert_eq!(memory, Some(80));
    }

    #[test]
    fn test_parse_a100_40g() {
        let (model, memory) = parse_gpu_string("A100-40G");
        assert_eq!(model, "A100");
        assert_eq!(memory, Some(40));
    }

    #[test]
    fn test_parse_h100_no_memory() {
        let (model, memory) = parse_gpu_string("H100");
        assert_eq!(model, "H100");
        assert_eq!(memory, None);
    }

    #[test]
    fn test_parse_h100_with_memory() {
        let (model, memory) = parse_gpu_string("H100-80GB");
        assert_eq!(model, "H100");
        assert_eq!(memory, Some(80));
    }

    #[test]
    fn test_normalize_a100() {
        assert_eq!(normalize_gpu_type("A100-80G-PCIe"), GpuCategory::A100);
        assert_eq!(normalize_gpu_type("A100-40G"), GpuCategory::A100);
        assert_eq!(normalize_gpu_type("A100"), GpuCategory::A100);
    }

    #[test]
    fn test_normalize_h100() {
        assert_eq!(normalize_gpu_type("H100"), GpuCategory::H100);
        assert_eq!(normalize_gpu_type("H100-80GB"), GpuCategory::H100);
    }

    #[test]
    fn test_normalize_b200() {
        assert_eq!(normalize_gpu_type("B200"), GpuCategory::B200);
    }

    #[test]
    fn test_normalize_unknown() {
        match normalize_gpu_type("RTX-4090-24G") {
            GpuCategory::Other(model) => assert!(model.contains("RTX")),
            _ => panic!("Expected Other variant"),
        }
    }

    #[test]
    fn test_parse_memory() {
        assert_eq!(parse_gpu_memory("A100-80G-PCIe"), Some(80));
        assert_eq!(parse_gpu_memory("H100-80GB"), Some(80));
        assert_eq!(parse_gpu_memory("H100"), None);
    }

    #[test]
    fn test_fallback_gpu_memory_l40() {
        assert_eq!(parse_gpu_memory("L40"), Some(48));
        assert_eq!(parse_gpu_memory("L40-spot"), Some(48));
    }

    #[test]
    fn test_fallback_gpu_memory_rtx() {
        assert_eq!(parse_gpu_memory("RTX-A4000"), Some(16));
        assert_eq!(parse_gpu_memory("RTX-A6000"), Some(48));
        assert_eq!(parse_gpu_memory("RTX-A6000-spot"), Some(48));
        assert_eq!(parse_gpu_memory("RTX-PRO6000-SE"), Some(48));
        assert_eq!(parse_gpu_memory("RTX-PRO6000-SE-spot"), Some(48));
    }

    #[test]
    fn test_fallback_unknown_gpu() {
        assert_eq!(parse_gpu_memory("RTX-4090"), None);
        assert_eq!(parse_gpu_memory("UNKNOWN-GPU"), None);
    }

    #[test]
    fn test_normalize_region() {
        // Store exactly what Hyperstack API provides
        assert_eq!(normalize_region("CANADA-1"), "CANADA-1");
        assert_eq!(normalize_region("US-WEST-2"), "US-WEST-2");
        assert_eq!(normalize_region("NORWAY-1"), "NORWAY-1");
        assert_eq!(normalize_region(""), "unknown");
    }

    #[test]
    fn test_parse_interconnect_pcie() {
        assert_eq!(
            parse_interconnect("H100-80G-PCIe"),
            Some("PCIe".to_string())
        );
        assert_eq!(
            parse_interconnect("A100-80G-PCIe"),
            Some("PCIe".to_string())
        );
        assert_eq!(
            parse_interconnect("H100-80G-PCIE"),
            Some("PCIe".to_string())
        );
    }

    #[test]
    fn test_parse_interconnect_sxm() {
        assert_eq!(
            parse_interconnect("H100-80G-SXM5"),
            Some("SXM5".to_string())
        );
        assert_eq!(
            parse_interconnect("A100-80G-SXM4"),
            Some("SXM4".to_string())
        );
    }

    #[test]
    fn test_parse_interconnect_pcie_nvlink() {
        assert_eq!(
            parse_interconnect("A100-80G-PCIe-NVLink"),
            Some("PCIe-NVLink".to_string())
        );
        assert_eq!(
            parse_interconnect("H100-80G-PCIe-NVLink"),
            Some("PCIe-NVLink".to_string())
        );
    }

    #[test]
    fn test_parse_interconnect_with_spot() {
        // spot suffix should be ignored, return the interconnect
        assert_eq!(
            parse_interconnect("H100-80G-PCIe-spot"),
            Some("PCIe".to_string())
        );
        assert_eq!(
            parse_interconnect("A100-80G-SXM4-spot"),
            Some("SXM4".to_string())
        );
    }

    #[test]
    fn test_parse_interconnect_none() {
        // Just GPU model without interconnect
        assert_eq!(parse_interconnect("H100"), None);
        assert_eq!(parse_interconnect("H100-80GB"), None);
    }
}
